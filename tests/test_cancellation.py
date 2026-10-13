"""Unit and integration tests for cancellation and partial-file cleanup (Section 10)."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from src.core.events import EventBus
from src.core.exceptions import DownloadCancelledError, InvalidStateTransitionError
from src.core.models import DownloadJob, DownloadMode, JobStatus, PlaylistMode
from src.services.download_service import DownloadService
from src.services.queue_service import QueueService
from tests.test_download_service import StubFFmpegService


class CancellationTestDownloadService(DownloadService):
    """DownloadService stub supporting stage-specific cancellation hooks."""

    def __init__(self) -> None:
        super().__init__(ffmpeg_service=StubFFmpegService(True))
        self.started_events: dict[str, threading.Event] = {}
        self.release_gates: dict[str, threading.Event] = {}
        self.stage_for_job: dict[str, str] = {}

    def prepare_job_sync(self, job_id: str, stage: str = "downloading") -> tuple[threading.Event, threading.Event]:
        started = threading.Event()
        gate = threading.Event()
        self.started_events[job_id] = started
        self.release_gates[job_id] = gate
        self.stage_for_job[job_id] = stage
        return started, gate

    def execute_job(
        self,
        job: DownloadJob,
        cancel_event: threading.Event | None = None,
        pause_event: threading.Event | None = None,
        tracked_files: list[Path] | None = None,
    ) -> DownloadJob:
        stage = self.stage_for_job.get(job.job_id, "downloading")
        job.transition_to(JobStatus.STARTING)

        if stage == "metadata":
            job.transition_to(JobStatus.FETCHING_METADATA)
            started = self.started_events.get(job.job_id)
            if started is not None:
                started.set()
            gate = self.release_gates.get(job.job_id)
            if gate is not None:
                gate.wait(timeout=3.0)
            if cancel_event is not None and cancel_event.is_set():
                job.transition_to(JobStatus.CANCELLED, error_message="Download cancelled during metadata check.")
                raise DownloadCancelledError("Cancelled during metadata check")

        job.transition_to(JobStatus.DOWNLOADING)
        part_file = job.destination_dir / f"item_{job.job_id}.mp4.part"
        part_file.write_bytes(b"incomplete-stream")

        if stage in {"downloading", "playlist"}:
            started = self.started_events.get(job.job_id)
            if started is not None:
                started.set()
            gate = self.release_gates.get(job.job_id)
            if gate is not None:
                gate.wait(timeout=3.0)
            if cancel_event is not None and cancel_event.is_set():
                self.cleanup_partial_files(job)
                job.transition_to(JobStatus.CANCELLED, error_message="Cancelled during download.")
                raise DownloadCancelledError("Cancelled during download")

        if stage == "postprocessing":
            job.transition_to(JobStatus.PROCESSING)
            started = self.started_events.get(job.job_id)
            if started is not None:
                started.set()
            gate = self.release_gates.get(job.job_id)
            if gate is not None:
                gate.wait(timeout=3.0)
            if cancel_event is not None and cancel_event.is_set():
                self.cleanup_partial_files(job)
                job.transition_to(JobStatus.CANCELLED, error_message="Cancelled during post-processing.")
                raise DownloadCancelledError("Cancelled during post-processing")

        final_file = job.destination_dir / f"item_{job.job_id}.mp4"
        if part_file.exists():
            part_file.unlink()
        final_file.write_bytes(b"complete-media-file")
        job.output_path = final_file
        job.file_size_bytes = final_file.stat().st_size
        job.transition_to(JobStatus.COMPLETED)
        return job


def test_1_cancel_queued_job_before_execution(tmp_path: Path) -> None:
    dl = CancellationTestDownloadService()
    queue = QueueService(download_service=dl, auto_start=False)
    job = queue.add_job("https://www.youtube.com/watch?v=cancel_q_001", destination_dir=tmp_path)
    assert job.status == JobStatus.QUEUED

    assert queue.cancel_job(job.job_id) is True
    assert job.status == JobStatus.CANCELLED

    # Starting the queue after cancellation must not execute the cancelled job
    queue.start_queue()
    assert queue.wait_for_idle(timeout=2.0) is True
    assert job.status == JobStatus.CANCELLED
    assert job.output_path is None
    queue.shutdown()


def test_2_and_4_cancel_active_download_during_progress_and_playlist_callbacks(tmp_path: Path) -> None:
    bus = EventBus()
    service = DownloadService(ffmpeg_service=StubFFmpegService(True), event_bus=bus)
    job = DownloadJob(
        url="https://www.youtube.com/playlist?list=PLcancel_playlist_4",
        destination_dir=tmp_path,
        playlist_mode=PlaylistMode.FULL_PLAYLIST,
    )
    job.transition_to(JobStatus.STARTING)
    job.transition_to(JobStatus.DOWNLOADING)

    cancel_ev = threading.Event()
    pause_ev = threading.Event()
    hook = service._make_progress_hook(job, cancel_ev, pause_ev)

    # First playlist item reports progress
    hook(
        {
            "status": "downloading",
            "downloaded_bytes": 500,
            "total_bytes": 1000,
            "info_dict": {"playlist_index": 1, "n_entries": 3, "playlist_title": "Test Playlist"},
        }
    )
    assert job.playlist_index == 1
    assert job.playlist_total == 3

    # User triggers cancel mid-playlist download
    cancel_ev.set()
    with pytest.raises(DownloadCancelledError):
        hook(
            {
                "status": "downloading",
                "downloaded_bytes": 750,
                "total_bytes": 1000,
                "info_dict": {"playlist_index": 2, "n_entries": 3},
            }
        )


def test_3_and_5_cancel_during_metadata_and_postprocessing(tmp_path: Path) -> None:
    dl = CancellationTestDownloadService()
    queue = QueueService(download_service=dl, max_concurrent_downloads=2, auto_start=False)

    job_meta = queue.add_job("https://www.youtube.com/watch?v=cancel_meta3", destination_dir=tmp_path)
    job_post = queue.add_job("https://www.youtube.com/watch?v=cancel_post5", destination_dir=tmp_path)
    started_meta, gate_meta = dl.prepare_job_sync(job_meta.job_id, stage="metadata")
    started_post, gate_post = dl.prepare_job_sync(job_post.job_id, stage="postprocessing")

    queue.start_queue()
    assert started_meta.wait(timeout=2.0) is True
    assert started_post.wait(timeout=2.0) is True
    assert job_post.status == JobStatus.PROCESSING

    # Also test DownloadService._make_postprocessor_hook directly
    post_cancel_ev = threading.Event()
    post_cancel_ev.set()
    pp_hook = dl._make_postprocessor_hook(job_post, post_cancel_ev, threading.Event())
    with pytest.raises(DownloadCancelledError):
        pp_hook({"status": "started", "postprocessor": "FFmpegExtractAudio"})

    assert queue.cancel_job(job_meta.job_id) is True
    assert queue.cancel_job(job_post.job_id) is True
    gate_meta.set()
    gate_post.set()
    assert queue.wait_for_idle(timeout=3.0) is True

    assert job_meta.status == JobStatus.CANCELLED
    assert job_post.status == JobStatus.CANCELLED
    queue.shutdown()


def test_6_cancel_one_job_while_another_continues(tmp_path: Path) -> None:
    dl = CancellationTestDownloadService()
    queue = QueueService(download_service=dl, max_concurrent_downloads=2, auto_start=False)

    job_cancel = queue.add_job("https://www.youtube.com/watch?v=cancel_one06", destination_dir=tmp_path)
    job_keep = queue.add_job("https://www.youtube.com/watch?v=keep_one_006", destination_dir=tmp_path)
    started_c, gate_c = dl.prepare_job_sync(job_cancel.job_id)
    started_k, gate_k = dl.prepare_job_sync(job_keep.job_id)

    queue.start_queue()
    assert started_c.wait(timeout=2.0) is True
    assert started_k.wait(timeout=2.0) is True

    assert queue.cancel_job(job_cancel.job_id) is True
    gate_c.set()
    gate_k.set()
    assert queue.wait_for_idle(timeout=3.0) is True

    assert job_cancel.status == JobStatus.CANCELLED
    assert job_keep.status == JobStatus.COMPLETED
    assert job_keep.output_path is not None and job_keep.output_path.exists()
    queue.shutdown()


def test_7_cancel_all_active_and_queued_jobs(tmp_path: Path) -> None:
    dl = CancellationTestDownloadService()
    queue = QueueService(download_service=dl, max_concurrent_downloads=1, auto_start=False)

    job1 = queue.add_job("https://www.youtube.com/watch?v=cancel_all_1", destination_dir=tmp_path)
    job2 = queue.add_job("https://www.youtube.com/watch?v=cancel_all_2", destination_dir=tmp_path)
    job3 = queue.add_job("https://www.youtube.com/watch?v=cancel_all_3", destination_dir=tmp_path)
    started1, gate1 = dl.prepare_job_sync(job1.job_id)

    queue.start_queue()
    assert started1.wait(timeout=2.0) is True

    for j in (job1, job2, job3):
        assert queue.cancel_job(j.job_id) is True
    gate1.set()
    assert queue.wait_for_idle(timeout=3.0) is True

    assert job1.status == JobStatus.CANCELLED
    assert job2.status == JobStatus.CANCELLED
    assert job3.status == JobStatus.CANCELLED
    queue.shutdown()


def test_8_and_9_repeated_cancel_calls_and_cancel_after_completion_preserves_files(tmp_path: Path) -> None:
    dl = CancellationTestDownloadService()
    queue = QueueService(download_service=dl, auto_start=False)

    job_c = queue.add_job("https://www.youtube.com/watch?v=repeat_can08", destination_dir=tmp_path)
    assert queue.cancel_job(job_c.job_id) is True
    # Repeated cancel on already-cancelled job returns False without throwing
    assert queue.cancel_job(job_c.job_id) is False
    assert job_c.status == JobStatus.CANCELLED

    # Complete a job and attempt to cancel it afterwards
    job_done = queue.add_job("https://www.youtube.com/watch?v=done_nocan09", destination_dir=tmp_path)
    queue.start_queue()
    assert queue.wait_for_idle(timeout=3.0) is True
    assert job_done.status == JobStatus.COMPLETED
    assert job_done.output_path is not None and job_done.output_path.exists()

    assert queue.cancel_job(job_done.job_id) is False
    assert job_done.status == JobStatus.COMPLETED
    assert job_done.output_path.exists() is True
    queue.shutdown()


def test_10_and_11_partial_part_cleanup_preserves_completed_files(tmp_path: Path) -> None:
    service = DownloadService(ffmpeg_service=StubFFmpegService(True))
    completed_mp4 = tmp_path / "Already Downloaded Video.mp4"
    completed_mp3 = tmp_path / "Already Downloaded Audio.mp3"
    part_1 = tmp_path / "InProgress Video.mp4.part"
    part_frag = tmp_path / "InProgress Video.mp4.part-Frag4"
    ytdl_meta = tmp_path / "InProgress Video.mp4.ytdl"

    completed_mp4.write_bytes(b"precious-completed-video")
    completed_mp3.write_bytes(b"precious-completed-audio")
    part_1.write_bytes(b"partial-1")
    part_frag.write_bytes(b"partial-frag")
    ytdl_meta.write_bytes(b"ytdl-state")

    job = DownloadJob(
        url="https://www.youtube.com/watch?v=cleanup_10_11",
        destination_dir=tmp_path,
    )
    removed = service.cleanup_partial_files(job)
    assert removed == 3
    assert part_1.exists() is False
    assert part_frag.exists() is False
    assert ytdl_meta.exists() is False
    assert completed_mp4.exists() is True
    assert completed_mp4.read_bytes() == b"precious-completed-video"
    assert completed_mp3.exists() is True


def test_12_cancelled_job_can_be_retried_cleanly(tmp_path: Path) -> None:
    dl = CancellationTestDownloadService()
    queue = QueueService(download_service=dl, auto_start=False)

    job = queue.add_job("https://www.youtube.com/watch?v=retry_canc12", destination_dir=tmp_path)
    started, gate = dl.prepare_job_sync(job.job_id)

    queue.start_queue()
    assert started.wait(timeout=2.0) is True
    assert queue.cancel_job(job.job_id) is True
    gate.set()
    assert queue.wait_for_idle(timeout=3.0) is True
    assert job.status == JobStatus.CANCELLED

    # Clear gate and retry the cancelled job
    dl.release_gates.pop(job.job_id, None)
    retried = queue.retry_job(job.job_id, start_immediately=True)
    assert queue.wait_for_idle(timeout=3.0) is True
    assert retried.status == JobStatus.COMPLETED
    assert retried.output_path is not None and retried.output_path.exists()
    queue.shutdown()
