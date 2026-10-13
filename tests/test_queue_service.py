"""Unit, concurrency, state machine, batch, and 30-job stress tests for QueueService (Sections 8 & 19)."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from src.core.events import EventBus, EventType
from src.core.exceptions import (
    DownloadCancelledError,
    DownloadError,
    DownloadPausedError,
    InvalidStateTransitionError,
    ValidationError,
)
from src.core.models import DownloadJob, DownloadMode, JobStatus
from src.database.db import DatabaseManager
from src.database.repositories import HistoryRepository
from src.services.download_service import DownloadService
from src.services.queue_service import QueueService
from tests.test_download_service import StubFFmpegService


class FakeDownloadService(DownloadService):
    """Controllable DownloadService stub for testing QueueService behavior without network I/O."""

    def __init__(
        self,
        fail_urls: set[str] | None = None,
        crash_urls: set[str] | None = None,
    ) -> None:
        super().__init__(ffmpeg_service=StubFFmpegService(True))
        self.fail_urls = fail_urls or set()
        self.crash_urls = crash_urls or set()
        self.gate: threading.Event | None = None
        self.active_count = 0
        self.max_observed_active = 0
        self.execution_order: list[str] = []
        self._lock = threading.Lock()

    def execute_job(
        self,
        job: DownloadJob,
        cancel_event: threading.Event | None = None,
        pause_event: threading.Event | None = None,
        tracked_files: list[Path] | None = None,
    ) -> DownloadJob:
        with self._lock:
            self.active_count += 1
            if self.active_count > self.max_observed_active:
                self.max_observed_active = self.active_count
            self.execution_order.append(job.url)

        try:
            job.transition_to(JobStatus.STARTING, preserve_progress=job.progress_percent > 0)
            job.transition_to(JobStatus.DOWNLOADING, preserve_progress=job.progress_percent > 0)
            job.progress_percent = 50.0
            job.speed_bps = 1024.0
            job.eta_seconds = 10

            if self.gate is not None:
                self.gate.wait(timeout=3.0)

            if cancel_event is not None and cancel_event.is_set():
                job.transition_to(JobStatus.CANCELLED, error_message="Cancelled by user.")
                raise DownloadCancelledError("Cancelled")

            if pause_event is not None and pause_event.is_set():
                job.transition_to(JobStatus.PAUSED, error_message="Paused by user.")
                raise DownloadPausedError("Paused")

            if job.url in self.crash_urls:
                raise RuntimeError("Unexpected third-party internal crash")

            if job.url in self.fail_urls:
                job.transition_to(JobStatus.FAILED, error_message="Simulated download failure")
                raise DownloadError("Simulated download failure")

            out = job.destination_dir / f"{job.job_id}.mp4"
            out.write_bytes(b"ok")
            job.output_path = out
            job.file_size_bytes = 2
            job.transition_to(JobStatus.COMPLETED)
            return job
        finally:
            with self._lock:
                self.active_count -= 1


def test_state_machine_valid_and_invalid_transitions(tmp_path: Path) -> None:
    job = DownloadJob(url="https://www.youtube.com/watch?v=state_test_1", destination_dir=tmp_path)
    assert job.status == JobStatus.QUEUED

    # Invalid direct transitions from QUEUED
    with pytest.raises(InvalidStateTransitionError):
        job.transition_to(JobStatus.COMPLETED)
    with pytest.raises(InvalidStateTransitionError):
        job.transition_to(JobStatus.PAUSED)

    # Valid: Queued -> Starting -> Downloading -> Paused -> Downloading -> Completed
    job.transition_to(JobStatus.STARTING)
    job.transition_to(JobStatus.DOWNLOADING)
    job.progress_percent = 33.0
    job.transition_to(JobStatus.PAUSED)
    assert job.status == JobStatus.PAUSED

    job.transition_to(JobStatus.DOWNLOADING, preserve_progress=True)
    assert job.progress_percent == pytest.approx(33.0)
    job.transition_to(JobStatus.COMPLETED)
    assert job.status == JobStatus.COMPLETED
    assert job.progress_percent == 100.0

    # Invalid transitions from COMPLETED
    for invalid_target in (JobStatus.CANCELLED, JobStatus.FAILED, JobStatus.QUEUED, JobStatus.PAUSED):
        with pytest.raises(InvalidStateTransitionError):
            job.transition_to(invalid_target)

    # Cancelled -> Completed without retry is rejected
    job_c = DownloadJob(url="https://www.youtube.com/watch?v=state_test_2", destination_dir=tmp_path)
    job_c.transition_to(JobStatus.CANCELLED)
    with pytest.raises(InvalidStateTransitionError):
        job_c.transition_to(JobStatus.COMPLETED)


def test_queue_add_duplicate_remove_and_cancel(tmp_path: Path) -> None:
    dl_service = FakeDownloadService()
    queue = QueueService(
        download_service=dl_service,
        max_concurrent_downloads=1,
        auto_start=False,
    )

    job1 = queue.add_job("https://www.youtube.com/watch?v=vid_one_001", destination_dir=tmp_path)
    assert job1.status == JobStatus.QUEUED

    # Duplicate active/queued URL in same mode is rejected by default
    with pytest.raises(ValidationError):
        queue.add_job("https://www.youtube.com/watch?v=vid_one_001", destination_dir=tmp_path)

    # Different mode (AUDIO) for same URL is allowed
    job_audio = queue.add_job(
        "https://www.youtube.com/watch?v=vid_one_001",
        destination_dir=tmp_path,
        mode=DownloadMode.AUDIO,
    )
    assert job_audio.mode == DownloadMode.AUDIO

    # Cancel a queued job directly transitions to CANCELLED
    assert queue.cancel_job(job_audio.job_id) is True
    assert job_audio.status == JobStatus.CANCELLED

    # Remove queued job1
    assert queue.remove_job(job1.job_id) is True
    assert queue.get_job(job1.job_id) is None


def test_queue_prevents_removing_active_job_until_cancelled_or_finished(tmp_path: Path) -> None:
    dl_service = FakeDownloadService()
    dl_service.gate = threading.Event()
    queue = QueueService(download_service=dl_service, max_concurrent_downloads=1, auto_start=False)

    job = queue.add_job("https://www.youtube.com/watch?v=active_rem_1", destination_dir=tmp_path)
    queue.start_queue()

    # Wait briefly until worker enters active state
    for _ in range(40):
        if job.status.is_active:
            break
        threading.Event().wait(0.02)

    assert job.status.is_active is True
    with pytest.raises(InvalidStateTransitionError):
        queue.remove_job(job.job_id)

    dl_service.gate.set()
    assert queue.wait_for_idle(timeout=3.0) is True
    assert queue.remove_job(job.job_id) is True
    queue.shutdown()


def test_queue_continues_after_individual_failure_or_crash_and_resets_fields_on_retry(tmp_path: Path) -> None:
    bad_url = "https://www.youtube.com/watch?v=bad_video_01"
    crash_url = "https://www.youtube.com/watch?v=crash_vid_01"
    good_url = "https://www.youtube.com/watch?v=good_vid_001"

    db = DatabaseManager(tmp_path / "queue_history.db")
    history_repo = HistoryRepository(db)
    bus = EventBus()
    dl_service = FakeDownloadService(fail_urls={bad_url}, crash_urls={crash_url})

    queue = QueueService(
        download_service=dl_service,
        history_repository=history_repo,
        event_bus=bus,
        max_concurrent_downloads=1,
        auto_start=False,
    )

    bad_job = queue.add_job(bad_url, destination_dir=tmp_path)
    crash_job = queue.add_job(crash_url, destination_dir=tmp_path)
    good_job = queue.add_job(good_url, destination_dir=tmp_path)

    queue.start_queue()
    assert queue.wait_for_idle(timeout=5.0) is True

    assert bad_job.status == JobStatus.FAILED
    assert crash_job.status == JobStatus.FAILED
    assert "Unexpected worker error" in crash_job.error_message
    assert good_job.status == JobStatus.COMPLETED

    # Verify history was persisted for all 3
    assert history_repo.get_by_id(bad_job.job_id) is not None
    assert history_repo.get_by_id(crash_job.job_id) is not None
    assert history_repo.get_by_id(good_job.job_id) is not None

    # Trying to retry a COMPLETED job raises InvalidStateTransitionError
    with pytest.raises(InvalidStateTransitionError):
        queue.retry_job(good_job.job_id)

    # Verify retry resets progress, error, speed, and ETA before starting
    dl_service.fail_urls.clear()
    retried = queue.retry_job(bad_job.job_id, start_immediately=False)
    assert retried.status == JobStatus.QUEUED
    assert retried.progress_percent == 0.0
    assert retried.error_message is None
    assert retried.speed_bps is None
    assert retried.eta_seconds is None

    queue.start_queue()
    assert queue.wait_for_idle(timeout=5.0) is True
    assert retried.status == JobStatus.COMPLETED

    # Clear completed jobs removes finished jobs while leaving none behind
    removed = queue.clear_completed_jobs()
    assert removed == 3
    assert queue.list_jobs() == []
    queue.shutdown()


def test_concurrency_one_vs_multi_worker_never_exceeds_limit(tmp_path: Path) -> None:
    # Concurrency = 1
    dl_serial = FakeDownloadService()
    q_serial = QueueService(download_service=dl_serial, max_concurrent_downloads=1, auto_start=False)
    for idx in range(4):
        q_serial.add_job(f"https://www.youtube.com/watch?v=serial_v_{idx:03d}", destination_dir=tmp_path)
    q_serial.start_queue()
    assert q_serial.wait_for_idle(timeout=4.0) is True
    assert dl_serial.max_observed_active == 1
    q_serial.shutdown()

    # Concurrency = 3 with 8 jobs held briefly at gate
    dl_parallel = FakeDownloadService()
    dl_parallel.gate = threading.Event()
    q_parallel = QueueService(download_service=dl_parallel, max_concurrent_downloads=3, auto_start=False)
    for idx in range(8):
        q_parallel.add_job(f"https://www.youtube.com/watch?v=parall_v_{idx:03d}", destination_dir=tmp_path)

    q_parallel.start_queue()
    # Release gate after workers reach concurrency cap
    threading.Timer(0.1, dl_parallel.gate.set).start()
    assert q_parallel.wait_for_idle(timeout=5.0) is True
    assert 1 <= dl_parallel.max_observed_active <= 3
    q_parallel.shutdown()


def test_batch_url_submission_mixed_valid_invalid_duplicates_and_whitespace(tmp_path: Path) -> None:
    dl = FakeDownloadService()
    queue = QueueService(download_service=dl, auto_start=False)

    raw_lines = [
        "   ",
        "https://www.youtube.com/watch?v=batch_valid1",
        "  https://youtu.be/batch_valid2  ",
        "https://www.youtube.com/watch?v=batch_valid1",  # duplicate of #1
        "ftp://invalid-scheme.example.com/video.mp4",
        "not-a-url-at-all",
        "",
    ]
    created, errors = queue.add_batch_jobs(raw_lines, destination_dir=tmp_path)
    assert len(created) == 2
    assert len(errors) == 3  # 1 duplicate + 2 invalid URLs
    assert {j.url for j in created} == {
        "https://www.youtube.com/watch?v=batch_valid1",
        "https://youtu.be/batch_valid2",
    }
    queue.shutdown()


def test_clear_completed_preserves_active_queued_and_paused_jobs(tmp_path: Path) -> None:
    dl = FakeDownloadService()
    queue = QueueService(download_service=dl, auto_start=False)

    j_done = queue.add_job("https://www.youtube.com/watch?v=clr_done_001", destination_dir=tmp_path)
    j_done.transition_to(JobStatus.STARTING)
    j_done.transition_to(JobStatus.COMPLETED)

    j_fail = queue.add_job("https://www.youtube.com/watch?v=clr_fail_002", destination_dir=tmp_path)
    j_fail.transition_to(JobStatus.STARTING)
    j_fail.transition_to(JobStatus.FAILED, error_message="err")

    j_queued = queue.add_job("https://www.youtube.com/watch?v=clr_queu_003", destination_dir=tmp_path)
    j_paused = queue.add_job("https://www.youtube.com/watch?v=clr_paus_004", destination_dir=tmp_path)
    j_paused.transition_to(JobStatus.STARTING)
    j_paused.transition_to(JobStatus.PAUSED)

    removed = queue.clear_completed_jobs()
    assert removed == 2
    remaining_ids = {j.job_id for j in queue.list_jobs()}
    assert remaining_ids == {j_queued.job_id, j_paused.job_id}
    queue.shutdown()


def test_stress_30_queued_jobs_mixed_outcomes_no_thread_leaks(tmp_path: Path) -> None:
    """Section 19: Stress-test the queue with 30 jobs, mixed outcomes, verifying clean thread exit."""
    baseline_threads = threading.active_count()
    fail_set = {f"https://www.youtube.com/watch?v=stress_v_{i:03d}" for i in (3, 9, 17, 24)}
    dl = FakeDownloadService(fail_urls=fail_set)
    bus = EventBus()
    db = DatabaseManager(tmp_path / "stress_history.db")
    repo = HistoryRepository(db)

    queue = QueueService(
        download_service=dl,
        history_repository=repo,
        event_bus=bus,
        max_concurrent_downloads=3,
        auto_start=False,
    )

    jobs: list[DownloadJob] = []
    for i in range(30):
        j = queue.add_job(
            f"https://www.youtube.com/watch?v=stress_v_{i:03d}",
            destination_dir=tmp_path,
        )
        jobs.append(j)

    # Cancel 2 queued jobs before starting
    queue.cancel_job(jobs[5].job_id)
    queue.cancel_job(jobs[15].job_id)

    queue.start_queue()
    assert queue.wait_for_idle(timeout=10.0) is True

    completed = [j for j in jobs if j.status == JobStatus.COMPLETED]
    failed = [j for j in jobs if j.status == JobStatus.FAILED]
    cancelled = [j for j in jobs if j.status == JobStatus.CANCELLED]

    assert len(completed) == 24
    assert len(failed) == 4
    assert len(cancelled) == 2
    assert dl.max_observed_active <= 3
    assert queue.active_worker_count == 0

    # Drain events and shut down cleanly
    drained = bus.poll_events(max_items=500)
    assert len(drained) >= 30
    queue.shutdown(timeout=3.0)

    # Verify all worker threads have exited
    assert threading.active_count() <= baseline_threads + 1
