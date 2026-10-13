"""Real media download service wrapping yt-dlp with progress hooks, pause/resume, and cancellation."""

from __future__ import annotations

import errno
import math
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yt_dlp  # type: ignore[import-untyped]

from src.core.events import EventBus, EventType
from src.core.exceptions import (
    DownloadCancelledError,
    DownloadError,
    DownloadPausedError,
    FFmpegMissingError,
    FilesystemError,
    ValidationError,
)
from src.core.models import (
    AudioFormat,
    DownloadJob,
    DownloadMode,
    JobStatus,
    OverwriteBehavior,
    PlaylistMode,
    VideoFormat,
    VideoQuality,
)
from src.core.validators import (
    validate_filename_template,
    validate_output_directory,
    validate_playlist_indices,
    validate_url,
)
from src.services.ffmpeg_service import FFmpegService
from src.utils.logging_config import get_logger
from src.utils.paths import resolve_unique_filepath

logger = get_logger("download_service")

ProgressCallback = Callable[[dict[str, Any]], None]

_TEMP_SUFFIXES = (".part", ".ytdl", ".temp")


class _JobYtdlpLogger:
    """Per-job logger adapter for yt-dlp."""

    def __init__(self, job_id: str) -> None:
        self._job_id = job_id

    def debug(self, msg: str) -> None:
        if msg.startswith("[debug]"):
            return
        logger.debug("[job:%s] %s", self._job_id, msg)

    def warning(self, msg: str) -> None:
        logger.warning("[job:%s] yt-dlp warning: %s", self._job_id, msg)

    def error(self, msg: str) -> None:
        logger.error("[job:%s] yt-dlp error: %s", self._job_id, msg)


