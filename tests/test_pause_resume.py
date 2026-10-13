"""Comprehensive unit and concurrency tests for Pause and Resume (Section 9)."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from src.core.events import EventBus, EventType
from src.core.exceptions import (
    DownloadCancelledError,
    DownloadError,
    DownloadPausedError,
    InvalidStateTransitionError,
)
from src.core.models import DownloadJob, JobStatus
from src.database.db import DatabaseManager
from src.database.repositories import HistoryRepository
from src.services.download_service import DownloadService
from src.services.queue_service import QueueService
from tests.test_download_service import StubFFmpegService


class ControllablePauseDownloadService(DownloadService):
    """DownloadService stub supporting cooperative pause/cancel, partial files, and concurrency tracking."""

    def __init__(self) -> None:
        super().__init__(ffmpeg_service=StubFFmpegService(True))
        self.started_events: dict[str, threading.Event] = {}
        self.release_gates: dict[str, threading.Event] = {}
        self.fail_on_resume_ids: set[str] = set()
        self.execution_counts: dict[str, int] = {}
        self.active_count = 0
        self.max_observed_active = 0
        self._lock = threading.Lock()

    def prepare_job_sync(self, job_id: str) -> tuple[threading.Event, threading.Event]:
        started = threading.Event()
        gate = threading.Event()
        self.started_events[job_id] = started
        self.release_gates[job_id] = gate
        return started, gate

    def execute_job(
        self,
        job: DownloadJob,
        cancel_event: threading.Event | None = None,
        pause_event: threading.Event | None = None,
        tracked_files: list[Path] | None = None,
    ) -> DownloadJob:
        with self._lock:
            self.execution_counts[job.job_id] = self.execution_counts.get(job.job_id, 0) + 1
            run_num = self.execution_counts[job.job_id]
            self.active_count += 1
            if self.active_count > self.max_observed_active:
                self.max_observed_active = self.active_count

        try:
            job.transition_to(
                JobStatus.STARTING,
                preserve_progress=job.progress_percent > 0,
            )
            job.transition_to(
                JobStatus.DOWNLOADING,
                preserve_progress=job.progress_percent > 0,
            )
            if job.progress_percent == 0.0:
                job.progress_percent = 45.0
                job.downloaded_bytes = 450_000
                job.total_bytes = 1_000_000

            part_path = job.destination_dir / f"video_{job.job_id}.mp4.part"
            if not part_path.exists():
                part_path.write_bytes(b"partial-stream-bytes")

            started = self.started_events.get(job.job_id)
            if started is not None:
                started.set()

            gate = self.release_gates.get(job.job_id)
            if gate is not None:
                gate.wait(timeout=4.0)

            if cancel_event is not None and cancel_event.is_set():
                self.cleanup_partial_files(job)
                job.transition_to(JobStatus.CANCELLED, error_message="Cancelled by user.")
                raise DownloadCancelledError("Cancelled")

            if pause_event is not None and pause_event.is_set():
                job.transition_to(JobStatus.PAUSED, error_message="Paused by user.")
                raise DownloadPausedError("Paused")

            if run_num > 1 and job.job_id in self.fail_on_resume_ids:
                job.transition_to(JobStatus.FAILED, error_message="Resume network failure")
                raise DownloadError("Resume network failure")

            final_path = job.destination_dir / f"video_{job.job_id}.mp4"
            if part_path.exists():
                part_data = part_path.read_bytes()
                final_path.write_bytes(part_data + b"-completed")
                part_path.unlink()
            else:
                final_path.write_bytes(b"completed")

            job.output_path = final_path
            job.file_size_bytes = final_path.stat().st_size
            job.transition_to(JobStatus.COMPLETED)
            return job
        finally:
            with self._lock:
                self.active_count -= 1


def test_1_queued_to_paused_rejected(tmp_path: Path) -> None:
    dl = ControllablePauseDownloadService()
    queue = QueueService(download_service=dl, auto_start=False)
    job = queue.add_job("https://www.youtube.com/watch?v=queued_vid01", destination_dir=tmp_path)
    assert job.status == JobStatus.QUEUED

    with pytest.raises(InvalidStateTransitionError):
        queue.pause_job(job.job_id)


def test_2_and_3_and_14_active_to_paused_and_resume_preserves_part_file(tmp_path: Path) -> None:
    dl = ControllablePauseDownloadService()
    bus = EventBus()
    queue = QueueService(download_service=dl, event_bus=bus, auto_start=False)

    job = queue.add_job("https://www.youtube.com/watch?v=pause_vid002", destination_dir=tmp_path)
    started, gate = dl.prepare_job_sync(job.job_id)

    queue.start_queue()
    assert started.wait(timeout=2.0) is True

    # Pause while active
    assert queue.pause_job(job.job_id) is True
    gate.set()
    assert queue.wait_for_idle(timeout=3.0) is True

    assert job.status == JobStatus.PAUSED
    assert job.progress_percent == pytest.approx(45.0)
    part_file = tmp_path / f"video_{job.job_id}.mp4.part"
    assert part_file.exists() is True
    assert part_file.read_bytes() == b"partial-stream-bytes"

    # Prepare new gate for resume and verify progress is preserved on transition to QUEUED
    dl.release_gates.pop(job.job_id, None)
    resumed = queue.resume_job(job.job_id, start_immediately=False)
    assert resumed.status == JobStatus.QUEUED
    assert resumed.progress_percent == pytest.approx(45.0)
    assert resumed.downloaded_bytes == 450_000

    queue.start_queue()
    assert queue.wait_for_idle(timeout=3.0) is True

    assert job.status == JobStatus.COMPLETED
    assert job.progress_percent == 100.0
    assert part_file.exists() is False
    assert job.output_path is not None and job.output_path.exists()
    assert job.output_path.read_bytes() == b"partial-stream-bytes-completed"
    queue.shutdown()


def test_4_paused_to_cancelled_cleans_up_part_files(tmp_path: Path) -> None:
    dl = ControllablePauseDownloadService()
    queue = QueueService(download_service=dl, auto_start=False)
    job = queue.add_job("https://www.youtube.com/watch?v=pause_cancel4", destination_dir=tmp_path)
    started, gate = dl.prepare_job_sync(job.job_id)

    queue.start_queue()
    assert started.wait(timeout=2.0) is True
    assert queue.pause_job(job.job_id) is True
    gate.set()
    assert queue.wait_for_idle(timeout=3.0) is True
    assert job.status == JobStatus.PAUSED

    part_file = tmp_path / f"video_{job.job_id}.mp4.part"
    assert part_file.exists() is True

    # Cancel while paused -> should transition to CANCELLED and delete .part file
    assert queue.cancel_job(job.job_id) is True
    assert job.status == JobStatus.CANCELLED
    assert part_file.exists() is False
    queue.shutdown()


def test_5_resume_on_completed_job_rejected(tmp_path: Path) -> None:
    dl = ControllablePauseDownloadService()
    queue = QueueService(download_service=dl, auto_start=True)
    job = queue.add_job("https://www.youtube.com/watch?v=completed_v5", destination_dir=tmp_path)
    assert queue.wait_for_idle(timeout=3.0) is True
    assert job.status == JobStatus.COMPLETED

    with pytest.raises(InvalidStateTransitionError):
        queue.resume_job(job.job_id)
    queue.shutdown()


def test_6_resume_on_failed_job_retries_cleanly(tmp_path: Path) -> None:
    dl = ControllablePauseDownloadService()
    queue = QueueService(download_service=dl, auto_start=False)
    job = queue.add_job("https://www.youtube.com/watch?v=failed_vid06", destination_dir=tmp_path)
    job.transition_to(JobStatus.STARTING)
    job.transition_to(JobStatus.FAILED, error_message="Temporary socket timeout")

    resumed = queue.resume_job(job.job_id, start_immediately=True)
    assert queue.wait_for_idle(timeout=3.0) is True
    assert resumed.status == JobStatus.COMPLETED
    queue.shutdown()


def test_7_pausing_job_a_does_not_pause_job_b(tmp_path: Path) -> None:
    dl = ControllablePauseDownloadService()
    queue = QueueService(download_service=dl, max_concurrent_downloads=2, auto_start=False)

    job_a = queue.add_job("https://www.youtube.com/watch?v=job_a_000007", destination_dir=tmp_path)
    job_b = queue.add_job("https://www.youtube.com/watch?v=job_b_000007", destination_dir=tmp_path)
    started_a, gate_a = dl.prepare_job_sync(job_a.job_id)
    started_b, gate_b = dl.prepare_job_sync(job_b.job_id)

    queue.start_queue()
    assert started_a.wait(timeout=2.0) is True
    assert started_b.wait(timeout=2.0) is True

    assert queue.pause_job(job_a.job_id) is True
    gate_a.set()
    gate_b.set()
    assert queue.wait_for_idle(timeout=3.0) is True

    assert job_a.status == JobStatus.PAUSED
    assert job_b.status == JobStatus.COMPLETED
    queue.shutdown()


def test_8_and_9_progress_hook_stops_when_paused_and_late_events_ignored(tmp_path: Path) -> None:
    bus = EventBus()
    service = DownloadService(ffmpeg_service=StubFFmpegService(True), event_bus=bus)
    job = DownloadJob(
        url="https://www.youtube.com/watch?v=hook_pause08",
        destination_dir=tmp_path,
    )
    job.transition_to(JobStatus.STARTING)
    job.transition_to(JobStatus.DOWNLOADING)

    cancel_ev = threading.Event()
    pause_ev = threading.Event()
    hook = service._make_progress_hook(job, cancel_ev, pause_ev)

    # Normal progress update succeeds
    hook(
        {
            "status": "downloading",
            "downloaded_bytes": 250,
            "total_bytes": 1000,
            "speed": 500.0,
            "eta": 2,
            "filename": str(tmp_path / "clip.mp4"),
        }
    )
    assert job.progress_percent == pytest.approx(25.0)

    # Set pause_ev -> hook raises DownloadPausedError immediately
    pause_ev.set()
    with pytest.raises(DownloadPausedError):
        hook(
            {
                "status": "downloading",
                "downloaded_bytes": 500,
                "total_bytes": 1000,
            }
        )

    # Transition job to PAUSED and verify a late progress callback with an un-set event does not overwrite PAUSED state
    job.transition_to(JobStatus.PAUSED, error_message="Paused by user.")
    late_hook = service._make_progress_hook(job, threading.Event(), threading.Event())
    bus.poll_events(max_items=100)
    late_hook(
        {
            "status": "downloading",
            "downloaded_bytes": 900,
            "total_bytes": 1000,
            "speed": 999.0,
        }
    )
    assert job.status == JobStatus.PAUSED
    assert job.progress_percent == pytest.approx(25.0)
    assert bus.poll_events(max_items=100) == []


def test_10_and_12_repeated_pause_resume_and_no_duplicate_workers(tmp_path: Path) -> None:
    dl = ControllablePauseDownloadService()
    queue = QueueService(download_service=dl, max_concurrent_downloads=2, auto_start=False)
    job = queue.add_job("https://www.youtube.com/watch?v=repeat_pr_10", destination_dir=tmp_path)
    started, gate = dl.prepare_job_sync(job.job_id)

    queue.start_queue()
    assert started.wait(timeout=2.0) is True

    # Calling pause_job multiple times while active is idempotent
    assert queue.pause_job(job.job_id) is True
    assert queue.pause_job(job.job_id) is True
    gate.set()
    assert queue.wait_for_idle(timeout=3.0) is True

    # Calling pause_job when already PAUSED returns True without error
    assert job.status == JobStatus.PAUSED
    assert queue.pause_job(job.job_id) is True

    # Resume with gate held so we can verify only 1 worker runs for this job
    started2, gate2 = dl.prepare_job_sync(job.job_id)
    queue.resume_job(job.job_id, start_immediately=True)
    assert started2.wait(timeout=2.0) is True
    assert queue.active_worker_count == 1

    gate2.set()
    assert queue.wait_for_idle(timeout=3.0) is True
    assert job.status == JobStatus.COMPLETED
    assert dl.execution_counts[job.job_id] == 2
    queue.shutdown()


def test_11_resume_failure_transitions_cleanly_to_failed(tmp_path: Path) -> None:
    dl = ControllablePauseDownloadService()
    queue = QueueService(download_service=dl, auto_start=False)
    job = queue.add_job("https://www.youtube.com/watch?v=resume_fail1", destination_dir=tmp_path)
    started, gate = dl.prepare_job_sync(job.job_id)

    queue.start_queue()
    assert started.wait(timeout=2.0) is True
    queue.pause_job(job.job_id)
    gate.set()
    assert queue.wait_for_idle(timeout=3.0) is True
    assert job.status == JobStatus.PAUSED

    dl.release_gates.pop(job.job_id, None)
    dl.fail_on_resume_ids.add(job.job_id)
    queue.resume_job(job.job_id, start_immediately=True)
    assert queue.wait_for_idle(timeout=3.0) is True

    assert job.status == JobStatus.FAILED
    assert "Resume network failure" in job.error_message
    queue.shutdown()


def test_13_shutdown_while_paused_persists_and_recovers_on_restart(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path / "restart_recovery.db")
    history_repo = HistoryRepository(db)
    dl = ControllablePauseDownloadService()

    queue1 = QueueService(
        download_service=dl,
        history_repository=history_repo,
        auto_start=False,
    )
    job = queue1.add_job("https://www.youtube.com/watch?v=recover_jb13", destination_dir=tmp_path)
    started, gate = dl.prepare_job_sync(job.job_id)

    queue1.start_queue()
    assert started.wait(timeout=2.0) is True
    # Shutting down with pause_active=True pauses active jobs so they can be resumed next launch
    threading.Timer(0.05, gate.set).start()
    queue1.shutdown(pause_active=True, timeout=3.0)

    assert job.status == JobStatus.PAUSED
    persisted = history_repo.get_by_id(job.job_id)
    assert persisted is not None
    assert persisted.status == JobStatus.PAUSED
    part_path = tmp_path / f"video_{job.job_id}.mp4.part"
    assert part_path.exists() is True

    # Simulate application restart with a fresh QueueService
    dl2 = ControllablePauseDownloadService()
    queue2 = QueueService(
        download_service=dl2,
        history_repository=history_repo,
        auto_start=False,
    )
    restored = queue2.restore_unfinished_jobs_from_history()
    assert len(restored) == 1
    assert restored[0].job_id == job.job_id
    assert restored[0].status == JobStatus.PAUSED

    # Resume restored job to completion (continuing from preserved .part file)
    queue2.resume_job(job.job_id, start_immediately=True)
    assert queue2.wait_for_idle(timeout=3.0) is True
    assert queue2.get_job(job.job_id).status == JobStatus.COMPLETED
    queue2.shutdown()


def test_15_concurrency_limit_respected_after_resume(tmp_path: Path) -> None:
    dl = ControllablePauseDownloadService()
    queue = QueueService(download_service=dl, max_concurrent_downloads=1, auto_start=False)

    job1 = queue.add_job("https://www.youtube.com/watch?v=conc_res_001", destination_dir=tmp_path)
    job2 = queue.add_job("https://www.youtube.com/watch?v=conc_res_002", destination_dir=tmp_path)
    started1, gate1 = dl.prepare_job_sync(job1.job_id)
    started2, gate2 = dl.prepare_job_sync(job2.job_id)

    queue.start_queue()
    assert started1.wait(timeout=2.0) is True

    # Pause job1; job2 should automatically start next with concurrency=1
    queue.pause_job(job1.job_id)
    gate1.set()
    assert started2.wait(timeout=2.0) is True

    # While job2 is active, resume job1 -> job1 must wait in QUEUED until job2 finishes
    dl.release_gates.pop(job1.job_id, None)
    queue.resume_job(job1.job_id, start_immediately=True)
    assert job1.status == JobStatus.QUEUED
    assert dl.max_observed_active == 1

    gate2.set()
    assert queue.wait_for_idle(timeout=3.0) is True
    assert job1.status == JobStatus.COMPLETED
    assert job2.status == JobStatus.COMPLETED
    assert dl.max_observed_active == 1
    queue.shutdown()
