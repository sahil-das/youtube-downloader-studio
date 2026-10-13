"""Download Queue view and individual QueueItemCard widgets."""

from __future__ import annotations

from typing import Callable

import customtkinter as ctk  # type: ignore[import-untyped]

from src.core.exceptions import AppError
from src.core.models import DownloadJob, JobStatus
from src.services.queue_service import QueueService
from src.ui.components import (
    STATUS_BADGE_COLORS,
    STATUS_LABELS,
    format_bytes,
    format_eta,
    format_speed,
)
from src.ui.dialogs import show_error_dialog
from src.utils.platform_utils import open_media_file, reveal_in_file_manager


class QueueItemCard(ctk.CTkFrame):
    """Widget representing a single DownloadJob in the active download queue."""

    def __init__(
        self,
        master: ctk.CTkBaseClass,
        job: DownloadJob,
        on_start: Callable[[str], None],
        on_pause: Callable[[str], None],
        on_resume: Callable[[str], None],
        on_cancel: Callable[[str], None],
        on_retry: Callable[[str], None],
        on_remove: Callable[[str], None],
    ) -> None:
        super().__init__(master, corner_radius=8, border_width=1, border_color=("gray80", "gray28"))
        self._job_id = job.job_id
        self._on_start = on_start
        self._on_pause = on_pause
        self._on_resume = on_resume
        self._on_cancel = on_cancel
        self._on_retry = on_retry
        self._on_remove = on_remove
        self._last_status: JobStatus | None = None

        self.grid_columnconfigure(0, weight=1)

        # Top row: Title + Status Badge
        top_row = ctk.CTkFrame(self, fg_color="transparent")
        top_row.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 2))
        top_row.grid_columnconfigure(0, weight=1)

        self._title_lbl = ctk.CTkLabel(
            top_row,
            text=job.display_title,
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w",
        )
        self._title_lbl.grid(row=0, column=0, sticky="ew", padx=(0, 8))

        self._status_badge = ctk.CTkLabel(
            top_row,
            text="Queued",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color="white",
            corner_radius=6,
            width=110,
            height=22,
        )
        self._status_badge.grid(row=0, column=1, sticky="e")

        # Second row: Mode/Quality + URL
        self._subtitle_lbl = ctk.CTkLabel(
            self,
            text="",
            font=ctk.CTkFont(size=11),
            text_color=("gray35", "gray70"),
            anchor="w",
        )
        self._subtitle_lbl.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 4))

        # Progress bar
        self._progress_bar = ctk.CTkProgressBar(self, height=10)
        self._progress_bar.grid(row=2, column=0, sticky="ew", padx=12, pady=(2, 4))
        self._progress_bar.set(0.0)

        # Bottom row: Metrics + Action Buttons
        bottom_row = ctk.CTkFrame(self, fg_color="transparent")
        bottom_row.grid(row=3, column=0, sticky="ew", padx=12, pady=(2, 8))
        bottom_row.grid_columnconfigure(0, weight=1)

        self._metrics_lbl = ctk.CTkLabel(
            bottom_row,
            text="",
            font=ctk.CTkFont(size=11),
            text_color=("gray30", "gray75"),
            anchor="w",
        )
        self._metrics_lbl.grid(row=0, column=0, sticky="ew")

        self._btn_frame = ctk.CTkFrame(bottom_row, fg_color="transparent")
        self._btn_frame.grid(row=0, column=1, sticky="e")

        self.update_from_job(job)

    def update_from_job(self, job: DownloadJob) -> None:
        """Refresh labels, progress bar, and contextual buttons from a DownloadJob state."""
        pl_prefix = ""
        if job.playlist_index and job.playlist_total:
            pl_prefix = f"[Item {job.playlist_index}/{job.playlist_total}] "

        src_label = str(job.local_source_path) if job.local_source_path else job.url
        self._title_lbl.configure(text=f"{pl_prefix}{job.display_title}")
        self._subtitle_lbl.configure(
            text=f"{job.requested_format_summary}  •  {src_label}  •  Dest: {job.destination_dir}"
        )

        badge_text = STATUS_LABELS.get(job.status, job.status.value.title())
        badge_color = STATUS_BADGE_COLORS.get(job.status, ("#64748b", "#475569"))
        self._status_badge.configure(text=badge_text, fg_color=badge_color)

        pct = max(0.0, min(100.0, float(job.progress_percent or 0.0)))
        self._progress_bar.set(pct / 100.0)

        if job.status == JobStatus.FETCHING_METADATA:
            stage_txt = job.stage_detail or "Retrieving media metadata..."
            self._metrics_lbl.configure(
                text=stage_txt,
                text_color=("gray25", "gray80"),
            )
        elif job.status == JobStatus.DOWNLOADING:
            if job.stage_detail and job.total_bytes == 0:
                self._metrics_lbl.configure(
                    text=f"{pct:.1f}%   •   {job.stage_detail}",
                    text_color=("gray25", "gray80"),
                )
            else:
                dl_str = format_bytes(job.downloaded_bytes)
                tot_str = format_bytes(job.total_bytes) if job.total_bytes else "Unknown size"
                spd_str = format_speed(job.speed_bps)
                eta_str = format_eta(job.eta_seconds)
                stage_prefix = f"{job.stage_detail}  •  " if job.stage_detail else ""
                self._metrics_lbl.configure(
                    text=f"{stage_prefix}{pct:.1f}%  ({dl_str} / {tot_str})   •   Speed: {spd_str}   •   ETA: {eta_str}",
                    text_color=("gray25", "gray80"),
                )
        elif job.status == JobStatus.PAUSED:
            dl_str = format_bytes(job.downloaded_bytes)
            self._metrics_lbl.configure(
                text=f"Paused at {pct:.1f}% ({dl_str}) — click Resume to continue transfer.",
                text_color=("#b45309", "#fbbf24"),
            )
        elif job.status == JobStatus.PROCESSING:
            stage_txt = (
                job.stage_detail
                or "100% downloaded — running FFmpeg stream merge / audio conversion..."
            )
            self._metrics_lbl.configure(
                text=stage_txt,
                text_color=("#6d28d9", "#c4b5fd"),
            )
        elif job.status == JobStatus.COMPLETED:
            size_str = format_bytes(job.file_size_bytes)
            out_name = job.output_path.name if job.output_path else "Saved"
            extra_subs = ""
            if len(job.exported_subtitle_paths) > 1:
                extra_subs = f" (+{len(job.exported_subtitle_paths) - 1} subtitle file(s))"
            self._metrics_lbl.configure(
                text=f"Completed ({size_str})  •  {out_name}{extra_subs}",
                text_color=("#15803d", "#4ade80"),
            )
        elif job.status == JobStatus.FAILED:
            err_line = (job.error_message or "Download failed.").splitlines()[0]
            self._metrics_lbl.configure(
                text=f"Error: {err_line}",
                text_color=("#b91c1c", "#f87171"),
            )
        elif job.status == JobStatus.CANCELLED:
            self._metrics_lbl.configure(
                text="Cancelled by user — click Retry to restart from the beginning.",
                text_color=("gray40", "gray65"),
            )
        else:
            self._metrics_lbl.configure(
                text=job.stage_detail or "Waiting in queue...",
                text_color=("gray40", "gray65"),
            )

        if self._last_status != job.status:
            self._last_status = job.status
            self._rebuild_action_buttons(job)

    def _rebuild_action_buttons(self, job: DownloadJob) -> None:
        for child in self._btn_frame.winfo_children():
            child.destroy()

        if job.status == JobStatus.QUEUED:
            ctk.CTkButton(
                self._btn_frame,
                text="Start",
                width=68,
                height=26,
                font=ctk.CTkFont(size=11),
                command=lambda: self._on_start(job.job_id),
            ).pack(side="left", padx=3)
            ctk.CTkButton(
                self._btn_frame,
                text="Cancel",
                width=68,
                height=26,
                font=ctk.CTkFont(size=11),
                fg_color=("#dc2626", "#991b1b"),
                hover_color=("#b91c1c", "#7f1d1d"),
                command=lambda: self._on_cancel(job.job_id),
            ).pack(side="left", padx=3)
            ctk.CTkButton(
                self._btn_frame,
                text="Remove",
                width=72,
                height=26,
                font=ctk.CTkFont(size=11),
                fg_color=("gray75", "gray30"),
                text_color=("gray10", "gray90"),
                command=lambda: self._on_remove(job.job_id),
            ).pack(side="left", padx=3)

        elif job.status.is_active:
            ctk.CTkButton(
                self._btn_frame,
                text="Pause",
                width=72,
                height=26,
                font=ctk.CTkFont(size=11),
                fg_color=("#d97706", "#b45309"),
                hover_color=("#b45309", "#92400e"),
                command=lambda: self._on_pause(job.job_id),
            ).pack(side="left", padx=3)
            ctk.CTkButton(
                self._btn_frame,
                text="Cancel",
                width=72,
                height=26,
                font=ctk.CTkFont(size=11),
                fg_color=("#dc2626", "#991b1b"),
                hover_color=("#b91c1c", "#7f1d1d"),
                command=lambda: self._on_cancel(job.job_id),
            ).pack(side="left", padx=3)

        elif job.status == JobStatus.PAUSED:
            ctk.CTkButton(
                self._btn_frame,
                text="Resume",
                width=76,
                height=26,
                font=ctk.CTkFont(size=11),
                fg_color=("#16a34a", "#15803d"),
                hover_color=("#15803d", "#166534"),
                command=lambda: self._on_resume(job.job_id),
            ).pack(side="left", padx=3)
            ctk.CTkButton(
                self._btn_frame,
                text="Cancel",
                width=68,
                height=26,
                font=ctk.CTkFont(size=11),
                fg_color=("#dc2626", "#991b1b"),
                hover_color=("#b91c1c", "#7f1d1d"),
                command=lambda: self._on_cancel(job.job_id),
            ).pack(side="left", padx=3)
            ctk.CTkButton(
                self._btn_frame,
                text="Remove",
                width=72,
                height=26,
                font=ctk.CTkFont(size=11),
                fg_color=("gray75", "gray30"),
                text_color=("gray10", "gray90"),
                command=lambda: self._on_remove(job.job_id),
            ).pack(side="left", padx=3)

        elif job.status in {JobStatus.FAILED, JobStatus.CANCELLED}:
            ctk.CTkButton(
                self._btn_frame,
                text="Retry",
                width=72,
                height=26,
                font=ctk.CTkFont(size=11),
                command=lambda: self._on_retry(job.job_id),
            ).pack(side="left", padx=3)
            ctk.CTkButton(
                self._btn_frame,
                text="Remove",
                width=72,
                height=26,
                font=ctk.CTkFont(size=11),
                fg_color=("gray75", "gray30"),
                text_color=("gray10", "gray90"),
                command=lambda: self._on_remove(job.job_id),
            ).pack(side="left", padx=3)

        elif job.status == JobStatus.COMPLETED:
            if job.output_path and job.output_path.is_file():
                ctk.CTkButton(
                    self._btn_frame,
                    text="Open File",
                    width=82,
                    height=26,
                    font=ctk.CTkFont(size=11),
                    fg_color=("#16a34a", "#15803d"),
                    hover_color=("#15803d", "#166534"),
                    command=lambda p=job.output_path: self._safe_open_file(p),
                ).pack(side="left", padx=3)
            ctk.CTkButton(
                self._btn_frame,
                text="Open Folder",
                width=92,
                height=26,
                font=ctk.CTkFont(size=11),
                command=lambda p=(job.output_path or job.destination_dir): self._safe_open_folder(p),
            ).pack(side="left", padx=3)
            ctk.CTkButton(
                self._btn_frame,
                text="Remove",
                width=72,
                height=26,
                font=ctk.CTkFont(size=11),
                fg_color=("gray75", "gray30"),
                text_color=("gray10", "gray90"),
                command=lambda: self._on_remove(job.job_id),
            ).pack(side="left", padx=3)

    def _safe_open_file(self, path: object) -> None:
        try:
            open_media_file(str(path))
        except AppError as exc:
            show_error_dialog(self, "Cannot Open Media File", exc.user_message, exc.remedy)

    def _safe_open_folder(self, path: object) -> None:
        try:
            reveal_in_file_manager(str(path))
        except AppError as exc:
            show_error_dialog(self, "Cannot Open Folder", exc.user_message, exc.remedy)


