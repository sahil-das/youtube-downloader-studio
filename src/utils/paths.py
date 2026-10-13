"""Filesystem path resolution and safe path utilities for Windows and PyInstaller."""

from __future__ import annotations

import os
import re
import sys
import uuid
from pathlib import Path

from src.core.exceptions import FilesystemError, ValidationError

APP_DIR_NAME = "YouTubeDownloaderStudio"

WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)

_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def is_frozen() -> bool:
    """Return True when running inside a PyInstaller packaged executable."""
    return bool(getattr(sys, "frozen", False))


def get_bundle_dir() -> Path:
    """Return the root directory containing packaged resources or source repository root."""
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass).resolve()
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent


def get_assets_dir() -> Path:
    """Return the path to the application's static assets directory."""
    return get_bundle_dir() / "assets"


def get_app_data_dir(custom_dir: Path | str | None = None) -> Path:
    """Return the writable user application data directory.

    Never returns a path inside the PyInstaller _MEIPASS extraction directory.
    """
    if custom_dir is not None:
        base = Path(custom_dir).expanduser().resolve()
    else:
        env_override = os.environ.get("YTD_STUDIO_DATA_DIR", "").strip()
        if env_override:
            base = Path(env_override).expanduser().resolve()
        elif sys.platform == "win32":
            local_app_data = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
            if local_app_data:
                base = Path(local_app_data).resolve() / APP_DIR_NAME
            else:
                base = Path.home().resolve() / "AppData" / "Local" / APP_DIR_NAME
        else:
            base = Path.home().resolve() / ".local" / "share" / APP_DIR_NAME

    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise FilesystemError(
            f"Cannot create application data directory at '{base}'.",
            remedy="Check folder permissions or set YTD_STUDIO_DATA_DIR to a writable folder.",
        ) from exc

    return base


def get_database_path(app_data_dir: Path | None = None) -> Path:
    """Return the path to the SQLite database file."""
    base = app_data_dir or get_app_data_dir()
    return base / "history.db"


def get_settings_path(app_data_dir: Path | None = None) -> Path:
    """Return the path to the persistent JSON settings file."""
    base = app_data_dir or get_app_data_dir()
    return base / "settings.json"


def get_logs_dir(app_data_dir: Path | None = None) -> Path:
    """Return the writable directory for application log files."""
    base = app_data_dir or get_app_data_dir()
    logs_dir = base / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir


def get_temp_dir(app_data_dir: Path | None = None) -> Path:
    """Return the writable temporary directory for in-progress operations."""
    base = app_data_dir or get_app_data_dir()
    temp_dir = base / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    return temp_dir


def get_default_download_dir() -> Path:
    """Return the default user Downloads directory for saved media."""
    downloads = Path.home() / "Downloads" / "YouTubeDownloaderStudio"
    try:
        downloads.mkdir(parents=True, exist_ok=True)
        return downloads
    except OSError:
        fallback = Path.home() / "Downloads"
        return fallback


def ensure_directory_writable(directory: Path | str, create_if_missing: bool = True) -> Path:
    """Validate that a directory exists (or can be created) and is writable by the current user."""
    raw = str(directory).strip()
    if not raw or "\x00" in raw:
        raise ValidationError(
            "Download destination path cannot be empty or contain invalid characters.",
            remedy="Select a valid folder on your computer.",
        )

    try:
        resolved = Path(raw).expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise FilesystemError(
            f"Invalid directory path '{raw}'.",
            remedy="Choose a standard local folder path.",
        ) from exc

    if resolved.exists() and not resolved.is_dir():
        raise FilesystemError(
            f"The path '{resolved}' points to an existing file, not a directory.",
            remedy="Select a folder rather than a file.",
        )

    if not resolved.exists():
        if not create_if_missing:
            raise FilesystemError(
                f"The directory '{resolved}' does not exist.",
                remedy="Create the folder first or enable directory creation.",
            )
        try:
            resolved.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise FilesystemError(
                f"Could not create directory '{resolved}': {exc}",
                remedy="Choose a location where your Windows user account has write permissions.",
            ) from exc

    probe_file = resolved / f".ytd_write_test_{uuid.uuid4().hex[:8]}.tmp"
    try:
        probe_file.write_bytes(b"ok")
        probe_file.unlink(missing_ok=True)
    except OSError as exc:
        raise FilesystemError(
            f"The directory '{resolved}' is not writable.",
            remedy="Select a folder where your user account has write permissions.",
        ) from exc

    return resolved


def truncate_safe_filename(filename: str, max_length: int = 180) -> str:
    """Sanitize and bound a filename so it is safe on Windows filesystems."""
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", filename).strip().strip(". ")
    if not cleaned:
        cleaned = "download"

    path_obj = Path(cleaned)
    ext = path_obj.suffix
    stem = path_obj.stem if ext else cleaned

    if stem.upper() in WINDOWS_RESERVED_NAMES:
        stem = f"_{stem}"

    if len(stem) + len(ext) > max_length:
        allowed_stem_len = max(1, max_length - len(ext))
        stem = stem[:allowed_stem_len].rstrip(". ")

    result = f"{stem}{ext}" if ext else stem
    return result or "download"


def resolve_unique_filepath(directory: Path, filename: str, max_attempts: int = 1000) -> Path:
    """Return a non-colliding file path inside directory by appending ' (N)' if needed."""
    safe_name = truncate_safe_filename(filename)
    candidate = directory / safe_name
    if not candidate.exists():
        return candidate

    path_obj = Path(safe_name)
    stem = path_obj.stem
    suffix = path_obj.suffix

    for index in range(1, max_attempts + 1):
        numbered_name = truncate_safe_filename(f"{stem} ({index}){suffix}")
        numbered_candidate = directory / numbered_name
        if not numbered_candidate.exists():
            return numbered_candidate

    unique_suffix = uuid.uuid4().hex[:6]
    return directory / truncate_safe_filename(f"{stem}_{unique_suffix}{suffix}")
