"""Dependency discovery and diagnostics for yt-dlp, FFmpeg, and FFprobe."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from src.core.models import DependencyStatus
from src.utils.logging_config import get_logger

logger = get_logger("ffmpeg_service")

_WINDOWS_COMMON_FFMPEG_DIRS: tuple[str, ...] = (
    r"C:\ffmpeg\bin",
    r"C:\Program Files\ffmpeg\bin",
    r"C:\ProgramData\chocolatey\bin",
)


class FFmpegService:
    """Detects and verifies FFmpeg, FFprobe, and yt-dlp without executing untrusted shells."""

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

    @staticmethod
    def get_missing_ffmpeg_explanation() -> str:
        """Return a clear explanation of what requires FFmpeg and how to install or configure it."""
        return (
            "FFmpeg is not currently detected on your system.\n\n"
            "Features requiring FFmpeg:\n"
            "• Merging separate high-resolution video and audio streams (e.g., 1080p/1440p/4K)\n"
            "• Converting audio streams into MP3, WAV, Opus, or FLAC\n"
            "• Remuxing containers when required\n\n"
            "Available without FFmpeg:\n"
            "• Pre-merged progressive video streams (typically up to 720p MP4)\n"
            "• Native audio streams ('Best Audio Stream' or native M4A)\n\n"
            "How to install FFmpeg on Windows:\n"
            "1. Open PowerShell and run:  winget install Gyan.FFmpeg\n"
            "2. Or download a release build from https://www.gyan.dev/ffmpeg/builds/ and extract ffmpeg.exe and ffprobe.exe to C:\\ffmpeg\\bin\n"
            "3. Or open Settings in YouTube Downloader Studio and browse to your ffmpeg.exe path."
        )
