"""Settings view allowing users to configure and persist application preferences."""

from __future__ import annotations

import tkinter as tk
from tkinter import filedialog
from typing import Callable

import customtkinter as ctk  # type: ignore[import-untyped]

from src.core.exceptions import AppError
from src.core.models import (
    AppSettings,
    AudioFormat,
    AudioQuality,
    DownloadMode,
    OverwriteBehavior,
    SubtitleFormat,
    ThemeMode,
    VideoFormat,
    VideoQuality,
)
from src.services.ffmpeg_service import FFmpegService
from src.services.settings_service import SettingsService
from src.ui.dialogs import (
    ask_confirmation_dialog,
    show_error_dialog,
    show_info_dialog,
)


class SettingsView(ctk.CTkFrame):
    """Form view for inspecting, validating, and saving application settings."""

    def __init__(
        self,
        master: ctk.CTkBaseClass,
        settings_service: SettingsService,
        ffmpeg_service: FFmpegService,
        on_settings_saved: Callable[[AppSettings], None],
        on_open_logs_dir: Callable[[], None],
    ) -> None:
        super().__init__(master, corner_radius=8)
        self._settings_service = settings_service
        self._ffmpeg_service = ffmpeg_service
        self._on_settings_saved = on_settings_saved
        self._on_open_logs_dir = on_open_logs_dir

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # Header
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 6))
        header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            header,
            text="Application Settings",
            font=ctk.CTkFont(size=16, weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")

        btn_row = ctk.CTkFrame(header, fg_color="transparent")
        btn_row.grid(row=0, column=1, sticky="e")

        ctk.CTkButton(
            btn_row,
            text="Reset to Defaults",
            width=136,
            height=30,
            fg_color=("gray75", "gray30"),
            text_color=("gray10", "gray90"),
            command=self._handle_reset_defaults,
        ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            btn_row,
            text="Save Settings",
            width=130,
            height=30,
            fg_color=("#16a34a", "#15803d"),
            hover_color=("#15803d", "#166534"),
            command=self._handle_save,
        ).pack(side="left")

        # Scrollable form body
        form = ctk.CTkScrollableFrame(self, fg_color="transparent")
        form.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        form.grid_columnconfigure(1, weight=1)

        row_idx = 0

        # 1. Default Download Directory
        ctk.CTkLabel(form, text="Default Download Folder:", font=ctk.CTkFont(weight="bold")).grid(
            row=row_idx, column=0, sticky="w", padx=10, pady=8
        )
        dir_frame = ctk.CTkFrame(form, fg_color="transparent")
        dir_frame.grid(row=row_idx, column=1, sticky="ew", padx=10, pady=8)
        dir_frame.grid_columnconfigure(0, weight=1)

        self._download_dir_entry = ctk.CTkEntry(dir_frame, height=32)
        self._download_dir_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ctk.CTkButton(
            dir_frame,
            text="Browse...",
            width=96,
            height=32,
            command=self._browse_download_dir,
        ).grid(row=0, column=1)
        row_idx += 1

        # 2. Default Mode
        ctk.CTkLabel(form, text="Default Download Mode:", font=ctk.CTkFont(weight="bold")).grid(
            row=row_idx, column=0, sticky="w", padx=10, pady=8
        )
        self._mode_menu = ctk.CTkOptionMenu(
            form,
            values=[DownloadMode.VIDEO.value, DownloadMode.AUDIO.value, DownloadMode.CAPTIONS.value],
            width=200,
        )
        self._mode_menu.grid(row=row_idx, column=1, sticky="w", padx=10, pady=8)
        row_idx += 1

        # 3. Default Video Quality & Format
        ctk.CTkLabel(form, text="Default Video Quality & Container:", font=ctk.CTkFont(weight="bold")).grid(
            row=row_idx, column=0, sticky="w", padx=10, pady=8
        )
        vid_frame = ctk.CTkFrame(form, fg_color="transparent")
        vid_frame.grid(row=row_idx, column=1, sticky="w", padx=10, pady=8)

        self._video_quality_menu = ctk.CTkOptionMenu(
            vid_frame,
            values=[q.value for q in VideoQuality],
            width=140,
        )
        self._video_quality_menu.pack(side="left", padx=(0, 8))

        self._video_format_menu = ctk.CTkOptionMenu(
            vid_frame,
            values=[f.value for f in VideoFormat],
            width=120,
        )
        self._video_format_menu.pack(side="left")
        row_idx += 1

        # 4. Default Audio Format & Bitrate
        ctk.CTkLabel(form, text="Default Audio Format & Bitrate (kbps):", font=ctk.CTkFont(weight="bold")).grid(
            row=row_idx, column=0, sticky="w", padx=10, pady=8
        )
        aud_frame = ctk.CTkFrame(form, fg_color="transparent")
        aud_frame.grid(row=row_idx, column=1, sticky="w", padx=10, pady=8)

        self._audio_format_menu = ctk.CTkOptionMenu(
            aud_frame,
            values=[a.value for a in AudioFormat],
            width=140,
        )
        self._audio_format_menu.pack(side="left", padx=(0, 8))

        self._audio_quality_menu = ctk.CTkOptionMenu(
            aud_frame,
            values=[q.value for q in AudioQuality],
            width=120,
        )
        self._audio_quality_menu.pack(side="left")
        row_idx += 1

        # 4b. Default Subtitle & Caption Preferences
        ctk.CTkLabel(form, text="Default Subtitle & Caption Options:", font=ctk.CTkFont(weight="bold")).grid(
            row=row_idx, column=0, sticky="w", padx=10, pady=8
        )
        sub_frame = ctk.CTkFrame(form, fg_color="transparent")
        sub_frame.grid(row=row_idx, column=1, sticky="w", padx=10, pady=8)

        self._subtitle_format_menu = ctk.CTkOptionMenu(
            sub_frame,
            values=[f.value for f in SubtitleFormat],
            width=100,
        )
        self._subtitle_format_menu.pack(side="left", padx=(0, 12))

        self._include_auto_captions_var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            sub_frame,
            text="Include auto-captions",
            variable=self._include_auto_captions_var,
        ).pack(side="left", padx=(0, 12))

        self._embed_subtitles_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            sub_frame,
            text="Embed soft subtitles",
            variable=self._embed_subtitles_var,
        ).pack(side="left", padx=(0, 12))

        self._keep_subtitle_files_var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            sub_frame,
            text="Keep separate subtitle files",
            variable=self._keep_subtitle_files_var,
        ).pack(side="left")
        row_idx += 1

        # 5. Appearance Theme
        ctk.CTkLabel(form, text="Appearance Theme:", font=ctk.CTkFont(weight="bold")).grid(
            row=row_idx, column=0, sticky="w", padx=10, pady=8
        )
        self._theme_menu = ctk.CTkOptionMenu(
            form,
            values=[t.value for t in ThemeMode],
            width=200,
        )
        self._theme_menu.grid(row=row_idx, column=1, sticky="w", padx=10, pady=8)
        row_idx += 1

        # 6. Max Concurrent Downloads
        ctk.CTkLabel(form, text="Max Concurrent Downloads (1-4):", font=ctk.CTkFont(weight="bold")).grid(
            row=row_idx, column=0, sticky="w", padx=10, pady=8
        )
        self._concurrency_menu = ctk.CTkOptionMenu(
            form,
            values=["1", "2", "3", "4"],
            width=200,
        )
        self._concurrency_menu.grid(row=row_idx, column=1, sticky="w", padx=10, pady=8)
        row_idx += 1

        # 7. Filename Template
        ctk.CTkLabel(form, text="Output Filename Template:", font=ctk.CTkFont(weight="bold")).grid(
            row=row_idx, column=0, sticky="w", padx=10, pady=8
        )
        tpl_frame = ctk.CTkFrame(form, fg_color="transparent")
        tpl_frame.grid(row=row_idx, column=1, sticky="ew", padx=10, pady=8)
        tpl_frame.grid_columnconfigure(0, weight=1)

        self._template_entry = ctk.CTkEntry(tpl_frame, height=32)
        self._template_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ctk.CTkButton(
            tpl_frame,
            text="Standard Preset",
            width=120,
            height=32,
            fg_color=("gray75", "gray30"),
            text_color=("gray10", "gray90"),
            command=lambda: self._set_template("%(title).150B [%(id)s].%(ext)s"),
        ).grid(row=0, column=1)
        row_idx += 1

        # 8. File Collision / Overwrite Behavior
        ctk.CTkLabel(form, text="Existing File Behavior:", font=ctk.CTkFont(weight="bold")).grid(
            row=row_idx, column=0, sticky="w", padx=10, pady=8
        )
        ov_frame = ctk.CTkFrame(form, fg_color="transparent")
        ov_frame.grid(row=row_idx, column=1, sticky="w", padx=10, pady=8)

        self._overwrite_menu = ctk.CTkOptionMenu(
            ov_frame,
            values=[o.value for o in OverwriteBehavior],
            width=160,
        )
        self._overwrite_menu.pack(side="left", padx=(0, 14))

        self._ask_overwrite_var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            ov_frame,
            text="Prompt before replacing existing file",
            variable=self._ask_overwrite_var,
        ).pack(side="left")
        row_idx += 1

        # 9. Notifications
        ctk.CTkLabel(form, text="Notifications:", font=ctk.CTkFont(weight="bold")).grid(
            row=row_idx, column=0, sticky="w", padx=10, pady=8
        )
        self._notify_var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            form,
            text="Show status bar banner when downloads complete",
            variable=self._notify_var,
        ).grid(row=row_idx, column=1, sticky="w", padx=10, pady=8)
        row_idx += 1

        # 10. Custom FFmpeg Path
        ctk.CTkLabel(
            form,
            text="Custom FFmpeg Path (Optional):",
            font=ctk.CTkFont(weight="bold"),
        ).grid(row=row_idx, column=0, sticky="w", padx=10, pady=8)
        ff_frame = ctk.CTkFrame(form, fg_color="transparent")
        ff_frame.grid(row=row_idx, column=1, sticky="ew", padx=10, pady=8)
        ff_frame.grid_columnconfigure(0, weight=1)

        self._ffmpeg_entry = ctk.CTkEntry(
            ff_frame,
            placeholder_text="Leave blank for automatic detection (e.g. C:\\ffmpeg\\bin\\ffmpeg.exe)",
            height=32,
        )
        self._ffmpeg_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ctk.CTkButton(
            ff_frame,
            text="Browse...",
            width=90,
            height=32,
            command=self._browse_ffmpeg_path,
        ).grid(row=0, column=1, padx=(0, 6))
        ctk.CTkButton(
            ff_frame,
            text="Test FFmpeg",
            width=100,
            height=32,
            command=self._test_ffmpeg,
        ).grid(row=0, column=2)
        row_idx += 1

        # 11. Logging Verbosity
        ctk.CTkLabel(form, text="Logging Verbosity:", font=ctk.CTkFont(weight="bold")).grid(
            row=row_idx, column=0, sticky="w", padx=10, pady=8
        )
        log_frame = ctk.CTkFrame(form, fg_color="transparent")
        log_frame.grid(row=row_idx, column=1, sticky="w", padx=10, pady=8)

        self._log_level_menu = ctk.CTkOptionMenu(
            log_frame,
            values=["DEBUG", "INFO", "WARNING", "ERROR"],
            width=160,
        )
        self._log_level_menu.pack(side="left", padx=(0, 12))

        ctk.CTkButton(
            log_frame,
            text="Open Logs Folder",
            width=140,
            height=30,
            command=self._on_open_logs_dir,
        ).pack(side="left")

        self.load_from_settings(self._settings_service.settings)

    def load_from_settings(self, settings: AppSettings) -> None:
        """Populate form controls with values from an AppSettings object."""
        self._download_dir_entry.delete(0, "end")
        self._download_dir_entry.insert(0, settings.default_download_dir)

        self._mode_menu.set(settings.default_mode.value)
        self._video_quality_menu.set(settings.default_video_quality.value)
        self._video_format_menu.set(settings.default_video_format.value)
        self._audio_format_menu.set(settings.default_audio_format.value)
        self._audio_quality_menu.set(settings.default_audio_quality.value)
        self._subtitle_format_menu.set(settings.default_subtitle_format.value)
        self._include_auto_captions_var.set(settings.default_include_auto_captions)
        self._embed_subtitles_var.set(settings.default_embed_subtitles)
        self._keep_subtitle_files_var.set(settings.default_keep_subtitle_files)
        self._theme_menu.set(settings.theme_mode.value)
        self._concurrency_menu.set(str(settings.max_concurrent_downloads))

        self._template_entry.delete(0, "end")
        self._template_entry.insert(0, settings.filename_template)

        self._overwrite_menu.set(settings.overwrite_behavior.value)
        self._ask_overwrite_var.set(settings.ask_before_overwrite)
        self._notify_var.set(settings.enable_completion_notifications)

        self._ffmpeg_entry.delete(0, "end")
        if settings.ffmpeg_custom_path:
            self._ffmpeg_entry.insert(0, settings.ffmpeg_custom_path)

        self._log_level_menu.set(settings.log_level)

    def _set_template(self, value: str) -> None:
        self._template_entry.delete(0, "end")
        self._template_entry.insert(0, value)

    def _browse_download_dir(self) -> None:
        chosen = filedialog.askdirectory(
            parent=self,
            title="Select Default Download Folder",
            initialdir=self._download_dir_entry.get().strip() or None,
        )
        if chosen:
            self._download_dir_entry.delete(0, "end")
            self._download_dir_entry.insert(0, chosen)

    def _browse_ffmpeg_path(self) -> None:
        chosen = filedialog.askopenfilename(
            parent=self,
            title="Select ffmpeg.exe",
            filetypes=[("FFmpeg Executable", "ffmpeg.exe"), ("All Files", "*.*")],
        )
        if chosen:
            self._ffmpeg_entry.delete(0, "end")
            self._ffmpeg_entry.insert(0, chosen)

    def _test_ffmpeg(self) -> None:
        custom = self._ffmpeg_entry.get().strip()
        self._ffmpeg_service.set_custom_path(custom)
        status = self._ffmpeg_service.check_dependencies(force_refresh=True)
        if status.ffmpeg_available:
            show_info_dialog(
                self,
                "FFmpeg Detected",
                f"FFmpeg is working!\n\nPath: {status.ffmpeg_path}\nVersion: {status.ffmpeg_version}",
            )
        else:
            show_error_dialog(
                self,
                "FFmpeg Not Found",
                "Could not locate or run FFmpeg at the specified path.",
                FFmpegService.get_missing_ffmpeg_explanation(),
            )

    def _handle_save(self) -> None:
        candidate = AppSettings(
            default_download_dir=self._download_dir_entry.get().strip(),
            default_mode=DownloadMode(self._mode_menu.get()),
            default_video_quality=VideoQuality(self._video_quality_menu.get()),
            default_video_format=VideoFormat(self._video_format_menu.get()),
            default_audio_format=AudioFormat(self._audio_format_menu.get()),
            default_audio_quality=AudioQuality(self._audio_quality_menu.get()),
            default_subtitle_format=SubtitleFormat(self._subtitle_format_menu.get()),
            default_include_auto_captions=bool(self._include_auto_captions_var.get()),
            default_embed_subtitles=bool(self._embed_subtitles_var.get()),
            default_keep_subtitle_files=bool(self._keep_subtitle_files_var.get()),
            theme_mode=ThemeMode(self._theme_menu.get()),
            max_concurrent_downloads=int(self._concurrency_menu.get()),
            filename_template=self._template_entry.get().strip(),
            ask_before_overwrite=bool(self._ask_overwrite_var.get()),
            overwrite_behavior=OverwriteBehavior(self._overwrite_menu.get()),
            enable_completion_notifications=bool(self._notify_var.get()),
            ffmpeg_custom_path=self._ffmpeg_entry.get().strip(),
            log_level=self._log_level_menu.get().strip(),
        )
        try:
            saved = self._settings_service.save(candidate)
            self._ffmpeg_service.set_custom_path(saved.ffmpeg_custom_path)
            self._ffmpeg_service.check_dependencies(force_refresh=True)
            self.load_from_settings(saved)
            self._on_settings_saved(saved)
            show_info_dialog(self, "Settings Saved", "Your preferences have been saved and applied.")
        except AppError as exc:
            show_error_dialog(self, "Invalid Settings", exc.user_message, exc.remedy)

    def _handle_reset_defaults(self) -> None:
        if not ask_confirmation_dialog(
            self,
            "Reset Settings",
            "Reset all preferences to their default values?",
        ):
            return
        try:
            defaults = self._settings_service.reset_to_defaults()
            self._ffmpeg_service.set_custom_path(defaults.ffmpeg_custom_path)
            self._ffmpeg_service.check_dependencies(force_refresh=True)
            self.load_from_settings(defaults)
            self._on_settings_saved(defaults)
        except AppError as exc:
            show_error_dialog(self, "Reset Failed", exc.user_message, exc.remedy)
