"""Safe platform integration utilities for opening folders and media files on Windows."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from src.core.exceptions import FilesystemError, ValidationError
from src.utils.logging_config import get_logger

logger = get_logger("platform_utils")

# File extensions that must NEVER be executed or launched from downloaded media actions
BLOCKED_EXECUTABLE_EXTENSIONS = frozenset(
    {
        ".exe",
        ".bat",
        ".cmd",
        ".com",
        ".ps1",
        ".psm1",
        ".vbs",
        ".vbe",
        ".js",
        ".jse",
        ".wsf",
        ".wsh",
        ".msi",
        ".msp",
        ".scr",
        ".hta",
        ".pif",
        ".reg",
        ".jar",
        ".cpl",
        ".dll",
        ".sys",
        ".lnk",
    }
)


def open_directory(directory_path: Path | str) -> None:
    """Safely open a directory in the system file manager without shell interpolation."""
    target = Path(directory_path).expanduser().resolve()
    if not target.exists():
        raise FilesystemError(
            f"The folder '{target}' no longer exists on disk.",
            remedy="Check if the folder was moved or deleted.",
        )
    if not target.is_dir():
        raise FilesystemError(
            f"The path '{target}' is not a folder.",
            remedy="Provide a valid directory path.",
        )

    logger.info("Opening directory: %s", target)
    try:
        if sys.platform == "win32" and hasattr(os, "startfile"):
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
    except OSError as exc:
        raise FilesystemError(
            f"Failed to open folder '{target}': {exc}",
            remedy="Open the folder manually in Windows Explorer.",
        ) from exc


def open_media_file(file_path: Path | str) -> None:
    """Safely open a completed media file after verifying it exists and is not an executable."""
    target = Path(file_path).expanduser().resolve()
    if not target.exists():
        raise FilesystemError(
            f"The file '{target}' does not exist on disk.",
            remedy="The file may have been moved, renamed, or deleted.",
        )
    if not target.is_file():
        raise FilesystemError(
            f"The path '{target}' is not a regular file.",
            remedy="Use 'Open Folder' to view directories.",
        )

    suffix = target.suffix.lower()
    if suffix in BLOCKED_EXECUTABLE_EXTENSIONS:
        raise ValidationError(
            f"Refusing to launch file with potentially executable extension '{suffix}'.",
            remedy="For security reasons, executable files are never launched by the application.",
        )

    logger.info("Opening media file: %s", target)
    try:
        if sys.platform == "win32" and hasattr(os, "startfile"):
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
    except OSError as exc:
        raise FilesystemError(
            f"Could not open media file '{target}': {exc}",
            remedy="Use 'Open Folder' to inspect the file in Windows Explorer.",
        ) from exc


def reveal_in_file_manager(path: Path | str) -> None:
    """Open the containing folder for a file, or the directory itself if a folder is passed."""
    target = Path(path).expanduser().resolve()
    if target.exists() and target.is_dir():
        open_directory(target)
        return

    if target.exists() and target.is_file():
        if sys.platform == "win32":
            try:
                subprocess.Popen(["explorer.exe", f"/select,{target}"])
                return
            except OSError:
                pass
        open_directory(target.parent)
        return

    parent = target.parent
    if parent.exists() and parent.is_dir():
        open_directory(parent)
        return

    raise FilesystemError(
        f"Neither '{target}' nor its parent folder '{parent}' exists on disk.",
        remedy="Verify the download destination folder still exists.",
    )
