"""Persistent settings service with validation, SQLite storage, and JSON backup."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.core.exceptions import SettingsError, ValidationError
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
from src.core.validators import validate_filename_template, validate_output_directory
from src.database.repositories import SettingsRepository
from src.utils.logging_config import get_logger, set_log_level
from src.utils.paths import get_default_download_dir, get_settings_path

logger = get_logger("settings_service")

ALLOWED_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR"})
MIN_CONCURRENT_DOWNLOADS = 1
MAX_CONCURRENT_DOWNLOADS = 4


class SettingsService:
    """Manages loading, validating, and persisting user settings across restarts."""

    def __init__(
        self,
        settings_path: Path | None = None,
        repository: SettingsRepository | None = None,
        default_download_dir: Path | None = None,
    ) -> None:
        self.settings_path = settings_path or get_settings_path()
        self._repository = repository
        self._fallback_download_dir = default_download_dir or get_default_download_dir()
        self._settings = self.load()

    @property
    def settings(self) -> AppSettings:
        """Return the currently active AppSettings instance."""
        return self._settings

    def create_default_settings(self) -> AppSettings:
        """Construct a fresh AppSettings instance with safe default values."""
        return AppSettings(
            default_download_dir=str(self._fallback_download_dir),
            default_mode=DownloadMode.VIDEO,
            default_video_quality=VideoQuality.Q1080P,
            default_video_format=VideoFormat.MP4,
            default_audio_format=AudioFormat.MP3,
            default_audio_quality=AudioQuality.K192,
            theme_mode=ThemeMode.DARK,
            max_concurrent_downloads=2,
            filename_template="%(title).150B [%(id)s].%(ext)s",
            ask_before_overwrite=True,
            overwrite_behavior=OverwriteBehavior.RENAME,
            enable_completion_notifications=True,
            ffmpeg_custom_path="",
            log_level="INFO",
        )

    def load(self) -> AppSettings:
        """Load settings from SQLite or JSON file, falling back safely on corruption."""
        raw_data: dict[str, Any] = {}

        if self.settings_path.exists():
            try:
                content = self.settings_path.read_text(encoding="utf-8")
                parsed = json.loads(content)
                if isinstance(parsed, dict):
                    raw_data.update(parsed)
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning(
                    "Could not read settings file at %s (%s); using defaults.",
                    self.settings_path,
                    exc,
                )

        if self._repository is not None:
            try:
                db_data = self._repository.get_all()
                if db_data:
                    raw_data.update(db_data)
            except Exception as exc:
                logger.warning("Could not load settings from SQLite (%s).", exc)

        settings = self._coerce_from_dict(raw_data)
        self._settings = settings
        return settings

    def _coerce_from_dict(self, data: dict[str, Any]) -> AppSettings:
        """Safely coerce a raw dictionary into a valid AppSettings object."""
        defaults = self.create_default_settings()
        if not isinstance(data, dict) or not data:
            return defaults

        download_dir_raw = str(data.get("default_download_dir", "")).strip()
        if download_dir_raw:
            try:
                validated_dir = validate_output_directory(download_dir_raw, create_if_missing=True)
                download_dir = str(validated_dir)
            except Exception:
                logger.warning(
                    "Configured download directory '%s' is invalid or unwritable; reverting to default.",
                    download_dir_raw,
                )
                download_dir = defaults.default_download_dir
        else:
            download_dir = defaults.default_download_dir

        try:
            mode = DownloadMode(str(data.get("default_mode", defaults.default_mode.value)))
        except ValueError:
            mode = defaults.default_mode

        try:
            video_quality = VideoQuality(
                str(data.get("default_video_quality", defaults.default_video_quality.value))
            )
        except ValueError:
            video_quality = defaults.default_video_quality

        try:
            video_format = VideoFormat(
                str(data.get("default_video_format", defaults.default_video_format.value))
            )
        except ValueError:
            video_format = defaults.default_video_format

        try:
            audio_format = AudioFormat(
                str(data.get("default_audio_format", defaults.default_audio_format.value))
            )
        except ValueError:
            audio_format = defaults.default_audio_format

        try:
            audio_quality = AudioQuality(
                str(data.get("default_audio_quality", defaults.default_audio_quality.value))
            )
        except ValueError:
            audio_quality = defaults.default_audio_quality

        try:
            theme_mode = ThemeMode(str(data.get("theme_mode", defaults.theme_mode.value)))
        except ValueError:
            theme_mode = defaults.theme_mode

        try:
            concurrency = int(data.get("max_concurrent_downloads", defaults.max_concurrent_downloads))
            if not (MIN_CONCURRENT_DOWNLOADS <= concurrency <= MAX_CONCURRENT_DOWNLOADS):
                concurrency = defaults.max_concurrent_downloads
        except (TypeError, ValueError):
            concurrency = defaults.max_concurrent_downloads

        template_raw = str(data.get("filename_template", defaults.filename_template))
        try:
            template = validate_filename_template(template_raw)
        except ValidationError:
            template = defaults.filename_template

        ask_overwrite = bool(data.get("ask_before_overwrite", defaults.ask_before_overwrite))

        try:
            overwrite_behavior = OverwriteBehavior(
                str(data.get("overwrite_behavior", defaults.overwrite_behavior.value))
            )
        except ValueError:
            overwrite_behavior = defaults.overwrite_behavior

        notifications = bool(
            data.get("enable_completion_notifications", defaults.enable_completion_notifications)
        )

        ffmpeg_custom = str(data.get("ffmpeg_custom_path", "")).strip()

        log_level = str(data.get("log_level", defaults.log_level)).strip().upper()
        if log_level not in ALLOWED_LOG_LEVELS:
            log_level = defaults.log_level

        return AppSettings(
            default_download_dir=download_dir,
            default_mode=mode,
            default_video_quality=video_quality,
            default_video_format=video_format,
            default_audio_format=audio_format,
            default_audio_quality=audio_quality,
            theme_mode=theme_mode,
            max_concurrent_downloads=concurrency,
            filename_template=template,
            ask_before_overwrite=ask_overwrite,
            overwrite_behavior=overwrite_behavior,
            enable_completion_notifications=notifications,
            ffmpeg_custom_path=ffmpeg_custom,
            log_level=log_level,
        )

    def validate_settings(self, candidate: AppSettings) -> AppSettings:
        """Strictly validate a candidate AppSettings instance before saving."""
        resolved_dir = validate_output_directory(
            candidate.default_download_dir,
            create_if_missing=True,
        )
        cleaned_template = validate_filename_template(candidate.filename_template)

        try:
            concurrency = int(candidate.max_concurrent_downloads)
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "Maximum concurrent downloads must be a whole number.",
                remedy=f"Choose a value between {MIN_CONCURRENT_DOWNLOADS} and {MAX_CONCURRENT_DOWNLOADS}.",
            ) from exc

        if not (MIN_CONCURRENT_DOWNLOADS <= concurrency <= MAX_CONCURRENT_DOWNLOADS):
            raise ValidationError(
                f"Maximum concurrent downloads must be between {MIN_CONCURRENT_DOWNLOADS} and {MAX_CONCURRENT_DOWNLOADS}.",
                remedy=f"Enter a number from {MIN_CONCURRENT_DOWNLOADS} to {MAX_CONCURRENT_DOWNLOADS}.",
            )

        log_level = str(candidate.log_level).strip().upper()
        if log_level not in ALLOWED_LOG_LEVELS:
            raise ValidationError(
                f"Unsupported log level '{candidate.log_level}'.",
                remedy="Select DEBUG, INFO, WARNING, or ERROR.",
            )

        ffmpeg_path_str = candidate.ffmpeg_custom_path.strip()
        if ffmpeg_path_str:
            if "\x00" in ffmpeg_path_str:
                raise ValidationError("FFmpeg path contains invalid characters.")
            ff_path = Path(ffmpeg_path_str).expanduser().resolve()
            if not ff_path.exists():
                raise ValidationError(
                    f"Configured FFmpeg path '{ff_path}' does not exist.",
                    remedy="Select the folder containing ffmpeg.exe or the ffmpeg.exe file itself, or leave blank for auto-detection.",
                )
            ffmpeg_path_str = str(ff_path)

        return AppSettings(
            default_download_dir=str(resolved_dir),
            default_mode=DownloadMode(candidate.default_mode),
            default_video_quality=VideoQuality(candidate.default_video_quality),
            default_video_format=VideoFormat(candidate.default_video_format),
            default_audio_format=AudioFormat(candidate.default_audio_format),
            default_audio_quality=AudioQuality(candidate.default_audio_quality),
            theme_mode=ThemeMode(candidate.theme_mode),
            max_concurrent_downloads=concurrency,
            filename_template=cleaned_template,
            ask_before_overwrite=bool(candidate.ask_before_overwrite),
            overwrite_behavior=OverwriteBehavior(candidate.overwrite_behavior),
            enable_completion_notifications=bool(candidate.enable_completion_notifications),
            ffmpeg_custom_path=ffmpeg_path_str,
            log_level=log_level,
        )

    def save(self, new_settings: AppSettings) -> AppSettings:
        """Validate and persist settings to both JSON and SQLite."""
        validated = self.validate_settings(new_settings)
        payload = validated.to_dict()

        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            temp_file = self.settings_path.with_suffix(".json.tmp")
            temp_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            temp_file.replace(self.settings_path)
        except OSError as exc:
            logger.error("Failed to write settings file at %s: %s", self.settings_path, exc)
            raise SettingsError(
                f"Could not save settings to '{self.settings_path}': {exc}",
                remedy="Verify that your user data folder is writable.",
            ) from exc

        if self._repository is not None:
            try:
                self._repository.save_all(payload)
            except Exception as exc:
                logger.warning("Failed to mirror settings to SQLite: %s", exc)

        self._settings = validated
        set_log_level(validated.log_level)
        logger.info("Application settings saved successfully.")
        return validated

    def reset_to_defaults(self) -> AppSettings:
        """Reset all settings to their factory default values and persist them."""
        defaults = self.create_default_settings()
        return self.save(defaults)
