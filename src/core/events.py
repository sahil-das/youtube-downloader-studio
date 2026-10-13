"""Thread-safe event bus for worker-to-UI communication."""

from __future__ import annotations

import queue
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class EventType(str, Enum):
    """Types of events emitted by background workers and services."""

    JOB_ADDED = "job_added"
    JOB_QUEUED = "job_queued"
    JOB_FETCHING_METADATA = "job_fetching_metadata"
    JOB_STARTING = "job_starting"
    JOB_PROGRESS = "job_progress"
    JOB_PROCESSING = "job_processing"
    JOB_WAITING_RETRY = "job_waiting_retry"
    JOB_PAUSED = "job_paused"
    JOB_COMPLETED = "job_completed"
    JOB_FAILED = "job_failed"
    JOB_CANCELLED = "job_cancelled"
    JOB_REMOVED = "job_removed"
    METADATA_FETCHED = "metadata_fetched"
    METADATA_FAILED = "metadata_failed"
    QUEUE_UPDATED = "queue_updated"
    HISTORY_UPDATED = "history_updated"
    DEPENDENCY_STATUS_UPDATED = "dependency_status_updated"
    STATUS_MESSAGE = "status_message"


@dataclass(slots=True)
class AppEvent:
    """Immutable-style event payload dispatched from background threads to the UI thread."""

    event_type: EventType
    job_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class EventBus:
    """Thread-safe FIFO event queue drained by the Tkinter main loop."""

    def __init__(self) -> None:
        self._queue: queue.Queue[AppEvent] = queue.Queue()

    def publish(
        self,
        event_type: EventType,
        job_id: str | None = None,
        **data: Any,
    ) -> None:
        """Publish an event from any thread without blocking."""
        event = AppEvent(event_type=event_type, job_id=job_id, data=data)
        self._queue.put_nowait(event)

    def publish_event(self, event: AppEvent) -> None:
        """Enqueue a pre-constructed AppEvent."""
        self._queue.put_nowait(event)

    def poll_events(self, max_items: int = 100) -> list[AppEvent]:
        """Drain up to max_items pending events on the UI thread."""
        events: list[AppEvent] = []
        for _ in range(max_items):
            try:
                events.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return events

    def clear(self) -> None:
        """Discard all pending events."""
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
