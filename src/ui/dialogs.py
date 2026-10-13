"""Modal dialogs for errors, confirmations, overwrite prompts, playlist selection, and diagnostics."""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox
from typing import Callable

import customtkinter as ctk  # type: ignore[import-untyped]

from src.core.models import (
    CaptionSource,
    CaptionTrackInfo,
    DependencyStatus,
    MediaMetadata,
    OverwriteBehavior,
    PlaylistMode,
)
from src.services.ffmpeg_service import FFmpegService


def show_error_dialog(
    parent: tk.Misc | None,
    title: str,
    message: str,
    remedy: str = "",
) -> None:
    """Display an understandable error dialog with an optional suggested remedy."""
    full_text = message.strip()
    if remedy and remedy.strip() and remedy.strip() not in full_text:
        full_text = f"{full_text}\n\nSuggested remedy:\n{remedy.strip()}"
    messagebox.showerror(title, full_text, parent=parent)


def show_warning_dialog(
    parent: tk.Misc | None,
    title: str,
    message: str,
) -> None:
    """Display a warning message box."""
    messagebox.showwarning(title, message, parent=parent)


def show_info_dialog(
    parent: tk.Misc | None,
    title: str,
    message: str,
) -> None:
    """Display an informational message box."""
    messagebox.showinfo(title, message, parent=parent)


def ask_confirmation_dialog(
    parent: tk.Misc | None,
    title: str,
    message: str,
) -> bool:
    """Ask the user a Yes/No confirmation question for destructive actions."""
    return bool(messagebox.askyesno(title, message, parent=parent))


def ask_overwrite_dialog(
    parent: tk.Misc | None,
    filename: str,
) -> OverwriteBehavior:
    """Prompt the user when a target file already exists and 'ask_before_overwrite' is enabled.

    Returns OverwriteBehavior.OVERWRITE (Yes), OverwriteBehavior.RENAME (No), or OverwriteBehavior.SKIP (Cancel).
    """
    prompt = (
        f"A file named '{filename}' already exists in the destination folder.\n\n"
        "• Click 'Yes' to overwrite the existing file.\n"
        "• Click 'No' to keep both files (auto-rename with a number).\n"
        "• Click 'Cancel' to skip this download."
    )
    answer = messagebox.askyesnocancel("File Already Exists", prompt, parent=parent)
    if answer is True:
        return OverwriteBehavior.OVERWRITE
    if answer is False:
        return OverwriteBehavior.RENAME
    return OverwriteBehavior.SKIP


