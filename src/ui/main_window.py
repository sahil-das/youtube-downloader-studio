"""Main application window for YouTube Downloader Studio."""

from __future__ import annotations

import threading
from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk  # type: ignore[import-untyped]

from src.core.events import AppEvent, EventBus, EventType
from src.core.exceptions import AppError, DownloadCancelledError
from src.core.models import (
    AppSettings,
    AudioFormat,
    AudioQuality,
    DependencyStatus,
    DownloadMode,
    HistoryEntry,
    JobStatus,
    MediaMetadata,
    PlaylistMode,
    ThemeMode,
    VideoFormat,
    VideoQuality,
)
from src.core.validators import (
    is_likely_playlist_url,
    parse_batch_urls,
    validate_output_directory,
)
from src.database.repositories import HistoryRepository
from src.services.ffmpeg_service import FFmpegService
from src.services.media_info_service import MediaInfoService
from src.services.queue_service import QueueService
from src.services.settings_service import SettingsService
from src.ui.components import MetadataPreviewCard
from src.ui.dialogs import (
    DiagnosticsDialog,
    PlaylistSelectionDialog,
    ask_confirmation_dialog,
    show_error_dialog,
    show_warning_dialog,
)
from src.ui.history_view import HistoryView
from src.ui.queue_view import QueueView
from src.ui.settings_view import SettingsView
from src.utils.logging_config import get_logger
from src.utils.platform_utils import open_directory

logger = get_logger("main_window")