class DownloadService:
    """Executes isolated video, audio, and playlist downloads using yt-dlp."""

    def __init__(
        self,
        ffmpeg_service: FFmpegService,
        event_bus: EventBus | None = None,
        ydl_factory: type[Any] | None = None,
    ) -> None:
        self._ffmpeg_service = ffmpeg_service
        self._event_bus = event_bus
        self._ydl_cls = ydl_factory or yt_dlp.YoutubeDL

    def build_video_format_selector(
        self,
        quality: VideoQuality,
        video_format: VideoFormat,
        ffmpeg_available: bool,
    ) -> str:
        """Construct a yt-dlp format selector string for video mode."""
        height = quality.max_height

        if not ffmpeg_available:
            # Progressive (pre-merged video+audio) streams only when FFmpeg is absent
            if quality == VideoQuality.LOWEST:
                return "worst[vcodec!=none][acodec!=none]/worst"
            if height is not None:
                if video_format == VideoFormat.MP4:
                    return (
                        f"best[height<={height}][ext=mp4][vcodec!=none][acodec!=none]/"
                        f"best[height<={height}][vcodec!=none][acodec!=none]/"
                        "best[vcodec!=none][acodec!=none]/best"
                    )
                return f"best[height<={height}][vcodec!=none][acodec!=none]/best[vcodec!=none][acodec!=none]/best"
            if video_format == VideoFormat.MP4:
                return "best[ext=mp4][vcodec!=none][acodec!=none]/best[vcodec!=none][acodec!=none]/best"
            return "best[vcodec!=none][acodec!=none]/best"

        # FFmpeg is available: allow adaptive high-resolution stream merging
        if quality == VideoQuality.LOWEST:
            return "worstvideo+worstaudio/worst"

        if height is not None:
            if video_format == VideoFormat.MP4:
                return (
                    f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
                    f"bestvideo[height<={height}]+bestaudio/"
                    f"best[height<={height}]/"
                    "best"
                )
            return f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"

        if video_format == VideoFormat.MP4:
            return "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
        return "bestvideo+bestaudio/best"

    def build_ydl_options(
        self,
        job: DownloadJob,
        cancel_event: threading.Event | None = None,
        pause_event: threading.Event | None = None,
        tracked_files: list[Path] | None = None,
    ) -> dict[str, Any]:
        """Construct the complete yt-dlp configuration dictionary for a DownloadJob."""
        validate_url(job.url)
        dest_dir = validate_output_directory(job.destination_dir, create_if_missing=True)
        template = validate_filename_template(job.filename_template)

        ffmpeg_ready = self._ffmpeg_service.is_ffmpeg_available()
        ffmpeg_location = self._ffmpeg_service.get_ffmpeg_location_for_ytdlp()

        if job.mode == DownloadMode.AUDIO and job.audio_format.requires_ffmpeg_conversion and not ffmpeg_ready:
            raise FFmpegMissingError(
                f"Converting audio to {job.audio_format.value.upper()} requires FFmpeg, which was not found.",
                remedy=(
                    "Install FFmpeg (e.g. 'winget install Gyan.FFmpeg') or configure its path in Settings, "
                    "or choose 'Best Audio Stream' / 'M4A' which can download without conversion."
                ),
            )

        outtmpl_path = dest_dir / template
        overwrites = job.overwrite_behavior == OverwriteBehavior.OVERWRITE
        nooverwrites = job.overwrite_behavior == OverwriteBehavior.SKIP

        opts: dict[str, Any] = {
            "outtmpl": {"default": str(outtmpl_path)},
            "windowsfilenames": True,
            "restrictfilenames": False,
            "trim_file_name": 180,
            "overwrites": overwrites,
            "nooverwrites": nooverwrites,
            "continuedl": True,
            "nopart": False,
            "retries": 3,
            "fragment_retries": 3,
            "socket_timeout": 20,
            "quiet": True,
            "no_warnings": True,
            "js_runtimes": {"deno": {}, "node": {}, "bun": {}, "quickjs": {}},
            "logger": _JobYtdlpLogger(job.job_id),
            "progress_hooks": [
                self._make_progress_hook(job, cancel_event, pause_event, tracked_files),
            ],
            "postprocessor_hooks": [
                self._make_postprocessor_hook(job, cancel_event, pause_event, tracked_files),
            ],
        }

        if ffmpeg_location:
            opts["ffmpeg_location"] = ffmpeg_location

        # Configure playlist behavior
        if job.playlist_mode == PlaylistMode.SINGLE:
            opts["noplaylist"] = True
        elif job.playlist_mode == PlaylistMode.FULL_PLAYLIST:
            opts["noplaylist"] = False
            opts["ignoreerrors"] = "only_download"
        elif job.playlist_mode == PlaylistMode.SELECTED_ITEMS:
            valid_indices = validate_playlist_indices(job.selected_playlist_indices)
            opts["noplaylist"] = False
            opts["ignoreerrors"] = "only_download"
            opts["playlist_items"] = ",".join(str(i) for i in valid_indices)

        # Configure Video vs Audio mode
        if job.mode == DownloadMode.VIDEO:
            opts["format"] = self.build_video_format_selector(
                job.video_quality,
                job.video_format,
                ffmpeg_available=ffmpeg_ready,
            )
            if ffmpeg_ready and job.video_format != VideoFormat.AUTO:
                opts["merge_output_format"] = job.video_format.value
        else:
            if job.audio_format == AudioFormat.BEST:
                opts["format"] = "bestaudio/best"
            elif job.audio_format == AudioFormat.M4A:
                opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"
                if ffmpeg_ready:
                    opts["postprocessors"] = [
                        {
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "m4a",
                            "preferredquality": job.audio_quality.value,
                        }
                    ]
            else:
                opts["format"] = "bestaudio/best"
                opts["postprocessors"] = [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": job.audio_format.value,
                        "preferredquality": job.audio_quality.value,
                    }
                ]

        return opts

    def execute_job(
        self,
        job: DownloadJob,
        cancel_event: threading.Event | None = None,
        pause_event: threading.Event | None = None,
        tracked_files: list[Path] | None = None,
    ) -> DownloadJob:
        """Execute a DownloadJob synchronously on a worker thread and update its state."""
        if tracked_files is None:
            tracked_files = []

        self._check_interruption(cancel_event, pause_event, job, tracked_files)

        try:
            resuming_partial = job.downloaded_bytes > 0 or any(
                p.exists() or Path(f"{p}.part").exists() for p in tracked_files
            )
            job.transition_to(JobStatus.STARTING)
            self._emit(EventType.JOB_STARTING, job)

            ydl_opts = self.build_ydl_options(
                job,
                cancel_event=cancel_event,
                pause_event=pause_event,
                tracked_files=tracked_files,
            )

            with self._ydl_cls(ydl_opts) as ydl:
                self._check_interruption(cancel_event, pause_event, job, tracked_files)

                job.transition_to(JobStatus.FETCHING_METADATA)
                self._emit(EventType.JOB_FETCHING_METADATA, job)

                info = ydl.extract_info(job.url, download=False)
                if not info or not isinstance(info, dict):
                    raise DownloadError(
                        "Failed to retrieve media information prior to download.",
                        remedy="Check that the video is still available and public.",
                    )

                self._check_interruption(cancel_event, pause_event, job, tracked_files)

                extracted_title = str(info.get("title") or "").strip()
                if extracted_title:
                    job.title = extracted_title

                is_playlist_result = (
                    str(info.get("_type", "")).lower() in {"playlist", "multi_video"}
                    or info.get("entries") is not None
                    or job.playlist_mode != PlaylistMode.SINGLE
                )

                # Check collision for single-item downloads when RENAME or ASK is configured
                # and we are not resuming an already-started partial file
                if (
                    not is_playlist_result
                    and not resuming_partial
                    and job.overwrite_behavior in {OverwriteBehavior.RENAME, OverwriteBehavior.ASK}
                ):
                    prepared_str = ydl.prepare_filename(info)
                    if prepared_str:
                        prepared_path = Path(prepared_str)
                        final_ext = self._predict_final_extension(job, prepared_path.suffix)
                        expected_final = prepared_path.with_suffix(final_ext)
                        if expected_final.exists() or prepared_path.exists():
                            unique_path = resolve_unique_filepath(
                                job.destination_dir,
                                expected_final.name,
                            )
                            unique_stem = unique_path.stem
                            new_template = f"{unique_stem}.%(ext)s"
                            ydl.params["outtmpl"] = {
                                "default": str(job.destination_dir / new_template)
                            }

                job.transition_to(JobStatus.DOWNLOADING)
                self._emit(EventType.JOB_PROGRESS, job)

                ydl.process_ie_result(info, download=True)

            self._check_interruption(cancel_event, pause_event, job, tracked_files)

            if is_playlist_result and job.playlist_mode in {
                PlaylistMode.FULL_PLAYLIST,
                PlaylistMode.SELECTED_ITEMS,
            }:
                entries = info.get("entries") if isinstance(info, dict) else None
                if isinstance(entries, list) and len(entries) == 0:
                    raise DownloadError(
                        "The playlist is empty or contains no downloadable videos.",
                        remedy="Check that the playlist has public videos.",
                    )
                job.output_path = job.destination_dir.resolve()
                if tracked_files:
                    total_sz = 0
                    for tf in tracked_files:
                        if tf.exists() and tf.is_file() and not tf.name.endswith(_TEMP_SUFFIXES):
                            try:
                                total_sz += tf.stat().st_size
                            except OSError:
                                pass
                    if total_sz > 0:
                        job.file_size_bytes = total_sz
            else:
                final_path = self._resolve_final_output_path(job, info, tracked_files)
                if final_path is None or not final_path.exists():
                    raise DownloadError(
                        "Download finished, but the expected output file could not be verified on disk.",
                        remedy="Check available disk space, FFmpeg configuration, and destination permissions.",
                    )
                try:
                    size_bytes = final_path.stat().st_size
                except OSError as exc:
                    raise DownloadError(
                        f"Could not inspect output file '{final_path.name}': {exc}",
                        remedy="Verify the destination folder is accessible.",
                    ) from exc

                if size_bytes <= 0:
                    raise DownloadError(
                        f"The output file '{final_path.name}' is empty (0 bytes).",
                        remedy="Delete the empty file and retry the download.",
                    )

                job.output_path = final_path.resolve()
                job.file_size_bytes = size_bytes

            job.transition_to(JobStatus.COMPLETED)
            self._emit(EventType.JOB_COMPLETED, job)
            logger.info("Job %s completed: %s -> %s", job.job_id, job.display_title, job.output_path)
            return job

        except DownloadCancelledError:
            self._mark_cancelled(job, tracked_files)
            raise
        except DownloadPausedError:
            self._mark_paused(job)
            raise
        except (ValidationError, FFmpegMissingError, FilesystemError, DownloadError) as exc:
            self._mark_failed(job, exc.format_for_user())
            raise
        except Exception as exc:
            if cancel_event is not None and cancel_event.is_set():
                self._mark_cancelled(job, tracked_files)
                raise DownloadCancelledError("Download cancelled by user.") from exc
            if pause_event is not None and pause_event.is_set():
                self._mark_paused(job)
                raise DownloadPausedError("Download paused by user.") from exc

            mapped = self.map_download_exception(exc)
            self._mark_failed(job, mapped.format_for_user())
            raise mapped from exc

    def _check_interruption(
        self,
        cancel_event: threading.Event | None,
        pause_event: threading.Event | None,
        job: DownloadJob,
        tracked_files: list[Path],
    ) -> None:
        """Raise DownloadCancelledError or DownloadPausedError if requested."""
        if cancel_event is not None and cancel_event.is_set():
            self._mark_cancelled(job, tracked_files)
            raise DownloadCancelledError("Download cancelled by user.")
        if pause_event is not None and pause_event.is_set():
            self._mark_paused(job)
            raise DownloadPausedError("Download paused by user.")

    def _make_progress_hook(
        self,
        job: DownloadJob,
        cancel_event: threading.Event | None,
        pause_event: threading.Event | None = None,
        tracked_files: list[Path] | None = None,
    ) -> ProgressCallback:
        """Create a yt-dlp progress hook bound to a specific job."""

        def _hook(d: dict[str, Any]) -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelledError("Download cancelled by user.")
            if pause_event is not None and pause_event.is_set():
                raise DownloadPausedError("Download paused by user.")

            if not isinstance(d, dict):
                return

            # Ignore late events if job already reached terminal or paused state
            if job.status.is_terminal or job.status == JobStatus.PAUSED:
                return

            filename = d.get("filename")
            if isinstance(filename, str) and filename and tracked_files is not None:
                file_path = Path(filename)
                if file_path not in tracked_files:
                    tracked_files.append(file_path)

            tmpfilename = d.get("tmpfilename")
            if isinstance(tmpfilename, str) and tmpfilename and tracked_files is not None:
                tmp_path = Path(tmpfilename)
                if tmp_path not in tracked_files:
                    tracked_files.append(tmp_path)

            info_dict = d.get("info_dict")
            new_playlist_item = False
            if isinstance(info_dict, dict):
                title = info_dict.get("title")
                if isinstance(title, str) and title.strip() and not job.title:
                    job.title = title.strip()

                pl_index = self._safe_positive_int(info_dict.get("playlist_index"))
                pl_count = self._safe_positive_int(
                    info_dict.get("n_entries") or info_dict.get("playlist_count")
                )
                if pl_index is not None:
                    if job.playlist_index is not None and pl_index != job.playlist_index:
                        new_playlist_item = True
                    job.playlist_index = pl_index
                if pl_count is not None:
                    job.playlist_total = pl_count

            status_str = str(d.get("status") or "").lower()
            if status_str == "downloading":
                if job.status != JobStatus.DOWNLOADING:
                    job.transition_to(JobStatus.DOWNLOADING)

                downloaded_raw = self._safe_non_negative_int(d.get("downloaded_bytes"))
                total = self._safe_positive_int(d.get("total_bytes")) or self._safe_positive_int(
                    d.get("total_bytes_estimate")
                )
                speed = self._safe_positive_float(d.get("speed"))
                eta = self._safe_non_negative_int(d.get("eta"))

                if downloaded_raw is not None:
                    if new_playlist_item or downloaded_raw >= job.downloaded_bytes:
                        job.downloaded_bytes = downloaded_raw
                job.total_bytes = total
                job.speed_bps = speed
                job.eta_seconds = eta

                if total and total > 0 and downloaded_raw is not None:
                    pct = max(0.0, min(100.0, (job.downloaded_bytes / total) * 100.0))
                    if new_playlist_item or pct >= job.progress_percent:
                        job.progress_percent = round(pct, 1)
                elif d.get("fragment_index") and d.get("fragment_count"):
                    frag_idx = self._safe_non_negative_int(d.get("fragment_index")) or 0
                    frag_cnt = self._safe_positive_int(d.get("fragment_count")) or 1
                    pct = max(0.0, min(100.0, (frag_idx / max(1, frag_cnt)) * 100.0))
                    if new_playlist_item or pct >= job.progress_percent:
                        job.progress_percent = round(pct, 1)

                self._emit(EventType.JOB_PROGRESS, job)

            elif status_str == "finished":
                job.progress_percent = 100.0
                job.speed_bps = None
                job.eta_seconds = None
                if isinstance(filename, str) and filename:
                    job.output_path = Path(filename)
                if job.status != JobStatus.PROCESSING:
                    job.transition_to(JobStatus.PROCESSING)
                self._emit(EventType.JOB_PROCESSING, job)

        return _hook

    def _make_postprocessor_hook(
        self,
        job: DownloadJob,
        cancel_event: threading.Event | None,
        pause_event: threading.Event | None = None,
        tracked_files: list[Path] | None = None,
    ) -> ProgressCallback:
        """Create a yt-dlp postprocessor hook to track merging/conversion and final file paths."""

        def _pp_hook(d: dict[str, Any]) -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelledError("Download cancelled during post-processing.")
            if pause_event is not None and pause_event.is_set():
                raise DownloadPausedError("Download paused prior to post-processing.")

            if not isinstance(d, dict):
                return

            if job.status.is_terminal or job.status == JobStatus.PAUSED:
                return

            status_str = str(d.get("status") or "").lower()
            info_dict = d.get("info_dict")
            if isinstance(info_dict, dict):
                filepath = info_dict.get("filepath") or info_dict.get("_filename")
                if isinstance(filepath, str) and filepath:
                    p = Path(filepath)
                    if tracked_files is not None and p not in tracked_files:
                        tracked_files.append(p)
                    if status_str == "finished" and p.exists():
                        job.output_path = p

            if status_str == "started" and job.status != JobStatus.PROCESSING:
                job.transition_to(JobStatus.PROCESSING)
                self._emit(EventType.JOB_PROCESSING, job)

        return _pp_hook

    @staticmethod
    def _predict_final_extension(job: DownloadJob, current_suffix: str) -> str:
        """Predict the final file extension after merging or audio extraction."""
        if job.mode == DownloadMode.AUDIO:
            if job.audio_format != AudioFormat.BEST:
                return f".{job.audio_format.value}"
            return current_suffix or ".m4a"
        if job.video_format != VideoFormat.AUTO:
            return f".{job.video_format.value}"
        return current_suffix or ".mp4"

    @classmethod
    def _resolve_final_output_path(
        cls,
        job: DownloadJob,
        info: dict[str, Any],
        tracked_files: list[Path],
    ) -> Path | None:
        """Determine and validate the final output path on disk after download and post-processing."""
        required_ext: str | None = None
        if job.mode == DownloadMode.AUDIO and job.audio_format != AudioFormat.BEST:
            required_ext = f".{job.audio_format.value.lower()}"

        candidates: list[Path] = []
        if job.output_path:
            candidates.append(job.output_path)

        req_downloads = info.get("requested_downloads")
        if isinstance(req_downloads, list):
            for item in reversed(req_downloads):
                if isinstance(item, dict):
                    fp = item.get("filepath") or item.get("_filename")
                    if isinstance(fp, str) and fp:
                        candidates.append(Path(fp))

        for tracked in reversed(tracked_files):
            if required_ext:
                converted = tracked.with_suffix(required_ext)
                if converted not in candidates:
                    candidates.append(converted)
            elif job.mode == DownloadMode.VIDEO and job.video_format != VideoFormat.AUTO:
                merged = tracked.with_suffix(f".{job.video_format.value}")
                if merged not in candidates:
                    candidates.append(merged)
            if tracked not in candidates:
                candidates.append(tracked)

        for candidate in candidates:
            if not candidate.exists() or not candidate.is_file():
                continue
            if candidate.name.endswith(_TEMP_SUFFIXES):
                continue
            if required_ext and candidate.suffix.lower() != required_ext:
                # Reject unconverted source stream when a specific audio format was requested
                continue
            return candidate

        return None

    @staticmethod
    def _is_partial_or_temp_filename(name: str) -> bool:
        """Return True only for partial/incomplete download artifacts, never completed media files."""
        lower = name.lower()
        if lower.endswith((".part", ".ytdl", ".temp", ".f137", ".f140", ".f248", ".f251")):
            return True
        if ".part-frag" in lower or lower.endswith(".part"):
            return True
        return False

    @classmethod
    def cleanup_partial_files(
        cls,
        tracked_files: list[Path] | DownloadJob,
        destination_dir: Path | None = None,
    ) -> int:
        """Remove temporary or partial files (.part, .part-Frag*, .ytdl, .temp) for a cancelled job."""
        removed = 0
        paths_to_check: list[Path] = []
        scan_dir = destination_dir

        if isinstance(tracked_files, DownloadJob):
            scan_dir = tracked_files.destination_dir
        else:
            for path in tracked_files:
                paths_to_check.extend((path, Path(f"{path}.part"), Path(f"{path}.ytdl")))

        if scan_dir is not None and scan_dir.exists() and scan_dir.is_dir():
            try:
                for entry in scan_dir.iterdir():
                    if entry.is_file() and cls._is_partial_or_temp_filename(entry.name):
                        if entry not in paths_to_check:
                            paths_to_check.append(entry)
            except OSError:
                pass

        seen: set[Path] = set()
        for candidate in paths_to_check:
            if candidate in seen:
                continue
            seen.add(candidate)
            try:
                if candidate.exists() and candidate.is_file():
                    if cls._is_partial_or_temp_filename(candidate.name):
                        candidate.unlink(missing_ok=True)
                        removed += 1
            except OSError:
                pass
        return removed

    def _mark_cancelled(self, job: DownloadJob, tracked_files: list[Path]) -> None:
        """Clean up partial temporary files and transition job to CANCELLED."""
        self.cleanup_partial_files(tracked_files, destination_dir=job.destination_dir)
        try:
            job.transition_to(JobStatus.CANCELLED, error_message="Cancelled by user.")
        except Exception:
            job.status = JobStatus.CANCELLED
            job.error_message = "Cancelled by user."

        self._emit(EventType.JOB_CANCELLED, job)
        logger.info("Job %s cancelled.", job.job_id)

    def _mark_paused(self, job: DownloadJob) -> None:
        """Transition job to PAUSED while preserving partial (.part) files on disk for resumption."""
        try:
            job.transition_to(JobStatus.PAUSED)
        except Exception:
            job.status = JobStatus.PAUSED
            job.speed_bps = None
            job.eta_seconds = None

        self._emit(EventType.JOB_PAUSED, job)
        logger.info("Job %s paused at %.1f%%.", job.job_id, job.progress_percent)

    def _mark_failed(self, job: DownloadJob, message: str) -> None:
        """Transition job to FAILED and emit failure event."""
        try:
            job.transition_to(JobStatus.FAILED, error_message=message)
        except Exception:
            job.status = JobStatus.FAILED
            job.error_message = message

        self._emit(EventType.JOB_FAILED, job)
        logger.error("Job %s failed: %s", job.job_id, message)

    def _emit(self, event_type: EventType, job: DownloadJob) -> None:
        """Publish a job status update event to the EventBus if attached."""
        if self._event_bus is not None:
            self._event_bus.publish(event_type, job_id=job.job_id, job=job)

    @staticmethod
    def map_download_exception(exc: Exception) -> DownloadError:
        """Map low-level yt-dlp or OS exceptions to clear user-facing DownloadError instances."""
        if isinstance(exc, OSError) and getattr(exc, "errno", None) == errno.ENOSPC:
            return DownloadError(
                "Insufficient disk space to complete the download.",
                remedy="Free up disk space or choose a different drive in Download Location.",
            )

        raw = str(exc)
        lowered = raw.lower()

        if "no space left on device" in lowered or "not enough space" in lowered:
            return DownloadError(
                "Insufficient disk space on the destination drive.",
                remedy="Free up space on your drive or select another download folder.",
            )
        if "ffmpeg" in lowered or "ffprobe" in lowered or "postprocessing" in lowered:
            return DownloadError(
                "Media processing failed because FFmpeg/FFprobe encountered an error or is missing.",
                remedy="Verify FFmpeg and FFprobe are installed and configured in Settings.",
            )
        if "429" in lowered or "too many requests" in lowered:
            return DownloadError(
                "YouTube temporarily rate-limited download requests (HTTP 429).",
                remedy="Wait a few minutes and retry the download.",
            )
        if "403" in lowered or "forbidden" in lowered:
            return DownloadError(
                "YouTube rejected the media stream request (HTTP 403 Forbidden).",
                remedy="Retry the download or ensure yt-dlp and a JS runtime (Node/Deno) are up to date.",
            )
        if "404" in lowered or "not found" in lowered:
            return DownloadError(
                "The requested media stream could not be found (HTTP 404).",
                remedy="Verify the video URL is still valid and retry.",
            )
        if "private video" in lowered or "sign in" in lowered or "members-only" in lowered:
            return DownloadError(
                "This video is private, age-restricted, or requires authentication.",
                remedy="Only publicly accessible content that you are authorized to save is supported.",
            )
        if "video unavailable" in lowered or "removed by the uploader" in lowered or "has been removed" in lowered:
            return DownloadError(
                "The requested video is no longer available on YouTube.",
                remedy="Check if the video was deleted or made private.",
            )
        if "requested format is not available" in lowered or "no video formats found" in lowered:
            return DownloadError(
                "The requested media format or resolution is not available for this video.",
                remedy="Select 'Best Available' quality or 'Auto' container format and retry.",
            )
        if (
            "timed out" in lowered
            or "getaddrinfo" in lowered
            or "network" in lowered
            or "connection reset" in lowered
            or "urlopen error" in lowered
            or "temporary failure in name resolution" in lowered
        ):
            return DownloadError(
                "A network connection error interrupted the download.",
                remedy="Check your internet connection and click 'Retry'.",
            )
        if "permission denied" in lowered or "access is denied" in lowered:
            return DownloadError(
                "Permission denied while writing the output file.",
                remedy="Ensure the destination folder is writable and the file is not open in another program.",
            )

        return DownloadError(
            "Download failed due to an unexpected extractor or stream error.",
            remedy="Check the application logs for technical details or retry the download.",
        )

    @staticmethod
    def _safe_non_negative_int(val: Any) -> int | None:
        if val is None or isinstance(val, bool):
            return None
        try:
            fval = float(val)
            if not math.isfinite(fval):
                return None
            ival = int(fval)
            return ival if ival >= 0 else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_positive_int(val: Any) -> int | None:
        if val is None or isinstance(val, bool):
            return None
        try:
            fval = float(val)
            if not math.isfinite(fval):
                return None
            ival = int(fval)
            return ival if ival > 0 else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_positive_float(val: Any) -> float | None:
        if val is None or isinstance(val, bool):
            return None
        try:
            fval = float(val)
            if not math.isfinite(fval) or fval <= 0.0:
                return None
            return fval
        except (TypeError, ValueError):
            return None
