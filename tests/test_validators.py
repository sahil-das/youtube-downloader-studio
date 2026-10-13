"""Comprehensive unit tests for URL, batch input, playlist index, filename, and path validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.exceptions import FilesystemError, ValidationError
from src.core.validators import (
    is_likely_playlist_url,
    parse_batch_urls,
    sanitize_filename,
    validate_filename_template,
    validate_output_directory,
    validate_playlist_indices,
    validate_url,
)


class TestUrlValidation:
    """Tests for validate_url covering all 20 scenarios in Section 4."""

    def test_valid_youtube_video_and_playlist_urls(self) -> None:
        valid_samples = [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtube.com/watch?v=dQw4w9WgXcQ&t=42s&feature= shared".replace(" ", ""),
            "  https://www.youtube.com/watch?v=dQw4w9WgXcQ&ab_channel=Test  ",
            "https://youtu.be/dQw4w9WgXcQ?si=YNqSFXDWRHPUJeHl",
            "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ",
            "https://www.youtube.com/playlist?list=PL1234567890_abcdef-XYZ",
            "https://www.youtube.com/shorts/abc123XYZ_-",
            "https://www.youtube.com/live/liveId_123",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ#t=1m30s",
        ]
        for sample in valid_samples:
            result = validate_url(sample)
            assert result == sample.strip()

    def test_empty_and_whitespace_inputs_rejected(self) -> None:
        with pytest.raises(ValidationError):
            validate_url("")
        with pytest.raises(ValidationError):
            validate_url("   \n\t  ")
        with pytest.raises(ValidationError):
            validate_url(None)  # type: ignore[arg-type]

    def test_extremely_long_url_rejected(self) -> None:
        long_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&extra=" + ("a" * 2100)
        with pytest.raises(ValidationError) as exc_info:
            validate_url(long_url)
        assert "length" in exc_info.value.user_message.lower()

    def test_unicode_in_url_parameters_and_invalid_ids(self) -> None:
        # Unicode query parameter outside v= is allowed if URL is well-formed
        url_with_unicode_param = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&title=%E6%97%A5%E6%9C%AC"
        assert validate_url(url_with_unicode_param) == url_with_unicode_param

        # Crafted path traversal or non-ID characters inside v= or list= or /shorts/ is rejected
        for crafted in (
            "https://www.youtube.com/watch?v=../../etc/passwd",
            "https://www.youtube.com/watch?v=<script>alert(1)</script>",
            "https://www.youtube.com/playlist?list=../../secret",
            "https://www.youtube.com/shorts/../../cmd.exe",
            "https://youtu.be/../../cmd.exe",
            "https://www.youtube.com/shorts/",
        ):
            with pytest.raises(ValidationError):
                validate_url(crafted)

    @pytest.mark.parametrize(
        "bad_url",
        [
            "file:///C:/Windows/System32/cmd.exe",
            "javascript:alert(1)",
            "vbscript:msgbox(1)",
            "data:text/html,<script>alert(1)</script>",
            "ftp://youtube.com/watch?v=123",
            "smb://server/share/video.mp4",
            "C:\\Users\\user\\video.mp4",
            "\\\\server\\share\\video.mp4",
            "/etc/passwd",
            "https://",
            "https:///watch?v=dQw4w9WgXcQ",
            "https://user:password@www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://example.com/watch?v=dQw4w9WgXcQ",
            "https://evil-youtube.com/watch?v=dQw4w9WgXcQ",
            "https://www.youtube.com/",
            "https://www.youtube.com/watch",
            "https://www.youtube.com/playlist",
            "https://youtu.be/",
            "https://www.youtube.com/watch?v=abc def",
            "https://www.youtube.com/watch?v=abc\x00def",
            "cmd.exe /c calc.exe",
            "not_a_url_at_all",
        ],
    )
    def test_invalid_or_unsafe_urls_rejected(self, bad_url: str) -> None:
        with pytest.raises(ValidationError) as exc_info:
            validate_url(bad_url)
        assert exc_info.value.format_for_user()

    def test_playlist_detection(self) -> None:
        assert is_likely_playlist_url("https://www.youtube.com/playlist?list=PL123")
        assert is_likely_playlist_url("https://www.youtube.com/watch?v=abc&list=PL123")
        assert not is_likely_playlist_url("https://www.youtube.com/watch?v=abc")


class TestBatchUrlParsing:
    """Tests for multiline batch URL parsing, deduplication, and error reporting."""

    def test_batch_parsing_trims_deduplicates_and_captures_invalid(self) -> None:
        raw = "\n".join(
            [
                "",
                "  https://www.youtube.com/watch?v=video_01  ",
                "https://youtu.be/video_02",
                "https://www.youtube.com/watch?v=video_01",  # duplicate
                "file:///C:/secret.txt",  # invalid scheme
                "   ",
                "https://evil.example.com/video",  # invalid host
                "https://www.youtube.com/playlist?list=PL_batch_03",
            ]
        )
        parsed = parse_batch_urls(raw)
        assert parsed.valid_urls == [
            "https://www.youtube.com/watch?v=video_01",
            "https://youtu.be/video_02",
            "https://www.youtube.com/playlist?list=PL_batch_03",
        ]
        assert parsed.duplicate_urls == ["https://www.youtube.com/watch?v=video_01"]
        assert parsed.has_duplicates is True
        assert parsed.has_errors is True
        assert len(parsed.invalid_entries) == 2
        assert parsed.invalid_entries[0].line_number == 5
        assert "file" in parsed.invalid_entries[0].reason.lower()
        assert parsed.invalid_entries[1].line_number == 7

    def test_empty_batch_returns_empty_result(self) -> None:
        res = parse_batch_urls("   \n  \n ")
        assert res.valid_urls == []
        assert res.invalid_entries == []
        assert res.duplicate_urls == []
        assert res.has_errors is False


class TestPlaylistIndicesValidation:
    """Tests for validate_playlist_indices."""

    def test_valid_indices_sorted_and_deduplicated(self) -> None:
        assert validate_playlist_indices([5, 1, 3, 1, 2]) == [1, 2, 3, 5]

    @pytest.mark.parametrize(
        "bad_indices",
        [
            None,
            [],
            [0],
            [-1, 2],
            [True],  # bool is a subclass of int in Python; must be rejected
            ["1"],  # type: ignore[list-item]
        ],
    )
    def test_invalid_playlist_indices_rejected(self, bad_indices: list[int] | None) -> None:
        with pytest.raises(ValidationError):
            validate_playlist_indices(bad_indices)


class TestFilenameAndPathValidation:
    """Tests for sanitize_filename, validate_filename_template, and validate_output_directory."""

    def test_sanitize_filename_strips_traversal_and_unsafe_chars(self) -> None:
        dirty = '../../CON: My <Video> "Title" | Part 1?.mp4'
        cleaned = sanitize_filename(dirty)
        assert ".." not in cleaned
        assert "/" not in cleaned
        assert "\\" not in cleaned
        for ch in '<>:"|?*':
            assert ch not in cleaned
        assert cleaned.endswith(".mp4")

    def test_sanitize_reserved_windows_names(self) -> None:
        assert sanitize_filename("CON.mp4") == "_CON.mp4"
        assert sanitize_filename("NUL") == "_NUL"
        assert sanitize_filename("COM1.mp3") == "_COM1.mp3"
        assert sanitize_filename("") == "download"
        assert sanitize_filename("   ") == "download"

    def test_sanitize_long_and_unicode_filename_preserves_extension(self) -> None:
        long_title = "日本語_" + ("A" * 300) + ".mkv"
        cleaned = sanitize_filename(long_title, max_length=120)
        assert len(cleaned) <= 120
        assert cleaned.endswith(".mkv")
        assert cleaned.startswith("日本語_")

    def test_validate_filename_template_accepts_valid(self) -> None:
        tpl = "  %(title).150B [%(id)s].%(ext)s  "
        assert validate_filename_template(tpl) == "%(title).150B [%(id)s].%(ext)s"

    @pytest.mark.parametrize(
        "bad_template",
        [
            "",
            "   ",
            "../%(title)s.%(ext)s",
            "subfolder/%(title)s.%(ext)s",
            "C:\\%(title)s.%(ext)s",
            "%(title)s.mp4",  # missing %(ext)s
            "%(title)s; rm -rf.%(ext)s",
            "%(title)s`calc`.%(ext)s",
            "%(title)s$HOME.%(ext)s",
            "CON.%(ext)s",
            "A" * 260 + ".%(ext)s",
        ],
    )
    def test_validate_filename_template_rejects_unsafe(self, bad_template: str) -> None:
        with pytest.raises(ValidationError):
            validate_filename_template(bad_template)

    def test_validate_output_directory_creates_and_verifies(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "downloads_文件夹"
        resolved = validate_output_directory(target, create_if_missing=True)
        assert resolved.exists()
        assert resolved.is_dir()

    def test_validate_output_directory_rejects_file(self, tmp_path: Path) -> None:
        file_target = tmp_path / "some_file.txt"
        file_target.write_text("hello", encoding="utf-8")
        with pytest.raises(FilesystemError):
            validate_output_directory(file_target)