class MainWindow(ctk.CTk):
    """Primary desktop window coordinating Downloader, Queue, History, and Settings views."""

    def __init__(
        self,
        settings_service: SettingsService,
        ffmpeg_service: FFmpegService,
        media_info_service: MediaInfoService,
        queue_service: QueueService,
        history_repo: HistoryRepository,
        event_bus: EventBus,
        app_data_dir: Path,
        log_file_path: Path,
        icon_path: Path | None = None,
    ) -> None:
        super().__init__()

        self._settings_service = settings_service
        self._ffmpeg_service = ffmpeg_service
        self._media_info_service = media_info_service
        self._queue_service = queue_service
        self._history_repo = history_repo
        self._event_bus = event_bus
        self._app_data_dir = app_data_dir
        self._log_file_path = log_file_path

        self._metadata_cancel_event: threading.Event | None = None
        self._current_metadata: MediaMetadata | None = None
        self._selected_playlist_mode: PlaylistMode = PlaylistMode.SINGLE
        self._selected_playlist_indices: list[int] = []

        # Apply saved theme
        self._apply_theme(self._settings_service.settings.theme_mode)

        self.title("YouTube Downloader Studio")
        self.geometry("1160x800")
        self.minsize(1020, 700)

        if icon_path and icon_path.exists():
            try:
                self.iconbitmap(str(icon_path))
            except Exception:
                pass

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        self._build_header()
        self._build_navigation()
        self._build_views()
        self._build_footer()

        self._sync_controls_from_settings(self._settings_service.settings)
        self._refresh_dependency_status_ui()
        self._queue_service.restore_unfinished_jobs_from_history()
        self._queue_view.refresh_all()

        self.protocol("WM_DELETE_WINDOW", self._on_close_window)
        self.after(100, self._poll_event_bus)

    # ------------------------------------------------------------------
    # Layout Builders
    # ------------------------------------------------------------------

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, corner_radius=0, height=56)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(1, weight=1)

        logo_badge = ctk.CTkLabel(
            header,
            text=" ▶ YTD STUDIO ",
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=("#dc2626", "#b91c1c"),
            text_color="white",
            corner_radius=6,
            height=30,
        )
        logo_badge.grid(row=0, column=0, padx=(16, 10), pady=12)

        title_block = ctk.CTkFrame(header, fg_color="transparent")
        title_block.grid(row=0, column=1, sticky="w", pady=8)

        ctk.CTkLabel(
            title_block,
            text="YouTube Downloader Studio",
            font=ctk.CTkFont(size=18, weight="bold"),
            anchor="w",
        ).pack(anchor="w")

        ctk.CTkLabel(
            title_block,
            text="Authorized Video, Audio & Playlist Desktop Manager",
            font=ctk.CTkFont(size=11),
            text_color=("gray35", "gray70"),
            anchor="w",
        ).pack(anchor="w")

        right_controls = ctk.CTkFrame(header, fg_color="transparent")
        right_controls.grid(row=0, column=2, padx=16, pady=10, sticky="e")

        self._diag_btn = ctk.CTkButton(
            right_controls,
            text="Diagnostics & FFmpeg",
            width=155,
            height=30,
            fg_color=("gray78", "gray28"),
            text_color=("gray10", "gray90"),
            command=self._open_diagnostics_dialog,
        )
        self._diag_btn.pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            right_controls,
            text="Open Logs",
            width=96,
            height=30,
            fg_color=("gray78", "gray28"),
            text_color=("gray10", "gray90"),
            command=self._open_logs_folder,
        ).pack(side="left", padx=(0, 8))

        self._theme_btn = ctk.CTkButton(
            right_controls,
            text="Theme: Dark",
            width=110,
            height=30,
            command=self._toggle_theme_quick,
        )
        self._theme_btn.pack(side="left")

    def _build_navigation(self) -> None:
        nav_frame = ctk.CTkFrame(self, fg_color="transparent")
        nav_frame.grid(row=1, column=0, sticky="ew", padx=16, pady=(8, 4))
        nav_frame.grid_columnconfigure(0, weight=1)

        self._nav_segmented = ctk.CTkSegmentedButton(
            nav_frame,
            values=["Downloader & Queue", "Download History", "Settings"],
            font=ctk.CTkFont(size=13, weight="bold"),
            height=34,
            command=self._on_navigation_changed,
        )
        self._nav_segmented.set("Downloader & Queue")
        self._nav_segmented.grid(row=0, column=0, sticky="ew")

    def _build_views(self) -> None:
        self._content_container = ctk.CTkFrame(self, fg_color="transparent")
        self._content_container.grid(row=2, column=0, sticky="nsew", padx=16, pady=4)
        self._content_container.grid_columnconfigure(0, weight=1)
        self._content_container.grid_rowconfigure(0, weight=1)

        # View 1: Downloader & Queue
        self._downloader_view = ctk.CTkFrame(self._content_container, fg_color="transparent")
        self._downloader_view.grid(row=0, column=0, sticky="nsew")
        self._downloader_view.grid_columnconfigure(0, weight=1)
        self._downloader_view.grid_rowconfigure(3, weight=1)

        self._build_url_input_section(self._downloader_view)
        self._build_metadata_section(self._downloader_view)
        self._build_options_section(self._downloader_view)

        self._queue_view = QueueView(self._downloader_view, queue_service=self._queue_service)
        self._queue_view.grid(row=3, column=0, sticky="nsew", pady=(4, 0))

        # View 2: History
        self._history_view = HistoryView(
            self._content_container,
            history_repo=self._history_repo,
            on_retry_entry=self._handle_retry_from_history,
        )

        # View 3: Settings
        self._settings_view = SettingsView(
            self._content_container,
            settings_service=self._settings_service,
            ffmpeg_service=self._ffmpeg_service,
            on_settings_saved=self._handle_settings_saved,
            on_open_logs_dir=self._open_logs_folder,
        )

    def _build_url_input_section(self, parent: ctk.CTkFrame) -> None:
        box = ctk.CTkFrame(parent, corner_radius=8)
        box.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        box.grid_columnconfigure(0, weight=1)

        top_bar = ctk.CTkFrame(box, fg_color="transparent")
        top_bar.grid(row=0, column=0, columnspan=2, sticky="ew", padx=12, pady=(8, 2))
        top_bar.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            top_bar,
            text="YouTube Video or Playlist URL(s) — paste one URL or multiple URLs (one per line):",
            font=ctk.CTkFont(size=12, weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")

        btn_strip = ctk.CTkFrame(top_bar, fg_color="transparent")
        btn_strip.grid(row=0, column=1, sticky="e")

        ctk.CTkButton(
            btn_strip,
            text="Paste Clipboard",
            width=120,
            height=26,
            font=ctk.CTkFont(size=11),
            command=self._paste_from_clipboard,
        ).pack(side="left", padx=(0, 6))

        ctk.CTkButton(
            btn_strip,
            text="Clear",
            width=68,
            height=26,
            font=ctk.CTkFont(size=11),
            fg_color=("gray75", "gray30"),
            text_color=("gray10", "gray90"),
            command=self._clear_url_input,
        ).pack(side="left", padx=(0, 6))

        self._fetch_info_btn = ctk.CTkButton(
            btn_strip,
            text="Fetch Info / Preview",
            width=142,
            height=26,
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self._start_metadata_fetch,
        )
        self._fetch_info_btn.pack(side="left")

        self._url_textbox = ctk.CTkTextbox(
            box,
            height=56,
            font=ctk.CTkFont(family="Consolas", size=12),
            wrap="none",
        )
        self._url_textbox.grid(row=1, column=0, sticky="ew", padx=12, pady=(2, 8))

    def _build_metadata_section(self, parent: ctk.CTkFrame) -> None:
        self._preview_card = MetadataPreviewCard(
            parent,
            on_retry_metadata=self._start_metadata_fetch,
            on_open_playlist_selector=self._open_playlist_selector_dialog,
        )
        self._preview_card.grid(row=1, column=0, sticky="ew", pady=(0, 6))

    def _build_options_section(self, parent: ctk.CTkFrame) -> None:
        opts_card = ctk.CTkFrame(parent, corner_radius=8)
        opts_card.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        opts_card.grid_columnconfigure(3, weight=1)

        # Row 0: Mode, Quality/Format, Playlist Mode
        row0 = ctk.CTkFrame(opts_card, fg_color="transparent")
        row0.grid(row=0, column=0, columnspan=4, sticky="ew", padx=12, pady=(8, 4))

        ctk.CTkLabel(row0, text="Mode:", font=ctk.CTkFont(size=12, weight="bold")).pack(
            side="left", padx=(0, 6)
        )
        self._mode_selector = ctk.CTkSegmentedButton(
            row0,
            values=["Video", "Audio Only"],
            width=170,
            command=self._on_mode_changed,
        )
        self._mode_selector.set("Video")
        self._mode_selector.pack(side="left", padx=(0, 16))

        # Video options frame
        self._video_opts_frame = ctk.CTkFrame(row0, fg_color="transparent")
        self._video_opts_frame.pack(side="left", padx=(0, 14))

        ctk.CTkLabel(self._video_opts_frame, text="Quality:", font=ctk.CTkFont(size=12)).pack(
            side="left", padx=(0, 6)
        )
        self._video_quality_menu = ctk.CTkOptionMenu(
            self._video_opts_frame,
            values=[q.value for q in VideoQuality],
            width=115,
        )
        self._video_quality_menu.set(VideoQuality.Q1080P.value)
        self._video_quality_menu.pack(side="left", padx=(0, 12))

        ctk.CTkLabel(self._video_opts_frame, text="Format:", font=ctk.CTkFont(size=12)).pack(
            side="left", padx=(0, 6)
        )
        self._video_format_menu = ctk.CTkOptionMenu(
            self._video_opts_frame,
            values=[f.value for f in VideoFormat],
            width=95,
        )
        self._video_format_menu.set(VideoFormat.MP4.value)
        self._video_format_menu.pack(side="left")

        # Audio options frame
        self._audio_opts_frame = ctk.CTkFrame(row0, fg_color="transparent")

        ctk.CTkLabel(self._audio_opts_frame, text="Audio Format:", font=ctk.CTkFont(size=12)).pack(
            side="left", padx=(0, 6)
        )
        self._audio_format_menu = ctk.CTkOptionMenu(
            self._audio_opts_frame,
            values=[a.value for a in AudioFormat],
            width=115,
        )
        self._audio_format_menu.set(AudioFormat.MP3.value)
        self._audio_format_menu.pack(side="left", padx=(0, 12))

        ctk.CTkLabel(self._audio_opts_frame, text="Bitrate (kbps):", font=ctk.CTkFont(size=12)).pack(
            side="left", padx=(0, 6)
        )
        self._audio_quality_menu = ctk.CTkOptionMenu(
            self._audio_opts_frame,
            values=[q.value for q in AudioQuality],
            width=95,
        )
        self._audio_quality_menu.set(AudioQuality.K192.value)
        self._audio_quality_menu.pack(side="left")

        # Playlist option
        pl_frame = ctk.CTkFrame(row0, fg_color="transparent")
        pl_frame.pack(side="right")

        ctk.CTkLabel(pl_frame, text="Playlist Handling:", font=ctk.CTkFont(size=12)).pack(
            side="left", padx=(0, 6)
        )
        self._playlist_mode_menu = ctk.CTkOptionMenu(
            pl_frame,
            values=["Single Video Only", "Full Playlist", "Selected Items"],
            width=150,
            command=self._on_playlist_menu_changed,
        )
        self._playlist_mode_menu.set("Single Video Only")
        self._playlist_mode_menu.pack(side="left")

        # Row 1: Destination folder + Add to Queue / Start Download buttons
        row1 = ctk.CTkFrame(opts_card, fg_color="transparent")
        row1.grid(row=1, column=0, columnspan=4, sticky="ew", padx=12, pady=(4, 10))
        row1.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(row1, text="Save To:", font=ctk.CTkFont(size=12, weight="bold")).grid(
            row=0, column=0, padx=(0, 8), sticky="w"
        )

        self._dest_entry = ctk.CTkEntry(row1, height=32)
        self._dest_entry.grid(row=0, column=1, sticky="ew", padx=(0, 6))

        ctk.CTkButton(
            row1,
            text="Browse...",
            width=86,
            height=32,
            fg_color=("gray75", "gray30"),
            text_color=("gray10", "gray90"),
            command=self._browse_destination,
        ).grid(row=0, column=2, padx=(0, 6))

        ctk.CTkButton(
            row1,
            text="Open Folder",
            width=96,
            height=32,
            fg_color=("gray75", "gray30"),
            text_color=("gray10", "gray90"),
            command=self._open_destination_folder,
        ).grid(row=0, column=3, padx=(0, 16))

        ctk.CTkButton(
            row1,
            text="+ Add to Queue",
            width=132,
            height=34,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=lambda: self._submit_urls(start_immediately=False),
        ).grid(row=0, column=4, padx=(0, 8))

        ctk.CTkButton(
            row1,
            text="▶ Download Now",
            width=148,
            height=34,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color=("#16a34a", "#15803d"),
            hover_color=("#15803d", "#166534"),
            command=lambda: self._submit_urls(start_immediately=True),
        ).grid(row=0, column=5)

    def _build_footer(self) -> None:
        footer = ctk.CTkFrame(self, corner_radius=0, height=30)
        footer.grid(row=3, column=0, sticky="ew")
        footer.grid_columnconfigure(0, weight=1)

        self._status_msg_lbl = ctk.CTkLabel(
            footer,
            text="Ready — Only download media you have permission or legal authorization to save.",
            font=ctk.CTkFont(size=11),
            text_color=("gray30", "gray75"),
            anchor="w",
        )
        self._status_msg_lbl.grid(row=0, column=0, padx=16, pady=4, sticky="ew")

        self._dep_status_lbl = ctk.CTkLabel(
            footer,
            text="Checking dependencies...",
            font=ctk.CTkFont(size=11, weight="bold"),
            anchor="e",
        )
        self._dep_status_lbl.grid(row=0, column=1, padx=16, pady=4, sticky="e")

    # ------------------------------------------------------------------
    # State Synchronization & Theme
    # ------------------------------------------------------------------

    def _apply_theme(self, theme: ThemeMode) -> None:
        ctk.set_appearance_mode(theme.value)

    def _toggle_theme_quick(self) -> None:
        current = ctk.get_appearance_mode().lower()
        new_theme = ThemeMode.LIGHT if current == "dark" else ThemeMode.DARK
        self._apply_theme(new_theme)
        self._theme_btn.configure(text=f"Theme: {new_theme.value.title()}")

        settings = self._settings_service.settings
        settings.theme_mode = new_theme
        try:
            self._settings_service.save(settings)
            self._settings_view.load_from_settings(settings)
        except Exception as exc:
            logger.debug("Could not persist quick theme toggle: %s", exc)

    def _sync_controls_from_settings(self, settings: AppSettings) -> None:
        self._apply_theme(settings.theme_mode)
        self._theme_btn.configure(text=f"Theme: {settings.theme_mode.value.title()}")

        self._dest_entry.delete(0, "end")
        self._dest_entry.insert(0, settings.default_download_dir)

        if settings.default_mode == DownloadMode.AUDIO:
            self._mode_selector.set("Audio Only")
            self._on_mode_changed("Audio Only")
        else:
            self._mode_selector.set("Video")
            self._on_mode_changed("Video")

        self._video_quality_menu.set(settings.default_video_quality.value)
        self._video_format_menu.set(settings.default_video_format.value)
        self._audio_format_menu.set(settings.default_audio_format.value)
        self._audio_quality_menu.set(settings.default_audio_quality.value)
        self._queue_service.set_max_concurrent_downloads(settings.max_concurrent_downloads)

    def _refresh_dependency_status_ui(self) -> DependencyStatus:
        status = self._ffmpeg_service.check_dependencies(force_refresh=True)
        self._dep_status_lbl.configure(
            text=status.summary_text,
            text_color=("#15803d", "#4ade80")
            if status.full_media_processing_ready
            else ("#b45309", "#fbbf24"),
        )

        # Update audio conversion options based on FFmpeg availability
        if status.ffmpeg_available:
            allowed_audio = [a.value for a in AudioFormat]
        else:
            # Do not offer FFmpeg-only conversion formats when FFmpeg is missing
            allowed_audio = [AudioFormat.BEST.value, AudioFormat.M4A.value]
            if self._audio_format_menu.get() not in allowed_audio:
                self._audio_format_menu.set(AudioFormat.BEST.value)

        self._audio_format_menu.configure(values=allowed_audio)
        return status

    # ------------------------------------------------------------------
    # Navigation & Mode Handlers
    # ------------------------------------------------------------------

    def _on_navigation_changed(self, selected: str) -> None:
        self._downloader_view.grid_forget()
        self._history_view.grid_forget()
        self._settings_view.grid_forget()

        if selected == "Downloader & Queue":
            self._downloader_view.grid(row=0, column=0, sticky="nsew")
            self._queue_view.refresh_all()
        elif selected == "Download History":
            self._history_view.grid(row=0, column=0, sticky="nsew")
            self._history_view.refresh()
        elif selected == "Settings":
            self._settings_view.grid(row=0, column=0, sticky="nsew")
            self._settings_view.load_from_settings(self._settings_service.settings)

    def _on_mode_changed(self, value: str) -> None:
        if value == "Audio Only":
            self._video_opts_frame.pack_forget()
            self._audio_opts_frame.pack(side="left", padx=(0, 14))
        else:
            self._audio_opts_frame.pack_forget()
            self._video_opts_frame.pack(side="left", padx=(0, 14))

    def _on_playlist_menu_changed(self, choice: str) -> None:
        mapping = {
            "Single Video Only": PlaylistMode.SINGLE,
            "Full Playlist": PlaylistMode.FULL_PLAYLIST,
            "Selected Items": PlaylistMode.SELECTED_ITEMS,
        }
        self._selected_playlist_mode = mapping.get(choice, PlaylistMode.SINGLE)
        if (
            self._selected_playlist_mode == PlaylistMode.SELECTED_ITEMS
            and self._current_metadata
            and self._current_metadata.is_playlist
        ):
            self._open_playlist_selector_dialog()

    # ------------------------------------------------------------------
    # URL Input & Background Metadata Fetch
    # ------------------------------------------------------------------

    def _get_raw_url_input(self) -> str:
        return self._url_textbox.get("1.0", "end").strip()

    def _paste_from_clipboard(self) -> None:
        try:
            clip = self.clipboard_get().strip()
        except Exception:
            show_warning_dialog(self, "Clipboard Empty", "No text was found on the Windows clipboard.")
            return

        if not clip:
            return
        existing = self._get_raw_url_input()
        if existing:
            self._url_textbox.insert("end", f"\n{clip}")
        else:
            self._url_textbox.delete("1.0", "end")
            self._url_textbox.insert("1.0", clip)

    def _clear_url_input(self) -> None:
        if self._metadata_cancel_event is not None:
            self._metadata_cancel_event.set()
        self._url_textbox.delete("1.0", "end")
        self._current_metadata = None
        self._selected_playlist_indices.clear()
        self._preview_card.clear()
        self._set_status_message("Cleared URL input.")

    def _start_metadata_fetch(self) -> None:
        raw = self._get_raw_url_input()
        parsed = parse_batch_urls(raw)

        if parsed.has_errors and not parsed.valid_urls:
            first_err = parsed.invalid_entries[0]
            show_error_dialog(
                self,
                "Invalid URL",
                f"Line {first_err.line_number}: {first_err.reason}",
                "Paste a valid https:// YouTube video or playlist link.",
            )
            return

        if not parsed.valid_urls:
            show_warning_dialog(
                self,
                "URL Required",
                "Please enter at least one YouTube URL before fetching metadata.",
            )
            return

        target_url = parsed.valid_urls[0]
        if self._metadata_cancel_event is not None:
            self._metadata_cancel_event.set()

        cancel_event = threading.Event()
        self._metadata_cancel_event = cancel_event

        self._preview_card.show_loading(target_url)
        self._fetch_info_btn.configure(state="disabled", text="Fetching...")
        self._set_status_message(f"Fetching metadata for {target_url}...")

        worker = threading.Thread(
            target=self._metadata_worker,
            args=(target_url, cancel_event),
            name="MetadataFetchWorker",
            daemon=True,
        )
        worker.start()

    def _metadata_worker(self, url: str, cancel_event: threading.Event) -> None:
        try:
            allow_pl = is_likely_playlist_url(url)
            meta = self._media_info_service.fetch_metadata(
                url,
                allow_playlist=allow_pl,
                cancel_event=cancel_event,
            )
            thumb_bytes: bytes | None = None
            if meta.thumbnail_url and not cancel_event.is_set():
                thumb_bytes = self._media_info_service.fetch_thumbnail_bytes(meta.thumbnail_url)

            if not cancel_event.is_set():
                self._event_bus.publish(
                    EventType.METADATA_FETCHED,
                    metadata=meta,
                    thumbnail_bytes=thumb_bytes,
                )
        except DownloadCancelledError:
            pass
        except AppError as exc:
            if not cancel_event.is_set():
                self._event_bus.publish(
                    EventType.METADATA_FAILED,
                    message=exc.user_message,
                    remedy=exc.remedy,
                )
        except Exception as exc:
            if not cancel_event.is_set():
                self._event_bus.publish(
                    EventType.METADATA_FAILED,
                    message=f"Could not inspect URL: {exc}",
                    remedy="Verify your internet connection and the YouTube URL.",
                )

    def _open_playlist_selector_dialog(self) -> None:
        if not self._current_metadata or not self._current_metadata.is_playlist:
            return

        def _on_playlist_confirmed(mode: PlaylistMode, indices: list[int]) -> None:
            self._selected_playlist_mode = mode
            self._selected_playlist_indices = indices
            menu_label = {
                PlaylistMode.SINGLE: "Single Video Only",
                PlaylistMode.FULL_PLAYLIST: "Full Playlist",
                PlaylistMode.SELECTED_ITEMS: "Selected Items",
            }.get(mode, "Single Video Only")
            self._playlist_mode_menu.set(menu_label)
            if self._current_metadata:
                self._preview_card.show_metadata(
                    self._current_metadata,
                    playlist_mode=mode,
                    selected_count=len(indices),
                )

        PlaylistSelectionDialog(
            self,
            metadata=self._current_metadata,
            initial_selected_indices=self._selected_playlist_indices,
            on_confirm=_on_playlist_confirmed,
        )

    # ------------------------------------------------------------------
    # Adding Jobs to Queue / Starting Downloads
    # ------------------------------------------------------------------

    def _submit_urls(self, start_immediately: bool) -> None:
        raw_text = self._get_raw_url_input()
        parsed = parse_batch_urls(raw_text)

        if not parsed.valid_urls and not parsed.has_errors:
            show_warning_dialog(
                self,
                "No URL Entered",
                "Paste one or more valid YouTube URLs into the input box.",
            )
            return

        if parsed.has_errors:
            err_lines = "\n".join(
                f"• Line {e.line_number} ({e.raw_value[:50]}): {e.reason}"
                for e in parsed.invalid_entries[:5]
            )
            if not parsed.valid_urls:
                show_error_dialog(
                    self,
                    "Invalid URL(s)",
                    f"Could not add URL(s) due to validation errors:\n\n{err_lines}",
                    "Ensure every URL starts with https:// and points to youtube.com or youtu.be.",
                )
                return
            proceed = ask_confirmation_dialog(
                self,
                "Some URLs Are Invalid",
                f"The following lines failed validation:\n\n{err_lines}\n\n"
                f"Do you want to continue with the {len(parsed.valid_urls)} valid URL(s)?",
            )
            if not proceed:
                return

        if parsed.has_duplicates:
            self._set_status_message(
                f"Skipped {len(parsed.duplicate_urls)} duplicate URL(s) in batch input."
            )

        dest_raw = self._dest_entry.get().strip()
        try:
            dest_dir = validate_output_directory(dest_raw, create_if_missing=True)
        except AppError as exc:
            show_error_dialog(self, "Invalid Download Folder", exc.user_message, exc.remedy)
            return

        mode = (
            DownloadMode.AUDIO
            if self._mode_selector.get() == "Audio Only"
            else DownloadMode.VIDEO
        )
        video_quality = VideoQuality(self._video_quality_menu.get())
        video_format = VideoFormat(self._video_format_menu.get())
        audio_format = AudioFormat(self._audio_format_menu.get())
        audio_quality = AudioQuality(self._audio_quality_menu.get())

        dep_status = self._ffmpeg_service.check_dependencies()
        if mode == DownloadMode.AUDIO and audio_format.requires_ffmpeg_conversion and not dep_status.ffmpeg_available:
            show_error_dialog(
                self,
                "FFmpeg Required for Audio Conversion",
                f"Converting audio to {audio_format.value.upper()} requires FFmpeg, which is not currently installed.",
                FFmpegService.get_missing_ffmpeg_explanation(),
            )
            return

        if (
            mode == DownloadMode.VIDEO
            and not dep_status.ffmpeg_available
            and video_quality in {VideoQuality.Q2160P, VideoQuality.Q1440P, VideoQuality.Q1080P}
        ):
            self._set_status_message(
                "Note: FFmpeg is not installed; falling back to highest available pre-merged progressive stream."
            )

        settings = self._settings_service.settings
        added_count = 0
        duplicate_queue_errors: list[str] = []

        for url in parsed.valid_urls:
            title_hint = ""
            pl_mode = self._selected_playlist_mode
            pl_indices = list(self._selected_playlist_indices)

            if self._current_metadata and self._current_metadata.url == url:
                title_hint = self._current_metadata.title
            elif is_likely_playlist_url(url) and pl_mode == PlaylistMode.SINGLE:
                # Respect current playlist selector choice
                pass

            try:
                self._queue_service.add_job(
                    url=url,
                    destination_dir=dest_dir,
                    mode=mode,
                    video_quality=video_quality,
                    video_format=video_format,
                    audio_format=audio_format,
                    audio_quality=audio_quality,
                    title=title_hint,
                    filename_template=settings.filename_template,
                    overwrite_behavior=settings.overwrite_behavior,
                    playlist_mode=pl_mode,
                    selected_playlist_indices=pl_indices,
                    allow_duplicate=False,
                    start_immediately=start_immediately,
                )
                added_count += 1
            except AppError as exc:
                duplicate_queue_errors.append(exc.user_message)

        self._queue_view.refresh_all()

        if duplicate_queue_errors and added_count == 0:
            show_warning_dialog(self, "Already in Queue", duplicate_queue_errors[0])
            return

        action_word = "Started downloading" if start_immediately else "Added to queue"
        self._set_status_message(f"{action_word}: {added_count} item(s).")

    def _handle_retry_from_history(self, entry: HistoryEntry) -> None:
        """Re-queue an item from HistoryView and switch to the Downloader & Queue tab."""
        try:
            v_qual = VideoQuality(entry.video_quality) if entry.video_quality in {q.value for q in VideoQuality} else VideoQuality.BEST
            v_fmt = VideoFormat(entry.video_format) if entry.video_format in {f.value for f in VideoFormat} else VideoFormat.MP4
            a_fmt = AudioFormat(entry.audio_format) if entry.audio_format in {a.value for a in AudioFormat} else AudioFormat.MP3

            existing = self._queue_service.get_job(entry.job_id)
            if existing and existing.status in {JobStatus.FAILED, JobStatus.CANCELLED}:
                self._queue_service.retry_job(existing.job_id, start_immediately=True)
            else:
                self._queue_service.add_job(
                    url=entry.url,
                    destination_dir=entry.destination_dir,
                    mode=entry.mode,
                    video_quality=v_qual,
                    video_format=v_fmt,
                    audio_format=a_fmt,
                    title=entry.title,
                    allow_duplicate=True,
                    start_immediately=True,
                )
            self._nav_segmented.set("Downloader & Queue")
            self._on_navigation_changed("Downloader & Queue")
            self._set_status_message(f"Re-queued '{entry.title or entry.url}'.")
        except AppError as exc:
            show_error_dialog(self, "Could Not Re-queue Item", exc.user_message, exc.remedy)

    # ------------------------------------------------------------------
    # Folder & Diagnostics Helpers
    # ------------------------------------------------------------------

    def _browse_destination(self) -> None:
        chosen = filedialog.askdirectory(
            parent=self,
            title="Select Download Destination Folder",
            initialdir=self._dest_entry.get().strip() or None,
        )
        if chosen:
            self._dest_entry.delete(0, "end")
            self._dest_entry.insert(0, chosen)

    def _open_destination_folder(self) -> None:
        dest = self._dest_entry.get().strip()
        try:
            valid_dir = validate_output_directory(dest, create_if_missing=True)
            open_directory(valid_dir)
        except AppError as exc:
            show_error_dialog(self, "Cannot Open Folder", exc.user_message, exc.remedy)

    def _open_logs_folder(self) -> None:
        try:
            open_directory(self._log_file_path.parent)
        except AppError as exc:
            show_error_dialog(self, "Cannot Open Logs Folder", exc.user_message, exc.remedy)

    def _open_diagnostics_dialog(self) -> None:
        status = self._ffmpeg_service.check_dependencies(force_refresh=True)
        DiagnosticsDialog(
            self,
            status=status,
            app_data_dir=str(self._app_data_dir),
            log_file_path=str(self._log_file_path),
            on_recheck=self._refresh_dependency_status_ui,
            on_open_logs=self._open_logs_folder,
        )

    def _handle_settings_saved(self, new_settings: AppSettings) -> None:
        self._sync_controls_from_settings(new_settings)
        self._refresh_dependency_status_ui()
        self._set_status_message("Preferences saved and applied.")

    def _set_status_message(self, message: str) -> None:
        self._status_msg_lbl.configure(text=message)

    # ------------------------------------------------------------------
    # Event Bus Polling (Main UI Thread Only)
    # ------------------------------------------------------------------

    def _poll_event_bus(self) -> None:
        events = self._event_bus.poll_events(max_items=50)
        needs_full_queue_refresh = False

        for event in events:
            try:
                self._handle_single_event(event)
                if event.event_type in {
                    EventType.JOB_ADDED,
                    EventType.JOB_QUEUED,
                    EventType.JOB_PAUSED,
                    EventType.JOB_REMOVED,
                    EventType.JOB_COMPLETED,
                    EventType.JOB_FAILED,
                    EventType.JOB_CANCELLED,
                    EventType.QUEUE_UPDATED,
                }:
                    needs_full_queue_refresh = True
            except Exception as exc:
                logger.error("Ignored invalid or unexpected UI event %s: %s", event, exc)

        if needs_full_queue_refresh:
            self._queue_view.refresh_all()

        self.after(100, self._poll_event_bus)

    def _handle_single_event(self, event: AppEvent) -> None:
        if not isinstance(event, AppEvent) or not isinstance(event.data, dict):
            return
        et = event.event_type
        job = event.data.get("job")

        if et == EventType.METADATA_FETCHED:
            self._fetch_info_btn.configure(state="normal", text="Fetch Info / Preview")
            meta: MediaMetadata | None = event.data.get("metadata")
            if meta is None:
                return
            thumb: bytes | None = event.data.get("thumbnail_bytes")
            self._current_metadata = meta

            if meta.is_playlist:
                self._selected_playlist_mode = PlaylistMode.FULL_PLAYLIST
                self._selected_playlist_indices = [
                    i.index for i in meta.playlist_items if i.available
                ]
                self._playlist_mode_menu.set("Full Playlist")
            else:
                self._selected_playlist_mode = PlaylistMode.SINGLE
                self._selected_playlist_indices = []
                self._playlist_mode_menu.set("Single Video Only")

            self._preview_card.show_metadata(
                meta,
                thumbnail_bytes=thumb,
                playlist_mode=self._selected_playlist_mode,
                selected_count=len(self._selected_playlist_indices),
            )
            self._set_status_message(f"Loaded metadata: {meta.title}")

        elif et == EventType.METADATA_FAILED:
            self._fetch_info_btn.configure(state="normal", text="Fetch Info / Preview")
            msg = str(event.data.get("message") or "Could not fetch metadata.")
            rem = str(event.data.get("remedy") or "")
            self._preview_card.show_error(msg, rem)
            self._set_status_message(f"Metadata error: {msg}")

        elif et in {
            EventType.JOB_STARTING,
            EventType.JOB_FETCHING_METADATA,
            EventType.JOB_PROGRESS,
            EventType.JOB_PROCESSING,
        }:
            if job is not None and not job.status.is_terminal and job.status != JobStatus.PAUSED:
                self._queue_view.refresh_job(job)

        elif et == EventType.JOB_PAUSED:
            if job is not None:
                self._set_status_message(f"Paused: {job.display_title}")

        elif et == EventType.JOB_COMPLETED:
            if job is not None and self._settings_service.settings.enable_completion_notifications:
                self._set_status_message(f"Completed: {job.display_title}")

        elif et == EventType.JOB_FAILED:
            if job is not None:
                err_first = (job.error_message or "Download failed").splitlines()[0]
                self._set_status_message(f"Failed ({job.display_title}): {err_first}")

        elif et == EventType.JOB_CANCELLED:
            if job is not None:
                self._set_status_message(f"Cancelled: {job.display_title}")

    # ------------------------------------------------------------------
    # Clean Shutdown
    # ------------------------------------------------------------------

    def _on_close_window(self) -> None:
        active_jobs = [j for j in self._queue_service.list_jobs() if j.status.is_active]
        if active_jobs:
            confirm = ask_confirmation_dialog(
                self,
                "Downloads in Progress",
                f"There are currently {len(active_jobs)} active download(s) running.\n\n"
                "Closing the application will cancel active downloads. Exit anyway?",
            )
            if not confirm:
                return

        if self._metadata_cancel_event is not None:
            self._metadata_cancel_event.set()
        self._queue_service.shutdown(timeout=2.5)
        self.destroy()
