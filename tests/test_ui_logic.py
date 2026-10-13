"""Automated GUI and UI logic tests for MainWindow, QueueView, HistoryView, and SettingsView (Section 17)."""

from __future__ import annotations

import io
import threading
from pathlib import Path
from typing import Any

from PIL import Image
import pytest

from src.core.exceptions import AppError
from src.core.events import AppEvent, EventBus, EventType
from src.core.models import (
    AudioFormat,
    CaptionSource,
    CaptionTrackInfo,
    DependencyStatus,
    DownloadJob,
    DownloadMode,
    JobStatus,
    LocalMediaInfo,
    MediaMetadata,
    PlaylistItemInfo,
    PlaylistMode,
    SourceType,
    SubtitleFormat,
    ThemeMode,
)
from src.database.db import DatabaseManager
from src.database.repositories import HistoryRepository, SettingsRepository
from src.services.download_service import DownloadService
from src.services.ffmpeg_service import FFmpegService
from src.services.media_info_service import MediaInfoService
from src.services.queue_service import QueueService
from src.services.settings_service import SettingsService
from src.ui.components import (
    STATUS_BADGE_COLORS,
    STATUS_LABELS,
    format_bytes,
    format_eta,
    format_speed,
)
from src.ui.dialogs import CaptionSelectionDialog
from src.ui.main_window import MainWindow


def _make_dummy_jpeg(color: tuple[int, int, int] = (255, 0, 0)) -> bytes:
    bio = io.BytesIO()
    img = Image.new("RGB", (32, 32), color=color)
    img.save(bio, format="JPEG")
    return bio.getvalue()


class ControllableFFmpegService(FFmpegService):
    """FFmpegService stub with toggleable FFmpeg/FFprobe availability for UI testing."""

    def __init__(self, ffmpeg_ok: bool = True, ffprobe_ok: bool = True) -> None:
        super().__init__()
        self.ffmpeg_ok = ffmpeg_ok
        self.ffprobe_ok = ffprobe_ok

    def check_dependencies(self, force_refresh: bool = False) -> DependencyStatus:
        return DependencyStatus(
            python_version="3.13.14",
            yt_dlp_available=True,
            yt_dlp_version="2026.8.19",
            ffmpeg_available=self.ffmpeg_ok,
            ffprobe_available=self.ffprobe_ok,
            ffmpeg_path="C:/ffmpeg/bin/ffmpeg.exe" if self.ffmpeg_ok else None,
            ffprobe_path="C:/ffmpeg/bin/ffprobe.exe" if self.ffprobe_ok else None,
            ffmpeg_version="ffmpeg version 7.0" if self.ffmpeg_ok else None,
            ffprobe_version="ffprobe version 7.0" if self.ffprobe_ok else None,
        )

    def probe_local_media(self, file_path: Path | str) -> LocalMediaInfo:
        resolved = Path(file_path).resolve()
        return LocalMediaInfo(
            file_path=resolved,
            duration_seconds=180.0,
            has_video=True,
            has_audio=True,
            video_codec="h264",
            audio_codec="aac",
            container_format="mp4",
        )


class FastUIDownloadService(DownloadService):
    """Non-network DownloadService stub for UI workflow testing."""

    def __init__(self, ffmpeg_service: FFmpegService) -> None:
        super().__init__(ffmpeg_service=ffmpeg_service)
        self.hold_active = False
        self.gate = threading.Event()

    def execute_job(
        self,
        job: DownloadJob,
        cancel_event: threading.Event | None = None,
        pause_event: threading.Event | None = None,
        tracked_files: list[Path] | None = None,
    ) -> DownloadJob:
        job.transition_to(JobStatus.STARTING, preserve_progress=job.progress_percent > 0)
        job.transition_to(JobStatus.DOWNLOADING, preserve_progress=job.progress_percent > 0)
        job.progress_percent = 50.0
        job.downloaded_bytes = 512_000
        job.total_bytes = 1_024_000

        if self.hold_active:
            self.gate.wait(timeout=2.0)

        if cancel_event is not None and cancel_event.is_set():
            job.transition_to(JobStatus.CANCELLED, error_message="Cancelled by user.")
            return job

        if pause_event is not None and pause_event.is_set():
            job.transition_to(JobStatus.PAUSED)
            return job

        out = job.destination_dir / f"ui_{job.job_id}.mp4"
        out.write_bytes(b"ui-video-bytes")
        job.output_path = out
        job.file_size_bytes = out.stat().st_size
        job.transition_to(JobStatus.COMPLETED)
        return job


