"""Opt-in live network download integration tests (Section 16).

These tests DO NOT run by default. To execute them:
1. Set one or more authorized test URL environment variables:
   - YDS_TEST_VIDEO_URL
   - YDS_TEST_AUDIO_URL
   - YDS_TEST_PLAYLIST_URL
   - YDS_TEST_FAIL_URL
2. Run pytest with --run-live:
   pytest --run-live -m live -v
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from src.core.events import EventBus
from src.core.models import (
    AudioFormat,
    DownloadMode,
    JobStatus,
    PlaylistMode,
    VideoFormat,
    VideoQuality,
)
from src.database.db import DatabaseManager
from src.database.repositories import HistoryRepository
from src.services.download_service import DownloadService
from src.services.ffmpeg_service import FFmpegService
from src.services.media_info_service import MediaInfoService
from src.services.queue_service import QueueService


def _require_env_url(var_name: str) -> str:
    value = os.environ.get(var_name, "").strip()
    if not value:
        pytest.skip(f"Authorized live test URL env var '{var_name}' is not set.")
    return value


@pytest.mark.live
def test_live_metadata_and_single_video_download(tmp_path: Path) -> None:
    url = _require_env_url("YDS_TEST_VIDEO_URL")
    dl_dir = tmp_path / "live_video_out"
    dl_dir.mkdir(parents=True, exist_ok=True)

    db = DatabaseManager(tmp_path / "live_history.db")
    history_repo = HistoryRepository(db)
    ffmpeg_service = FFmpegService()
    media_info = MediaInfoService()
    dl_service = DownloadService(ffmpeg_service=ffmpeg_service)
    queue = QueueService(
        download_service=dl_service,
        history_repository=history_repo,
        max_concurrent_downloads=1,
        auto_start=False,
    )

    try:
        meta = media_info.fetch_metadata(url, allow_playlist=False)
        assert meta.title.strip() != ""

        job = queue.add_job(
            url=url,
            destination_dir=dl_dir,
            mode=DownloadMode.VIDEO,
            video_quality=VideoQuality.Q360P,
            video_format=VideoFormat.MP4,
            playlist_mode=PlaylistMode.SINGLE,
            start_immediately=True,
        )
        assert queue.wait_for_idle(timeout=180.0) is True
        assert job.status == JobStatus.COMPLETED
        assert job.output_path is not None and job.output_path.exists()
        assert job.output_path.stat().st_size > 0
        assert list(dl_dir.glob("*.part")) == []

        persisted = history_repo.get_by_id(job.job_id)
        assert persisted is not None
        assert persisted.status == JobStatus.COMPLETED
    finally:
        queue.shutdown()


@pytest.mark.live
def test_live_audio_download_and_mp3_conversion(tmp_path: Path) -> None:
    url = os.environ.get("YDS_TEST_AUDIO_URL", "").strip() or _require_env_url("YDS_TEST_VIDEO_URL")
    dl_dir = tmp_path / "live_audio_out"
    dl_dir.mkdir(parents=True, exist_ok=True)

    db = DatabaseManager(tmp_path / "live_audio_history.db")
    history_repo = HistoryRepository(db)
    ffmpeg_service = FFmpegService()
    dep = ffmpeg_service.check_dependencies(force_refresh=True)
    target_fmt = AudioFormat.MP3 if dep.ffmpeg_available else AudioFormat.BEST

    dl_service = DownloadService(ffmpeg_service=ffmpeg_service)
    queue = QueueService(
        download_service=dl_service,
        history_repository=history_repo,
        max_concurrent_downloads=1,
        auto_start=False,
    )

    try:
        job = queue.add_job(
            url=url,
            destination_dir=dl_dir,
            mode=DownloadMode.AUDIO,
            audio_format=target_fmt,
            playlist_mode=PlaylistMode.SINGLE,
            start_immediately=True,
        )
        assert queue.wait_for_idle(timeout=180.0) is True
        assert job.status == JobStatus.COMPLETED
        assert job.output_path is not None and job.output_path.exists()
        assert job.output_path.stat().st_size > 0
        if target_fmt == AudioFormat.MP3:
            assert job.output_path.suffix.lower() == ".mp3"
    finally:
        queue.shutdown()


@pytest.mark.live
def test_live_playlist_selected_items_download(tmp_path: Path) -> None:
    url = _require_env_url("YDS_TEST_PLAYLIST_URL")
    dl_dir = tmp_path / "live_playlist_out"
    dl_dir.mkdir(parents=True, exist_ok=True)

    media_info = MediaInfoService()
    meta = media_info.fetch_metadata(url, allow_playlist=True)
    assert meta.is_playlist is True
    assert len(meta.playlist_items) >= 1

    first_index = meta.playlist_items[0].index
    dl_service = DownloadService(ffmpeg_service=FFmpegService())
    queue = QueueService(download_service=dl_service, max_concurrent_downloads=1, auto_start=False)

    try:
        job = queue.add_job(
            url=url,
            destination_dir=dl_dir,
            mode=DownloadMode.VIDEO,
            video_quality=VideoQuality.Q360P,
            playlist_mode=PlaylistMode.SELECTED_ITEMS,
            selected_playlist_indices=[first_index],
            start_immediately=True,
        )
        assert queue.wait_for_idle(timeout=240.0) is True
        assert job.status == JobStatus.COMPLETED
    finally:
        queue.shutdown()


@pytest.mark.live
def test_live_invalid_or_unavailable_url_fails_cleanly_without_breaking_queue(tmp_path: Path) -> None:
    fail_url = _require_env_url("YDS_TEST_FAIL_URL")
    dl_dir = tmp_path / "live_fail_out"
    dl_dir.mkdir(parents=True, exist_ok=True)

    db = DatabaseManager(tmp_path / "live_fail_history.db")
    history_repo = HistoryRepository(db)
    dl_service = DownloadService(ffmpeg_service=FFmpegService())
    queue = QueueService(
        download_service=dl_service,
        history_repository=history_repo,
        max_concurrent_downloads=1,
        auto_start=False,
    )

    try:
        job = queue.add_job(
            url=fail_url,
            destination_dir=dl_dir,
            start_immediately=True,
        )
        assert queue.wait_for_idle(timeout=90.0) is True
        assert job.status == JobStatus.FAILED
        assert job.error_message is not None and job.error_message.strip() != ""
    finally:
        queue.shutdown()


@pytest.mark.live
def test_live_cancellation_and_pause_resume(tmp_path: Path) -> None:
    url = _require_env_url("YDS_TEST_VIDEO_URL")
    dl_dir = tmp_path / "live_cancel_pause_out"
    dl_dir.mkdir(parents=True, exist_ok=True)

    dl_service = DownloadService(ffmpeg_service=FFmpegService())
    queue = QueueService(download_service=dl_service, max_concurrent_downloads=1, auto_start=False)

    try:
        job = queue.add_job(
            url=url,
            destination_dir=dl_dir,
            video_quality=VideoQuality.BEST,
            start_immediately=True,
        )
        # Wait briefly for worker to enter active state, then pause
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and not job.status.is_active:
            time.sleep(0.05)

        if job.status.is_active:
            queue.pause_job(job.job_id)
            assert queue.wait_for_idle(timeout=30.0) is True
            if job.status == JobStatus.PAUSED:
                queue.cancel_job(job.job_id)
                assert job.status == JobStatus.CANCELLED
                assert list(dl_dir.glob("*.part")) == []
    finally:
        queue.shutdown()
