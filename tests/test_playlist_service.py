"""Comprehensive unit tests for playlist metadata, selection modes, bounds, and execution."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from src.core.exceptions import (
    DownloadCancelledError,
    DownloadError,
    MetadataError,
    ValidationError,
)
from src.core.models import (
    DownloadJob,
    DownloadMode,
    JobStatus,
    OverwriteBehavior,
    PlaylistMode,
)
from src.services.download_service import DownloadService
from src.services.media_info_service import MAX_PLAYLIST_PREVIEW_ITEMS, MediaInfoService
from tests.test_download_service import StubFFmpegService


def test_playlist_metadata_valid_unavailable_and_bounded() -> None:
    """Verify playlist parsing, unavailable item detection, and MAX_PLAYLIST_PREVIEW_ITEMS bound."""
    large_entries = [
        {
            "id": f"vid_{i}",
            "title": "[Deleted video]" if i == 2 else f"Lecture {i}",
            "duration": 60 * i,
            "url": f"https://www.youtube.com/watch?v=vid_{i}",
        }
        for i in range(1, 260)
    ]

    class FakeLargePlaylistYDL:
        def __init__(self, opts: dict[str, Any]) -> None:
            self.opts = opts

        def __enter__(self) -> FakeLargePlaylistYDL:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def extract_info(self, url: str, download: bool = False) -> dict[str, Any]:
            assert self.opts["extract_flat"] == "in_playlist"
            assert self.opts["playlistend"] == MAX_PLAYLIST_PREVIEW_ITEMS
            return {
                "_type": "playlist",
                "id": "PL_LARGE",
                "title": "University Course",
                "uploader": "CS Dept",
                "playlist_count": 259,
                "entries": large_entries,
            }

    service = MediaInfoService(ydl_factory=FakeLargePlaylistYDL)
    meta = service.fetch_metadata("https://www.youtube.com/playlist?list=PL_LARGE")
    assert meta.is_playlist is True
    assert meta.playlist_count == 259
    # Bounded to MAX_PLAYLIST_PREVIEW_ITEMS (200)
    assert len(meta.playlist_items) == MAX_PLAYLIST_PREVIEW_ITEMS
    assert meta.playlist_items[0].available is True
    assert meta.playlist_items[1].available is False  # index 2 was "[Deleted video]"


def test_playlist_metadata_private_or_failed_raises_metadata_error() -> None:
    class FakePrivatePlaylistYDL:
        def __init__(self, opts: dict[str, Any]) -> None:
            pass

        def __enter__(self) -> FakePrivatePlaylistYDL:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def extract_info(self, url: str, download: bool = False) -> dict[str, Any]:
            raise RuntimeError("ERROR: This playlist is private. Sign in to access.")

    service = MediaInfoService(ydl_factory=FakePrivatePlaylistYDL)
    with pytest.raises(MetadataError) as exc_info:
        service.fetch_metadata("https://www.youtube.com/playlist?list=PL_PRIVATE")
    assert "private" in exc_info.value.user_message.lower()


def test_playlist_modes_ydl_options_construction(tmp_path: Path) -> None:
    dl_service = DownloadService(ffmpeg_service=StubFFmpegService(True))

    # 1. Single video mode on a playlist URL sets noplaylist=True
    single_job = DownloadJob(
        url="https://www.youtube.com/watch?v=vid_1&list=PL_123",
        destination_dir=tmp_path,
        playlist_mode=PlaylistMode.SINGLE,
    )
    single_opts = dl_service.build_ydl_options(single_job)
    assert single_opts["noplaylist"] is True
    assert "playlist_items" not in single_opts

    # 2. Full playlist mode sets noplaylist=False and ignoreerrors="only_download"
    full_job = DownloadJob(
        url="https://www.youtube.com/playlist?list=PL_123",
        destination_dir=tmp_path,
        playlist_mode=PlaylistMode.FULL_PLAYLIST,
    )
    full_opts = dl_service.build_ydl_options(full_job)
    assert full_opts["noplaylist"] is False
    assert full_opts["ignoreerrors"] == "only_download"

    # 3. Selected items mode sets playlist_items="1,3,5"
    selected_job = DownloadJob(
        url="https://www.youtube.com/playlist?list=PL_123",
        destination_dir=tmp_path,
        playlist_mode=PlaylistMode.SELECTED_ITEMS,
        selected_playlist_indices=[5, 1, 3, 1],
    )
    selected_opts = dl_service.build_ydl_options(selected_job)
    assert selected_opts["noplaylist"] is False
    assert selected_opts["playlist_items"] == "1,3,5"

    # 4. Selected items mode with empty or invalid indices raises ValidationError
    bad_selected_job = DownloadJob(
        url="https://www.youtube.com/playlist?list=PL_123",
        destination_dir=tmp_path,
        playlist_mode=PlaylistMode.SELECTED_ITEMS,
        selected_playlist_indices=[],
    )
    with pytest.raises(ValidationError):
        dl_service.build_ydl_options(bad_selected_job)


def test_empty_playlist_execution_raises_download_error(tmp_path: Path) -> None:
    class FakeEmptyPlaylistYDL:
        def __init__(self, params: dict[str, Any]) -> None:
            self.params = params

        def __enter__(self) -> FakeEmptyPlaylistYDL:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def extract_info(self, url: str, download: bool = False) -> dict[str, Any]:
            return {"_type": "playlist", "id": "PL_EMPTY", "title": "Empty List", "entries": []}

        def process_ie_result(self, info: dict[str, Any], download: bool = True) -> dict[str, Any]:
            return info

    dl_service = DownloadService(
        ffmpeg_service=StubFFmpegService(True),
        ydl_factory=FakeEmptyPlaylistYDL,
    )
    job = DownloadJob(
        url="https://www.youtube.com/playlist?list=PL_EMPTY",
        destination_dir=tmp_path,
        playlist_mode=PlaylistMode.FULL_PLAYLIST,
    )
    with pytest.raises(DownloadError) as exc_info:
        dl_service.execute_job(job)
    assert "empty" in exc_info.value.user_message.lower()
    assert job.status == JobStatus.FAILED


def test_playlist_progress_and_mid_playlist_cancellation(tmp_path: Path) -> None:
    cancel_event = threading.Event()

    class FakePlaylistDownloadYDL:
        def __init__(self, params: dict[str, Any]) -> None:
            self.params = params

        def __enter__(self) -> FakePlaylistDownloadYDL:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def extract_info(self, url: str, download: bool = False) -> dict[str, Any]:
            return {
                "_type": "playlist",
                "id": "PL_3",
                "title": "Three Videos",
                "entries": [{"id": "v1"}, {"id": "v2"}, {"id": "v3"}],
            }

        def process_ie_result(self, info: dict[str, Any], download: bool = True) -> dict[str, Any]:
            hooks = self.params.get("progress_hooks", [])
            # Item 1 finishes
            file1 = tmp_path / "Item1.mp4"
            file1.write_bytes(b"item1_data")
            for h in hooks:
                h(
                    {
                        "status": "downloading",
                        "downloaded_bytes": 100,
                        "total_bytes": 100,
                        "filename": str(file1),
                        "info_dict": {"title": "Item 1", "playlist_index": 1, "n_entries": 3},
                    }
                )
            # Item 2 starts and user cancels halfway
            part2 = tmp_path / "Item2.mp4.part"
            part2.write_bytes(b"partial_item2")
            cancel_event.set()
            for h in hooks:
                h(
                    {
                        "status": "downloading",
                        "downloaded_bytes": 50,
                        "total_bytes": 100,
                        "tmpfilename": str(part2),
                        "info_dict": {"title": "Item 2", "playlist_index": 2, "n_entries": 3},
                    }
                )
            return info

    dl_service = DownloadService(
        ffmpeg_service=StubFFmpegService(True),
        ydl_factory=FakePlaylistDownloadYDL,
    )
    job = DownloadJob(
        url="https://www.youtube.com/playlist?list=PL_3",
        destination_dir=tmp_path,
        playlist_mode=PlaylistMode.FULL_PLAYLIST,
        overwrite_behavior=OverwriteBehavior.RENAME,
    )
    with pytest.raises(DownloadCancelledError):
        dl_service.execute_job(job, cancel_event=cancel_event)

    assert job.status == JobStatus.CANCELLED
    assert job.playlist_index == 1
    assert job.playlist_total == 3
    # Item 1 completed file is preserved, while Item 2 .part file is cleaned up if tracked
    assert (tmp_path / "Item1.mp4").exists()
