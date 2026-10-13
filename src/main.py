"""Application bootstrap and dependency wiring for YouTube Downloader Studio."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.core.events import EventBus
from src.core.models import DependencyStatus
from src.database.db import DatabaseManager
from src.database.repositories import HistoryRepository, SettingsRepository
from src.services.download_service import DownloadService
from src.services.ffmpeg_service import FFmpegService
from src.services.media_info_service import MediaInfoService
from src.services.queue_service import QueueService
from src.services.settings_service import SettingsService
from src.utils.logging_config import get_logger, set_log_level, setup_logging
from src.utils.paths import (
    get_app_data_dir,
    get_assets_dir,
    get_database_path,
    get_logs_dir,
    get_settings_path,
)


def create_app_context(custom_data_dir: Path | None = None) -> dict[str, object]:
    """Initialize directories, logging, database, and services without launching the GUI."""
    app_data_dir = get_app_data_dir(custom_data_dir)
    logs_dir = get_logs_dir(app_data_dir)
    log_file_path = setup_logging(logs_dir, level="INFO")
    logger = get_logger("main")

    db_path = get_database_path(app_data_dir)
    settings_path = get_settings_path(app_data_dir)

    db_manager = DatabaseManager(db_path)
    history_repo = HistoryRepository(db_manager)
    settings_repo = SettingsRepository(db_manager)

    settings_service = SettingsService(
        settings_path=settings_path,
        repository=settings_repo,
    )
    set_log_level(settings_service.settings.log_level)

    ffmpeg_service = FFmpegService(
        custom_ffmpeg_path=settings_service.settings.ffmpeg_custom_path
    )
    dep_status = ffmpeg_service.check_dependencies()
    logger.info("Startup dependency check: %s", dep_status.summary_text)

    event_bus = EventBus()
    media_info_service = MediaInfoService()
    download_service = DownloadService(
        ffmpeg_service=ffmpeg_service,
        event_bus=event_bus,
    )
    queue_service = QueueService(
        download_service=download_service,
        history_repository=history_repo,
        event_bus=event_bus,
        max_concurrent_downloads=settings_service.settings.max_concurrent_downloads,
        auto_start=False,
    )

    return {
        "app_data_dir": app_data_dir,
        "log_file_path": log_file_path,
        "db_manager": db_manager,
        "history_repo": history_repo,
        "settings_repo": settings_repo,
        "settings_service": settings_service,
        "ffmpeg_service": ffmpeg_service,
        "media_info_service": media_info_service,
        "download_service": download_service,
        "queue_service": queue_service,
        "event_bus": event_bus,
        "dependency_status": dep_status,
    }


def main(argv: list[str] | None = None) -> int:
    """Entry point for YouTube Downloader Studio."""
    parser = argparse.ArgumentParser(
        prog="YouTubeDownloaderStudio",
        description="YouTube Downloader Studio -- Desktop Media Manager for Windows",
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default=None,
        help="Optional custom directory for application state (settings, SQLite DB, logs).",
    )
    parser.add_argument(
        "--check-env",
        action="store_true",
        help="Initialize services, print dependency diagnostics, and exit (used for build/smoke verification).",
    )
    parser.add_argument(
        "--smoke-test-ui",
        action="store_true",
        help="Launch the CustomTkinter window briefly and close it automatically to verify UI startup.",
    )
    args = parser.parse_args(argv)

    custom_dir = Path(args.data_dir) if args.data_dir else None
    ctx = create_app_context(custom_data_dir=custom_dir)

    if args.check_env:
        dep: DependencyStatus = ctx["dependency_status"]  # type: ignore[assignment]
        print("YouTube Downloader Studio -- Diagnostics OK")
        print(f"App Data Dir : {ctx['app_data_dir']}")
        print(f"Status       : {dep.summary_text}")
        return 0

    from src.ui.main_window import MainWindow

    icon_candidate = get_assets_dir() / "app_icon.ico"
    app = MainWindow(
        settings_service=ctx["settings_service"],  # type: ignore[arg-type]
        ffmpeg_service=ctx["ffmpeg_service"],  # type: ignore[arg-type]
        media_info_service=ctx["media_info_service"],  # type: ignore[arg-type]
        queue_service=ctx["queue_service"],  # type: ignore[arg-type]
        history_repo=ctx["history_repo"],  # type: ignore[arg-type]
        event_bus=ctx["event_bus"],  # type: ignore[arg-type]
        app_data_dir=ctx["app_data_dir"],  # type: ignore[arg-type]
        log_file_path=ctx["log_file_path"],  # type: ignore[arg-type]
        icon_path=icon_candidate if icon_candidate.exists() else None,
    )

    if args.smoke_test_ui:
        app.after(400, app._on_close_window)

    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
