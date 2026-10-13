"""Core domain models, enums, and state transition rules for YouTube Downloader Studio."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from src.core.exceptions import InvalidStateTransitionError


class DownloadMode(str, Enum):
    """Supported download modes."""

    VIDEO = "video"
    AUDIO = "audio"


class VideoQuality(str, Enum):
    """Supported video quality preferences."""

    BEST = "best"
    Q2160P = "2160p"
    Q1440P = "1440p"
    Q1080P = "1080p"
    Q720P = "720p"
    Q480P = "480p"
    Q360P = "360p"
    LOWEST = "lowest"

    @property
    def max_height(self) -> int | None:
        """Return the numeric pixel height for bounded quality presets."""
        mapping = {
            VideoQuality.Q2160P: 2160,
            VideoQuality.Q1440P: 1440,
            VideoQuality.Q1080P: 1080,
            VideoQuality.Q720P: 720,
            VideoQuality.Q480P: 480,
            VideoQuality.Q360P: 360,
        }
        return mapping.get(self)

    @property
    def label(self) -> str:
        """Human-readable label for UI display."""
        labels = {
            VideoQuality.BEST: "Best Available",
            VideoQuality.Q2160P: "2160p (4K)",
            VideoQuality.Q1440P: "1440p (2K)",
            VideoQuality.Q1080P: "1080p (Full HD)",
            VideoQuality.Q720P: "720p (HD)",
            VideoQuality.Q480P: "480p (SD)",
            VideoQuality.Q360P: "360p",
            VideoQuality.LOWEST: "Lowest Available",
        }
        return labels.get(self, self.value)


class VideoFormat(str, Enum):
    """Supported video container formats."""

    AUTO = "auto"
    MP4 = "mp4"
    MKV = "mkv"
    WEBM = "webm"


class AudioFormat(str, Enum):
    """Supported audio stream and conversion formats."""

    BEST = "best"  # Keep original best audio stream without re-encoding
    M4A = "m4a"
    MP3 = "mp3"
    WAV = "wav"
    OPUS = "opus"
    FLAC = "flac"

    @property
    def requires_ffmpeg_conversion(self) -> bool:
        """Return True when this audio format requires FFmpeg post-processing conversion."""
        return self in {AudioFormat.MP3, AudioFormat.WAV, AudioFormat.OPUS, AudioFormat.FLAC}

    @property
    def label(self) -> str:
        """Human-readable label distinguishing stream copy from FFmpeg conversion."""
        labels = {
            AudioFormat.BEST: "Best Audio Stream (No Re-encode)",
            AudioFormat.M4A: "M4A (Native Stream / Remux)",
            AudioFormat.MP3: "MP3 (Convert via FFmpeg)",
            AudioFormat.WAV: "WAV (Convert via FFmpeg)",
            AudioFormat.OPUS: "Opus (Convert via FFmpeg)",
            AudioFormat.FLAC: "FLAC (Convert via FFmpeg)",
        }
        return labels.get(self, self.value)


class AudioQuality(str, Enum):
    """Target audio bitrate/quality for FFmpeg conversion."""

    BEST = "0"
    K320 = "320"
    K256 = "256"
    K192 = "192"
    K128 = "128"


class PlaylistMode(str, Enum):
    """How a URL pointing to a playlist should be handled."""

    SINGLE = "single"
    FULL_PLAYLIST = "playlist"
    SELECTED_ITEMS = "selected_items"


class ThemeMode(str, Enum):
    """Application visual appearance modes."""

    DARK = "dark"
    LIGHT = "light"
    SYSTEM = "system"


class OverwriteBehavior(str, Enum):
    """How existing files with the same target filename should be handled."""

    ASK = "ask"
    RENAME = "rename"
    SKIP = "skip"
    OVERWRITE = "overwrite"


class JobStatus(str, Enum):
    """Lifecycle states of a download job."""

    QUEUED = "queued"
    FETCHING_METADATA = "fetching_metadata"
    STARTING = "starting"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    WAITING_RETRY = "waiting_retry"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """Return True if the job has finished executing."""
        return self in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}

    @property
    def is_active(self) -> bool:
        """Return True if the job is currently being processed by a worker."""
        return self in {
            JobStatus.FETCHING_METADATA,
            JobStatus.STARTING,
            JobStatus.DOWNLOADING,
            JobStatus.PROCESSING,
            JobStatus.WAITING_RETRY,
        }


VALID_STATUS_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset(
        {
            JobStatus.FETCHING_METADATA,
            JobStatus.STARTING,
            JobStatus.DOWNLOADING,
            JobStatus.CANCELLED,
            JobStatus.FAILED,
        }
    ),
    JobStatus.FETCHING_METADATA: frozenset(
        {
            JobStatus.STARTING,
            JobStatus.DOWNLOADING,
            JobStatus.PROCESSING,
            JobStatus.WAITING_RETRY,
            JobStatus.PAUSED,
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.STARTING: frozenset(
        {
            JobStatus.FETCHING_METADATA,
            JobStatus.DOWNLOADING,
            JobStatus.PROCESSING,
            JobStatus.WAITING_RETRY,
            JobStatus.PAUSED,
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.DOWNLOADING: frozenset(
        {
            JobStatus.DOWNLOADING,
            JobStatus.PROCESSING,
            JobStatus.WAITING_RETRY,
            JobStatus.PAUSED,
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.PROCESSING: frozenset(
        {
            JobStatus.PROCESSING,
            JobStatus.DOWNLOADING,
            JobStatus.PAUSED,
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.WAITING_RETRY: frozenset(
        {
            JobStatus.STARTING,
            JobStatus.FETCHING_METADATA,
            JobStatus.DOWNLOADING,
            JobStatus.PAUSED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.PAUSED: frozenset(
        {
            JobStatus.QUEUED,
            JobStatus.STARTING,
            JobStatus.FETCHING_METADATA,
            JobStatus.DOWNLOADING,
            JobStatus.CANCELLED,
            JobStatus.FAILED,
        }
    ),
    JobStatus.FAILED: frozenset({JobStatus.QUEUED, JobStatus.CANCELLED}),
    JobStatus.CANCELLED: frozenset({JobStatus.QUEUED}),
    JobStatus.COMPLETED: frozenset(),
}


def can_transition_status(current: JobStatus, target: JobStatus) -> bool:
    """Return True when transitioning from current status to target status is valid."""
    if current == target and not current.is_terminal:
        return True
    allowed = VALID_STATUS_TRANSITIONS.get(current, frozenset())
    return target in allowed


@dataclass(slots=True)
class PlaylistItemInfo:
    """Metadata summary for a single entry inside a playlist."""

    index: int
    video_id: str
    title: str
    url: str
    duration_seconds: int | None = None
    uploader: str | None = None
    available: bool = True


@dataclass(slots=True)
class MediaMetadata:
    """Extracted metadata for a single video or playlist."""

    url: str
    webpage_url: str
    video_id: str
    title: str
    uploader: str = "Unknown Channel"
    duration_seconds: int | None = None
    thumbnail_url: str | None = None
    is_playlist: bool = False
    playlist_title: str | None = None
    playlist_count: int = 0
    playlist_items: list[PlaylistItemInfo] = field(default_factory=list)
    available_heights: list[int] = field(default_factory=list)
    available_video_exts: list[str] = field(default_factory=list)
    available_audio_exts: list[str] = field(default_factory=list)
    has_separate_streams: bool = False
    has_muxed_streams: bool = True

    @property
    def formatted_duration(self) -> str:
        """Return a human-readable HH:MM:SS or MM:SS duration string."""
        if self.duration_seconds is None or self.duration_seconds < 0:
            return "Unknown duration"
        total = int(self.duration_seconds)
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 0:
            return f"{hours:d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"


@dataclass(slots=True)
class DownloadJob:
    """Represents a queued, active, or completed download job."""

    url: str
    destination_dir: Path
    mode: DownloadMode = DownloadMode.VIDEO
    video_quality: VideoQuality = VideoQuality.BEST
    video_format: VideoFormat = VideoFormat.MP4
    audio_format: AudioFormat = AudioFormat.MP3
    audio_quality: AudioQuality = AudioQuality.K192
    title: str = ""
    filename_template: str = "%(title).150B [%(id)s].%(ext)s"
    overwrite_behavior: OverwriteBehavior = OverwriteBehavior.RENAME
    playlist_mode: PlaylistMode = PlaylistMode.SINGLE
    selected_playlist_indices: list[int] = field(default_factory=list)
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: JobStatus = JobStatus.QUEUED
    progress_percent: float = 0.0
    downloaded_bytes: int = 0
    total_bytes: int | None = None
    speed_bps: float | None = None
    eta_seconds: int | None = None
    output_path: Path | None = None
    file_size_bytes: int | None = None
    error_message: str | None = None
    playlist_index: int | None = None
    playlist_total: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None

    @property
    def display_title(self) -> str:
        """Return the job title or fallback to its URL."""
        return self.title.strip() if self.title and self.title.strip() else self.url

    @property
    def requested_format_summary(self) -> str:
        """Return a concise description of the requested quality and format."""
        if self.mode == DownloadMode.AUDIO:
            if self.audio_format == AudioFormat.BEST:
                return "Audio (Best native)"
            if self.audio_format == AudioFormat.M4A:
                return "Audio (M4A)"
            return f"Audio ({self.audio_format.value.upper()} {self.audio_quality.value}k)"
        fmt = self.video_format.value.upper()
        return f"Video ({self.video_quality.value}, {fmt})"

    def transition_to(
        self,
        new_status: JobStatus,
        error_message: str | None = None,
        preserve_progress: bool = False,
    ) -> None:
        """Validate and apply a status transition on this job."""
        if not can_transition_status(self.status, new_status):
            raise InvalidStateTransitionError(
                f"Cannot transition job '{self.job_id}' from {self.status.value} to {new_status.value}."
            )
        previous_status = self.status
        self.status = new_status
        if error_message is not None:
            self.error_message = error_message
        elif new_status in {JobStatus.QUEUED, JobStatus.STARTING, JobStatus.DOWNLOADING, JobStatus.PAUSED}:
            self.error_message = None

        if new_status.is_terminal:
            self.completed_at = datetime.now(timezone.utc)
            self.speed_bps = None
            self.eta_seconds = None
            if new_status == JobStatus.COMPLETED:
                self.progress_percent = 100.0
        elif new_status == JobStatus.PAUSED:
            self.speed_bps = None
            self.eta_seconds = None
        elif new_status == JobStatus.QUEUED:
            self.completed_at = None
            self.speed_bps = None
            self.eta_seconds = None
            if not (preserve_progress or previous_status == JobStatus.PAUSED):
                self.progress_percent = 0.0
                self.downloaded_bytes = 0


@dataclass(slots=True)
class HistoryEntry:
    """Persisted download history record stored in SQLite."""

    job_id: str
    url: str
    title: str
    mode: DownloadMode
    requested_quality: str
    output_path: str | None
    destination_dir: str
    status: JobStatus
    file_size_bytes: int | None
    error_message: str | None
    created_at: str
    completed_at: str | None
    video_quality: str = "best"
    video_format: str = "mp4"
    audio_format: str = "mp3"
    playlist_mode: str = "single"


@dataclass(slots=True)
class AppSettings:
    """Persistent user preferences for YouTube Downloader Studio."""

    default_download_dir: str = ""
    default_mode: DownloadMode = DownloadMode.VIDEO
    default_video_quality: VideoQuality = VideoQuality.Q1080P
    default_video_format: VideoFormat = VideoFormat.MP4
    default_audio_format: AudioFormat = AudioFormat.MP3
    default_audio_quality: AudioQuality = AudioQuality.K192
    theme_mode: ThemeMode = ThemeMode.DARK
    max_concurrent_downloads: int = 2
    filename_template: str = "%(title).150B [%(id)s].%(ext)s"
    ask_before_overwrite: bool = True
    overwrite_behavior: OverwriteBehavior = OverwriteBehavior.RENAME
    enable_completion_notifications: bool = True
    ffmpeg_custom_path: str = ""
    log_level: str = "INFO"

    def to_dict(self) -> dict[str, Any]:
        """Serialize settings to a JSON-compatible dictionary."""
        return {
            "default_download_dir": self.default_download_dir,
            "default_mode": self.default_mode.value,
            "default_video_quality": self.default_video_quality.value,
            "default_video_format": self.default_video_format.value,
            "default_audio_format": self.default_audio_format.value,
            "default_audio_quality": self.default_audio_quality.value,
            "theme_mode": self.theme_mode.value,
            "max_concurrent_downloads": self.max_concurrent_downloads,
            "filename_template": self.filename_template,
            "ask_before_overwrite": self.ask_before_overwrite,
            "overwrite_behavior": self.overwrite_behavior.value,
            "enable_completion_notifications": self.enable_completion_notifications,
            "ffmpeg_custom_path": self.ffmpeg_custom_path,
            "log_level": self.log_level,
        }


@dataclass(slots=True)
class DependencyStatus:
    """Diagnostic snapshot of runtime and media processing dependencies."""

    python_version: str
    yt_dlp_available: bool
    yt_dlp_version: str | None = None
    ffmpeg_available: bool = False
    ffmpeg_path: str | None = None
    ffmpeg_version: str | None = None
    ffprobe_available: bool = False
    ffprobe_path: str | None = None
    ffprobe_version: str | None = None

    @property
    def full_media_processing_ready(self) -> bool:
        """Return True if yt-dlp and FFmpeg are available."""
        return self.yt_dlp_available and self.ffmpeg_available

    @property
    def summary_text(self) -> str:
        """Concise status bar summary of dependencies."""
        yt_part = f"yt-dlp {self.yt_dlp_version}" if self.yt_dlp_available else "yt-dlp MISSING"
        if self.ffmpeg_available and self.ffprobe_available:
            ff_part = "FFmpeg + FFprobe Ready"
        elif self.ffmpeg_available:
            ff_part = "FFmpeg Ready (FFprobe missing)"
        else:
            ff_part = "FFmpeg Not Found (Limited mode)"
        return f"{yt_part}  |  {ff_part}"
