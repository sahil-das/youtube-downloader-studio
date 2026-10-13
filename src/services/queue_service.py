"""Thread-safe download queue manager with bounded concurrency, pause/resume, and SQLite sync."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from src.core.events import EventBus, EventType
from src.core.exceptions import (
    AppError,
    DownloadCancelledError,
    DownloadPausedError,
    InvalidStateTransitionError,
    ValidationError,
)
from src.core.models import (
    AudioFormat,
    AudioQuality,
    DownloadJob,
    DownloadMode,
    HistoryEntry,
    JobStatus,
    OverwriteBehavior,
    PlaylistMode,
    VideoFormat,
    VideoQuality,
)
from src.core.validators import (
    validate_output_directory,
    validate_playlist_indices,
    validate_url,
)
from src.database.repositories import HistoryRepository
from src.services.download_service import DownloadService
from src.utils.logging_config import get_logger

logger = get_logger("queue_service")


class QueueService:
    """Manages queued, active, paused, and finished DownloadJobs with bounded background workers."""

    def __init__(
        self,
        download_service: DownloadService,
        history_repository: HistoryRepository | None = None,
        event_bus: EventBus | None = None,
        max_concurrent_downloads: int = 2,
        auto_start: bool = False,
    ) -> None:
        self._download_service = download_service
        self._history_repo = history_repository
        self._event_bus = event_bus
        self._max_concurrent = max(1, min(4, int(max_concurrent_downloads)))
        self._auto_start = auto_start

        self._lock = threading.RLock()
        self._jobs: dict[str, DownloadJob] = {}
        self._job_order: list[str] = []
        self._cancel_events: dict[str, threading.Event] = {}
        self._pause_events: dict[str, threading.Event] = {}
        self._tracked_files: dict[str, list[Path]] = {}
        self._active_threads: dict[str, threading.Thread] = {}
        self._processing_enabled = auto_start
        self._shutting_down = False

    @property
    def max_concurrent_downloads(self) -> int:
        """Return the maximum number of simultaneous active downloads."""
        with self._lock:
            return self._max_concurrent

    @property
    def active_worker_count(self) -> int:
        """Return the number of currently running worker threads."""
        with self._lock:
            return sum(1 for t in self._active_threads.values() if t.is_alive())

    def set_max_concurrent_downloads(self, limit: int) -> None:
        """Update concurrency limit and schedule queued jobs if capacity increased."""
        with self._lock:
            self._max_concurrent = max(1, min(4, int(limit)))
        if self._processing_enabled:
            self._schedule_next_jobs()

    def list_jobs(self) -> list[DownloadJob]:
        """Return all jobs currently in the queue in insertion order."""
        with self._lock:
            return [self._jobs[jid] for jid in self._job_order if jid in self._jobs]

    def get_job(self, job_id: str) -> DownloadJob | None:
        """Return a job by its ID or None if not found."""
        with self._lock:
            return self._jobs.get(job_id)

    def has_active_or_queued_duplicate(self, url: str, mode: DownloadMode) -> bool:
        """Return True if an identical URL and mode is already queued, paused, or actively downloading."""
        normalized = url.strip()
        with self._lock:
            for job in self._jobs.values():
                if (
                    job.url == normalized
                    and job.mode == mode
                    and not job.status.is_terminal
                ):
                    return True
        return False

    def add_job(
        self,
        url: str,
        destination_dir: Path | str,
        mode: DownloadMode = DownloadMode.VIDEO,
        video_quality: VideoQuality = VideoQuality.BEST,
        video_format: VideoFormat = VideoFormat.MP4,
        audio_format: AudioFormat = AudioFormat.MP3,
        audio_quality: AudioQuality = AudioQuality.K192,
        title: str = "",
        filename_template: str = "%(title).150B [%(id)s].%(ext)s",
        overwrite_behavior: OverwriteBehavior = OverwriteBehavior.RENAME,
        playlist_mode: PlaylistMode = PlaylistMode.SINGLE,
        selected_playlist_indices: list[int] | None = None,
        allow_duplicate: bool = False,
        start_immediately: bool | None = None,
    ) -> DownloadJob:
        """Validate and add a new DownloadJob to the queue."""
        normalized_url = validate_url(url)
        resolved_dir = validate_output_directory(destination_dir, create_if_missing=True)

        validated_indices: list[int] = []
        if playlist_mode == PlaylistMode.SELECTED_ITEMS:
            validated_indices = validate_playlist_indices(selected_playlist_indices)
        elif selected_playlist_indices:
            validated_indices = list(selected_playlist_indices)

        with self._lock:
            if not allow_duplicate and self.has_active_or_queued_duplicate(normalized_url, mode):
                raise ValidationError(
                    f"URL is already in the active queue: {normalized_url}",
                    remedy="Wait for the existing job to complete or remove it from the queue first.",
                )

            job = DownloadJob(
                url=normalized_url,
                destination_dir=resolved_dir,
                mode=mode,
                video_quality=video_quality,
                video_format=video_format,
                audio_format=audio_format,
                audio_quality=audio_quality,
                title=title,
                filename_template=filename_template,
                overwrite_behavior=overwrite_behavior,
                playlist_mode=playlist_mode,
                selected_playlist_indices=validated_indices,
            )
            self._jobs[job.job_id] = job
            self._job_order.append(job.job_id)
            self._cancel_events[job.job_id] = threading.Event()
            self._pause_events[job.job_id] = threading.Event()
            self._tracked_files[job.job_id] = []
            self._persist_job(job)

        logger.info("Added job %s to queue (%s)", job.job_id, job.url)
        if self._event_bus is not None:
            self._event_bus.publish(EventType.JOB_ADDED, job_id=job.job_id, job=job)
            self._event_bus.publish(EventType.QUEUE_UPDATED)

        should_start = start_immediately if start_immediately is not None else self._processing_enabled
        if should_start:
            self.start_queue()

        return job

    def add_batch_jobs(
        self,
        urls: list[str] | str,
        destination_dir: Path | str,
        mode: DownloadMode = DownloadMode.VIDEO,
        video_quality: VideoQuality = VideoQuality.BEST,
        video_format: VideoFormat = VideoFormat.MP4,
        audio_format: AudioFormat = AudioFormat.MP3,
        audio_quality: AudioQuality = AudioQuality.K192,
        filename_template: str = "%(title).150B [%(id)s].%(ext)s",
        overwrite_behavior: OverwriteBehavior = OverwriteBehavior.RENAME,
        playlist_mode: PlaylistMode = PlaylistMode.SINGLE,
        allow_duplicate: bool = False,
        start_immediately: bool | None = None,
    ) -> tuple[list[DownloadJob], list[str]]:
        """Validate and add a batch of URLs (ignoring blank lines), returning (created_jobs, error_messages)."""
        raw_lines = urls.splitlines() if isinstance(urls, str) else list(urls)
        created: list[DownloadJob] = []
        errors: list[str] = []

        for idx, raw_line in enumerate(raw_lines, start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                job = self.add_job(
                    url=stripped,
                    destination_dir=destination_dir,
                    mode=mode,
                    video_quality=video_quality,
                    video_format=video_format,
                    audio_format=audio_format,
                    audio_quality=audio_quality,
                    filename_template=filename_template,
                    overwrite_behavior=overwrite_behavior,
                    playlist_mode=playlist_mode,
                    allow_duplicate=allow_duplicate,
                    start_immediately=False,
                )
                created.append(job)
            except AppError as exc:
                errors.append(f"Line {idx}: {exc.user_message}")

        should_start = start_immediately if start_immediately is not None else self._processing_enabled
        if should_start and created:
            self.start_queue()

        return created, errors

    def start_queue(self) -> None:
        """Enable queue processing and launch workers up to the concurrency limit."""
        with self._lock:
            if self._shutting_down:
                return
            self._processing_enabled = True
        self._schedule_next_jobs()

    def start_single_job(self, job_id: str) -> None:
        """Ensure a specific queued or paused job starts processing."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            if job.status == JobStatus.PAUSED:
                self.resume_job(job_id, start_immediately=True)
                return
            self._processing_enabled = True
        self._schedule_next_jobs()

    def remove_job(self, job_id: str) -> bool:
        """Remove a non-active job from the queue."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job.status.is_active or job_id in self._active_threads:
                raise InvalidStateTransitionError(
                    "Cannot remove an actively running job. Cancel or pause it first."
                )
            self._jobs.pop(job_id, None)
            if job_id in self._job_order:
                self._job_order.remove(job_id)
            self._cancel_events.pop(job_id, None)
            self._pause_events.pop(job_id, None)
            self._tracked_files.pop(job_id, None)

        if self._event_bus is not None:
            self._event_bus.publish(EventType.JOB_REMOVED, job_id=job_id)
            self._event_bus.publish(EventType.QUEUE_UPDATED)
        return True

    def clear_completed_jobs(self, include_failed: bool = True) -> int:
        """Remove finished (completed, cancelled, and optionally failed) jobs from the active queue view."""
        target_statuses = {JobStatus.COMPLETED, JobStatus.CANCELLED}
        if include_failed:
            target_statuses.add(JobStatus.FAILED)

        removed_count = 0
        with self._lock:
            to_remove = [
                jid
                for jid, job in self._jobs.items()
                if job.status in target_statuses
                and jid not in self._active_threads
            ]
            for jid in to_remove:
                self._jobs.pop(jid, None)
                if jid in self._job_order:
                    self._job_order.remove(jid)
                self._cancel_events.pop(jid, None)
                self._pause_events.pop(jid, None)
                self._tracked_files.pop(jid, None)
                removed_count += 1

        if removed_count > 0 and self._event_bus is not None:
            self._event_bus.publish(EventType.QUEUE_UPDATED)
        return removed_count

    def pause_job(self, job_id: str) -> bool:
        """Request cooperative pause for an actively running job.

        Rejects pausing QUEUED or terminal jobs with InvalidStateTransitionError.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise ValidationError(f"Job '{job_id}' was not found in the queue.")

            if job.status == JobStatus.PAUSED:
                return True

            if not job.status.is_active:
                raise InvalidStateTransitionError(
                    f"Cannot pause job '{job_id}' while in '{job.status.value}' state; only active downloads can be paused."
                )

            pause_event = self._pause_events.setdefault(job_id, threading.Event())
            pause_event.set()

        logger.info("Pause requested for active job %s", job_id)
        return True

    def resume_job(self, job_id: str, start_immediately: bool = True) -> DownloadJob:
        """Resume a PAUSED job (continuing from partial files) or retry a FAILED job.

        Rejects COMPLETED, QUEUED, CANCELLED, or currently active jobs with InvalidStateTransitionError.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise ValidationError(f"Job '{job_id}' was not found in the queue.")

            if job.status == JobStatus.FAILED:
                return self.retry_job(job_id, start_immediately=start_immediately)

            if job.status != JobStatus.PAUSED:
                raise InvalidStateTransitionError(
                    f"Cannot resume job '{job_id}' from '{job.status.value}' state."
                )

            if job_id in self._active_threads and self._active_threads[job_id].is_alive():
                return job

            self._pause_events[job_id] = threading.Event()
            self._cancel_events[job_id] = threading.Event()
            job.transition_to(JobStatus.QUEUED, preserve_progress=True)
            self._persist_job(job)

        logger.info("Resuming paused job %s from %.1f%%", job_id, job.progress_percent)
        if self._event_bus is not None:
            self._event_bus.publish(EventType.JOB_QUEUED, job_id=job.job_id, job=job)
            self._event_bus.publish(EventType.QUEUE_UPDATED)

        if start_immediately:
            self.start_queue()
        return job

    def cancel_job(self, job_id: str) -> bool:
        """Request cooperative cancellation for a queued, paused, or active job."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status.is_terminal:
                return False

            cancel_event = self._cancel_events.setdefault(job_id, threading.Event())
            cancel_event.set()

            if job.status in {JobStatus.QUEUED, JobStatus.PAUSED} and job_id not in self._active_threads:
                tracked = self._tracked_files.get(job_id, [])
                DownloadService.cleanup_partial_files(tracked, destination_dir=job.destination_dir)
                job.transition_to(JobStatus.CANCELLED, error_message="Cancelled by user.")
                self._persist_job(job)
                if self._event_bus is not None:
                    self._event_bus.publish(EventType.JOB_CANCELLED, job_id=job.job_id, job=job)
                    self._event_bus.publish(EventType.QUEUE_UPDATED)
                    self._event_bus.publish(EventType.HISTORY_UPDATED)
                return True

        logger.info("Cancellation requested for active job %s", job_id)
        return True

    def retry_job(self, job_id: str, start_immediately: bool = True) -> DownloadJob:
        """Reset a failed or cancelled job back to QUEUED and optionally start processing."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise ValidationError(f"Job '{job_id}' was not found in the queue.")
            if job.status not in {JobStatus.FAILED, JobStatus.CANCELLED}:
                raise InvalidStateTransitionError(
                    f"Only failed or cancelled jobs can be retried (current status: {job.status.value})."
                )
            if job_id in self._active_threads and self._active_threads[job_id].is_alive():
                raise InvalidStateTransitionError(
                    f"Job '{job_id}' still has an active worker thread."
                )

            job.transition_to(JobStatus.QUEUED, preserve_progress=False)
            self._cancel_events[job.job_id] = threading.Event()
            self._pause_events[job.job_id] = threading.Event()
            self._tracked_files[job.job_id] = []
            self._persist_job(job)

        if self._event_bus is not None:
            self._event_bus.publish(EventType.JOB_QUEUED, job_id=job.job_id, job=job)
            self._event_bus.publish(EventType.QUEUE_UPDATED)

        if start_immediately:
            self.start_queue()
        return job

    def restore_unfinished_jobs_from_history(self) -> list[DownloadJob]:
        """Recover unfinished or paused jobs from SQLite history on application startup.

        - Jobs persisted as PAUSED or QUEUED are restored into the queue in their respective state.
        - Jobs left in an active state (STARTING, FETCHING_METADATA, DOWNLOADING, PROCESSING)
          due to an abrupt exit are restored as PAUSED so the user can resume or cancel them safely.
        """
        if self._history_repo is None:
            return []

        restored: list[DownloadJob] = []
        try:
            entries = self._history_repo.list_entries(limit=500)
        except Exception as exc:
            logger.error("Could not query history for unfinished job recovery: %s", exc)
            return []

        with self._lock:
            for entry in reversed(entries):
                if entry.status.is_terminal or entry.job_id in self._jobs:
                    continue

                target_status = (
                    JobStatus.QUEUED if entry.status == JobStatus.QUEUED else JobStatus.PAUSED
                )
                v_qual = (
                    VideoQuality(entry.video_quality)
                    if entry.video_quality in {q.value for q in VideoQuality}
                    else VideoQuality.BEST
                )
                v_fmt = (
                    VideoFormat(entry.video_format)
                    if entry.video_format in {f.value for f in VideoFormat}
                    else VideoFormat.MP4
                )
                a_fmt = (
                    AudioFormat(entry.audio_format)
                    if entry.audio_format in {a.value for a in AudioFormat}
                    else AudioFormat.MP3
                )
                pl_mode = (
                    PlaylistMode(entry.playlist_mode)
                    if entry.playlist_mode in {p.value for p in PlaylistMode}
                    else PlaylistMode.SINGLE
                )

                job = DownloadJob(
                    job_id=entry.job_id,
                    url=entry.url,
                    title=entry.title,
                    destination_dir=Path(entry.destination_dir),
                    mode=entry.mode,
                    video_quality=v_qual,
                    video_format=v_fmt,
                    audio_format=a_fmt,
                    playlist_mode=pl_mode,
                    status=target_status,
                )
                self._jobs[job.job_id] = job
                self._job_order.append(job.job_id)
                self._cancel_events[job.job_id] = threading.Event()
                self._pause_events[job.job_id] = threading.Event()
                self._tracked_files[job.job_id] = []
                if entry.status != target_status:
                    self._persist_job(job)
                restored.append(job)

        return restored

    def _schedule_next_jobs(self) -> None:
        """Launch worker threads for queued jobs while active count is below max_concurrent."""
        with self._lock:
            if self._shutting_down or not self._processing_enabled:
                return

            finished_ids = [
                jid for jid, thr in self._active_threads.items() if not thr.is_alive()
            ]
            for jid in finished_ids:
                self._active_threads.pop(jid, None)

            available_slots = self._max_concurrent - len(self._active_threads)
            if available_slots <= 0:
                return

            queued_jobs = [
                self._jobs[jid]
                for jid in self._job_order
                if jid in self._jobs
                and self._jobs[jid].status == JobStatus.QUEUED
                and jid not in self._active_threads
            ]

            for job in queued_jobs[:available_slots]:
                cancel_event = self._cancel_events.setdefault(job.job_id, threading.Event())
                pause_event = self._pause_events.setdefault(job.job_id, threading.Event())
                tracked = self._tracked_files.setdefault(job.job_id, [])
                worker = threading.Thread(
                    target=self._run_job_worker,
                    args=(job, cancel_event, pause_event, tracked),
                    name=f"DownloadWorker-{job.job_id}",
                    daemon=True,
                )
                self._active_threads[job.job_id] = worker
                worker.start()

    def _run_job_worker(
        self,
        job: DownloadJob,
        cancel_event: threading.Event,
        pause_event: threading.Event,
        tracked_files: list[Path],
    ) -> None:
        """Worker thread entry point that runs a single job and schedules the next one."""
        try:
            self._download_service.execute_job(
                job,
                cancel_event=cancel_event,
                pause_event=pause_event,
                tracked_files=tracked_files,
            )
        except DownloadCancelledError:
            logger.info("Worker finished cancelled job %s", job.job_id)
        except DownloadPausedError:
            logger.info("Worker suspended paused job %s", job.job_id)
        except AppError as exc:
            logger.warning("Worker caught application error for job %s: %s", job.job_id, exc)
        except Exception as exc:
            logger.exception("Unexpected error in worker for job %s: %s", job.job_id, exc)
            with self._lock:
                if cancel_event.is_set():
                    job.status = JobStatus.CANCELLED
                    job.error_message = "Cancelled by user."
                elif pause_event.is_set():
                    job.status = JobStatus.PAUSED
                elif not job.status.is_terminal:
                    job.status = JobStatus.FAILED
                    job.error_message = f"Unexpected worker error: {exc}"
        finally:
            with self._lock:
                self._persist_job(job)
                self._active_threads.pop(job.job_id, None)

            if self._event_bus is not None:
                self._event_bus.publish(EventType.QUEUE_UPDATED)
                self._event_bus.publish(EventType.HISTORY_UPDATED)

            self._schedule_next_jobs()

    def _persist_job(self, job: DownloadJob) -> None:
        """Record job status in SQLite history repository without raising on transient DB errors."""
        if self._history_repo is None:
            return
        try:
            self._history_repo.record_job(job)
        except Exception as exc:
            logger.error("Failed to record job %s in history database: %s", job.job_id, exc)

    def wait_for_idle(self, timeout: float = 5.0) -> bool:
        """Wait until all active worker threads finish (used in tests and shutdown)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                alive = [t for t in self._active_threads.values() if t.is_alive()]
                queued = [
                    j
                    for j in self._jobs.values()
                    if j.status == JobStatus.QUEUED and self._processing_enabled
                ]
            if not alive and not queued:
                return True
            time.sleep(0.01)
        return False

    def shutdown(self, timeout: float = 3.0, pause_active: bool = True) -> None:
        """Signal all active downloads to pause (or cancel) and wait for threads to exit cleanly."""
        with self._lock:
            self._shutting_down = True
            self._processing_enabled = False
            for jid in list(self._active_threads.keys()):
                if pause_active:
                    p_ev = self._pause_events.get(jid)
                    if p_ev is not None:
                        p_ev.set()
                else:
                    c_ev = self._cancel_events.get(jid)
                    if c_ev is not None:
                        c_ev.set()
            threads = list(self._active_threads.values())

        for thr in threads:
            thr.join(timeout=timeout)

        # Ensure any job whose worker did not exit within timeout still has consistent state in DB
        with self._lock:
            for jid, job in self._jobs.items():
                if job.status.is_active:
                    job.status = JobStatus.PAUSED if pause_active else JobStatus.CANCELLED
                    self._persist_job(job)

        logger.info("QueueService shutdown complete.")