class PlaylistSelectionDialog(ctk.CTkToplevel):
    """Modal dialog allowing the user to inspect and select individual playlist items."""

    def __init__(
        self,
        parent: ctk.CTk | ctk.CTkBaseClass,
        metadata: MediaMetadata,
        initial_selected_indices: list[int] | None = None,
        on_confirm: Callable[[PlaylistMode, list[int]], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.title(f"Playlist Items — {metadata.playlist_title or metadata.title}")
        self.geometry("680x520")
        self.minsize(560, 400)
        self.transient(parent.winfo_toplevel())
        self.grab_set()

        self._metadata = metadata
        self._on_confirm = on_confirm
        self._checkbox_vars: dict[int, tk.BooleanVar] = {}

        initial_set = set(initial_selected_indices or [])
        if not initial_set:
            initial_set = {item.index for item in metadata.playlist_items if item.available}

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # Header
        header_frame = ctk.CTkFrame(self, fg_color="transparent")
        header_frame.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 6))
        header_frame.grid_columnconfigure(0, weight=1)

        title_lbl = ctk.CTkLabel(
            header_frame,
            text=f"Playlist: {metadata.playlist_title or metadata.title}",
            font=ctk.CTkFont(size=16, weight="bold"),
            anchor="w",
        )
        title_lbl.grid(row=0, column=0, sticky="ew")

        avail_count = sum(1 for item in metadata.playlist_items if item.available)
        sub_lbl = ctk.CTkLabel(
            header_frame,
            text=(
                f"Showing {len(metadata.playlist_items)} of {metadata.playlist_count} items "
                f"({avail_count} available)"
            ),
            font=ctk.CTkFont(size=12),
            text_color=("gray35", "gray70"),
            anchor="w",
        )
        sub_lbl.grid(row=1, column=0, sticky="ew", pady=(2, 0))

        # Selection toolbar
        toolbar = ctk.CTkFrame(self, fg_color="transparent")
        toolbar.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 6))

        ctk.CTkButton(
            toolbar,
            text="Select All Available",
            width=140,
            height=28,
            command=self._select_all_available,
        ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            toolbar,
            text="Deselect All",
            width=110,
            height=28,
            fg_color=("gray75", "gray30"),
            text_color=("gray10", "gray90"),
            hover_color=("gray65", "gray40"),
            command=self._deselect_all,
        ).pack(side="left")

        # Scrollable list of items
        scroll = ctk.CTkScrollableFrame(self, label_text="Playlist Entries")
        scroll.grid(row=2, column=0, sticky="nsew", padx=16, pady=6)
        scroll.grid_columnconfigure(0, weight=1)

        for row_idx, item in enumerate(metadata.playlist_items):
            is_checked = item.available and (item.index in initial_set)
            var = tk.BooleanVar(value=is_checked)
            self._checkbox_vars[item.index] = var

            dur_str = ""
            if item.duration_seconds:
                mins, secs = divmod(int(item.duration_seconds), 60)
                dur_str = f" ({mins}:{secs:02d})"

            status_suffix = "" if item.available else " [Unavailable / Private]"
            label_text = f"#{item.index:02d}  {item.title}{dur_str}{status_suffix}"

            cb = ctk.CTkCheckBox(
                scroll,
                text=label_text,
                variable=var,
                state="normal" if item.available else "disabled",
            )
            cb.grid(row=row_idx, column=0, sticky="w", padx=8, pady=4)

        # Footer actions
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=3, column=0, sticky="ew", padx=16, pady=(8, 14))

        ctk.CTkButton(
            footer,
            text="Cancel",
            width=100,
            fg_color=("gray75", "gray30"),
            text_color=("gray10", "gray90"),
            command=self.destroy,
        ).pack(side="right", padx=(8, 0))

        ctk.CTkButton(
            footer,
            text="Use Selected Items",
            width=160,
            command=self._confirm_selected,
        ).pack(side="right", padx=(8, 0))

        ctk.CTkButton(
            footer,
            text="Download Entire Playlist",
            width=170,
            fg_color=("#2b7de9", "#1f6aa5"),
            command=self._confirm_full_playlist,
        ).pack(side="left")

    def _select_all_available(self) -> None:
        for item in self._metadata.playlist_items:
            if item.available and item.index in self._checkbox_vars:
                self._checkbox_vars[item.index].set(True)

    def _deselect_all(self) -> None:
        for var in self._checkbox_vars.values():
            var.set(False)

    def _confirm_selected(self) -> None:
        chosen = sorted(idx for idx, var in self._checkbox_vars.items() if var.get())
        if not chosen:
            show_warning_dialog(
                self,
                "No Items Selected",
                "Select at least one available playlist item, or choose 'Download Entire Playlist'.",
            )
            return
        if self._on_confirm:
            self._on_confirm(PlaylistMode.SELECTED_ITEMS, chosen)
        self.destroy()

    def _confirm_full_playlist(self) -> None:
        all_avail = [item.index for item in self._metadata.playlist_items if item.available]
        if self._on_confirm:
            self._on_confirm(PlaylistMode.FULL_PLAYLIST, all_avail)
        self.destroy()


