"""Subtitle discovery, structured parsing, format conversion (SRT/VTT/TXT), and Captions-Only execution."""

from __future__ import annotations

import html
import json
import re
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yt_dlp  # type: ignore[import-untyped]

from src.core.events import EventBus, EventType
from src.core.exceptions import (
    AppError,
    DownloadCancelledError,
    DownloadError,
    DownloadPausedError,
    ValidationError,
)
from src.core.models import (
    CaptionSource,
    CaptionTrackInfo,
    DownloadJob,
    JobStatus,
    OverwriteBehavior,
    PlaylistMode,
    SubtitleFormat,
)
from src.core.validators import (
    format_timestamp,
    parse_timestamp,
    sanitize_filename,
    validate_output_directory,
    validate_playlist_indices,
    validate_subtitle_languages,
    validate_url,
)
from src.utils.logging_config import get_logger
from src.utils.paths import resolve_unique_filepath

logger = get_logger("subtitle_service")

LANGUAGE_NAMES: dict[str, str] = {
    "af": "Afrikaans",
    "ar": "Arabic",
    "bn": "Bengali",
    "bg": "Bulgarian",
    "ca": "Catalan",
    "cs": "Czech",
    "da": "Danish",
    "de": "German",
    "el": "Greek",
    "en": "English",
    "en-gb": "English (UK)",
    "en-us": "English (US)",
    "en-orig": "English (Original)",
    "es": "Spanish",
    "es-419": "Spanish (Latin America)",
    "es-es": "Spanish (Spain)",
    "et": "Estonian",
    "fa": "Persian",
    "fi": "Finnish",
    "fil": "Filipino",
    "fr": "French",
    "gu": "Gujarati",
    "he": "Hebrew",
    "hi": "Hindi",
    "hr": "Croatian",
    "hu": "Hungarian",
    "id": "Indonesian",
    "it": "Italian",
    "ja": "Japanese",
    "kn": "Kannada",
    "ko": "Korean",
    "lt": "Lithuanian",
    "lv": "Latvian",
    "ml": "Malayalam",
    "mr": "Marathi",
    "ms": "Malay",
    "nl": "Dutch",
    "no": "Norwegian",
    "pa": "Punjabi",
    "pl": "Polish",
    "pt": "Portuguese",
    "pt-br": "Portuguese (Brazil)",
    "pt-pt": "Portuguese (Portugal)",
    "ro": "Romanian",
    "ru": "Russian",
    "sk": "Slovak",
    "sl": "Slovenian",
    "sr": "Serbian",
    "sv": "Swedish",
    "sw": "Swahili",
    "ta": "Tamil",
    "te": "Telugu",
    "th": "Thai",
    "tr": "Turkish",
    "uk": "Ukrainian",
    "ur": "Urdu",
    "vi": "Vietnamese",
    "zh": "Chinese",
    "zh-cn": "Chinese (Simplified)",
    "zh-hans": "Chinese (Simplified)",
    "zh-hant": "Chinese (Traditional)",
    "zh-tw": "Chinese (Traditional)",
}

_INLINE_TAG_RE = re.compile(
    r"</?(?:c(?:\.[^>\s]+)?|b|i|u|font|ruby|rt|v|lang|\d{2}:\d{2}(?::\d{2})?\.\d{3})(?:\s+[^>]*)?>",
    re.IGNORECASE,
)
_GENERIC_ANGLE_TAG_RE = re.compile(r"<[^>]+>")
_CUE_TIMING_LINE_RE = re.compile(
    r"^\s*(\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3})(?:\s+.*)?$"
)
_RAW_SUBTITLE_EXTS = (".vtt", ".srt", ".json3", ".srv3", ".ttml", ".srv1", ".srv2")


@dataclass(slots=True)
class SubtitleCue:
    """A single timed subtitle cue with cleaned text."""

    start_seconds: float
    end_seconds: float
    text: str


