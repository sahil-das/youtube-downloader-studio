"""Dedicated Full Queue pop-up window (CTkToplevel) with category filter tabs and bulk actions."""

from __future__ import annotations

import logging
from typing import Callable

import customtkinter as ctk  # type: ignore[import-untyped]

from src.core.models import DownloadJob
from src.services.queue_service import QueueService
from src.ui.queue_view import QueueView

logger = logging.getLogger(__name__)


class FullQueueWindow(ctk.CTkToplevel):
    """Separate pop-up window displaying the complete download queue.

    Provides status filter tabs (All, Active, Paused, Queued, Completed, Failed, Cancelled)
    with live category counts, bulk queue actions, and detailed per-job cards.
    Closing this window NEVER stops or interrupts active downloads.
    """

    def __init__(
        self,
        master: ctk.CTkBaseClass,
        queue_service: QueueService,
        on_closed: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(master)
        self._queue_service = queue_service
        self._on_closed = on_closed

        self.title("Download Queue — YouTube Downloader Studio")
        self.geometry("900x580")
        self.minsize(680, 420)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # Embed complete QueueView
        self._queue_view = QueueView(self, queue_service=self._queue_service)
        self._queue_view.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(50, self._initial_focus)

    def _initial_focus(self) -> None:
        try:
            self.lift()
            self.focus()
            self._queue_view.refresh_all()
        except Exception:
            pass

    @property
    def queue_view(self) -> QueueView:
        """Access the underlying QueueView component."""
        return self._queue_view

    def refresh_all(self) -> None:
        """Synchronize all cards and filter counts with current QueueService state."""
        try:
            if self.winfo_exists():
                self._queue_view.refresh_all()
        except Exception as exc:
            logger.debug("Failed refreshing FullQueueWindow: %s", exc)

    def refresh_job(self, job: DownloadJob) -> None:
        """Fast-path update for a single job card."""
        try:
            if self.winfo_exists():
                self._queue_view.refresh_job(job)
        except Exception as exc:
            logger.debug("Failed refreshing job in FullQueueWindow: %s", exc)

    def _on_close(self) -> None:
        """Close the pop-up window without interrupting any downloads."""
        logger.debug("Closing FullQueueWindow without interrupting downloads.")
        try:
            self.destroy()
        finally:
            if self._on_closed is not None:
                try:
                    self._on_closed()
                except Exception as exc:
                    logger.debug("Error in FullQueueWindow on_closed callback: %s", exc)