class DiagnosticsDialog(ctk.CTkToplevel):
    """Modal window showing runtime dependency status, paths, and FFmpeg guidance."""

    def __init__(
        self,
        parent: ctk.CTk | ctk.CTkBaseClass,
        status: DependencyStatus,
        app_data_dir: str,
        log_file_path: str,
        on_recheck: Callable[[], DependencyStatus] | None = None,
        on_open_logs: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.title("System & Dependency Diagnostics")
        self.geometry("660x500")
        self.minsize(560, 420)
        self.transient(parent.winfo_toplevel())
        self.grab_set()

        self._on_recheck = on_recheck
        self._on_open_logs = on_open_logs
        self._app_data_dir = app_data_dir
        self._log_file_path = log_file_path

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        title_lbl = ctk.CTkLabel(
            self,
            text="Runtime & Media Dependency Diagnostics",
            font=ctk.CTkFont(size=16, weight="bold"),
            anchor="w",
        )
        title_lbl.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 6))

        self._textbox = ctk.CTkTextbox(self, wrap="word", font=ctk.CTkFont(family="Consolas", size=12))
        self._textbox.grid(row=1, column=0, sticky="nsew", padx=16, pady=6)

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=2, column=0, sticky="ew", padx=16, pady=(6, 14))

        if self._on_open_logs:
            ctk.CTkButton(
                footer,
                text="Open Logs Folder",
                width=140,
                command=self._on_open_logs,
            ).pack(side="left", padx=(0, 8))

        if self._on_recheck:
            ctk.CTkButton(
                footer,
                text="Re-check Dependencies",
                width=160,
                command=self._handle_recheck,
            ).pack(side="left")

        ctk.CTkButton(
            footer,
            text="Close",
            width=100,
            command=self.destroy,
        ).pack(side="right")

        self._render_status(status)

    def _handle_recheck(self) -> None:
        if self._on_recheck:
            updated = self._on_recheck()
            self._render_status(updated)

    def _render_status(self, status: DependencyStatus) -> None:
        lines = [
            "=== Runtime Environment ===",
            f"Python Version      : {status.python_version}",
            f"yt-dlp Available    : {'Yes' if status.yt_dlp_available else 'NO'}",
            f"yt-dlp Version      : {status.yt_dlp_version or 'Not found'}",
            "",
            "=== Media Processing (FFmpeg / FFprobe) ===",
            f"FFmpeg Available    : {'Yes' if status.ffmpeg_available else 'NO'}",
            f"FFmpeg Path         : {status.ffmpeg_path or 'Not found'}",
            f"FFmpeg Version      : {status.ffmpeg_version or 'N/A'}",
            f"FFprobe Available   : {'Yes' if status.ffprobe_available else 'NO (Optional metadata probe)'}",
            f"FFprobe Path        : {status.ffprobe_path or 'Not found'}",
            f"FFprobe Version     : {status.ffprobe_version or 'N/A'}",
            "",
            "=== Application Storage Paths ===",
            f"App Data Directory  : {self._app_data_dir}",
            f"Active Log File     : {self._log_file_path}",
        ]
        if not status.ffmpeg_available:
            lines.extend(
                [
                    "",
                    "=== FFmpeg Guidance ===",
                    FFmpegService.get_missing_ffmpeg_explanation(),
                ]
            )
        elif not status.ffprobe_available:
            lines.extend(
                [
                    "",
                    "=== Note on FFprobe ===",
                    "FFmpeg is installed and ready for stream merging and audio conversion.",
                    "Installing ffprobe.exe alongside ffmpeg.exe is recommended for advanced metadata probing.",
                ]
            )

        self._textbox.configure(state="normal")
        self._textbox.delete("1.0", "end")
        self._textbox.insert("1.0", "\n".join(lines))
        self._textbox.configure(state="disabled")


