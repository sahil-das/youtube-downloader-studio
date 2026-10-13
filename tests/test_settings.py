"""Unit tests for SettingsService persistence, validation, backward compatibility, and recovery."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.core.exceptions import FilesystemError, ValidationError
from src.core.models import (
    AppSettings,
    AudioFormat,
    AudioQuality,
    DownloadMode,
    OverwriteBehavior,
    ThemeMode,
    VideoFormat,
    VideoQuality,
)
from src.database.db import DatabaseManager
from src.database.repositories import SettingsRepository
from src.services.settings_service import SettingsService


def test_default_settings_and_full_persistence_across_restart(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    download_dir = tmp_path / "downloads"
    db = DatabaseManager(tmp_path / "app.db")
    repo = SettingsRepository(db)

    service = SettingsService(
        settings_path=settings_file,
        repository=repo,
        default_download_dir=download_dir,
    )
    defaults = service.settings
    assert defaults.default_mode == DownloadMode.VIDEO
    assert defaults.theme_mode == ThemeMode.DARK
    assert Path(defaults.default_download_dir) == download_dir.resolve()

    custom_dl = tmp_path / "my_videos"
    fake_ffmpeg = tmp_path / "ffmpeg.exe"
    fake_ffmpeg.write_bytes(b"fake")

    updated = AppSettings(
        default_download_dir=str(custom_dl),
        default_mode=DownloadMode.AUDIO,
        default_video_quality=VideoQuality.Q720P,
        default_video_format=VideoFormat.MKV,
        default_audio_format=AudioFormat.WAV,
        default_audio_quality=AudioQuality.K320,
        theme_mode=ThemeMode.LIGHT,
        max_concurrent_downloads=3,
        filename_template="%(title).100B.%(ext)s",
        ask_before_overwrite=False,
        overwrite_behavior=OverwriteBehavior.SKIP,
        enable_completion_notifications=False,
        ffmpeg_custom_path=str(fake_ffmpeg),
        log_level="DEBUG",
    )
    service.save(updated)

    # Reload in a fresh service instance
    reloaded_service = SettingsService(
        settings_path=settings_file,
        repository=repo,
        default_download_dir=download_dir,
    )
    loaded = reloaded_service.settings
    assert loaded.default_mode == DownloadMode.AUDIO
    assert loaded.default_video_quality == VideoQuality.Q720P
    assert loaded.default_video_format == VideoFormat.MKV
    assert loaded.default_audio_format == AudioFormat.WAV
    assert loaded.default_audio_quality == AudioQuality.K320
    assert loaded.theme_mode == ThemeMode.LIGHT
    assert loaded.max_concurrent_downloads == 3
    assert loaded.filename_template == "%(title).100B.%(ext)s"
    assert loaded.ask_before_overwrite is False
    assert loaded.overwrite_behavior == OverwriteBehavior.SKIP
    assert loaded.enable_completion_notifications is False
    assert Path(loaded.ffmpeg_custom_path) == fake_ffmpeg.resolve()
    assert loaded.log_level == "DEBUG"
    assert Path(loaded.default_download_dir) == custom_dl.resolve()

    # Reset to defaults
    reset = reloaded_service.reset_to_defaults()
    assert reset.default_mode == DownloadMode.VIDEO
    assert reset.theme_mode == ThemeMode.DARK


def test_corrupted_and_backward_compatible_settings_recovery(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{not valid json", encoding="utf-8")
    download_dir = tmp_path / "downloads"

    service = SettingsService(
        settings_path=settings_file,
        default_download_dir=download_dir,
    )
    assert service.settings.default_mode == DownloadMode.VIDEO
    assert service.settings.max_concurrent_downloads == 2

    # Write partial settings with unknown future keys and invalid enum values
    settings_file.write_text(
        json.dumps(
            {
                "default_mode": "unknown_future_mode",
                "default_video_quality": "1440p",
                "max_concurrent_downloads": 999,
                "future_feature_flag": True,
            }
        ),
        encoding="utf-8",
    )
    service2 = SettingsService(
        settings_path=settings_file,
        default_download_dir=download_dir,
    )
    assert service2.settings.default_mode == DownloadMode.VIDEO
    assert service2.settings.default_video_quality == VideoQuality.Q1440P
    assert service2.settings.max_concurrent_downloads == 2


def test_invalid_settings_rejected_on_save(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    download_dir = tmp_path / "downloads"
    service = SettingsService(
        settings_path=settings_file,
        default_download_dir=download_dir,
    )

    # Invalid concurrency
    bad_concurrency = service.create_default_settings()
    bad_concurrency.max_concurrent_downloads = 99
    with pytest.raises(ValidationError):
        service.save(bad_concurrency)

    # Invalid log level
    bad_log = service.create_default_settings()
    bad_log.log_level = "VERBOSE_INVALID"
    with pytest.raises(ValidationError):
        service.save(bad_log)

    # Invalid template with traversal
    bad_template = service.create_default_settings()
    bad_template.filename_template = "../%(title)s.%(ext)s"
    with pytest.raises(ValidationError):
        service.save(bad_template)

    # Non-existent custom FFmpeg path
    bad_ffmpeg = service.create_default_settings()
    bad_ffmpeg.ffmpeg_custom_path = str(tmp_path / "missing_ffmpeg.exe")
    with pytest.raises(ValidationError):
        service.save(bad_ffmpeg)

    # Output path pointing to an existing file instead of a directory
    blocking_file = tmp_path / "not_a_dir.txt"
    blocking_file.write_text("file", encoding="utf-8")
    bad_dir = service.create_default_settings()
    bad_dir.default_download_dir = str(blocking_file)
    with pytest.raises(FilesystemError):
        service.save(bad_dir)
