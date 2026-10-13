"""Custom exception hierarchy for YouTube Downloader Studio."""

from __future__ import annotations


class AppError(Exception):
    """Base exception for all known application errors."""

    def __init__(self, message: str, remedy: str | None = None) -> None:
        super().__init__(message)
        self.user_message = message
        self.remedy = remedy or ""

    def format_for_user(self) -> str:
        """Return a user-friendly message including the suggested remedy if present."""
        if self.remedy:
            return f"{self.user_message}\n\nSuggested remedy: {self.remedy}"
        return self.user_message


class ValidationError(AppError):
    """Raised when user input (URL, filename, path, or setting) fails validation."""


class MetadataError(AppError):
    """Raised when fetching media or playlist metadata fails."""


class DownloadError(AppError):
    """Raised when a media download or post-processing step fails."""


class DownloadCancelledError(AppError):
    """Raised when an active download or metadata fetch is cancelled by the user."""


class DownloadPausedError(AppError):
    """Raised when an active download is cooperatively paused by the user."""


class FFmpegMissingError(AppError):
    """Raised when an operation requires FFmpeg/FFprobe and it is not available."""


class FilesystemError(AppError):
    """Raised when a directory or file path cannot be created, accessed, or written to."""


class DatabaseError(AppError):
    """Raised when a SQLite database operation fails."""


class SettingsError(AppError):
    """Raised when loading or saving application settings fails."""


class InvalidStateTransitionError(AppError):
    """Raised when a job attempts an invalid status transition."""