class CaptionSelectionDialog(ctk.CTkToplevel):
    """Modal dialog allowing users to search, filter, and choose caption/subtitle tracks."""

    def __init__(
        self,
        parent: ctk.CTk | ctk.CTkBaseClass,
        tracks: list[CaptionTrackInfo],
        initial_selected_codes: list[str],
        on_confirm: Callable[[list[str]], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._tracks = list(tracks)
        self._on_confirm = on_confirm
        self._selected_codes: set[str] = set(initial_selected_codes)
        self._current_category: str = "all"  # "all", "manual", "auto"
        self._search_text: str = ""
        self._checkbox_vars: dict[str, tk.BooleanVar] = {}

        self.title("Choose Captions & Subtitles")
        self.geometry("660x540")
        self.minsize(540, 420)
        self.transient(parent.winfo_toplevel())
        try:
            self.grab_set()
        except Exception:
            pass

        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # Header
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 4))
        header.grid_columnconfigure(0, weight=1)

        title_lbl = ctk.CTkLabel(
            header,
            text="Choose Captions & Subtitles",
            font=ctk.CTkFont(size=15, weight="bold"),
            anchor="w",
        )
        title_lbl.grid(row=0, column=0, sticky="w")

        manual_cnt = sum(1 for t in self._tracks if t.source == CaptionSource.MANUAL)
        auto_cnt = sum(1 for t in self._tracks if t.source == CaptionSource.AUTOMATIC)
        sub_lbl = ctk.CTkLabel(
            header,
            text=f"{len(self._tracks)} track(s) reported by yt-dlp ({manual_cnt} manual, {auto_cnt} automatic).",
            font=ctk.CTkFont(size=11),
            text_color=("gray35", "gray70"),
            anchor="w",
        )
        sub_lbl.grid(row=1, column=0, sticky="w", pady=(2, 0))

        # Filter bar: Search input + Category segmented button + Select All / Clear
        filter_bar = ctk.CTkFrame(self, fg_color="transparent")
        filter_bar.grid(row=1, column=0, sticky="ew", padx=16, pady=(4, 6))
        filter_bar.grid_columnconfigure(0, weight=1)

        self._search_entry = ctk.CTkEntry(
            filter_bar,
            placeholder_text="Search language name or code (e.g. English, es)...",
            height=28,
            font=ctk.CTkFont(size=11),
        )
        self._search_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self._search_entry.bind("<KeyRelease>", self._on_search_key_released)

        cat_labels = [f"All ({len(self._tracks)})", f"Manual ({manual_cnt})", f"Auto ({auto_cnt})"]
        self._category_segmented = ctk.CTkSegmentedButton(
            filter_bar,
            values=cat_labels,
            font=ctk.CTkFont(size=11),
            height=28,
            command=self._on_category_changed,
        )
        self._category_segmented.set(cat_labels[0])
        self._category_segmented.grid(row=0, column=1, padx=(0, 8))

        btn_box = ctk.CTkFrame(filter_bar, fg_color="transparent")
        btn_box.grid(row=0, column=2, sticky="e")

        ctk.CTkButton(
            btn_box,
            text="Select All",
            width=70,
            height=28,
            font=ctk.CTkFont(size=11),
            command=self._select_all_visible,
        ).pack(side="left", padx=(0, 4))

        ctk.CTkButton(
            btn_box,
            text="Clear",
            width=54,
            height=28,
            font=ctk.CTkFont(size=11),
            fg_color=("gray75", "gray30"),
            text_color=("gray10", "gray90"),
            command=self._deselect_all,
        ).pack(side="left")

        # Scrollable list
        self._scroll = ctk.CTkScrollableFrame(self, label_text="Available Caption Tracks")
        self._scroll.grid(row=2, column=0, sticky="nsew", padx=16, pady=4)
        self._scroll.grid_columnconfigure(0, weight=1)

        # Footer actions
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=3, column=0, sticky="ew", padx=16, pady=(8, 14))
        footer.grid_columnconfigure(0, weight=1)

        self._summary_lbl = ctk.CTkLabel(
            footer,
            text="",
            font=ctk.CTkFont(size=11, weight="bold"),
            anchor="w",
        )
        self._summary_lbl.grid(row=0, column=0, sticky="w")

        btn_footer = ctk.CTkFrame(footer, fg_color="transparent")
        btn_footer.grid(row=0, column=1, sticky="e")

        ctk.CTkButton(
            btn_footer,
            text="Cancel",
            width=88,
            height=30,
            fg_color=("gray75", "gray30"),
            text_color=("gray10", "gray90"),
            command=self._on_cancel,
        ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            btn_footer,
            text="Confirm Selection",
            width=140,
            height=30,
            font=ctk.CTkFont(weight="bold"),
            command=self._confirm_selected,
        ).pack(side="left")

        self._render_tracks()

    def _get_filtered_tracks(self) -> list[CaptionTrackInfo]:
        filtered: list[CaptionTrackInfo] = []
        query = self._search_text.strip().lower()

        for t in self._tracks:
            # Category filter
            if self._current_category == "manual" and t.source != CaptionSource.MANUAL:
                continue
            if self._current_category == "auto" and t.source != CaptionSource.AUTOMATIC:
                continue

            # Search filter
            if query:
                name_match = query in t.language_name.lower()
                code_match = query in t.language_code.lower()
                if not (name_match or code_match):
                    continue

            filtered.append(t)
        return filtered

    def _on_category_changed(self, choice: str) -> None:
        if "Manual" in choice:
            self._current_category = "manual"
        elif "Auto" in choice:
            self._current_category = "auto"
        else:
            self._current_category = "all"
        self._render_tracks()

    def _on_search_key_released(self, _event: object) -> None:
        text = self._search_entry.get()
        if text != self._search_text:
            self._search_text = text
            self._render_tracks()

    def _on_checkbox_toggled(self, code: str, is_checked: bool) -> None:
        if is_checked:
            self._selected_codes.add(code)
        else:
            self._selected_codes.discard(code)
        self._update_summary()

    def _render_tracks(self) -> None:
        for child in self._scroll.winfo_children():
            child.destroy()
        self._checkbox_vars.clear()

        visible = self._get_filtered_tracks()

        if not self._tracks:
            empty_lbl = ctk.CTkLabel(
                self._scroll,
                text="No caption tracks available for this video.",
                font=ctk.CTkFont(size=12),
                text_color=("gray45", "gray60"),
                justify="center",
            )
            empty_lbl.grid(row=0, column=0, pady=40)
            self._update_summary()
            return

        if not visible:
            empty_msg = (
                f"No matching tracks for '{self._search_text}'."
                if self._search_text
                else f"No {self._current_category} tracks found."
            )
            empty_lbl = ctk.CTkLabel(
                self._scroll,
                text=empty_msg,
                font=ctk.CTkFont(size=12),
                text_color=("gray45", "gray60"),
                justify="center",
            )
            empty_lbl.grid(row=0, column=0, pady=40)
            self._update_summary()
            return

        for idx, track in enumerate(visible):
            row_frame = ctk.CTkFrame(self._scroll, fg_color="transparent")
            row_frame.grid(row=idx, column=0, sticky="ew", padx=6, pady=3)
            row_frame.grid_columnconfigure(0, weight=1)

            is_selected = track.language_code in self._selected_codes
            cb_var = tk.BooleanVar(value=is_selected)
            self._checkbox_vars[track.language_code] = cb_var

            display_name = f"{track.language_name} ({track.language_code})"
            cb = ctk.CTkCheckBox(
                row_frame,
                text=display_name,
                variable=cb_var,
                font=ctk.CTkFont(size=12),
                checkbox_width=18,
                checkbox_height=18,
                command=lambda c=track.language_code, v=cb_var: self._on_checkbox_toggled(c, v.get()),
            )
            cb.grid(row=0, column=0, sticky="w")

            is_manual = track.source == CaptionSource.MANUAL
            badge_text = "Manual" if is_manual else "Auto"
            badge_fg = ("#0284c7", "#0369a1") if is_manual else ("#64748b", "#475569")

            badge = ctk.CTkLabel(
                row_frame,
                text=badge_text,
                font=ctk.CTkFont(size=10, weight="bold"),
                text_color="white",
                corner_radius=4,
                width=54,
                height=20,
                fg_color=badge_fg,
            )
            badge.grid(row=0, column=1, sticky="e", padx=(8, 4))

        self._update_summary()

    def _update_summary(self) -> None:
        sel_count = len(self._selected_codes)
        # Order codes matching tracks
        ordered_codes = [t.language_code for t in self._tracks if t.language_code in self._selected_codes]
        sel_str = ", ".join(ordered_codes) if ordered_codes else "None"
        self._summary_lbl.configure(text=f"Selected: {sel_count} track(s) ({sel_str})")

    def _select_all_visible(self) -> None:
        visible = self._get_filtered_tracks()
        for t in visible:
            self._selected_codes.add(t.language_code)
            var = self._checkbox_vars.get(t.language_code)
            if var is not None:
                var.set(True)
        self._update_summary()

    def _deselect_all(self) -> None:
        self._selected_codes.clear()
        for var in self._checkbox_vars.values():
            var.set(False)
        self._update_summary()

    def _confirm_selected(self) -> None:
        ordered_codes = [t.language_code for t in self._tracks if t.language_code in self._selected_codes]
        if self._on_confirm is not None:
            self._on_confirm(ordered_codes)
        self.destroy()

    def _on_cancel(self) -> None:
        self.destroy()
