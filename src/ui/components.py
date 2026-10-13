"""Reusable UI formatting helpers and the MetadataPreviewCard widget."""

from __future__ import annotations

import io
import tkinter as tk
from typing import Callable

import customtkinter as ctk  # type: ignore[import-untyped]
from PIL import Image

from src.core.exceptions import ValidationError
from src.core.models import (
    JobStatus,
    LocalMediaInfo,
    MediaMetadata,
    PlaylistMode,
    TrimConfig,
)
from src.core.validators import (
    format_timestamp,
    parse_timestamp,
    validate_trim_range,
)

STATUS_LABELS: dict[JobStatus, str] = {
    JobStatus.QUEUED: "Queued",
    JobStatus.FETCHING_METADATA: "Fetching Info...",
    JobStatus.STARTING: "Starting...",
    JobStatus.DOWNLOADING: "Downloading",
    JobStatus.PROCESSING: "Processing...",
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
    """Displays video, playlist, or local file metadata, thumbnail preview, and caption availability."""

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
        self._current_local_info: LocalMediaInfo | None = None
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
            text="No media loaded — paste a YouTube URL above or select a local media file.",
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w",
            justify="left",
            wraplength=680,
        )
        self._title_label.grid(row=0, column=1, sticky="ew", padx=(0, 12), pady=(10, 2))

        self._meta_label = ctk.CTkLabel(
            self,
            text="Channel • Duration • Available Formats • Captions",
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

    @property
    def current_local_info(self) -> LocalMediaInfo | None:
        """Return the currently displayed LocalMediaInfo or None."""
        return self._current_local_info

    def show_loading(self, url: str) -> None:
        """Display a responsive loading state while metadata is being fetched."""
        self._current_metadata = None
        self._current_local_info = None
        self._clear_thumbnail("LOADING...")
        self._title_label.configure(text=f"Fetching media information for: {url}")
        self._meta_label.configure(
            text="Inspecting available video/audio streams, duration, and caption tracks..."
        )
        self._status_badge.configure(text="Working...", text_color=("#0284c7", "#38bdf8"))
        self._playlist_btn.pack_forget()
        self._retry_btn.pack_forget()

    def show_error(self, message: str, remedy: str = "") -> None:
        """Display metadata error details and reveal the Retry button."""
        self._current_metadata = None
        self._current_local_info = None
        self._clear_thumbnail("INFO\nERROR")
        self._title_label.configure(text=f"Metadata error: {message}")
        self._meta_label.configure(
            text=f"Remedy: {remedy}" if remedy else "Verify the URL or file and try again."
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
        self._current_local_info = None
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
            manual_cnt = len(metadata.manual_caption_tracks)
            auto_cnt = len(metadata.automatic_caption_tracks)
            if manual_cnt or auto_cnt:
                cap_str = f"Captions: {manual_cnt} Manual, {auto_cnt} Auto"
            else:
                cap_str = "Captions: None"
            self._meta_label.configure(
                text=(
                    f"Channel: {metadata.uploader}  •  "
                    f"Duration: {metadata.formatted_duration}  •  "
                    f"Qualities: {heights_str}  •  {cap_str}"
                )
            )
            self._status_badge.configure(
                text="Metadata Ready",
                text_color=("#16a34a", "#4ade80"),
            )
            self._playlist_btn.pack_forget()

    def show_local_media(self, info: LocalMediaInfo) -> None:
        """Render inspected LocalMediaInfo for local file trimming."""
        self._current_metadata = None
        self._current_local_info = info
        self._retry_btn.pack_forget()
        self._playlist_btn.pack_forget()

        badge = "LOCAL\nVIDEO" if info.has_video else "LOCAL\nAUDIO"
        self._clear_thumbnail(badge)
        self._title_label.configure(text=f"Local File: {info.file_path.name}")

        streams_desc: list[str] = []
        if info.has_video:
            streams_desc.append(f"Video ({info.video_codec or 'stream'})")
        if info.has_audio:
            streams_desc.append(f"Audio ({info.audio_codec or 'stream'})")
        stream_str = " + ".join(streams_desc) if streams_desc else "Unknown streams"

        self._meta_label.configure(
            text=(
                f"Duration: {info.formatted_duration} ({info.duration_seconds:.1f}s)  •  "
                f"Streams: {stream_str}  •  Path: {info.file_path.parent}"
            )
        )
        self._status_badge.configure(
            text="Local Media Inspected & Ready to Trim",
            text_color=("#16a34a", "#4ade80"),
        )

    def clear(self) -> None:
        """Reset the preview card to its initial empty state."""
        self._current_metadata = None
        self._current_local_info = None
        self._clear_thumbnail("MEDIA\nPREVIEW")
        self._title_label.configure(
            text="No media loaded — paste a YouTube URL above or select a local media file."
        )
        self._meta_label.configure(text="Channel • Duration • Available Formats • Captions")
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


class TrimTimelineWidget(ctk.CTkFrame):
    """Interactive visual trim timeline with draggable Start/End handles and synchronized timestamp inputs."""

    MIN_CLIP_SECONDS = 0.5
    DEFAULT_FALLBACK_DURATION = 180.0

    def __init__(
        self,
        master: ctk.CTkBaseClass,
        on_change: Callable[[TrimConfig], None] | None = None,
    ) -> None:
        super().__init__(master, corner_radius=6, border_width=1, border_color=("gray78", "gray28"))
        self._on_change = on_change
        self._media_duration: float | None = None
        self._start_seconds: float = 0.0
        self._end_seconds: float = 60.0
        self._active_handle: str | None = None
        self._updating_programmatically: bool = False

        self.grid_columnconfigure(0, weight=1)

        # Top row: Enable toggle, Start/End entries, Accurate Cut toggle, Reset button, Summary
        ctrl_row = ctk.CTkFrame(self, fg_color="transparent")
        ctrl_row.grid(row=0, column=0, sticky="ew", padx=10, pady=(6, 2))

        self._enabled_var = tk.BooleanVar(value=False)
        self._enable_cb = ctk.CTkCheckBox(
            ctrl_row,
            text="Trim Clip Segment",
            variable=self._enabled_var,
            font=ctk.CTkFont(size=12, weight="bold"),
            width=145,
            command=self._on_enabled_toggled,
        )
        self._enable_cb.pack(side="left", padx=(0, 10))

        ctk.CTkLabel(ctrl_row, text="Start:", font=ctk.CTkFont(size=11)).pack(side="left", padx=(0, 4))
        self._start_entry = ctk.CTkEntry(
            ctrl_row,
            width=84,
            height=26,
            font=ctk.CTkFont(family="Consolas", size=12),
        )
        self._start_entry.insert(0, "00:00")
        self._start_entry.pack(side="left", padx=(0, 8))
        self._start_entry.bind("<Return>", self._on_entry_event)
        self._start_entry.bind("<FocusOut>", self._on_entry_event)

        ctk.CTkLabel(ctrl_row, text="End:", font=ctk.CTkFont(size=11)).pack(side="left", padx=(0, 4))
        self._end_entry = ctk.CTkEntry(
            ctrl_row,
            width=84,
            height=26,
            font=ctk.CTkFont(family="Consolas", size=12),
        )
        self._end_entry.insert(0, "01:00")
        self._end_entry.pack(side="left", padx=(0, 10))
        self._end_entry.bind("<Return>", self._on_entry_event)
        self._end_entry.bind("<FocusOut>", self._on_entry_event)

        self._apply_btn = ctk.CTkButton(
            ctrl_row,
            text="Set Range",
            width=76,
            height=26,
            font=ctk.CTkFont(size=11),
            command=self.apply_entry_inputs,
        )
        self._apply_btn.pack(side="left", padx=(0, 8))

        self._reset_btn = ctk.CTkButton(
            ctrl_row,
            text="Full Length",
            width=82,
            height=26,
            font=ctk.CTkFont(size=11),
            fg_color=("gray75", "gray30"),
            text_color=("gray10", "gray90"),
            command=self.reset_range,
        )
        self._reset_btn.pack(side="left", padx=(0, 10))

        self._accurate_var = tk.BooleanVar(value=True)
        self._accurate_cb = ctk.CTkCheckBox(
            ctrl_row,
            text="Accurate Cut (Re-encode)",
            variable=self._accurate_var,
            font=ctk.CTkFont(size=11),
            width=160,
            command=self._notify_change,
        )
        self._accurate_cb.pack(side="left", padx=(0, 10))

        self._summary_lbl = ctk.CTkLabel(
            ctrl_row,
            text="Clip: 00:00 -> 01:00 (01:00)",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=("gray30", "gray75"),
        )
        self._summary_lbl.pack(side="right")

        # Interactive timeline canvas
        self._canvas = tk.Canvas(
            self,
            height=42,
            bg="#1e293b",
            highlightthickness=0,
            cursor="hand2",
        )
        self._canvas.grid(row=1, column=0, sticky="ew", padx=10, pady=(2, 6))
        self._canvas.bind("<Configure>", lambda _e: self._redraw_timeline())
        self._canvas.bind("<ButtonPress-1>", self._on_canvas_press)
        self._canvas.bind("<B1-Motion>", self._on_canvas_drag)
        self._canvas.bind("<ButtonRelease-1>", self._on_canvas_release)

        self._sync_summary_and_canvas()

    # ------------------------------------------------------------------
    # Public API & Synchronization
    # ------------------------------------------------------------------

    @property
    def media_duration(self) -> float | None:
        """Return the known media duration in seconds, or None if not yet inspected."""
        return self._media_duration

    @property
    def effective_timeline_duration(self) -> float:
        """Return the total duration represented on the visual timeline track."""
        if self._media_duration and self._media_duration > 0:
            return float(self._media_duration)
        return max(self.DEFAULT_FALLBACK_DURATION, self._end_seconds, 10.0)

    @property
    def start_seconds(self) -> float:
        """Return the current start timestamp in seconds."""
        return self._start_seconds

    @property
    def end_seconds(self) -> float:
        """Return the current end timestamp in seconds."""
        return self._end_seconds

    @property
    def clip_duration(self) -> float:
        """Return the selected clip duration in seconds."""
        return max(0.0, round(self._end_seconds - self._start_seconds, 3))

    def is_enabled(self) -> bool:
        """Return True if segment trimming is enabled."""
        return bool(self._enabled_var.get())

    def set_enabled(self, enabled: bool, lock_enabled: bool = False) -> None:
        """Enable or disable segment trimming (and optionally lock it on for local files)."""
        self._enabled_var.set(bool(enabled))
        if lock_enabled:
            self._enable_cb.configure(state="disabled")
        else:
            self._enable_cb.configure(state="normal")
        self._sync_summary_and_canvas()
        self._notify_change()

    def is_accurate_trim(self) -> bool:
        """Return True if frame-accurate re-encoding is enabled."""
        return bool(self._accurate_var.get())

    def set_accurate_trim(self, accurate: bool) -> None:
        """Set whether frame-accurate re-encoding is used."""
        self._accurate_var.set(bool(accurate))

    def get_start_text(self) -> str:
        """Return the text currently inside the Start Time entry."""
        return self._start_entry.get().strip()

    def get_end_text(self) -> str:
        """Return the text currently inside the End Time entry."""
        return self._end_entry.get().strip()

    def get_trim_config(self) -> TrimConfig:
        """Validate current inputs and return a structured TrimConfig."""
        if self.is_enabled():
            self.apply_entry_inputs(raise_on_error=True)
        return TrimConfig(
            enabled=self.is_enabled(),
            start_seconds=self._start_seconds,
            end_seconds=self._end_seconds,
            source_duration=self._media_duration,
            accurate_trim=self.is_accurate_trim(),
        )

    def set_media_duration(self, duration_seconds: float | int | None, reset_to_full: bool = True) -> None:
        """Update the known total media duration and synchronize the timeline and entries."""
        if duration_seconds is not None and float(duration_seconds) > 0:
            self._media_duration = round(float(duration_seconds), 3)
            if reset_to_full or self._end_seconds > self._media_duration or self._end_seconds <= self._start_seconds:
                self._start_seconds = 0.0
                self._end_seconds = self._media_duration
        else:
            self._media_duration = None
            if reset_to_full:
                self._start_seconds = 0.0
                self._end_seconds = 60.0

        self._write_entries_from_seconds()
        self._sync_summary_and_canvas()
        self._notify_change()

    def set_total_duration(self, duration_seconds: float | int | None, reset_to_full: bool = True) -> None:
        """Alias for set_media_duration."""
        self.set_media_duration(duration_seconds, reset_to_full=reset_to_full)

    def reset_range(self) -> None:
        """Reset Start Time to 00:00 and End Time to the full media duration."""
        self._start_seconds = 0.0
        self._end_seconds = (
            self._media_duration
            if (self._media_duration and self._media_duration > 0)
            else 60.0
        )
        self._write_entries_from_seconds()
        self._sync_summary_and_canvas()
        self._notify_change()

    def set_range_seconds(self, start_seconds: float, end_seconds: float) -> None:
        """Programmatically set start and end timestamps, updating both text fields and handle positions."""
        valid_start, valid_end = validate_trim_range(
            start_seconds,
            end_seconds,
            media_duration=self._media_duration,
            min_clip_seconds=self.MIN_CLIP_SECONDS,
        )
        self._start_seconds = valid_start
        self._end_seconds = valid_end
        self._write_entries_from_seconds()
        self._sync_summary_and_canvas()
        self._notify_change()

    def set_handle_seconds(self, handle: str, target_seconds: float) -> None:
        """Move either the 'start' or 'end' timeline handle to `target_seconds` with clamping and sync."""
        max_dur = self.effective_timeline_duration
        clamped = max(0.0, min(max_dur, float(target_seconds)))

        if handle == "start":
            max_allowed_start = max(0.0, self._end_seconds - self.MIN_CLIP_SECONDS)
            self._start_seconds = round(min(clamped, max_allowed_start), 1)
        elif handle == "end":
            min_allowed_end = min(max_dur, self._start_seconds + self.MIN_CLIP_SECONDS)
            self._end_seconds = round(max(clamped, min_allowed_end), 1)
        else:
            raise ValueError(f"Unknown handle '{handle}'; expected 'start' or 'end'.")

        self._write_entries_from_seconds()
        self._sync_summary_and_canvas()
        self._notify_change()

    def get_handle_fractions(self) -> tuple[float, float]:
        """Return normalized (0.0..1.0) positions of the start and end handles along the timeline."""
        total = max(1.0, self.effective_timeline_duration)
        start_frac = max(0.0, min(1.0, self._start_seconds / total))
        end_frac = max(0.0, min(1.0, self._end_seconds / total))
        return round(start_frac, 4), round(end_frac, 4)

    def apply_entry_inputs(self, raise_on_error: bool = False) -> bool:
        """Parse Start and End text entries, validate range, and synchronize the visual timeline."""
        start_raw = self._start_entry.get().strip()
        end_raw = self._end_entry.get().strip()
        try:
            valid_start, valid_end = validate_trim_range(
                start_raw,
                end_raw,
                media_duration=self._media_duration,
                min_clip_seconds=self.MIN_CLIP_SECONDS,
            )
            self._start_seconds = valid_start
            self._end_seconds = valid_end
            self._write_entries_from_seconds()
            self._sync_summary_and_canvas()
            self._notify_change()
            return True
        except ValidationError as exc:
            self._summary_lbl.configure(
                text=f"Invalid range: {exc.user_message}",
                text_color=("#dc2626", "#f87171"),
            )
            self._redraw_timeline()
            if raise_on_error:
                raise
            return False

    # ------------------------------------------------------------------
    # Internal Event & Drawing Helpers
    # ------------------------------------------------------------------

    def _on_enabled_toggled(self) -> None:
        if self.is_enabled():
            self.apply_entry_inputs(raise_on_error=False)
        else:
            self._sync_summary_and_canvas()
        self._notify_change()

    def _on_entry_event(self, _event: object = None) -> None:
        if self._updating_programmatically:
            return
        self.apply_entry_inputs(raise_on_error=False)

    def _write_entries_from_seconds(self) -> None:
        self._updating_programmatically = True
        try:
            use_millis = (self._start_seconds % 1.0 != 0.0) or (self._end_seconds % 1.0 != 0.0)
            force_hrs = self.effective_timeline_duration >= 3600.0
            start_txt = format_timestamp(
                self._start_seconds,
                include_millis=use_millis,
                force_hours=force_hrs,
            )
            end_txt = format_timestamp(
                self._end_seconds,
                include_millis=use_millis,
                force_hours=force_hrs,
            )
            self._start_entry.delete(0, "end")
            self._start_entry.insert(0, start_txt)
            self._end_entry.delete(0, "end")
            self._end_entry.insert(0, end_txt)
        finally:
            self._updating_programmatically = False

    def _sync_summary_and_canvas(self) -> None:
        clip_len = self.clip_duration
        start_str = format_timestamp(self._start_seconds)
        end_str = format_timestamp(self._end_seconds)
        clip_str = format_timestamp(clip_len)
        total_str = (
            format_timestamp(self._media_duration)
            if (self._media_duration and self._media_duration > 0)
            else "Unprobed"
        )
        if self.is_enabled():
            self._summary_lbl.configure(
                text=f"Selected: {start_str} -> {end_str}  (Clip: {clip_str} / Total: {total_str})",
                text_color=("#0284c7", "#38bdf8"),
            )
        else:
            self._summary_lbl.configure(
                text=f"Full Media ({total_str}) — enable checkbox to trim {start_str} -> {end_str}",
                text_color=("gray35", "gray70"),
            )
        self._redraw_timeline()

    def _track_bounds(self) -> tuple[float, float]:
        width = max(240, int(self._canvas.winfo_width() or 640))
        pad = 24.0
        return pad, max(pad + 60.0, width - pad)

    def _seconds_to_x(self, seconds: float) -> float:
        left_x, right_x = self._track_bounds()
        total = max(1.0, self.effective_timeline_duration)
        frac = max(0.0, min(1.0, seconds / total))
        return left_x + frac * (right_x - left_x)

    def _x_to_seconds(self, x_coord: float) -> float:
        left_x, right_x = self._track_bounds()
        span = max(1.0, right_x - left_x)
        frac = max(0.0, min(1.0, (x_coord - left_x) / span))
        return round(frac * self.effective_timeline_duration, 1)

    def _on_canvas_press(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        if not self.is_enabled():
            self._enabled_var.set(True)

        x_pos = float(getattr(event, "x", 0.0))
        start_x = self._seconds_to_x(self._start_seconds)
        end_x = self._seconds_to_x(self._end_seconds)

        if abs(x_pos - start_x) <= abs(x_pos - end_x):
            self._active_handle = "start"
        else:
            self._active_handle = "end"

        self.set_handle_seconds(self._active_handle, self._x_to_seconds(x_pos))

    def _on_canvas_drag(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        if not self._active_handle:
            return
        x_pos = float(getattr(event, "x", 0.0))
        self.set_handle_seconds(self._active_handle, self._x_to_seconds(x_pos))

    def _on_canvas_release(self, _event: tk.Event) -> None:  # type: ignore[type-arg]
        self._active_handle = None

    def _redraw_timeline(self) -> None:
        self._canvas.delete("all")
        is_dark = ctk.get_appearance_mode().lower() == "dark"
        bg_col = "#1e293b" if is_dark else "#e2e8f0"
        track_col = "#334155" if is_dark else "#cbd5e1"
        active_col = "#2563eb" if self.is_enabled() else ("#475569" if is_dark else "#94a3b8")
        handle_col = "#38bdf8" if self.is_enabled() else "#64748b"
        text_col = "#cbd5e1" if is_dark else "#334155"

        self._canvas.configure(bg=bg_col)
        left_x, right_x = self._track_bounds()
        mid_y = 16.0

        # Base timeline bar
        self._canvas.create_rectangle(
            left_x,
            mid_y - 5,
            right_x,
            mid_y + 5,
            fill=track_col,
            outline="",
        )

        start_x = self._seconds_to_x(self._start_seconds)
        end_x = self._seconds_to_x(self._end_seconds)

        # Highlighted selected segment
        self._canvas.create_rectangle(
            start_x,
            mid_y - 6,
            max(start_x + 2, end_x),
            mid_y + 6,
            fill=active_col,
            outline="",
        )

        # Time scale ticks (0%, 25%, 50%, 75%, 100%)
        total_dur = self.effective_timeline_duration
        for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
            tx = left_x + frac * (right_x - left_x)
            self._canvas.create_line(tx, mid_y + 7, tx, mid_y + 12, fill=text_col, width=1)
            tick_label = format_timestamp(frac * total_dur)
            self._canvas.create_text(
                tx,
                mid_y + 19,
                text=tick_label,
                fill=text_col,
                font=("Segoe UI", 8),
            )

        # Start and End draggable handles
        for hx, tag_txt in ((start_x, "["), (end_x, "]")):
            self._canvas.create_rectangle(
                hx - 5,
                mid_y - 11,
                hx + 5,
                mid_y + 11,
                fill=handle_col,
                outline="#ffffff" if is_dark else "#0f172a",
                width=1,
            )
            self._canvas.create_text(
                hx,
                mid_y,
                text=tag_txt,
                fill="#0f172a",
                font=("Consolas", 8, "bold"),
            )

    def _notify_change(self) -> None:
        if self._on_change is not None:
            self._on_change(
                TrimConfig(
                    enabled=self.is_enabled(),
                    start_seconds=self._start_seconds,
                    end_seconds=self._end_seconds,
                    source_duration=self._media_duration,
                    accurate_trim=self.is_accurate_trim(),
                )
            )
