"""Dependency discovery, local media probing, trimming, and soft-subtitle muxing via FFmpeg/FFprobe."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from src.core.exceptions import (
    DownloadCancelledError,
    DownloadError,
    FFmpegMissingError,
    ValidationError,
)
from src.core.models import (
    AudioFormat,
    AudioQuality,
    DependencyStatus,
    LocalMediaInfo,
)
from src.core.validators import (
    SUPPORTED_LOCAL_AUDIO_EXTENSIONS,
    format_timestamp,
    parse_timestamp,
    validate_local_media_path,
    validate_trim_range,
)
from src.utils.logging_config import get_logger

logger = get_logger("ffmpeg_service")

_WINDOWS_COMMON_FFMPEG_DIRS: tuple[str, ...] = (
    r"C:\ffmpeg\bin",
    r"C:\Program Files\ffmpeg\bin",
    r"C:\ProgramData\chocolatey\bin",
)

_FFMPEG_DURATION_RE = re.compile(r"Duration:\s*(\d{2}:\d{2}:\d{2}(?:\.\d+)?)", re.IGNORECASE)
_FFMPEG_STREAM_RE = re.compile(
    r"Stream\s+#\d+:\d+.*?:\s*(Video|Audio|Subtitle):\s*([A-Za-z0-9_]+)?",
    re.IGNORECASE,
)


class FFmpegService:
    """Detects and executes FFmpeg and FFprobe safely without invoking untrusted shells."""

    def __init__(self, custom_ffmpeg_path: str = "") -> None:
        self._custom_path = custom_ffmpeg_path.strip()
        self._cached_status: DependencyStatus | None = None

    def set_custom_path(self, custom_path: str) -> None:
        """Update the user-configured FFmpeg path and invalidate cached status."""
        self._custom_path = (custom_path or "").strip()
        self._cached_status = None

    @staticmethod
    def _resolve_binary(binary_name: str, custom_path: str = "") -> Path | None:
        """Locate an executable binary (ffmpeg or ffprobe) across custom, env, PATH, and standard folders."""
        exe_name = f"{binary_name}.exe" if sys.platform == "win32" else binary_name

        candidates: list[Path] = []
        for raw_hint in (custom_path, os.environ.get("YTD_STUDIO_FFMPEG_PATH", "").strip()):
            if not raw_hint:
                continue
            hint_path = Path(raw_hint).expanduser()
            if hint_path.is_file():
                if hint_path.stem.lower().startswith(binary_name.lower()):
                    candidates.append(hint_path)
                else:
                    candidates.append(hint_path.parent / exe_name)
            elif hint_path.is_dir():
                candidates.append(hint_path / exe_name)
                candidates.append(hint_path / "bin" / exe_name)

        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                return candidate.resolve()

        which_hit = shutil.which(binary_name) or shutil.which(exe_name)
        if which_hit:
            return Path(which_hit).resolve()

        if sys.platform == "win32":
            local_appdata = os.environ.get("LOCALAPPDATA", "")
            user_profile = os.environ.get("USERPROFILE", "")
            extra_dirs = list(_WINDOWS_COMMON_FFMPEG_DIRS)
            if local_appdata:
                extra_dirs.append(str(Path(local_appdata) / "Microsoft" / "WinGet" / "Links"))
            if user_profile:
                extra_dirs.append(str(Path(user_profile) / "scoop" / "shims"))
            for folder in extra_dirs:
                probe = Path(folder) / exe_name
                if probe.exists() and probe.is_file():
                    return probe.resolve()

        return None

    @staticmethod
    def _query_binary_version(binary_path: Path) -> str | None:
        """Run `<binary> -version` safely via argument list and return the first version line."""
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        try:
            proc = subprocess.run(
                [str(binary_path), "-version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                creationflags=creationflags,
            )
            if proc.returncode == 0 and proc.stdout:
                first_line = proc.stdout.splitlines()[0].strip()
                return first_line[:120] if first_line else "Available"
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("Failed to execute %s -version: %s", binary_path, exc)
        return None

    @staticmethod
    def _detect_yt_dlp() -> tuple[bool, str | None]:
        """Verify yt-dlp module availability and version."""
        try:
            import yt_dlp.version  # type: ignore[import-untyped]

            return True, str(getattr(yt_dlp.version, "__version__", "unknown"))
        except Exception as exc:
            logger.error("yt-dlp import failed: %s", exc)
            return False, None

    def check_dependencies(self, force_refresh: bool = False) -> DependencyStatus:
        """Inspect runtime dependencies and cache the resulting DependencyStatus."""
        if self._cached_status is not None and not force_refresh:
            return self._cached_status

        py_ver = platform.python_version()
        yt_ok, yt_ver = self._detect_yt_dlp()

        ffmpeg_path = self._resolve_binary("ffmpeg", self._custom_path)
        ffmpeg_ver: str | None = None
        ffmpeg_ok = False
        if ffmpeg_path is not None:
            ffmpeg_ver = self._query_binary_version(ffmpeg_path)
            ffmpeg_ok = ffmpeg_ver is not None

        ffprobe_path = self._resolve_binary("ffprobe", self._custom_path)
        ffprobe_ver: str | None = None
        ffprobe_ok = False
        if ffprobe_path is not None:
            ffprobe_ver = self._query_binary_version(ffprobe_path)
            ffprobe_ok = ffprobe_ver is not None

        status = DependencyStatus(
            python_version=py_ver,
            yt_dlp_available=yt_ok,
            yt_dlp_version=yt_ver,
            ffmpeg_available=ffmpeg_ok,
            ffmpeg_path=str(ffmpeg_path) if ffmpeg_ok and ffmpeg_path else None,
            ffmpeg_version=ffmpeg_ver,
            ffprobe_available=ffprobe_ok,
            ffprobe_path=str(ffprobe_path) if ffprobe_ok and ffprobe_path else None,
            ffprobe_version=ffprobe_ver,
        )
        self._cached_status = status
        logger.info(
            "Dependency check complete: yt-dlp=%s, ffmpeg=%s (%s), ffprobe=%s (%s)",
            yt_ver,
            ffmpeg_ok,
            status.ffmpeg_path,
            ffprobe_ok,
            status.ffprobe_path,
        )
        return status

    def is_ffmpeg_available(self) -> bool:
        """Return True if a working ffmpeg executable is available."""
        return self.check_dependencies().ffmpeg_available

    def get_ffmpeg_binary_path(self) -> Path:
        """Return the resolved Path to ffmpeg.exe or raise FFmpegMissingError."""
        status = self.check_dependencies()
        if not status.ffmpeg_available or not status.ffmpeg_path:
            raise FFmpegMissingError(
                "FFmpeg is required for this operation, but was not detected on your system.",
                remedy=self.get_missing_ffmpeg_explanation(),
            )
        return Path(status.ffmpeg_path)

    def get_ffmpeg_location_for_ytdlp(self) -> str | None:
        """Return the directory or binary path to pass as `ffmpeg_location` to yt-dlp."""
        status = self.check_dependencies()
        if not status.ffmpeg_available or not status.ffmpeg_path:
            return None
        path_obj = Path(status.ffmpeg_path)
        if status.ffprobe_available and status.ffprobe_path:
            ffprobe_obj = Path(status.ffprobe_path)
            if ffprobe_obj.parent == path_obj.parent:
                return str(path_obj.parent)
        return str(path_obj)

    def run_cancellable_command(
        self,
        args: list[str],
        cancel_event: threading.Event | None = None,
        timeout_seconds: float = 1800.0,
    ) -> subprocess.CompletedProcess[str]:
        """Run a subprocess argument list safely with cooperative cancellation polling."""
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelledError("Media processing cancelled by user.")

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        try:
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
            )
        except OSError as exc:
            raise DownloadError(
                f"Failed to start media processing command: {exc}",
                remedy="Check that FFmpeg is properly installed and accessible.",
            ) from exc

        start_time = time.monotonic()
        while True:
            if cancel_event is not None and cancel_event.is_set():
                self._terminate_process(proc)
                raise DownloadCancelledError("Media processing cancelled by user.")

            if (time.monotonic() - start_time) > timeout_seconds:
                self._terminate_process(proc)
                raise DownloadError(
                    "FFmpeg processing timed out.",
                    remedy="Try trimming a shorter segment or using Fast Stream Copy mode.",
                )

            try:
                stdout_str, stderr_str = proc.communicate(timeout=0.05)
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=proc.returncode or 0,
                    stdout=stdout_str or "",
                    stderr=stderr_str or "",
                )
            except subprocess.TimeoutExpired:
                continue

    @staticmethod
    def _terminate_process(proc: subprocess.Popen[str]) -> None:
        """Terminate and, if necessary, kill a running subprocess cleanly."""
        try:
            proc.terminate()
            proc.wait(timeout=1.5)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=1.0)
            except Exception:
                pass

    def probe_local_media(self, file_path: Path | str) -> LocalMediaInfo:
        """Inspect a local video or audio file using FFprobe (or FFmpeg -i fallback when FFprobe is absent)."""
        validated_path = validate_local_media_path(file_path)
        status = self.check_dependencies()

        if status.ffprobe_available and status.ffprobe_path:
            return self._probe_with_ffprobe(Path(status.ffprobe_path), validated_path)

        if status.ffmpeg_available and status.ffmpeg_path:
            return self._probe_with_ffmpeg_stderr(Path(status.ffmpeg_path), validated_path)

        raise FFmpegMissingError(
            "Inspecting local media files requires FFmpeg or FFprobe, neither of which was detected.",
            remedy=self.get_missing_ffmpeg_explanation(),
        )

    def _probe_with_ffprobe(self, ffprobe_bin: Path, media_path: Path) -> LocalMediaInfo:
        """Inspect media duration and stream layout via ffprobe JSON output."""
        proc = self.run_cancellable_command(
            [
                str(ffprobe_bin),
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(media_path),
            ],
            timeout_seconds=20.0,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            # Fallback to ffmpeg stderr if ffprobe exists alongside ffmpeg
            status = self.check_dependencies()
            if status.ffmpeg_available and status.ffmpeg_path:
                return self._probe_with_ffmpeg_stderr(Path(status.ffmpeg_path), media_path)
            raise DownloadError(
                f"Could not inspect media file '{media_path.name}'. The file may be corrupted or unsupported.",
                remedy="Choose a valid video or audio file.",
            )

        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise DownloadError(
                f"Could not parse media stream metadata for '{media_path.name}'.",
                remedy="Verify the media file is not corrupted.",
            ) from exc

        streams = payload.get("streams") if isinstance(payload, dict) else None
        fmt_info = payload.get("format") if isinstance(payload, dict) else None

        has_video = False
        has_audio = False
        v_codec: str | None = None
        a_codec: str | None = None
        sub_count = 0
        max_stream_dur = 0.0

        if isinstance(streams, list):
            for st in streams:
                if not isinstance(st, dict):
                    continue
                codec_type = str(st.get("codec_type") or "").lower()
                codec_name = str(st.get("codec_name") or "").strip() or None
                st_dur = self._safe_float(st.get("duration"))
                if st_dur and st_dur > max_stream_dur:
                    max_stream_dur = st_dur

                if codec_type == "video" and codec_name not in {"mjpeg", "png", "webp"}:
                    has_video = True
                    v_codec = v_codec or codec_name
                elif codec_type == "audio":
                    has_audio = True
                    a_codec = a_codec or codec_name
                elif codec_type == "subtitle":
                    sub_count += 1

        fmt_dur = self._safe_float(fmt_info.get("duration")) if isinstance(fmt_info, dict) else None
        duration = fmt_dur if (fmt_dur and fmt_dur > 0) else max_stream_dur

        if not has_video and not has_audio:
            raise DownloadError(
                f"No playable video or audio streams were found in '{media_path.name}'.",
                remedy="Select a valid video or audio file.",
            )

        if duration <= 0:
            raise DownloadError(
                f"Could not determine a valid duration for '{media_path.name}'.",
                remedy="Ensure the media file has a valid header and duration.",
            )

        container = (
            str(fmt_info.get("format_name") or "")
            if isinstance(fmt_info, dict)
            else media_path.suffix.lstrip(".")
        )
        return LocalMediaInfo(
            file_path=media_path,
            duration_seconds=round(duration, 3),
            has_video=has_video,
            has_audio=has_audio,
            video_codec=v_codec,
            audio_codec=a_codec,
            subtitle_streams=sub_count,
            container_format=container,
        )

    def _probe_with_ffmpeg_stderr(self, ffmpeg_bin: Path, media_path: Path) -> LocalMediaInfo:
        """Inspect media duration and streams by parsing `ffmpeg -hide_banner -i <file>` stderr output."""
        proc = self.run_cancellable_command(
            [str(ffmpeg_bin), "-hide_banner", "-i", str(media_path)],
            timeout_seconds=20.0,
        )
        output = f"{proc.stderr}\n{proc.stdout}"
        dur_match = _FFMPEG_DURATION_RE.search(output)
        if not dur_match:
            raise DownloadError(
                f"Could not inspect media file '{media_path.name}'. It may be corrupted or an unsupported format.",
                remedy="Choose a valid, playable video or audio file.",
            )

        try:
            duration = parse_timestamp(dur_match.group(1))
        except ValidationError as exc:
            raise DownloadError(
                f"Could not parse duration of '{media_path.name}'.",
                remedy="Verify the file is a valid video or audio recording.",
            ) from exc

        if duration <= 0:
            raise DownloadError(
                f"Media file '{media_path.name}' reported a duration of 0 seconds.",
                remedy="Choose a media file with a non-zero duration.",
            )

        has_video = False
        has_audio = False
        v_codec: str | None = None
        a_codec: str | None = None
        sub_count = 0

        for match in _FFMPEG_STREAM_RE.finditer(output):
            kind = match.group(1).lower()
            codec = (match.group(2) or "").lower() or None
            if kind == "video" and codec not in {"mjpeg", "png", "webp"}:
                has_video = True
                v_codec = v_codec or codec
            elif kind == "audio":
                has_audio = True
                a_codec = a_codec or codec
            elif kind == "subtitle":
                sub_count += 1

        if not has_video and not has_audio:
            raise DownloadError(
                f"No video or audio streams were detected in '{media_path.name}'.",
                remedy="Select a valid video or audio file.",
            )

        return LocalMediaInfo(
            file_path=media_path,
            duration_seconds=round(duration, 3),
            has_video=has_video,
            has_audio=has_audio,
            video_codec=v_codec,
            audio_codec=a_codec,
            subtitle_streams=sub_count,
            container_format=media_path.suffix.lower().lstrip("."),
        )

    def trim_media_file(
        self,
        source_path: Path | str,
        output_path: Path | str,
        start_seconds: float,
        end_seconds: float,
        accurate_trim: bool = True,
        audio_only_output: bool = False,
        audio_format: AudioFormat = AudioFormat.MP3,
        audio_quality: AudioQuality = AudioQuality.K192,
        cancel_event: threading.Event | None = None,
        probed_info: LocalMediaInfo | None = None,
        audio_only: bool | None = None,
    ) -> Path:
        """Trim a local video or audio file into a new output file without modifying the source file."""
        if audio_only is not None:
            audio_only_output = audio_only
        ffmpeg_bin = self.get_ffmpeg_binary_path()
        src = validate_local_media_path(source_path)
        info = probed_info or self.probe_local_media(src)

        valid_start, valid_end = validate_trim_range(
            start_seconds,
            end_seconds,
            media_duration=info.duration_seconds,
        )
        clip_duration = round(valid_end - valid_start, 3)

        out = Path(output_path).expanduser().resolve()
        # Safety guarantee: never overwrite the original source file in-place
        if out == src:
            ts_tag = (
                f"{format_timestamp(valid_start).replace(':', '-')}_to_"
                f"{format_timestamp(valid_end).replace(':', '-')}"
            )
            out = src.with_name(f"{src.stem}_trimmed_{ts_tag}{src.suffix}")

        out.parent.mkdir(parents=True, exist_ok=True)
        temp_out = out.with_name(f".ytd_trim_tmp_{out.name}")

        is_source_audio_only = (not info.has_video) or (src.suffix.lower() in SUPPORTED_LOCAL_AUDIO_EXTENSIONS)
        produce_audio_only = audio_only_output or is_source_audio_only

        if produce_audio_only and not info.has_audio:
            raise DownloadError(
                f"Cannot extract an audio clip from '{src.name}' because it contains no audio stream.",
                remedy="Use Video mode to trim this video-only file.",
            )

        args: list[str] = [
            str(ffmpeg_bin),
            "-hide_banner",
            "-y",
            "-ss",
            f"{valid_start:.3f}",
            "-i",
            str(src),
            "-t",
            f"{clip_duration:.3f}",
        ]

        if produce_audio_only:
            args.append("-vn")
            target_ext = out.suffix.lower().lstrip(".")
            if not accurate_trim and target_ext == src.suffix.lower().lstrip("."):
                args.extend(["-c:a", "copy"])
            else:
                codec_args = self._build_audio_codec_args(target_ext, audio_format, audio_quality)
                args.extend(codec_args)
        else:
            if not accurate_trim:
                args.extend(["-c", "copy"])
            else:
                target_ext = out.suffix.lower().lstrip(".")
                if target_ext == "webm":
                    args.extend(["-c:v", "libvpx-vp9", "-crf", "30", "-b:v", "0"])
                    if info.has_audio:
                        args.extend(["-c:a", "libopus"])
                else:
                    args.extend(["-c:v", "libx264", "-preset", "fast", "-crf", "18"])
                    if info.has_audio:
                        args.extend(["-c:a", "aac", "-b:a", "192k"])
                    else:
                        args.append("-an")

        args.append(str(temp_out))
        logger.info(
            "Running FFmpeg trim: %s [%.2fs -> %.2fs] -> %s",
            src.name,
            valid_start,
            valid_end,
            out.name,
        )

        try:
            proc = self.run_cancellable_command(args, cancel_event=cancel_event)
            if proc.returncode != 0:
                err_tail = "\n".join(proc.stderr.strip().splitlines()[-4:]) if proc.stderr else "Unknown FFmpeg error"
                raise DownloadError(
                    f"FFmpeg failed while trimming '{src.name}': {err_tail}",
                    remedy="Try toggling 'Accurate Cut (Re-encode)' or choosing an MP4/MKV output format.",
                )

            if not temp_out.exists() or temp_out.stat().st_size <= 0:
                raise DownloadError(
                    f"Trimming produced an empty or missing output file for '{src.name}'.",
                    remedy="Verify the start and end timestamps fall within a playable section of the file.",
                )

            temp_out.replace(out)
            return out
        except Exception:
            try:
                temp_out.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    @staticmethod
    def _build_audio_codec_args(
        target_ext: str,
        audio_format: AudioFormat,
        audio_quality: AudioQuality,
    ) -> list[str]:
        """Return FFmpeg audio encoder arguments for a target extension."""
        ext = target_ext or audio_format.value
        bitrate = f"{audio_quality.value}k" if audio_quality != AudioQuality.BEST else "256k"
        if ext == "mp3":
            return ["-c:a", "libmp3lame", "-b:a", bitrate]
        if ext == "wav":
            return ["-c:a", "pcm_s16le"]
        if ext == "flac":
            return ["-c:a", "flac"]
        if ext in {"opus", "ogg"}:
            return ["-c:a", "libopus", "-b:a", bitrate]
        return ["-c:a", "aac", "-b:a", bitrate]

    def embed_soft_subtitles(
        self,
        video_path: Path | str,
        subtitle_tracks: list[tuple[Path, str]],
        cancel_event: threading.Event | None = None,
    ) -> Path:
        """Mux one or more subtitle files as soft subtitle streams into a video container without re-encoding video/audio.

        Preserves the original video file untouched if muxing or verification fails.
        """
        ffmpeg_bin = self.get_ffmpeg_binary_path()
        vid = Path(video_path).expanduser().resolve()
        if not vid.exists() or not vid.is_file():
            raise DownloadError(
                f"Cannot embed subtitles because video file '{vid.name}' does not exist.",
                remedy="Ensure the video download succeeded before embedding subtitles.",
            )

        valid_subs = [(Path(p).resolve(), lang) for p, lang in subtitle_tracks if Path(p).exists() and Path(p).stat().st_size > 0]
        if not valid_subs:
            raise DownloadError(
                "No valid subtitle files were available to embed into the video.",
                remedy="Verify the selected caption language exists for this video.",
            )

        ext = vid.suffix.lower()
        if ext in {".mp4", ".m4v", ".mov"}:
            sub_codec = "mov_text"
        elif ext == ".mkv":
            sub_codec = "srt"
        elif ext == ".webm":
            sub_codec = "webvtt"
        else:
            raise DownloadError(
                f"Container format '{ext}' does not support soft-subtitle embedding.",
                remedy="Select MP4, MKV, or WebM as the video format when embedding soft subtitles.",
            )

        temp_muxed = vid.with_name(f".ytd_submux_tmp_{vid.name}")
        args: list[str] = [
            str(ffmpeg_bin),
            "-hide_banner",
            "-y",
            "-i",
            str(vid),
        ]
        for sub_file, _lang in valid_subs:
            args.extend(["-i", str(sub_file)])

        args.extend(["-map", "0:v?", "-map", "0:a?"])
        for idx in range(len(valid_subs)):
            args.extend(["-map", f"{idx + 1}:0"])

        args.extend(["-c:v", "copy", "-c:a", "copy", "-c:s", sub_codec])

        for idx, (_sub_file, lang_code) in enumerate(valid_subs):
            iso_lang = (lang_code or "und").split("-")[0][:3].lower()
            args.extend(
                [
                    f"-metadata:s:s:{idx}",
                    f"language={iso_lang}",
                    f"-metadata:s:s:{idx}",
                    f"title={lang_code}",
                ]
            )

        args.append(str(temp_muxed))
        logger.info("Embedding %d soft-subtitle track(s) into %s (codec=%s)", len(valid_subs), vid.name, sub_codec)

        try:
            proc = self.run_cancellable_command(args, cancel_event=cancel_event)
            if proc.returncode != 0:
                err_tail = "\n".join(proc.stderr.strip().splitlines()[-4:]) if proc.stderr else "Unknown FFmpeg mux error"
                raise DownloadError(
                    f"Soft-subtitle embedding failed for '{vid.name}': {err_tail}",
                    remedy="Choose MKV or MP4 container format, or keep separate subtitle files.",
                )

            if not temp_muxed.exists() or temp_muxed.stat().st_size <= 0:
                raise DownloadError(
                    f"Subtitle muxing produced an empty output file for '{vid.name}'.",
                    remedy="Keep separate subtitle files or try MKV container format.",
                )

            if not self.verify_subtitle_streams(temp_muxed, min_streams=len(valid_subs)):
                raise DownloadError(
                    f"Output verification failed: embedded subtitle stream was not detected in '{vid.name}'.",
                    remedy="Try using MKV or MP4 container format.",
                )

            temp_muxed.replace(vid)
            return vid
        except Exception:
            try:
                temp_muxed.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def verify_subtitle_streams(self, video_path: Path | str, min_streams: int = 1) -> bool:
        """Return True if the video container exists and has at least `min_streams` subtitle streams."""
        try:
            info = self.probe_local_media(video_path)
            return info.subtitle_streams >= min_streams
        except Exception as exc:
            logger.warning("Subtitle stream verification failed for %s: %s", video_path, exc)
            return False

    @staticmethod
    def _safe_float(val: object) -> float | None:
        if val is None:
            return None
        try:
            return float(str(val))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def get_missing_ffmpeg_explanation() -> str:
        """Return a clear explanation of what requires FFmpeg and how to install or configure it."""
        return (
            "FFmpeg is not currently detected on your system.\n\n"
            "Features requiring FFmpeg:\n"
            "• Merging separate high-resolution video and audio streams (e.g., 1080p/1440p/4K)\n"
            "• Converting audio streams into MP3, WAV, Opus, or FLAC\n"
            "• Video and audio segment trimming (YouTube URL clips and local file trimming)\n"
            "• Embedding soft subtitles into MP4, MKV, or WebM containers\n\n"
            "Available WITHOUT FFmpeg:\n"
            "• Standalone 'Captions Only' mode (downloads & exports SRT, VTT, and TXT in pure Python)\n"
            "• Separate subtitle file downloads alongside progressive videos\n"
            "• Pre-merged progressive video streams (typically up to 720p MP4)\n"
            "• Native audio streams ('Best Audio Stream' or native M4A)\n\n"
            "How to install FFmpeg on Windows:\n"
            "1. Open PowerShell and run:  winget install Gyan.FFmpeg\n"
            "2. Or download a release build from https://www.gyan.dev/ffmpeg/builds/ and extract ffmpeg.exe and ffprobe.exe to C:\\ffmpeg\\bin\n"
            "3. Or open Settings in YouTube Downloader Studio and browse to your ffmpeg.exe path."
        )