def test_ui_formatters_and_status_badges_cover_all_job_statuses() -> None:
    assert format_bytes(None) == "—"
    assert format_bytes(-50) == "—"
    assert format_bytes(0) == "0 B"
    assert format_bytes(1024) == "1.0 KB"
    assert format_bytes(1024 * 1024 * 5) == "5.0 MB"

    assert format_speed(None) == "—"
    assert format_speed(0) == "—"
    assert format_speed(1024 * 512) == "512.0 KB/s"

    assert format_eta(None) == "—"
    assert format_eta(-1) == "—"
    assert format_eta(0) == "00:00"
    assert format_eta(65) == "01:05"
    assert format_eta(3661) == "1:01:01"

    for status in JobStatus:
        assert status in STATUS_LABELS
        assert status in STATUS_BADGE_COLORS


@pytest.mark.gui
def test_main_window_complete_user_workflows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise MainWindow navigation, modes, validation, queue actions, pause/resume, history, settings, and theme."""
    # Capture modal dialogs instead of blocking on user interaction
    dialog_calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "src.ui.main_window.show_error_dialog",
        lambda _p, title, msg, rem="": dialog_calls.append(("error", title, msg)),
    )
    monkeypatch.setattr(
        "src.ui.main_window.show_warning_dialog",
        lambda _p, title, msg: dialog_calls.append(("warning", title, msg)),
    )
    monkeypatch.setattr(
        "src.ui.main_window.ask_confirmation_dialog",
        lambda _p, title, msg: True,
    )
    monkeypatch.setattr(
        "src.ui.settings_view.show_info_dialog",
        lambda _p, title, msg: dialog_calls.append(("info", title, msg)),
    )
    monkeypatch.setattr(
        "src.ui.settings_view.ask_confirmation_dialog",
        lambda _p, title, msg: True,
    )
    monkeypatch.setattr(
        "src.ui.history_view.ask_confirmation_dialog",
        lambda _p, title, msg: True,
    )

    data_dir = tmp_path / "appdata"
    dl_dir = tmp_path / "downloads"
    dl_dir.mkdir(parents=True, exist_ok=True)
    log_path = data_dir / "logs" / "app.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("init log\n", encoding="utf-8")

    db = DatabaseManager(data_dir / "history.db")
    settings_repo = SettingsRepository(db)
    history_repo = HistoryRepository(db)
    ffmpeg_service = ControllableFFmpegService(ffmpeg_ok=True, ffprobe_ok=True)
    settings_service = SettingsService(
        settings_path=data_dir / "settings.json",
        repository=settings_repo,
        default_download_dir=dl_dir,
    )
    settings_service.load()

    bus = EventBus()
    dl_service = FastUIDownloadService(ffmpeg_service=ffmpeg_service)
    media_info_service = MediaInfoService()
    queue_service = QueueService(
        download_service=dl_service,
        history_repository=history_repo,
        event_bus=bus,
        max_concurrent_downloads=2,
        auto_start=False,
    )

    win = MainWindow(
        settings_service=settings_service,
        ffmpeg_service=ffmpeg_service,
        media_info_service=media_info_service,
        queue_service=queue_service,
        history_repo=history_repo,
        event_bus=bus,
        app_data_dir=data_dir,
        log_file_path=log_path,
    )
    win.update_idletasks()

    try:
        # 1. Navigation tabs
        for tab in ("Download History", "Settings", "Downloader & Queue"):
            win._nav_segmented.set(tab)
            win._on_navigation_changed(tab)
            win.update_idletasks()

        # 2. Switch between Video and Audio Only modes
        win._mode_selector.set("Audio Only")
        win._on_mode_changed("Audio Only")
        win._mode_selector.set("Video")
        win._on_mode_changed("Video")

        # 3. Verify FFmpeg missing restricts audio conversion choices to best/m4a
        ffmpeg_service.ffmpeg_ok = False
        dep_missing = win._refresh_dependency_status_ui()
        assert dep_missing.ffmpeg_available is False
        assert set(win._audio_format_menu.cget("values")) == {
            AudioFormat.BEST.value,
            AudioFormat.M4A.value,
        }

        # Restore FFmpeg availability
        ffmpeg_service.ffmpeg_ok = True
        dep_ready = win._refresh_dependency_status_ui()
        assert dep_ready.ffmpeg_available is True
        assert AudioFormat.MP3.value in win._audio_format_menu.cget("values")

        # 4. Invalid URL submission shows error dialog and does not add to queue
        dialog_calls.clear()
        win._url_textbox.delete("1.0", "end")
        win._url_textbox.insert("1.0", "ftp://not-youtube.example.com/bad")
        win._submit_urls(start_immediately=False)
        assert any(call[0] == "error" for call in dialog_calls)
        assert queue_service.list_jobs() == []

        # 5. Repeated metadata fetching in the same session without restart (URL A -> URL B -> URL C)
        meta_a = MediaMetadata(
            url="https://www.youtube.com/watch?v=fetch_alpha_01",
            webpage_url="https://www.youtube.com/watch?v=fetch_alpha_01",
            video_id="fetch_alpha_01",
            title="Alpha Video Title",
            uploader="Alpha Creator",
            duration_seconds=120,
            thumbnail_url="https://example.com/thumb_a.jpg",
            available_heights=[1080, 720],
            caption_tracks=[
                CaptionTrackInfo(lang_code="en", name="English", source=CaptionSource.MANUAL),
                CaptionTrackInfo(lang_code="es", name="Spanish", source=CaptionSource.MANUAL),
            ],
        )
        thumb_a = _make_dummy_jpeg((200, 50, 50))

        meta_b = MediaMetadata(
            url="https://www.youtube.com/watch?v=fetch_bravo_02",
            webpage_url="https://www.youtube.com/watch?v=fetch_bravo_02",
            video_id="fetch_bravo_02",
            title="Bravo Video Title",
            uploader="Bravo Creator",
            duration_seconds=300,
            thumbnail_url="https://example.com/thumb_b.jpg",
            available_heights=[2160, 1080],
            caption_tracks=[
                CaptionTrackInfo(lang_code="fr", name="French", source=CaptionSource.MANUAL),
                CaptionTrackInfo(lang_code="de", name="German", source=CaptionSource.AUTOMATIC),
            ],
        )
        thumb_b = _make_dummy_jpeg((50, 200, 50))

        meta_c = MediaMetadata(
            url="https://www.youtube.com/watch?v=fetch_charlie_03",
            webpage_url="https://www.youtube.com/watch?v=fetch_charlie_03",
            video_id="fetch_charlie_03",
            title="Charlie Video Title",
            uploader="Charlie Creator",
            duration_seconds=45,
            thumbnail_url="https://example.com/thumb_c.jpg",
            available_heights=[720, 480],
            caption_tracks=[
                CaptionTrackInfo(lang_code="ja", name="Japanese", source=CaptionSource.MANUAL),
            ],
        )
        thumb_c = _make_dummy_jpeg((50, 50, 200))

        # 5a. Fetch URL A
        media_info_service.fetch_metadata = lambda *args, **kwargs: meta_a
        media_info_service.fetch_thumbnail_bytes = lambda *args, **kwargs: thumb_a
        win._url_textbox.delete("1.0", "end")
        win._url_textbox.insert("1.0", meta_a.url)
        win._start_metadata_fetch()
        win.update_idletasks()
        for evt in bus.poll_events(max_items=10):
            win._handle_single_event(evt)
        win.update_idletasks()

        assert win._current_metadata is not None
        assert win._current_metadata.video_id == "fetch_alpha_01"
        assert win._preview_card._title_label.cget("text") == "Alpha Video Title"
        assert win._trim_timeline.media_duration == 120.0
        assert win._fetch_info_btn.cget("state") == "normal"
        assert "English (en) [Manual]" in win._caption_track_menu.cget("values")
        assert "Spanish (es) [Manual]" in win._caption_track_menu.cget("values")

        # 5b. Fetch URL B (different URL, same session, no application restart)
        media_info_service.fetch_metadata = lambda *args, **kwargs: meta_b
        media_info_service.fetch_thumbnail_bytes = lambda *args, **kwargs: thumb_b
        win._url_textbox.delete("1.0", "end")
        win._url_textbox.insert("1.0", meta_b.url)
        win._start_metadata_fetch()
        win.update_idletasks()

        # Previous metadata invalidated while loading
        assert win._current_metadata is None

        for evt in bus.poll_events(max_items=10):
            win._handle_single_event(evt)
        win.update_idletasks()

        assert win._current_metadata is not None
        assert win._current_metadata.video_id == "fetch_bravo_02"
        assert win._preview_card._title_label.cget("text") == "Bravo Video Title"
        assert win._trim_timeline.media_duration == 300.0
        assert win._fetch_info_btn.cget("state") == "normal"
        assert "English (en) [Manual]" not in win._caption_track_menu.cget("values")
        assert "French (fr) [Manual]" in win._caption_track_menu.cget("values")

        # Toggling auto-captions includes German auto track
        win._include_auto_captions_var.set(True)
        win._on_auto_captions_filter_changed()
        assert "German (de) [Auto]" in win._caption_track_menu.cget("values")

        # 5c. Fetch URL C (third URL, same session)
        media_info_service.fetch_metadata = lambda *args, **kwargs: meta_c
        media_info_service.fetch_thumbnail_bytes = lambda *args, **kwargs: thumb_c
        win._url_textbox.delete("1.0", "end")
        win._url_textbox.insert("1.0", meta_c.url)
        win._start_metadata_fetch()
        win.update_idletasks()

        for evt in bus.poll_events(max_items=10):
            win._handle_single_event(evt)
        win.update_idletasks()

        assert win._current_metadata is not None
        assert win._current_metadata.video_id == "fetch_charlie_03"
        assert win._preview_card._title_label.cget("text") == "Charlie Video Title"
        assert win._trim_timeline.media_duration == 45.0
        assert win._fetch_info_btn.cget("state") == "normal"
        assert "Japanese (ja) [Manual]" in win._caption_track_menu.cget("values")

        # 5d. Monotonic request ID guards against out-of-order/stale worker responses
        worker_gate = threading.Event()
        media_info_service.fetch_metadata = lambda *a, **k: worker_gate.wait(timeout=2.0) and meta_a
        win._url_textbox.delete("1.0", "end")
        win._url_textbox.insert("1.0", "https://www.youtube.com/watch?v=slow_01")
        win._start_metadata_fetch()
        stale_id = win._metadata_request_id

        win._url_textbox.delete("1.0", "end")
        win._url_textbox.insert("1.0", "https://www.youtube.com/watch?v=fast_02")
        win._start_metadata_fetch()
        active_id = win._metadata_request_id
        assert active_id > stale_id

        # Stale response from slow_01 arrives
        bus.publish(
            EventType.METADATA_FETCHED,
            metadata=meta_a,
            thumbnail_bytes=None,
            request_id=stale_id,
        )
        for evt in bus.poll_events(max_items=10):
            win._handle_single_event(evt)
        assert win._current_metadata is None

        # Active response arrives
        bus.publish(
            EventType.METADATA_FETCHED,
            metadata=meta_b,
            thumbnail_bytes=None,
            request_id=active_id,
        )
        for evt in bus.poll_events(max_items=10):
            win._handle_single_event(evt)
        assert win._current_metadata is not None
        assert win._current_metadata.video_id == "fetch_bravo_02"
        worker_gate.set()

        # 5e. Error recovery and button state restoration
        def _failing_fetch(*args, **kwargs):
            raise AppError("Simulated metadata error", remedy="Check URL")
        media_info_service.fetch_metadata = _failing_fetch
        win._url_textbox.delete("1.0", "end")
        win._url_textbox.insert("1.0", "https://www.youtube.com/watch?v=err_url_99")
        win._start_metadata_fetch()
        win.update_idletasks()
        for evt in bus.poll_events(max_items=10):
            win._handle_single_event(evt)
        win.update_idletasks()
        assert win._current_metadata is None
        assert win._fetch_info_btn.cget("state") == "normal"
        assert "Metadata error" in win._preview_card._title_label.cget("text")

        # 5f. Simulated metadata fetched event updates preview card and playlist state
        playlist_meta = MediaMetadata(
            url="https://www.youtube.com/playlist?list=PLui_test_12345",
            webpage_url="https://www.youtube.com/playlist?list=PLui_test_12345",
            video_id="PLui_test_12345",
            title="UI Test Playlist",
            uploader="QA Channel",
            duration_seconds=120,
            is_playlist=True,
            playlist_count=2,
            playlist_items=[
                PlaylistItemInfo(
                    index=1,
                    video_id="v1",
                    title="Part 1",
                    url="https://www.youtube.com/watch?v=v1",
                    duration_seconds=60,
                ),
                PlaylistItemInfo(
                    index=2,
                    video_id="v2",
                    title="Part 2",
                    url="https://www.youtube.com/watch?v=v2",
                    duration_seconds=60,
                ),
            ],
        )
        bus.publish(EventType.METADATA_FETCHED, metadata=playlist_meta, thumbnail_bytes=None)
        win._handle_single_event(bus.poll_events()[0])
        assert win._current_metadata is not None
        assert win._selected_playlist_mode == PlaylistMode.FULL_PLAYLIST

        # 6. Clear URL input and add two valid video URLs to the queue
        win._clear_url_input()
        win._url_textbox.insert(
            "1.0",
            "https://www.youtube.com/watch?v=ui_vid_0001\nhttps://www.youtube.com/watch?v=ui_vid_0002",
        )
        win._submit_urls(start_immediately=False)
        jobs = queue_service.list_jobs()
        assert len(jobs) == 2
        assert len(win._queue_view._cards) == 2

        # 7. Exercise Pause -> Resume and Cancel -> Retry via QueueView handlers
        job1, job2 = jobs[0], jobs[1]
        job1.transition_to(JobStatus.STARTING)
        job1.transition_to(JobStatus.DOWNLOADING)
        job1.progress_percent = 40.0
        job1.transition_to(JobStatus.PAUSED)
        win._queue_view.refresh_all()

        # Resume paused job1 and wait for completion
        win._queue_view._handle_resume_job(job1.job_id)
        assert queue_service.wait_for_idle(timeout=3.0) is True
        assert job1.status == JobStatus.COMPLETED
        assert job2.status == JobStatus.COMPLETED

        # 8. Exercise HistoryView search, status filter, and retry from history
        win._nav_segmented.set("Download History")
        win._on_navigation_changed("Download History")
        win._history_view._status_filter.set("Completed")
        win._history_view.refresh()
        entries = history_repo.list_entries(status_filter=JobStatus.COMPLETED)
        assert len(entries) == 2

        win._history_view._search_entry.delete(0, "end")
        win._history_view._search_entry.insert(0, "ui_vid_0001")
        win._history_view.refresh()

        # Re-queue from history
        win._handle_retry_from_history(entries[0])
        assert queue_service.wait_for_idle(timeout=3.0) is True

        # 9. Exercise SettingsView save, theme toggle, and reset to defaults
        win._nav_segmented.set("Settings")
        win._on_navigation_changed("Settings")
        win._settings_view._concurrency_menu.set("3")
        win._settings_view._theme_menu.set(ThemeMode.LIGHT.value)
        win._settings_view._handle_save()
        assert settings_service.settings.max_concurrent_downloads == 3
        assert settings_service.settings.theme_mode == ThemeMode.LIGHT

        win._toggle_theme_quick()
        assert settings_service.settings.theme_mode == ThemeMode.DARK

        win._settings_view._handle_reset_defaults()
        assert settings_service.settings.max_concurrent_downloads == 2

        # 10. Malformed event resilience on EventBus
        win._handle_single_event(AppEvent(event_type=EventType.JOB_PROGRESS, data={"job": None}))
        win._queue_view._handle_clear_completed()

        # 11. TrimTimelineWidget two-way synchronization and clamping
        win._nav_segmented.set("Downloader & Queue")
        win._on_navigation_changed("Downloader & Queue")
        win._source_selector.set("YouTube URL")
        win._on_source_type_changed("YouTube URL")
        win._mode_selector.set("Video")
        win._on_mode_changed("Video")

        timeline = win._trim_timeline
        timeline.set_enabled(True)
        timeline.set_media_duration(300.0)
        assert timeline.media_duration == 300.0
        assert timeline.clip_duration == 300.0

        timeline._start_entry.delete(0, "end")
        timeline._start_entry.insert(0, "00:30")
        timeline._end_entry.delete(0, "end")
        timeline._end_entry.insert(0, "02:00")
        assert timeline.apply_entry_inputs() is True
        assert timeline.start_seconds == 30.0
        assert timeline.end_seconds == 120.0
        assert timeline.clip_duration == 90.0

        # Moving handle updates text entry
        timeline.set_handle_seconds("start", 45.0)
        assert timeline.get_start_text() == "00:45"
        assert timeline.start_seconds == 45.0

        # Clamping: moving start past end clamps to end - MIN_CLIP_SECONDS
        timeline.set_handle_seconds("start", 250.0)
        assert timeline.start_seconds <= 120.0 - timeline.MIN_CLIP_SECONDS
        assert timeline.start_seconds < timeline.end_seconds

        # 12. Captions Only mode UI switching, track menu population, and FFmpeg-free queuing
        dialog_calls.clear()
        win._mode_selector.set("Captions Only")
        win._on_mode_changed("Captions Only")

        # In Captions Only mode, captions row is active and trim timeline is hidden
        assert win._captions_row.winfo_ismapped() or win._captions_row.grid_info() != {}
        assert win._trim_timeline.grid_info() == {}

        # Populate caption track menu from discovered metadata tracks
        meta_with_captions = MediaMetadata(
            url="https://www.youtube.com/watch?v=caption_ui_test",
            webpage_url="https://www.youtube.com/watch?v=caption_ui_test",
            video_id="caption_ui_test",
            title="Captions UI Video",
            caption_tracks=[
                CaptionTrackInfo(lang_code="en", name="English", source=CaptionSource.MANUAL),
                CaptionTrackInfo(lang_code="es", name="Spanish", source=CaptionSource.AUTOMATIC),
            ],
        )
        win._include_auto_captions_var.set(True)
        win._update_discovered_caption_tracks(meta_with_captions)
        assert any("English" in opt for opt in win._caption_track_menu.cget("values"))
        assert any("Spanish" in opt for opt in win._caption_track_menu.cget("values"))

        # Select Spanish track
        chosen_opt = next(opt for opt in win._caption_track_menu.cget("values") if "Spanish" in opt)
        win._caption_track_menu.set(chosen_opt)
        win._on_caption_track_selected(chosen_opt)
        assert win._subtitle_langs_entry.get().strip() == "es"

        # Captions Only mode can be queued even when FFmpeg is missing
        ffmpeg_service.ffmpeg_ok = False
        win._refresh_dependency_status_ui()
        win._url_textbox.delete("1.0", "end")
        win._url_textbox.insert("1.0", "https://www.youtube.com/watch?v=caption_ui_test")
        win._submit_urls(start_immediately=False)

        queued_cap_job = queue_service.list_jobs()[-1]
        assert queued_cap_job.mode == DownloadMode.CAPTIONS
        assert queued_cap_job.subtitle_languages == ["es"]
        assert queued_cap_job.url == "https://www.youtube.com/watch?v=caption_ui_test"

        # 13. Local Media File source mode, inspection, and local trim submission
        ffmpeg_service.ffmpeg_ok = True
        win._refresh_dependency_status_ui()

        local_vid = tmp_path / "sample_local.mp4"
        local_vid.write_bytes(b"local-media-file-bytes" * 50)

        win._source_selector.set("Local Media File")
        win._on_source_type_changed("Local Media File")
        assert win._local_path_entry.winfo_ismapped() or win._local_path_entry.grid_info() != {}
        assert win._url_textbox.grid_info() == {}

        win._local_path_entry.delete(0, "end")
        win._local_path_entry.insert(0, str(local_vid))
        win._inspect_local_media_file()

        assert win._current_local_media is not None
        assert win._current_local_media.path == local_vid.resolve()
        assert win._trim_timeline.media_duration == 180.0

        win._trim_timeline.set_range_seconds(15.0, 75.0)
        win._submit_local_trim_job(start_immediately=False)

        queued_trim_job = queue_service.list_jobs()[-1]
        assert queued_trim_job.source_type == SourceType.LOCAL_FILE
        assert queued_trim_job.local_source_path == local_vid.resolve()
        assert queued_trim_job.trim_start_seconds == 15.0
        assert queued_trim_job.trim_end_seconds == 75.0

        # 14. Caption Selection: Compact layout verification & CaptionSelectionDialog modal testing
        win._source_selector.set("YouTube URL")
        win._on_source_type_changed("YouTube URL")
        win._mode_selector.set("Captions Only")
        win._on_mode_changed("Captions Only")

        cap_meta = MediaMetadata(
            url="https://www.youtube.com/watch?v=selector_test_v1",
            webpage_url="https://www.youtube.com/watch?v=selector_test_v1",
            video_id="selector_test_v1",
            title="Selector Test Video",
            caption_tracks=[
                CaptionTrackInfo(lang_code="en", name="English", source=CaptionSource.MANUAL),
                CaptionTrackInfo(lang_code="es", name="Spanish", source=CaptionSource.AUTOMATIC),
                CaptionTrackInfo(lang_code="fr", name="French", source=CaptionSource.MANUAL),
            ],
        )
        win._current_metadata = cap_meta
        win._update_discovered_caption_tracks(cap_meta)

        # Verify compact layout: full list is not mapped/packed in the visible layout
        assert not win._caption_selector.winfo_ismapped()
        assert "Available: 2 manual, 1 automatic" in win._captions_summary_lbl.cget("text")
        assert "Selected: 1 (en)" in win._captions_summary_lbl.cget("text")
        assert win._choose_captions_btn.cget("text") == "Choose Captions..."

        # Test CaptionSelectionDialog: filter, search, selection, and confirmation
        dialog = CaptionSelectionDialog(
            win,
            tracks=cap_meta.caption_tracks,
            initial_selected_codes=["en"],
            on_confirm=win._on_captions_dialog_confirmed,
        )
        assert len(dialog._tracks) == 3
        assert dialog._current_category == "all"
        assert len(dialog._get_filtered_tracks()) == 3

        # Test search filter in dialog
        dialog._search_entry.delete(0, "end")
        dialog._search_entry.insert(0, "Span")
        dialog._on_search_key_released(None)
        filtered_search = dialog._get_filtered_tracks()
        assert len(filtered_search) == 1
        assert filtered_search[0].language_code == "es"

        # Test category filter in dialog
        dialog._search_entry.delete(0, "end")
        dialog._on_search_key_released(None)
        dialog._on_category_changed("Manual (2)")
        assert dialog._current_category == "manual"
        assert len(dialog._get_filtered_tracks()) == 2
        assert {t.language_code for t in dialog._get_filtered_tracks()} == {"en", "fr"}

        dialog._on_category_changed("Auto (1)")
        assert dialog._current_category == "auto"
        assert len(dialog._get_filtered_tracks()) == 1
        assert dialog._get_filtered_tracks()[0].language_code == "es"

        # Reset category to all and select multiple
        dialog._on_category_changed("All (3)")
        dialog._on_checkbox_toggled("es", True)
        assert dialog._selected_codes == {"en", "es"}
        dialog._confirm_selected()

        # Verify selection is reflected on main window compact summary and entry
        assert win._selected_caption_codes == ["en", "es"]
        assert "en, es" in win._subtitle_langs_entry.get()
        assert "Selected: 2 (en, es)" in win._captions_summary_lbl.cget("text")

        # Test URL Clear resets captions summary
        win._clear_url_input()
        assert win._selected_caption_codes == []
        assert "Available: 0 · Selected: 0" in win._captions_summary_lbl.cget("text")

        # 15. CurrentJobsPanel: Displays at most two download jobs by deterministic priority
        queue_service.wait_for_idle(timeout=2.0)
        queue_service.clear_completed_jobs()
        queue_service._processing_enabled = False

        job_q1 = queue_service.add_job("https://www.youtube.com/watch?v=panel_j1", destination_dir=tmp_path)
        job_q2 = queue_service.add_job("https://www.youtube.com/watch?v=panel_j2", destination_dir=tmp_path)
        job_q3 = queue_service.add_job("https://www.youtube.com/watch?v=panel_j3", destination_dir=tmp_path)
        job_q4 = queue_service.add_job("https://www.youtube.com/watch?v=panel_j4", destination_dir=tmp_path)

        panel = win._current_jobs_panel
        panel.refresh_all()
        assert len(panel._cards) == 2
        assert "Current Jobs (2)" in panel._summary_lbl.cget("text")
        assert f"Open Full Queue ({len(queue_service.list_jobs())})" in panel._open_queue_btn.cget("text")

        # Deterministic priority: Active > Paused > Queued
        job_q3.transition_to(JobStatus.STARTING)
        job_q3.transition_to(JobStatus.DOWNLOADING)
        job_q2.transition_to(JobStatus.STARTING)
        job_q2.transition_to(JobStatus.DOWNLOADING)
        job_q2.transition_to(JobStatus.PAUSED)

        panel.refresh_all()
        assert len(panel._cards) == 2
        assert job_q3.job_id in panel._cards
        assert job_q2.job_id in panel._cards

        # 16. FullQueueWindow: Pop-up lifecycle, single-instance, filter tabs, bulk actions, and non-destructive close
        win.open_full_queue_window()
        assert win._full_queue_window is not None
        assert win._full_queue_window.winfo_exists()

        # Single instance enforcement
        existing_popup = win._full_queue_window
        win.open_full_queue_window()
        assert win._full_queue_window is existing_popup

        fqw_view = win._full_queue_window.queue_view
        fqw_view.refresh_all()

        # Filter tabs and counts
        filter_vals = fqw_view._filter_segmented.cget("values")
        assert any("Active (" in v for v in filter_vals)
        assert any("Paused (" in v for v in filter_vals)
        assert any("Queued (" in v for v in filter_vals)

        # Filtering by Active
        active_tab = next(v for v in filter_vals if "Active" in v)
        fqw_view._on_filter_changed(active_tab)
        assert len(fqw_view._cards) == 1
        assert job_q3.job_id in fqw_view._cards

        # Filtering by Paused
        paused_tab = next(v for v in filter_vals if "Paused" in v)
        fqw_view._on_filter_changed(paused_tab)
        assert len(fqw_view._cards) == 1
        assert job_q2.job_id in fqw_view._cards

        # Reset to All
        all_tab = next(v for v in filter_vals if "All" in v)
        fqw_view._on_filter_changed(all_tab)
        assert len(fqw_view._cards) >= 4

        # Bulk pause sets pause event on active job
        fqw_view._handle_pause_all()
        assert queue_service._pause_events[job_q3.job_id].is_set() is True
        job_q3.transition_to(JobStatus.PAUSED)

        # Bulk clear failed
        fqw_view._handle_clear_failed()

        # Non-destructive close: closing pop-up does not cancel active/paused jobs
        win._full_queue_window._on_close()
        assert win._full_queue_window is None
        assert job_q2.status == JobStatus.PAUSED
        assert job_q3.status == JobStatus.PAUSED
    finally:
        win._on_close_window()