class QueueView(ctk.CTkFrame):
    """Scrollable multi-item download queue view with batch actions and status summary."""

    def __init__(
        self,
        master: ctk.CTkBaseClass,
        queue_service: QueueService,
    ) -> None:
        super().__init__(master, corner_radius=8)
        self._queue_service = queue_service
        self._cards: dict[str, QueueItemCard] = {}

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # Toolbar header
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))
        header.grid_columnconfigure(0, weight=1)

        self._summary_lbl = ctk.CTkLabel(
            header,
            text="Download Queue (0 items)",
            font=ctk.CTkFont(size=14, weight="bold"),
            anchor="w",
        )
        self._summary_lbl.grid(row=0, column=0, sticky="w")

        actions = ctk.CTkFrame(header, fg_color="transparent")
        actions.grid(row=0, column=1, sticky="e")

        self._start_all_btn = ctk.CTkButton(
            actions,
            text="Start Queued Jobs",
            width=136,
            height=28,
            command=self._handle_start_queue,
        )
        self._start_all_btn.pack(side="left", padx=(0, 8))

        self._clear_done_btn = ctk.CTkButton(
            actions,
            text="Clear Completed",
            width=126,
            height=28,
            fg_color=("gray75", "gray30"),
            text_color=("gray10", "gray90"),
            command=self._handle_clear_completed,
        )
        self._clear_done_btn.pack(side="left")

        # Scrollable container
        self._scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self._scroll.grid(row=1, column=0, sticky="nsew", padx=8, pady=(2, 8))
        self._scroll.grid_columnconfigure(0, weight=1)

        self._empty_label = ctk.CTkLabel(
            self._scroll,
            text=(
                "Your download queue is currently empty.\n"
                "Paste one or more YouTube URLs above and click 'Add to Queue' or 'Download Now'."
            ),
            font=ctk.CTkFont(size=12),
            text_color=("gray45", "gray60"),
            justify="center",
        )
        self._empty_label.grid(row=0, column=0, pady=36)

    def refresh_all(self) -> None:
        """Synchronize all QueueItemCard widgets with the current QueueService state."""
        jobs = self._queue_service.list_jobs()
        current_ids = {job.job_id for job in jobs}

        for stale_id in list(self._cards.keys()):
            if stale_id not in current_ids:
                card = self._cards.pop(stale_id)
                card.destroy()

        if not jobs:
            self._empty_label.grid(row=0, column=0, pady=36)
        else:
            self._empty_label.grid_forget()

        queued_cnt = 0
        active_cnt = 0
        paused_cnt = 0
        done_cnt = 0
        failed_cnt = 0

        for idx, job in enumerate(jobs):
            if job.status == JobStatus.QUEUED:
                queued_cnt += 1
            elif job.status.is_active:
                active_cnt += 1
            elif job.status == JobStatus.PAUSED:
                paused_cnt += 1
            elif job.status == JobStatus.COMPLETED:
                done_cnt += 1
            elif job.status == JobStatus.FAILED:
                failed_cnt += 1

            card = self._cards.get(job.job_id)
            if card is None:
                card = QueueItemCard(
                    self._scroll,
                    job=job,
                    on_start=self._handle_start_job,
                    on_pause=self._handle_pause_job,
                    on_resume=self._handle_resume_job,
                    on_cancel=self._handle_cancel_job,
                    on_retry=self._handle_retry_job,
                    on_remove=self._handle_remove_job,
                )
                self._cards[job.job_id] = card
            else:
                card.update_from_job(job)

            card.grid(row=idx, column=0, sticky="ew", padx=4, pady=4)

        self._summary_lbl.configure(
            text=(
                f"Download Queue ({len(jobs)})   •   "
                f"Active: {active_cnt}   •   Paused: {paused_cnt}   •   Queued: {queued_cnt}   •   "
                f"Completed: {done_cnt}   •   Failed: {failed_cnt}"
            )
        )

    def refresh_job(self, job: DownloadJob) -> None:
        """Fast-path update for a single job's progress without rebuilding all cards."""
        card = self._cards.get(job.job_id)
        if card is not None:
            card.update_from_job(job)
        else:
            self.refresh_all()

    def _handle_start_queue(self) -> None:
        self._queue_service.start_queue()
        self.refresh_all()

    def _handle_clear_completed(self) -> None:
        self._queue_service.clear_completed_jobs()
        self.refresh_all()

    def _handle_start_job(self, job_id: str) -> None:
        self._queue_service.start_single_job(job_id)
        self.refresh_all()

    def _handle_pause_job(self, job_id: str) -> None:
        try:
            self._queue_service.pause_job(job_id)
            self.refresh_all()
        except AppError as exc:
            show_error_dialog(self, "Cannot Pause Job", exc.user_message, exc.remedy)

    def _handle_resume_job(self, job_id: str) -> None:
        try:
            self._queue_service.resume_job(job_id, start_immediately=True)
            self.refresh_all()
        except AppError as exc:
            show_error_dialog(self, "Cannot Resume Job", exc.user_message, exc.remedy)

    def _handle_cancel_job(self, job_id: str) -> None:
        self._queue_service.cancel_job(job_id)
        self.refresh_all()

    def _handle_retry_job(self, job_id: str) -> None:
        try:
            self._queue_service.retry_job(job_id, start_immediately=True)
            self.refresh_all()
        except AppError as exc:
            show_error_dialog(self, "Cannot Retry Job", exc.user_message, exc.remedy)

    def _handle_remove_job(self, job_id: str) -> None:
        try:
            self._queue_service.remove_job(job_id)
            self.refresh_all()
        except AppError as exc:
            show_error_dialog(self, "Cannot Remove Job", exc.user_message, exc.remedy)
