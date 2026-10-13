"""Unit tests for filesystem path helpers, collision resolution, and platform_utils security."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.exceptions import FilesystemError, ValidationError
from src.utils.paths import (
    ensure_directory_writable,
    get_app_data_dir,
    get_database_path,
    get_logs_dir,
    get_settings_path,
    get_temp_dir,
    resolve_unique_filepath,
    truncate_safe_filename,
)
from src.utils.platform_utils import (
    BLOCKED_EXECUTABLE_EXTENSIONS,
    open_directory,
    open_media_file,
    reveal_in_file_manager,
)


def test_app_data_dir_respects_custom_path(tmp_path: Path) -> None:
    custom = tmp_path / "custom_appdata"
    resolved = get_app_data_dir(custom)
    assert resolved == custom.resolve()
    assert resolved.exists()
    assert get_database_path(resolved) == resolved / "history.db"
    assert get_settings_path(resolved) == resolved / "settings.json"
    assert get_logs_dir(resolved) == resolved / "logs"
    assert get_temp_dir(resolved) == resolved / "temp"


def test_app_data_dir_respects_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_dir = tmp_path / "env_appdata_space dir"
    monkeypatch.setenv("YTD_STUDIO_DATA_DIR", str(env_dir))
    resolved = get_app_data_dir()
    assert resolved == env_dir.resolve()
    assert resolved.exists()


def test_ensure_directory_writable_missing_when_create_disabled(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist"
    with pytest.raises(FilesystemError):
        ensure_directory_writable(missing, create_if_missing=False)


def test_ensure_directory_writable_rejects_empty_and_null_bytes() -> None:
    with pytest.raises(ValidationError):
        ensure_directory_writable("   ")
    with pytest.raises(ValidationError):
        ensure_directory_writable("C:\\folder\x00bad")


def test_ensure_directory_writable_handles_unwritable_simulation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _deny_write(self: Path, data: bytes) -> int:
        raise PermissionError("Simulated read-only directory")

    monkeypatch.setattr(Path, "write_bytes", _deny_write)
    with pytest.raises(FilesystemError) as exc_info:
        ensure_directory_writable(tmp_path)
    assert "not writable" in exc_info.value.user_message.lower()


def test_resolve_unique_filepath_avoids_overwrites(tmp_path: Path) -> None:
    first = resolve_unique_filepath(tmp_path, "lecture.mp4")
    assert first.name == "lecture.mp4"
    first.write_bytes(b"data1")

    second = resolve_unique_filepath(tmp_path, "lecture.mp4")
    assert second.name == "lecture (1).mp4"
    second.write_bytes(b"data2")

    third = resolve_unique_filepath(tmp_path, "lecture.mp4")
    assert third.name == "lecture (2).mp4"


def test_truncate_safe_filename_handles_empty_reserved_and_unicode() -> None:
    assert truncate_safe_filename("...") == "download"
    assert truncate_safe_filename("LPT1.txt") == "_LPT1.txt"
    unicode_name = truncate_safe_filename("Café_Müsic_🎵_Track.mp3")
    assert unicode_name.endswith(".mp3")


@pytest.mark.parametrize("ext", sorted(BLOCKED_EXECUTABLE_EXTENSIONS))
def test_open_media_file_blocks_executable_extensions(tmp_path: Path, ext: str) -> None:
    dangerous_file = tmp_path / f"disguised_video{ext}"
    dangerous_file.write_bytes(b"MZ")
    with pytest.raises(ValidationError) as exc_info:
        open_media_file(dangerous_file)
    assert "executable" in exc_info.value.user_message.lower()


def test_open_file_and_directory_missing_paths_raise_filesystem_error(tmp_path: Path) -> None:
    missing_file = tmp_path / "gone.mp4"
    with pytest.raises(FilesystemError):
        open_media_file(missing_file)

    missing_dir = tmp_path / "gone_folder"
    with pytest.raises(FilesystemError):
        open_directory(missing_dir)

    with pytest.raises(FilesystemError):
        reveal_in_file_manager(tmp_path / "nonexistent_parent" / "gone.mp4")
