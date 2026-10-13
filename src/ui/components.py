"""Reusable UI formatting helpers and the MetadataPreviewCard widget."""

from __future__ import annotations

import io
from typing import Callable

import customtkinter as ctk  # type: ignore[import-untyped]
from PIL import Image

from src.core.models import JobStatus, MediaMetadata, PlaylistMode

STATUS_LABELS: dict[JobStatus, str] = {
    JobStatus.QUEUED: "Queued",
    JobStatus.FETCHING_METADATA: "Fetching Info...",
    JobStatus.STARTING: "Starting...",
    JobStatus.DOWNLOADING: "Downloading",
    JobStatus.PROCESSING: "Processing (FFmpeg)...",
    JobStatus.WAITING_RETRY: "Waiting to Retry",
    JobStatus.PAUSED: "Paused",
    JobStatus.COMPLETED: "Completed",
    JobStatus.FAILED: "Failed",
    JobStatus.CANCELLED: "Cancelled",
}

STATUS_BADGE_COLORS: dict[JobStatus, tuple[str, str]] = {
    JobStatus.QUEUED: ("#64748b", "#475569"),
    JobStatus.FETCHING_METADATA: ("#0284c7", "#0369a1"),
    JobStatus.STARTING: ("#0284c7", "#0369a1"),
    JobStatus.DOWNLOADING: ("#2563eb", "#1d4ed8"),
    JobStatus.PROCESSING: ("#7c3aed", "#6d28d9"),
    JobStatus.WAITING_RETRY: ("#d97706", "#b45309"),
    JobStatus.PAUSED: ("#d97706", "#b45309"),
    JobStatus.COMPLETED: ("#16a34a", "#15803d"),
    JobStatus.FAILED: ("#dc2626", "#b91c1c"),
    JobStatus.CANCELLED: ("#6b7280", "#4b5563"),
}


def format_bytes(num_bytes: int | float | None) -> str:
    """Format a byte count into a human-readable string (KB, MB, GB)."""
    if num_bytes is None or num_bytes < 0:
        return "—"
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"


def format_speed(speed_bps: float | None) -> str:
    """Format a transfer speed in bytes/second into human-readable units."""
    if speed_bps is None or speed_bps <= 0:
        return "—"
    return f"{format_bytes(speed_bps)}/s"


def format_eta(eta_seconds: int | None) -> str:
    """Format an estimated remaining time in seconds into MM:SS or HH:MM:SS."""
    if eta_seconds is None or eta_seconds < 0:
        return "—"
    total = int(eta_seconds)
    hours, rem = divmod(total, 3600)
    mins, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours:d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


