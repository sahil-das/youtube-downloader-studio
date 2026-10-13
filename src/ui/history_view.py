"""SQLite Download History view with search, status filtering, date sorting, and retry."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import customtkinter as ctk  # type: ignore[import-untyped]

from src.core.exceptions import AppError
from src.core.models import HistoryEntry, JobStatus
from src.database.repositories import HistoryRepository
from src.ui.components import STATUS_BADGE_COLORS, STATUS_LABELS, format_bytes
from src.ui.dialogs import ask_confirmation_dialog, show_error_dialog
from src.utils.platform_utils import open_media_file, reveal_in_file_manager


class HistoryView(ctk.CTkFrame):
    """Displays persistent download history with search, status filtering, sorting, and actions."""

    def __init__(
        self,
        master: ctk.CTkBaseClass,
        history_repo: HistoryRepository,
        on_retry_entry: Callable[[HistoryEntry], None],
    ) -> None:
        super().__init__(master, corner_radius=8)
        self._history_repo = history_repo
        self._on_retry_entry = on_retry_entry

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # Header
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 4))
        header.grid_columnconfigure(0, weight=1)

        self._title_lbl = ctk.CTkLabel(
            header,
            text="Download History",
            font=ctk.CTkFont(size=16, weight="bold"),
            anchor="w",
        )
        self._title_lbl.grid(row=0, column=0, sticky="w")

        ctk.CTkButton(
            header,
            text="Clear All History",
            width=130,
            height=28,
            fg_color=("#dc2626", "#991b1b"),
            hover_color=("#b91c1c", "#7f1d1d"),
            command=self._handle_clear_all,
        ).grid(row=0, column=1, sticky="e")

        # Filter & Search bar
        filter_bar = ctk.CTkFrame(self, fg_color="transparent")
        filter_bar.grid(row=1, column=0, sticky="ew", padx=14, pady=(2, 8))
        filter_bar.grid_columnconfigure(0, weight=1)

        self._search_entry = ctk.CTkEntry(
            filter_bar,
            placeholder_text="Search history by video title or URL...",
            height=32,
        )
        self._search_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self._search_entry.bind("<Return>", lambda _e: self.refresh())
        self._search_entry.bind("<KeyRelease>", lambda _e: self.refresh())

        self._status_filter = ctk.CTkOptionMenu(
            filter_bar,
            values=["All Statuses", "Completed", "Paused", "Failed", "Cancelled"],
            width=140,
            height=32,
            command=lambda _v: self.refresh(),
        )
        self._status_filter.set("All Statuses")
        self._status_filter.grid(row=0, column=1, padx=(0, 8))

        self._sort_order = ctk.CTkOptionMenu(
            filter_bar,
            values=["Newest First", "Oldest First"],
            width=135,
            height=32,
            command=lambda _v: self.refresh(),
        )
        self._sort_order.set("Newest First")
        self._sort_order.grid(row=0, column=2, padx=(0, 8))

        ctk.CTkButton(
            filter_bar,
            text="Refresh",
            width=86,
            height=32,
            command=self.refresh,
        ).grid(row=0, column=3)

        # Scrollable list of history cards
        self._scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self._scroll.grid(row=2, column=0, sticky="nsew", padx=10, pady=(0, 10))
        self._scroll.grid_columnconfigure(0, weight=1)

    def refresh(self) -> None:
        """Query SQLite history with current search/filter settings and render rows."""
        for child in self._scroll.winfo_children():
            child.destroy()

        query = self._search_entry.get().strip()
        status_label = self._status_filter.get()
        status_map = {
            "All Statuses": None,
            "Completed": JobStatus.COMPLETED,
            "Paused": JobStatus.PAUSED,
            "Failed": JobStatus.FAILED,
            "Cancelled": JobStatus.CANCELLED,
        }
        status_filter = status_map.get(status_label)
        sort_desc = self._sort_order.get() == "Newest First"

        try:
            entries = self._history_repo.list_entries(
                search_query=query,
                status_filter=status_filter,
                sort_descending=sort_desc,
            )
        except AppError as exc:
            show_error_dialog(self, "History Database Error", exc.user_message, exc.remedy)
            return

        self._title_lbl.configure(text=f"Download History ({len(entries)} records)")

        if not entries:
            empty_msg = (
                "No matching history entries found."
                if (query or status_filter)
                else "No download history yet. Completed, failed, and cancelled jobs will appear here."
            )
            ctk.CTkLabel(
                self._scroll,
                text=empty_msg,
                font=ctk.CTkFont(size=12),
                text_color=("gray45", "gray60"),
            ).grid(row=0, column=0, pady=40)
            return

        for idx, entry in enumerate(entries):
            self._build_entry_row(idx, entry)

    def _build_entry_row(self, row_idx: int, entry: HistoryEntry) -> None:
        card = ctk.CTkFrame(
            self._scroll,
            corner_radius=8,
            border_width=1,
            border_color=("gray80", "gray28"),
        )
        card.grid(row=row_idx, column=0, sticky="ew", padx=4, pady=4)
        card.grid_columnconfigure(0, weight=1)

        top = ctk.CTkFrame(card, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 2))
        top.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            top,
            text=entry.title or entry.url,
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=(0, 8))

        badge_text = STATUS_LABELS.get(entry.status, entry.status.value.title())
        badge_color = STATUS_BADGE_COLORS.get(entry.status, ("#64748b", "#475569"))
        ctk.CTkLabel(
            top,
            text=badge_text,
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color="white",
            fg_color=badge_color,
            corner_radius=6,
            width=105,
            height=22,
        ).grid(row=0, column=1, sticky="e")

        timestamp_raw = entry.completed_at or entry.created_at or ""
        timestamp_display = timestamp_raw[:19].replace("T", " ")
        size_display = format_bytes(entry.file_size_bytes)

        details_text = (
            f"{entry.requested_quality}  •  Size: {size_display}  •  "
            f"Date: {timestamp_display}  •  URL: {entry.url}"
        )
        ctk.CTkLabel(
            card,
            text=details_text,
            font=ctk.CTkFont(size=11),
            text_color=("gray35", "gray70"),
            anchor="w",
        ).grid(row=1, column=0, sticky="ew", padx=12, pady=1)

        bottom = ctk.CTkFrame(card, fg_color="transparent")
        bottom.grid(row=2, column=0, sticky="ew", padx=12, pady=(2, 8))
        bottom.grid_columnconfigure(0, weight=1)

        path_or_err = entry.output_path or entry.destination_dir
        if entry.status == JobStatus.FAILED and entry.error_message:
            first_err = entry.error_message.splitlines()[0]
            path_or_err = f"Error: {first_err}"

        ctk.CTkLabel(
            bottom,
            text=str(path_or_err),
            font=ctk.CTkFont(size=11),
            text_color=("#b91c1c", "#f87171")
            if entry.status == JobStatus.FAILED
            else ("gray40", "gray65"),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=(0, 8))

        btns = ctk.CTkFrame(bottom, fg_color="transparent")
        btns.grid(row=0, column=1, sticky="e")

        if entry.output_path and Path(entry.output_path).is_file():
            ctk.CTkButton(
                btns,
                text="Open File",
                width=82,
                height=26,
                font=ctk.CTkFont(size=11),
                fg_color=("#16a34a", "#15803d"),
                hover_color=("#15803d", "#166534"),
                command=lambda p=entry.output_path: self._handle_open_file(str(p)),
            ).pack(side="left", padx=3)

        folder_target = entry.output_path or entry.destination_dir
        ctk.CTkButton(
            btns,
            text="Open Folder",
            width=90,
            height=26,
            font=ctk.CTkFont(size=11),
            command=lambda p=folder_target: self._handle_open_folder(str(p)),
        ).pack(side="left", padx=3)

        retry_label = "Retry" if entry.status in {JobStatus.FAILED, JobStatus.CANCELLED} else "Re-download"
        ctk.CTkButton(
            btns,
            text=retry_label,
            width=88,
            height=26,
            font=ctk.CTkFont(size=11),
            command=lambda e=entry: self._on_retry_entry(e),
        ).pack(side="left", padx=3)

        ctk.CTkButton(
            btns,
            text="Remove",
            width=72,
            height=26,
            font=ctk.CTkFont(size=11),
            fg_color=("gray75", "gray30"),
            text_color=("gray10", "gray90"),
            command=lambda jid=entry.job_id: self._handle_delete_entry(jid),
        ).pack(side="left", padx=3)

    def _handle_open_file(self, file_path: str) -> None:
        try:
            open_media_file(file_path)
        except AppError as exc:
            show_error_dialog(self, "Cannot Open File", exc.user_message, exc.remedy)

    def _handle_open_folder(self, path_str: str) -> None:
        try:
            reveal_in_file_manager(path_str)
        except AppError as exc:
            show_error_dialog(self, "Cannot Open Folder", exc.user_message, exc.remedy)

    def _handle_delete_entry(self, job_id: str) -> None:
        try:
            self._history_repo.delete_entry(job_id)
            self.refresh()
        except AppError as exc:
            show_error_dialog(self, "Delete Error", exc.user_message, exc.remedy)

    def _handle_clear_all(self) -> None:
        confirmed = ask_confirmation_dialog(
            self,
            "Clear Download History",
            "Are you sure you want to remove all records from your download history?\n\n"
            "(Your downloaded video and audio files on disk will NOT be deleted.)",
        )
        if not confirmed:
            return
        try:
            self._history_repo.clear_all()
            self.refresh()
        except AppError as exc:
            show_error_dialog(self, "Clear History Error", exc.user_message, exc.remedy)
