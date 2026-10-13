"""Modal dialogs for errors, confirmations, overwrite prompts, playlist selection, and diagnostics."""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox
from typing import Callable

import customtkinter as ctk  # type: ignore[import-untyped]

from src.core.models import (
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