class _SilentSubtitleYtdlpLogger:
    """Logger adapter for yt-dlp caption operations."""

    def __init__(self, job_id: str) -> None:
        self._job_id = job_id

    def debug(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        logger.debug("[captions:%s] yt-dlp warning: %s", self._job_id, msg)

    def error(self, msg: str) -> None:
        logger.warning("[captions:%s] yt-dlp error: %s", self._job_id, msg)


def resolve_language_name(lang_code: str, raw_name: str | None = None) -> str:
    """Return a human-readable language name for a subtitle language code."""
    if raw_name and isinstance(raw_name, str) and raw_name.strip():
        cleaned = raw_name.strip()
        # Strip trailing "(auto-generated)" if yt-dlp already appended it so we don't duplicate
        cleaned = re.sub(r"\s*\(auto-generated\)\s*$", "", cleaned, flags=re.IGNORECASE).strip()
        if cleaned:
            return cleaned

    code_clean = (lang_code or "").strip()
    if not code_clean:
        return "Unknown"

    lower = code_clean.lower()
    if lower in LANGUAGE_NAMES:
        return LANGUAGE_NAMES[lower]

    base = lower.split("-")[0]
    if base in LANGUAGE_NAMES:
        return f"{LANGUAGE_NAMES[base]} ({code_clean})"

    return code_clean


def extract_caption_tracks(info: dict[str, Any]) -> list[CaptionTrackInfo]:
    """Discover manual and automatic caption tracks from a yt-dlp info dictionary."""
    if not isinstance(info, dict):
        return []

    tracks: list[CaptionTrackInfo] = []

    manual_dict = info.get("subtitles")
    if isinstance(manual_dict, dict):
        for lang_code, formats in sorted(manual_dict.items()):
            if not lang_code or lang_code == "live_chat":
                continue
            exts, name_hint = _inspect_track_formats(formats)
            tracks.append(
                CaptionTrackInfo(
                    lang_code=str(lang_code),
                    name=resolve_language_name(str(lang_code), name_hint),
                    source=CaptionSource.MANUAL,
                    available_exts=exts,
                )
            )

    auto_dict = info.get("automatic_captions")
    if isinstance(auto_dict, dict):
        for lang_code, formats in sorted(auto_dict.items()):
            if not lang_code or lang_code == "live_chat":
                continue
            exts, name_hint = _inspect_track_formats(formats)
            tracks.append(
                CaptionTrackInfo(
                    lang_code=str(lang_code),
                    name=resolve_language_name(str(lang_code), name_hint),
                    source=CaptionSource.AUTOMATIC,
                    available_exts=exts,
                )
            )

    return tracks


def _inspect_track_formats(formats: Any) -> tuple[list[str], str | None]:
    """Extract supported subtitle extensions and optional human-readable name from track formats."""
    exts: list[str] = []
    name_hint: str | None = None
    if isinstance(formats, list):
        for item in formats:
            if not isinstance(item, dict):
                continue
            ext = str(item.get("ext") or "").strip().lower()
            if ext and ext not in exts:
                exts.append(ext)
            if not name_hint and isinstance(item.get("name"), str) and item["name"].strip():
                name_hint = item["name"].strip()
    return exts, name_hint


def clean_cue_text(raw_text: str) -> str:
    """Remove VTT/SRT markup tags, decode HTML entities, and normalize whitespace."""
    if not raw_text:
        return ""

    # Strip inline VTT/SRT tags such as <c>, </c>, <00:00:01.200>, <b>, <i>, <v Speaker>
    text = _INLINE_TAG_RE.sub("", raw_text)
    text = _GENERIC_ANGLE_TAG_RE.sub("", text)
    text = html.unescape(text)
    # Replace non-breaking spaces with standard spaces
    text = text.replace("\u00a0", " ").replace("\ufeff", "")

    cleaned_lines: list[str] = []
    for line in text.splitlines():
        stripped = " ".join(line.split())
        if stripped:
            cleaned_lines.append(stripped)

    return "\n".join(cleaned_lines)


def parse_subtitle_content(raw_content: str, source_ext: str = "vtt") -> list[SubtitleCue]:
    """Parse VTT, SRT, JSON3, or SRV3/XML subtitle text into structured SubtitleCue items."""
    if not raw_content or not raw_content.strip():
        return []

    ext = source_ext.lower().lstrip(".")
    content = raw_content.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")

    if ext == "json3" or content.lstrip().startswith("{"):
        cues = _parse_json3_content(content)
        if cues:
            return cues

    if ext in {"srv3", "ttml", "srv1", "srv2", "xml"} or content.lstrip().startswith("<?xml") or content.lstrip().startswith("<timedtext") or content.lstrip().startswith("<tt"):
        cues = _parse_xml_subtitle_content(content)
        if cues:
            return cues

    return _parse_vtt_or_srt_content(content)


def _parse_vtt_or_srt_content(content: str) -> list[SubtitleCue]:
    """Parse WebVTT or SubRip (SRT) blocks into structured cues."""
    lines = content.split("\n")
    cues: list[SubtitleCue] = []
    idx = 0
    total_lines = len(lines)

    while idx < total_lines:
        line = lines[idx].strip()

        # Skip empty lines and WebVTT headers/metadata blocks
        if not line:
            idx += 1
            continue

        if line.upper().startswith("WEBVTT") or line.startswith(("Kind:", "Language:")):
            idx += 1
            continue

        if line.startswith(("NOTE", "STYLE", "REGION")):
            idx += 1
            while idx < total_lines and lines[idx].strip():
                idx += 1
            continue

        match = _CUE_TIMING_LINE_RE.match(line)
        if not match and (idx + 1) < total_lines:
            # Optional cue identifier or numeric index on current line, timing on next line
            next_match = _CUE_TIMING_LINE_RE.match(lines[idx + 1].strip())
            if next_match:
                idx += 1
                match = next_match

        if not match:
            idx += 1
            continue

        start_str, end_str = match.group(1), match.group(2)
        try:
            start_sec = parse_timestamp(start_str)
            end_sec = parse_timestamp(end_str)
        except ValidationError:
            idx += 1
            continue

        idx += 1
        text_lines: list[str] = []
        while idx < total_lines and lines[idx].strip() != "":
            # Stop if another timing line immediately follows without a blank line
            if _CUE_TIMING_LINE_RE.match(lines[idx].strip()):
                break
            text_lines.append(lines[idx])
            idx += 1

        cleaned_text = clean_cue_text("\n".join(text_lines))
        if not cleaned_text:
            continue

        if end_sec < start_sec:
            end_sec = start_sec + 1.0
        elif abs(end_sec - start_sec) < 0.005:
            # Skip zero-duration transition cues common in YouTube rolling VTT
            continue

        # Merge or deduplicate identical consecutive cues
        if cues and cues[-1].text == cleaned_text and abs(cues[-1].end_seconds - start_sec) <= 0.2:
            cues[-1].end_seconds = max(cues[-1].end_seconds, end_sec)
        else:
            cues.append(
                SubtitleCue(
                    start_seconds=start_sec,
                    end_seconds=end_sec,
                    text=cleaned_text,
                )
            )

    return cues


def _parse_json3_content(content: str) -> list[SubtitleCue]:
    """Parse YouTube JSON3 caption events into SubtitleCue objects."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return []

    events = data.get("events") if isinstance(data, dict) else None
    if not isinstance(events, list):
        return []

    cues: list[SubtitleCue] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        segs = ev.get("segs")
        if not isinstance(segs, list):
            continue
        raw_text = "".join(str(s.get("utf8") or "") for s in segs if isinstance(s, dict))
        cleaned = clean_cue_text(raw_text)
        if not cleaned:
            continue
        start_ms = float(ev.get("tStartMs") or 0.0)
        dur_ms = float(ev.get("dDurationMs") or 2000.0)
        start_sec = round(max(0.0, start_ms / 1000.0), 3)
        end_sec = round(max(start_sec + 0.2, (start_ms + dur_ms) / 1000.0), 3)
        if cues and cues[-1].text == cleaned and abs(cues[-1].end_seconds - start_sec) <= 0.2:
            cues[-1].end_seconds = max(cues[-1].end_seconds, end_sec)
        else:
            cues.append(SubtitleCue(start_seconds=start_sec, end_seconds=end_sec, text=cleaned))
    return cues


def _parse_xml_subtitle_content(content: str) -> list[SubtitleCue]:
    """Parse YouTube SRV1/SRV3/TTML XML subtitle format into SubtitleCue objects."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return []

    cues: list[SubtitleCue] = []
    for elem in root.iter():
        tag_local = elem.tag.split("}")[-1].lower()
        if tag_local not in {"text", "p"}:
            continue
        raw_text = "".join(elem.itertext())
        cleaned = clean_cue_text(raw_text)
        if not cleaned:
            continue

        start_sec = 0.0
        end_sec = 2.0
        if "start" in elem.attrib:
            start_sec = float(elem.attrib.get("start") or 0.0)
            dur = float(elem.attrib.get("dur") or 2.0)
            end_sec = start_sec + max(0.2, dur)
        elif "t" in elem.attrib:
            start_sec = float(elem.attrib.get("t") or 0.0) / 1000.0
            dur_ms = float(elem.attrib.get("d") or 2000.0)
            end_sec = start_sec + max(0.2, dur_ms / 1000.0)
        elif "begin" in elem.attrib:
            try:
                start_sec = parse_timestamp(elem.attrib["begin"])
                end_sec = (
                    parse_timestamp(elem.attrib["end"])
                    if "end" in elem.attrib
                    else start_sec + 2.0
                )
            except ValidationError:
                continue

        cues.append(
            SubtitleCue(
                start_seconds=round(max(0.0, start_sec), 3),
                end_seconds=round(max(start_sec + 0.1, end_sec), 3),
                text=cleaned,
            )
        )
    return cues


def export_cues_to_srt(cues: list[SubtitleCue]) -> str:
    """Format SubtitleCue list into valid SubRip (.srt) content."""
    blocks: list[str] = []
    for index, cue in enumerate(cues, start=1):
        start_ts = format_timestamp(
            cue.start_seconds,
            include_millis=True,
            force_hours=True,
            srt_comma=True,
        )
        end_ts = format_timestamp(
            max(cue.start_seconds + 0.05, cue.end_seconds),
            include_millis=True,
            force_hours=True,
            srt_comma=True,
        )
        blocks.append(f"{index}\n{start_ts} --> {end_ts}\n{cue.text}")
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def export_cues_to_vtt(cues: list[SubtitleCue]) -> str:
    """Format SubtitleCue list into valid WebVTT (.vtt) content."""
    blocks: list[str] = ["WEBVTT"]
    for cue in cues:
        start_ts = format_timestamp(
            cue.start_seconds,
            include_millis=True,
            force_hours=True,
            srt_comma=False,
        )
        end_ts = format_timestamp(
            max(cue.start_seconds + 0.05, cue.end_seconds),
            include_millis=True,
            force_hours=True,
            srt_comma=False,
        )
        blocks.append(f"{start_ts} --> {end_ts}\n{cue.text}")
    return "\n\n".join(blocks) + "\n"


def export_cues_to_txt(cues: list[SubtitleCue], include_timestamps: bool = False) -> str:
    """Format SubtitleCue list into a clean, readable plain-text transcript (.txt).

    Deduplicates rolling auto-caption overlap lines and removes all timing/markup metadata
    unless include_timestamps=True is explicitly requested.
    """
    output_lines: list[str] = []
    recent_lines: list[str] = []

    for cue in cues:
        cue_lines = [ln.strip() for ln in cue.text.splitlines() if ln.strip()]
        new_lines_for_cue: list[str] = []
        for ln in cue_lines:
            # Avoid repeating identical consecutive lines caused by rolling VTT captions
            if not recent_lines or ln != recent_lines[-1]:
                new_lines_for_cue.append(ln)
                recent_lines.append(ln)
                if len(recent_lines) > 4:
                    recent_lines.pop(0)

        if not new_lines_for_cue:
            continue

        joined = " ".join(new_lines_for_cue)
        if include_timestamps:
            ts = format_timestamp(cue.start_seconds, include_millis=False)
            output_lines.append(f"[{ts}] {joined}")
        else:
            output_lines.append(joined)

    return "\n".join(output_lines) + ("\n" if output_lines else "")


def convert_subtitle_content(
    raw_content: str,
    target_format: SubtitleFormat,
    source_ext: str = "vtt",
    txt_include_timestamps: bool = False,
) -> str:
    """Parse raw subtitle content and convert it into the requested target SubtitleFormat."""
    cues = parse_subtitle_content(raw_content, source_ext=source_ext)
    if not cues:
        raise DownloadError(
            "The downloaded subtitle track contained no readable caption cues.",
            remedy="Try another language track or enable Auto-generated Captions.",
        )

    if target_format == SubtitleFormat.SRT:
        return export_cues_to_srt(cues)
    if target_format == SubtitleFormat.VTT:
        return export_cues_to_vtt(cues)
    if target_format == SubtitleFormat.TXT:
        return export_cues_to_txt(cues, include_timestamps=txt_include_timestamps)

    raise ValidationError(f"Unsupported subtitle export format '{target_format}'.")


def convert_subtitle_file(
    source_file: Path,
    target_file: Path,
    target_format: SubtitleFormat,
    txt_include_timestamps: bool = False,
) -> Path:
    """Read a downloaded subtitle file, convert it to SRT/VTT/TXT, and write UTF-8 output."""
    if not source_file.exists() or not source_file.is_file():
        raise DownloadError(
            f"Subtitle source file was not found: '{source_file.name}'",
            remedy="Retry the caption download.",
        )

    raw_text = source_file.read_text(encoding="utf-8", errors="replace")
    converted = convert_subtitle_content(
        raw_text,
        target_format=target_format,
        source_ext=source_file.suffix.lstrip("."),
        txt_include_timestamps=txt_include_timestamps,
    )
    if not converted.strip():
        raise DownloadError(
            f"Converted subtitle file '{target_file.name}' would be empty.",
            remedy="Check if the selected caption track has actual spoken dialogue.",
        )

    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text(converted, encoding="utf-8")
    return target_file


class SubtitleService:
    """Executes standalone Captions-Only jobs and subtitle downloads/conversions without FFmpeg."""

    def __init__(
        self,
        event_bus: EventBus | None = None,
        ydl_factory: type[Any] | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._ydl_cls = ydl_factory or yt_dlp.YoutubeDL

    def execute_captions_only_job(
        self,
        job: DownloadJob,
        cancel_event: threading.Event | None = None,
        pause_event: threading.Event | None = None,
        tracked_files: list[Path] | None = None,
    ) -> DownloadJob:
        """Execute a standalone Captions Only job.

        Guarantees:
        - Never downloads video or audio streams (`skip_download=True`).
        - Never invokes FFmpeg or FFprobe (succeeds even when FFmpeg is absent).
        - Converts subtitles in pure Python to SRT, VTT, or TXT.
        """
        if tracked_files is None:
            tracked_files = []

        self._check_interruption(cancel_event, pause_event, job, tracked_files)

        try:
            validate_url(job.url)
            dest_dir = validate_output_directory(job.destination_dir, create_if_missing=True)
            requested_langs = validate_subtitle_languages(job.subtitle_languages)

            job.transition_to(JobStatus.STARTING)
            self._update_stage(job, "Starting caption job...", 5.0)

            self._check_interruption(cancel_event, pause_event, job, tracked_files)
            job.transition_to(JobStatus.FETCHING_METADATA)
            self._update_stage(job, "Retrieving video metadata", 15.0)

            meta_opts: dict[str, Any] = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "writesubtitles": False,
                "writeautomaticsub": False,
                "noplaylist": job.playlist_mode == PlaylistMode.SINGLE,
                "socket_timeout": 20,
                "retries": 2,
                "js_runtimes": {"deno": {}, "node": {}, "bun": {}, "quickjs": {}},
                "logger": _SilentSubtitleYtdlpLogger(job.job_id),
            }
            if job.playlist_mode == PlaylistMode.SELECTED_ITEMS:
                valid_indices = validate_playlist_indices(job.selected_playlist_indices)
                meta_opts["playlist_items"] = ",".join(str(i) for i in valid_indices)

            with self._ydl_cls(meta_opts) as ydl:
                info = ydl.extract_info(job.url, download=False)

            if not info or not isinstance(info, dict):
                raise DownloadError(
                    "Could not retrieve metadata for caption discovery.",
                    remedy="Verify the YouTube URL is public and available.",
                )

            self._check_interruption(cancel_event, pause_event, job, tracked_files)

            entries: list[dict[str, Any]]
            if str(info.get("_type", "")).lower() in {"playlist", "multi_video"} or isinstance(info.get("entries"), list):
                raw_entries = [e for e in (info.get("entries") or []) if isinstance(e, dict)]
                if not raw_entries:
                    raise DownloadError(
                        "No accessible videos were found in the playlist for caption extraction.",
                        remedy="Check that the playlist contains public videos.",
                    )
                entries = raw_entries
                pl_title = str(info.get("title") or info.get("playlist_title") or "").strip()
                if pl_title and not job.title:
                    job.title = pl_title
            else:
                entries = [info]
                extracted_title = str(info.get("title") or "").strip()
                if extracted_title:
                    job.title = extracted_title

            self._update_stage(job, "Discovering caption tracks", 30.0)

            all_exported: list[Path] = []
            missing_notes: list[str] = []
            total_entries = len(entries)

            for idx, entry in enumerate(entries, start=1):
                self._check_interruption(cancel_event, pause_event, job, tracked_files)
                if total_entries > 1:
                    job.playlist_index = idx
                    job.playlist_total = total_entries

                exported_for_entry, entry_missing = self._process_entry_captions(
                    job=job,
                    entry_info=entry,
                    dest_dir=dest_dir,
                    requested_langs=requested_langs,
                    cancel_event=cancel_event,
                    pause_event=pause_event,
                    tracked_files=tracked_files,
                    entry_index=idx,
                    total_entries=total_entries,
                )
                all_exported.extend(exported_for_entry)
                missing_notes.extend(entry_missing)

            self._check_interruption(cancel_event, pause_event, job, tracked_files)

            self._update_stage(job, "Verifying subtitle output files", 95.0, status=JobStatus.PROCESSING)

            verified_files: list[Path] = []
            total_bytes = 0
            for path in all_exported:
                if path.exists() and path.is_file():
                    sz = path.stat().st_size
                    if sz > 0:
                        verified_files.append(path.resolve())
                        total_bytes += sz

            if not verified_files:
                detail = "; ".join(missing_notes[:3]) if missing_notes else "No matching caption tracks found."
                raise DownloadError(
                    f"Could not download or export any requested caption tracks ({detail}).",
                    remedy="Enable 'Include Auto-Generated Captions' or choose one of the available languages.",
                )

            job.exported_subtitle_paths = verified_files
            job.output_path = verified_files[0] if len(verified_files) == 1 else dest_dir.resolve()
            job.file_size_bytes = total_bytes
            job.downloaded_bytes = total_bytes
            job.total_bytes = total_bytes

            job.transition_to(JobStatus.COMPLETED)
            if missing_notes:
                job.stage_detail = f"Completed ({len(verified_files)} saved; partial: {'; '.join(missing_notes[:2])})"
            else:
                job.stage_detail = f"Completed ({len(verified_files)} subtitle file(s) saved)"

            self._emit(EventType.JOB_COMPLETED, job)
            logger.info(
                "Captions-Only job %s completed: %d file(s) -> %s",
                job.job_id,
                len(verified_files),
                job.output_path,
            )
            return job

        except DownloadCancelledError:
            self._cleanup_temp_subtitle_files(tracked_files)
            try:
                job.transition_to(JobStatus.CANCELLED, error_message="Cancelled by user.")
            except Exception:
                job.status = JobStatus.CANCELLED
                job.error_message = "Cancelled by user."
            self._emit(EventType.JOB_CANCELLED, job)
            raise
        except DownloadPausedError:
            try:
                job.transition_to(JobStatus.PAUSED)
            except Exception:
                job.status = JobStatus.PAUSED
            self._emit(EventType.JOB_PAUSED, job)
            raise
        except (ValidationError, DownloadError) as exc:
            self._cleanup_temp_subtitle_files(tracked_files)
            self._mark_failed(job, exc.format_for_user())
            raise
        except Exception as exc:
            self._cleanup_temp_subtitle_files(tracked_files)
            if cancel_event is not None and cancel_event.is_set():
                job.status = JobStatus.CANCELLED
                job.error_message = "Cancelled by user."
                self._emit(EventType.JOB_CANCELLED, job)
                raise DownloadCancelledError("Caption download cancelled by user.") from exc
            err = DownloadError(
                f"Caption download failed: {exc}",
                remedy="Verify the video has accessible subtitles and check your network connection.",
            )
            self._mark_failed(job, err.format_for_user())
            raise err from exc

    def download_subtitles_for_video_entry(
        self,
        job: DownloadJob,
        entry_info: dict[str, Any],
        dest_dir: Path,
        video_stem: str | None = None,
        cancel_event: threading.Event | None = None,
        pause_event: threading.Event | None = None,
        tracked_files: list[Path] | None = None,
        force_srt_copy_for_embedding: bool = False,
    ) -> tuple[list[Path], list[tuple[Path, str]], list[str]]:
        """Download and convert subtitles for a single video entry.

        Returns:
            (exported_user_files, embed_ready_srt_or_vtt_files_with_lang, missing_warnings)
        """
        if tracked_files is None:
            tracked_files = []
        requested_langs = validate_subtitle_languages(job.subtitle_languages)
        return self._download_and_convert_tracks(
            job=job,
            entry_info=entry_info,
            dest_dir=dest_dir,
            requested_langs=requested_langs,
            video_stem=video_stem,
            cancel_event=cancel_event,
            pause_event=pause_event,
            tracked_files=tracked_files,
            force_srt_copy_for_embedding=force_srt_copy_for_embedding,
        )

    def _process_entry_captions(
        self,
        job: DownloadJob,
        entry_info: dict[str, Any],
        dest_dir: Path,
        requested_langs: list[str],
        cancel_event: threading.Event | None,
        pause_event: threading.Event | None,
        tracked_files: list[Path],
        entry_index: int,
        total_entries: int,
    ) -> tuple[list[Path], list[str]]:
        """Process captions for one video entry during a Captions-Only job."""
        # If playlist entry was flat, fetch full entry metadata first
        if "subtitles" not in entry_info and "automatic_captions" not in entry_info:
            entry_url = str(
                entry_info.get("webpage_url")
                or entry_info.get("url")
                or (f"https://www.youtube.com/watch?v={entry_info.get('id')}" if entry_info.get("id") else job.url)
            )
            with self._ydl_cls(
                {
                    "quiet": True,
                    "no_warnings": True,
                    "skip_download": True,
                    "noplaylist": True,
                    "socket_timeout": 20,
                    "logger": _SilentSubtitleYtdlpLogger(job.job_id),
                }
            ) as ydl:
                full_entry = ydl.extract_info(entry_url, download=False)
                if isinstance(full_entry, dict):
                    entry_info = full_entry

        progress_base = 30.0 + ((entry_index - 1) / max(1, total_entries)) * 50.0
        self._update_stage(
            job,
            "Downloading selected caption tracks",
            min(80.0, progress_base + 20.0),
            status=JobStatus.DOWNLOADING,
        )

        exported, _embed_tracks, warnings = self._download_and_convert_tracks(
            job=job,
            entry_info=entry_info,
            dest_dir=dest_dir,
            requested_langs=requested_langs,
            video_stem=None,
            cancel_event=cancel_event,
            pause_event=pause_event,
            tracked_files=tracked_files,
            force_srt_copy_for_embedding=False,
        )
        return exported, warnings

    def _download_and_convert_tracks(
        self,
        job: DownloadJob,
        entry_info: dict[str, Any],
        dest_dir: Path,
        requested_langs: list[str],
        video_stem: str | None,
        cancel_event: threading.Event | None,
        pause_event: threading.Event | None,
        tracked_files: list[Path],
        force_srt_copy_for_embedding: bool,
    ) -> tuple[list[Path], list[tuple[Path, str]], list[str]]:
        """Match requested languages against available manual/auto tracks, download raw subs, and convert."""
        tracks = extract_caption_tracks(entry_info)
        manual_by_lang = {t.lang_code.lower(): t for t in tracks if not t.is_automatic}
        auto_by_lang = {t.lang_code.lower(): t for t in tracks if t.is_automatic}

        if not manual_by_lang and not auto_by_lang:
            vid_title = str(entry_info.get("title") or job.display_title)
            raise DownloadError(
                f"No manual subtitles or automatic captions are available for '{vid_title}'.",
                remedy="Try a different video that provides captions.",
            )

        selected_plan: list[tuple[str, CaptionSource]] = []
        missing_warnings: list[str] = []

        if "all" in [lang.lower() for lang in requested_langs]:
            for t in tracks:
                if not t.is_automatic or job.include_auto_captions:
                    selected_plan.append((t.lang_code, t.source))
        else:
            for req in requested_langs:
                req_low = req.lower()
                matched_this_lang = False

                # Exact or prefix match in manual tracks
                manual_match = self._match_lang_code(req_low, manual_by_lang)
                if manual_match is not None:
                    selected_plan.append((manual_match.lang_code, CaptionSource.MANUAL))
                    matched_this_lang = True

                # Match in automatic captions if requested (either as fallback or alongside manual)
                if job.include_auto_captions:
                    auto_match = self._match_lang_code(req_low, auto_by_lang)
                    if auto_match is not None and not matched_this_lang:
                        selected_plan.append((auto_match.lang_code, CaptionSource.AUTOMATIC))
                        matched_this_lang = True

                if not matched_this_lang:
                    if not job.include_auto_captions and self._match_lang_code(req_low, auto_by_lang) is not None:
                        missing_warnings.append(
                            f"Language '{req}' has no manual subtitles (auto-generated captions are available)"
                        )
                    else:
                        avail_codes = sorted({t.lang_code for t in tracks if not t.is_automatic or job.include_auto_captions})
                        avail_preview = ", ".join(avail_codes[:8]) if avail_codes else "none"
                        missing_warnings.append(
                            f"Language '{req}' not available (available: {avail_preview})"
                        )

        if not selected_plan:
            detail = "; ".join(missing_warnings) if missing_warnings else "No matching tracks."
            raise DownloadError(
                f"Requested subtitle track(s) could not be found: {detail}.",
                remedy="Enable 'Include Auto-Generated Captions' or choose one of the available subtitle languages.",
            )

        vid_id = sanitize_filename(str(entry_info.get("id") or "video"), max_length=32)
        vid_title = sanitize_filename(str(entry_info.get("title") or job.title or "video"), max_length=120)
        base_stem = video_stem if video_stem else f"{vid_title} [{vid_id}]"

        entry_url = str(entry_info.get("webpage_url") or entry_info.get("url") or job.url)
        raw_temp_prefix = f".ytd_sub_tmp_{job.job_id}_{vid_id}"
        temp_outtmpl = str(dest_dir / f"{raw_temp_prefix}.%(ext)s")

        need_manual = any(src == CaptionSource.MANUAL for _, src in selected_plan)
        need_auto = any(src == CaptionSource.AUTOMATIC for _, src in selected_plan)
        langs_to_fetch = list(dict.fromkeys(code for code, _ in selected_plan))

        dl_opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "writesubtitles": need_manual,
            "writeautomaticsub": need_auto,
            "subtitleslangs": langs_to_fetch,
            "subtitlesformat": "vtt/srt/best",
            "outtmpl": {"default": temp_outtmpl},
            "overwrites": True,
            "noplaylist": True,
            "socket_timeout": 20,
            "retries": 2,
            "js_runtimes": {"deno": {}, "node": {}, "bun": {}, "quickjs": {}},
            "logger": _SilentSubtitleYtdlpLogger(job.job_id),
        }

        self._check_interruption(cancel_event, pause_event, job, tracked_files)

        try:
            with self._ydl_cls(dl_opts) as ydl:
                ydl.download([entry_url])
        finally:
            for candidate in sorted(dest_dir.glob(f"{raw_temp_prefix}*")):
                if candidate.is_file() and candidate not in tracked_files:
                    tracked_files.append(candidate)

        self._check_interruption(cancel_event, pause_event, job, tracked_files)
        self._update_stage(job, "Converting caption formats", 85.0, status=JobStatus.PROCESSING)

        # Discover downloaded raw subtitle files matching raw_temp_prefix
        downloaded_raw_files: list[Path] = [p for p in tracked_files if p.name.startswith(raw_temp_prefix) and p.is_file()]

        # Also support unit tests where a fake ydl writes directly or provides inline subtitle data
        if not downloaded_raw_files:
            downloaded_raw_files = self._synthesize_from_inline_subtitles_if_present(
                entry_info=entry_info,
                selected_plan=selected_plan,
                dest_dir=dest_dir,
                raw_temp_prefix=raw_temp_prefix,
                tracked_files=tracked_files,
            )

        exported_files: list[Path] = []
        embed_ready_files: list[tuple[Path, str]] = []

        for lang_code, source_kind in selected_plan:
            raw_file = self._find_matching_raw_file(downloaded_raw_files, raw_temp_prefix, lang_code)
            if raw_file is None or not raw_file.exists():
                missing_warnings.append(f"Subtitle file for '{lang_code}' was not produced by extractor")
                continue

            suffix_tag = f".{lang_code}.auto" if source_kind == CaptionSource.AUTOMATIC else f".{lang_code}"
            desired_name = f"{base_stem}{suffix_tag}.{job.subtitle_format.value}"
            target_path = dest_dir / desired_name

            if target_path.exists():
                if job.overwrite_behavior == OverwriteBehavior.SKIP:
                    exported_files.append(target_path)
                    continue
                if job.overwrite_behavior in {OverwriteBehavior.RENAME, OverwriteBehavior.ASK}:
                    target_path = resolve_unique_filepath(dest_dir, desired_name)

            try:
                converted_path = convert_subtitle_file(
                    source_file=raw_file,
                    target_file=target_path,
                    target_format=job.subtitle_format,
                    txt_include_timestamps=job.txt_include_timestamps,
                )
                exported_files.append(converted_path)
            except AppError as exc:
                missing_warnings.append(f"Language '{lang_code}': {exc.user_message}")
                continue

            if force_srt_copy_for_embedding:
                if job.subtitle_format in {SubtitleFormat.SRT, SubtitleFormat.VTT}:
                    embed_ready_files.append((converted_path, lang_code))
                else:
                    # When user exported TXT but also enabled soft-subtitle embedding, generate a temp SRT for muxing
                    embed_srt_path = dest_dir / f"{raw_temp_prefix}_embed.{lang_code}.srt"
                    tracked_files.append(embed_srt_path)
                    convert_subtitle_file(
                        source_file=raw_file,
                        target_file=embed_srt_path,
                        target_format=SubtitleFormat.SRT,
                    )
                    embed_ready_files.append((embed_srt_path, lang_code))

        # Clean up raw temporary files downloaded by yt-dlp
        for raw_f in downloaded_raw_files:
            try:
                raw_f.unlink(missing_ok=True)
            except OSError:
                pass

        return exported_files, embed_ready_files, missing_warnings

    @staticmethod
    def _match_lang_code(
        requested_lower: str,
        available_map: dict[str, CaptionTrackInfo],
    ) -> CaptionTrackInfo | None:
        """Find an exact or language-prefix match in available_map."""
        if requested_lower in available_map:
            return available_map[requested_lower]
        for code_low, track in available_map.items():
            if code_low.startswith(f"{requested_lower}-") or code_low.startswith(f"{requested_lower}_"):
                return track
        return None

    @staticmethod
    def _find_matching_raw_file(
        raw_files: list[Path],
        prefix: str,
        lang_code: str,
    ) -> Path | None:
        """Match a downloaded raw subtitle file for a specific language code."""
        lang_low = lang_code.lower()
        for f in raw_files:
            name_after = f.name[len(prefix) :].lower()
            # Typically `.en.vtt` or `.en-US.srt`
            if name_after.startswith(f".{lang_low}."):
                return f
        for f in raw_files:
            name_after = f.name[len(prefix) :].lower()
            if f".{lang_low.split('-')[0]}" in name_after:
                return f
        return raw_files[0] if len(raw_files) == 1 else None

    @staticmethod
    def _synthesize_from_inline_subtitles_if_present(
        entry_info: dict[str, Any],
        selected_plan: list[tuple[str, CaptionSource]],
        dest_dir: Path,
        raw_temp_prefix: str,
        tracked_files: list[Path],
    ) -> list[Path]:
        """Support inline 'data' fields inside subtitle format dicts (used by some extractors and offline tests)."""
        created: list[Path] = []
        for lang_code, source_kind in selected_plan:
            source_dict = (
                entry_info.get("automatic_captions")
                if source_kind == CaptionSource.AUTOMATIC
                else entry_info.get("subtitles")
            )
            if not isinstance(source_dict, dict):
                continue
            fmt_list = source_dict.get(lang_code)
            if not isinstance(fmt_list, list):
                # Try case-insensitive lookup
                for k, v in source_dict.items():
                    if str(k).lower() == lang_code.lower() and isinstance(v, list):
                        fmt_list = v
                        break
            if not isinstance(fmt_list, list):
                continue

            for fmt in fmt_list:
                if isinstance(fmt, dict) and isinstance(fmt.get("data"), str) and fmt["data"].strip():
                    ext = str(fmt.get("ext") or "vtt").lower().lstrip(".")
                    tmp_path = dest_dir / f"{raw_temp_prefix}.{lang_code}.{ext}"
                    tmp_path.write_text(fmt["data"], encoding="utf-8")
                    created.append(tmp_path)
                    tracked_files.append(tmp_path)
                    break
        return created

    def _check_interruption(
        self,
        cancel_event: threading.Event | None,
        pause_event: threading.Event | None,
        job: DownloadJob,
        tracked_files: list[Path],
    ) -> None:
        if cancel_event is not None and cancel_event.is_set():
            self._cleanup_temp_subtitle_files(tracked_files)
            raise DownloadCancelledError("Caption job cancelled by user.")
        if pause_event is not None and pause_event.is_set():
            raise DownloadPausedError("Caption job paused by user.")

    @staticmethod
    def _cleanup_temp_subtitle_files(tracked_files: list[Path]) -> None:
        for path in tracked_files:
            try:
                if path.exists() and path.is_file() and (
                    path.name.startswith(".ytd_sub_tmp_") or path.suffix.lower() in _RAW_SUBTITLE_EXTS
                ):
                    path.unlink(missing_ok=True)
            except OSError:
                pass

    def _update_stage(
        self,
        job: DownloadJob,
        stage_text: str,
        progress_percent: float,
        status: JobStatus | None = None,
    ) -> None:
        if status is not None and job.status != status:
            job.transition_to(status)
        job.stage_detail = stage_text
        job.progress_percent = round(max(job.progress_percent, min(100.0, progress_percent)), 1)
        event_type = EventType.JOB_PROGRESS
        if job.status == JobStatus.STARTING:
            event_type = EventType.JOB_STARTING
        elif job.status == JobStatus.FETCHING_METADATA:
            event_type = EventType.JOB_FETCHING_METADATA
        elif job.status == JobStatus.PROCESSING:
            event_type = EventType.JOB_PROCESSING
        self._emit(event_type, job)

    def _mark_failed(self, job: DownloadJob, message: str) -> None:
        try:
            job.transition_to(JobStatus.FAILED, error_message=message)
        except Exception:
            job.status = JobStatus.FAILED
            job.error_message = message
        self._emit(EventType.JOB_FAILED, job)
        logger.error("Caption job %s failed: %s", job.job_id, message)

    def _emit(self, event_type: EventType, job: DownloadJob) -> None:
        if self._event_bus is not None:
            self._event_bus.publish(event_type, job_id=job.job_id, job=job)