class MetadataPreviewCard(ctk.CTkFrame):
    """Displays video or playlist metadata, thumbnail preview, and playlist item controls."""

    def __init__(
        self,
        master: ctk.CTkBaseClass,
        on_retry_metadata: Callable[[], None] | None = None,
        on_open_playlist_selector: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(master, corner_radius=8)
        self._on_retry_metadata = on_retry_metadata
        self._on_open_playlist_selector = on_open_playlist_selector
        self._current_metadata: MediaMetadata | None = None
        self._ctk_thumb_image: ctk.CTkImage | None = None

        self.grid_columnconfigure(1, weight=1)

        # Left thumbnail box (local placeholder by default)
        self._thumb_label = ctk.CTkLabel(
            self,
            text="MEDIA\nPREVIEW",
            width=144,
            height=82,
            corner_radius=6,
            fg_color=("gray82", "gray22"),
            text_color=("gray35", "gray70"),
            font=ctk.CTkFont(size=11, weight="bold"),
        )
        self._thumb_label.grid(row=0, column=0, rowspan=3, padx=12, pady=10, sticky="nw")

        # Details column
        self._title_label = ctk.CTkLabel(
            self,
            text="No media loaded — paste a YouTube URL above and click 'Fetch Info' or 'Download Now'.",
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w",
            justify="left",
            wraplength=680,
        )
        self._title_label.grid(row=0, column=1, sticky="ew", padx=(0, 12), pady=(10, 2))

        self._meta_label = ctk.CTkLabel(
            self,
            text="Channel • Duration • Available Formats",
            font=ctk.CTkFont(size=12),
            text_color=("gray35", "gray70"),
            anchor="w",
            justify="left",
        )
        self._meta_label.grid(row=1, column=1, sticky="ew", padx=(0, 12), pady=2)

        # Action row for playlist selection or retry
        self._action_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._action_frame.grid(row=2, column=1, sticky="ew", padx=(0, 12), pady=(2, 8))

        self._status_badge = ctk.CTkLabel(
            self._action_frame,
            text="Ready",
            font=ctk.CTkFont(size=11),
            text_color=("gray40", "gray65"),
        )
        self._status_badge.pack(side="left")

        self._playlist_btn = ctk.CTkButton(
            self._action_frame,
            text="Select Playlist Items...",
            width=160,
            height=26,
            font=ctk.CTkFont(size=12),
            command=self._handle_playlist_btn,
        )

        self._retry_btn = ctk.CTkButton(
            self._action_frame,
            text="Retry Info Fetch",
            width=130,
            height=26,
            font=ctk.CTkFont(size=12),
            command=self._handle_retry_btn,
        )

    @property
    def current_metadata(self) -> MediaMetadata | None:
        """Return the currently displayed MediaMetadata or None."""
        return self._current_metadata

    def show_loading(self, url: str) -> None:
        """Display a responsive loading state while metadata is being fetched."""
        self._current_metadata = None
        self._clear_thumbnail("LOADING...")
        self._title_label.configure(text=f"Fetching media information for: {url}")
        self._meta_label.configure(text="Inspecting available video/audio streams and playlist info...")
        self._status_badge.configure(text="Working...", text_color=("#0284c7", "#38bdf8"))
        self._playlist_btn.pack_forget()
        self._retry_btn.pack_forget()

    def show_error(self, message: str, remedy: str = "") -> None:
        """Display metadata error details and reveal the Retry button."""
        self._current_metadata = None
        self._clear_thumbnail("INFO\nERROR")
        self._title_label.configure(text=f"Metadata error: {message}")
        self._meta_label.configure(
            text=f"Remedy: {remedy}" if remedy else "Verify the URL and try again."
        )
        self._status_badge.configure(text="Failed to fetch metadata", text_color=("#dc2626", "#f87171"))
        self._playlist_btn.pack_forget()
        if self._on_retry_metadata:
            self._retry_btn.pack(side="right", padx=(8, 0))

    def show_metadata(
        self,
        metadata: MediaMetadata,
        thumbnail_bytes: bytes | None = None,
        playlist_mode: PlaylistMode = PlaylistMode.SINGLE,
        selected_count: int = 0,
    ) -> None:
        """Render fetched MediaMetadata and optional thumbnail image bytes."""
        self._current_metadata = metadata
        self._retry_btn.pack_forget()

        if thumbnail_bytes:
            self._set_thumbnail_from_bytes(thumbnail_bytes)
        else:
            badge_txt = "PLAYLIST" if metadata.is_playlist else "VIDEO"
            self._clear_thumbnail(badge_txt)

        self._title_label.configure(text=metadata.title)

        if metadata.is_playlist:
            avail_items = sum(1 for item in metadata.playlist_items if item.available)
            self._meta_label.configure(
                text=(
                    f"Playlist by {metadata.uploader}  •  "
                    f"{metadata.playlist_count} total items ({avail_items} available in preview)"
                )
            )
            mode_desc = {
                PlaylistMode.SINGLE: "Mode: Single Video",
                PlaylistMode.FULL_PLAYLIST: f"Mode: Full Playlist ({metadata.playlist_count} items)",
                PlaylistMode.SELECTED_ITEMS: f"Mode: Selected Items ({selected_count} chosen)",
            }.get(playlist_mode, "Playlist")
            self._status_badge.configure(text=mode_desc, text_color=("#16a34a", "#4ade80"))
            if self._on_open_playlist_selector:
                self._playlist_btn.pack(side="right", padx=(8, 0))
        else:
            heights_str = (
                ", ".join(f"{h}p" for h in metadata.available_heights[:6])
                if metadata.available_heights
                else "Auto"
            )
            self._meta_label.configure(
                text=(
                    f"Channel: {metadata.uploader}  •  "
                    f"Duration: {metadata.formatted_duration}  •  "
                    f"Qualities: {heights_str}"
                )
            )
            self._status_badge.configure(
                text="Metadata Ready",
                text_color=("#16a34a", "#4ade80"),
            )
            self._playlist_btn.pack_forget()

    def clear(self) -> None:
        """Reset the preview card to its initial empty state."""
        self._current_metadata = None
        self._clear_thumbnail("MEDIA\nPREVIEW")
        self._title_label.configure(
            text="No media loaded — paste a YouTube URL above and click 'Fetch Info' or 'Download Now'."
        )
        self._meta_label.configure(text="Channel • Duration • Available Formats")
        self._status_badge.configure(text="Ready", text_color=("gray40", "gray65"))
        self._playlist_btn.pack_forget()
        self._retry_btn.pack_forget()

    def _clear_thumbnail(self, placeholder_text: str) -> None:
        self._ctk_thumb_image = None
        self._thumb_label.configure(image=None, text=placeholder_text)

    def _set_thumbnail_from_bytes(self, raw_bytes: bytes) -> None:
        try:
            with Image.open(io.BytesIO(raw_bytes)) as pil_img:
                rgb_img = pil_img.convert("RGB")
                rgb_img.thumbnail((144, 82))
                self._ctk_thumb_image = ctk.CTkImage(
                    light_image=rgb_img,
                    dark_image=rgb_img,
                    size=(144, 82),
                )
                self._thumb_label.configure(image=self._ctk_thumb_image, text="")
        except Exception:
            self._clear_thumbnail("VIDEO")

    def _handle_playlist_btn(self) -> None:
        if self._on_open_playlist_selector:
            self._on_open_playlist_selector()

    def _handle_retry_btn(self) -> None:
        if self._on_retry_metadata:
            self._on_retry_metadata()
