"""Validation utilities for URLs, batch inputs, filenames, templates, and paths."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from src.core.exceptions import ValidationError
from src.utils.paths import (
    WINDOWS_RESERVED_NAMES,
    ensure_directory_writable,
    truncate_safe_filename,
)

SUPPORTED_YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "kids.youtube.com",
        "youtu.be",
        "www.youtu.be",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
    }
)

DANGEROUS_SCHEMES = frozenset(
    {
        "file",
        "javascript",
        "vbscript",
        "data",
        "ftp",
        "ftps",
        "sftp",
        "smb",
        "ssh",
        "telnet",
        "mailto",
        "about",
        "blob",
        "chrome",
        "edge",
    }
)

_CONTROL_OR_WHITESPACE_RE = re.compile(r"[\x00-\x20\x7f]")
_WINDOWS_DRIVE_RE = re.compile(r"^[a-zA-Z]:")
_UNSAFE_TEMPLATE_CHARS_RE = re.compile(r'[<>"|?*\x00-\x1f;&`$]')


@dataclass(slots=True)
class InvalidUrlEntry:
    """Details of an invalid line discovered during batch URL parsing."""

    line_number: int
    raw_value: str
    reason: str


@dataclass(slots=True)
class BatchUrlParseResult:
    """Result of parsing single or multi-line URL input."""

    valid_urls: list[str] = field(default_factory=list)
    duplicate_urls: list[str] = field(default_factory=list)
    invalid_entries: list[InvalidUrlEntry] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        """Return True if any line failed validation."""
        return len(self.invalid_entries) > 0

    @property
    def has_duplicates(self) -> bool:
        """Return True if duplicate URLs were stripped."""
        return len(self.duplicate_urls) > 0


def validate_url(raw_url: str) -> str:
    """Validate and normalize a user-supplied URL for supported YouTube media.

    Raises ValidationError if the URL is empty, malformed, uses a dangerous or
    non-HTTP(S) scheme, embeds credentials, or points to an unsupported domain.
    """
    if raw_url is None:
        raise ValidationError(
            "URL cannot be empty.",
            remedy="Paste a valid YouTube video or playlist URL (starting with https://).",
        )

    cleaned = raw_url.strip()
    if not cleaned:
        raise ValidationError(
            "Please enter a YouTube URL.",
            remedy="Paste a valid YouTube video or playlist link into the URL box.",
        )

    if len(cleaned) > 2048:
        raise ValidationError(
            "The provided URL exceeds the maximum allowed length (2048 characters).",
            remedy="Paste a standard YouTube video or playlist URL.",
        )

    if _CONTROL_OR_WHITESPACE_RE.search(cleaned):
        raise ValidationError(
            "URL contains spaces or control characters.",
            remedy="Place each URL on its own line and remove any spaces inside the URL.",
        )

    if _WINDOWS_DRIVE_RE.match(cleaned) or cleaned.startswith("\\\\") or cleaned.startswith("/"):
        raise ValidationError(
            "Local filesystem paths are not valid download URLs.",
            remedy="Provide an https:// YouTube URL rather than a local file path.",
        )

    try:
        parsed = urlparse(cleaned)
    except ValueError as exc:
        raise ValidationError(
            f"Malformed URL: {exc}",
            remedy="Check the URL syntax and try again.",
        ) from exc

    scheme = (parsed.scheme or "").lower()
    if not scheme:
        raise ValidationError(
            "URL is missing a protocol scheme (such as https://).",
            remedy="Include 'https://' at the start of the URL.",
        )

    if scheme in DANGEROUS_SCHEMES or scheme not in {"http", "https"}:
        raise ValidationError(
            f"Unsupported or unsafe URL scheme '{scheme}:'. Only HTTP and HTTPS are permitted.",
            remedy="Use a standard https:// YouTube URL.",
        )

    if parsed.username or parsed.password:
        raise ValidationError(
            "URLs containing embedded usernames or passwords are not permitted.",
            remedy="Remove any credentials from the URL.",
        )

    hostname = (parsed.hostname or "").lower().rstrip(".")
    if not hostname:
        raise ValidationError(
            "URL does not include a valid domain name.",
            remedy="Provide a complete YouTube link (e.g., https://www.youtube.com/watch?v=...).",
        )

    if hostname not in SUPPORTED_YOUTUBE_HOSTS:
        raise ValidationError(
            f"Unsupported domain '{hostname}'. Only official YouTube domains are supported.",
            remedy="Use a link from youtube.com or youtu.be.",
        )

    path = parsed.path or ""
    query = parse_qs(parsed.query)
    safe_id_re = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

    if hostname in {"youtu.be", "www.youtu.be"}:
        video_slug = path.strip("/")
        if not video_slug or not safe_id_re.match(video_slug):
            raise ValidationError(
                "Short YouTube link (youtu.be) is missing a valid video ID.",
                remedy="Copy the full share link from YouTube (e.g., https://youtu.be/VIDEO_ID).",
            )
    else:
        v_vals = query.get("v") or []
        list_vals = query.get("list") or []
        has_video_or_list = bool(v_vals or list_vals)
        valid_path_prefix = any(
            path.startswith(prefix)
            for prefix in ("/watch", "/playlist", "/shorts/", "/live/", "/embed/", "/v/")
        )
        if not (has_video_or_list or valid_path_prefix):
            raise ValidationError(
                "This YouTube URL does not appear to point to a video, short, livestream, or playlist.",
                remedy="Paste a direct link to a YouTube video, Short, or playlist.",
            )
        if path.startswith("/watch") and not has_video_or_list:
            raise ValidationError(
                "YouTube watch URL is missing a video ('v=') or playlist ('list=') parameter.",
                remedy="Copy the full video URL including '?v=...'.",
            )
        if path.startswith("/playlist") and not list_vals:
            raise ValidationError(
                "YouTube playlist URL is missing a 'list=' parameter.",
                remedy="Copy the full playlist URL including '?list=...'.",
            )
        for v_id in v_vals:
            if not safe_id_re.match(v_id):
                raise ValidationError(
                    "YouTube video parameter ('v=') contains invalid characters.",
                    remedy="Verify the video ID in the URL.",
                )
        for list_id in list_vals:
            if not safe_id_re.match(list_id):
                raise ValidationError(
                    "YouTube playlist parameter ('list=') contains invalid characters.",
                    remedy="Verify the playlist ID in the URL.",
                )
        for prefix in ("/shorts/", "/live/", "/embed/", "/v/"):
            if path.startswith(prefix):
                slug = path[len(prefix) :].strip("/")
                if not slug or not safe_id_re.match(slug):
                    raise ValidationError(
                        f"YouTube '{prefix}' URL is missing a valid video ID.",
                        remedy="Copy the full video or Short link from YouTube.",
                    )

    return cleaned


def is_Likely_playlist_url(url: str) -> bool:
    """Return True if the URL contains playlist query parameters or /playlist path."""
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    query = parse_qs(parsed.query)
    return parsed.path.startswith("/playlist") or bool(query.get("list"))


def is_likely_playlist_url(url: str) -> bool:
    """Return True if the URL contains playlist query parameters or /playlist path."""
    return is_Likely_playlist_url(url)


def parse_batch_urls(raw_text: str) -> BatchUrlParseResult:
    """Parse one or more newline-separated URLs with trimming, deduplication, and validation."""
    result = BatchUrlParseResult()
    if not raw_text or not raw_text.strip():
        return result

    seen: set[str] = set()
    lines = raw_text.splitlines()
    for idx, raw_line in enumerate(lines, start=1):
        trimmed = raw_line.strip()
        if not trimmed:
            continue
        try:
            normalized = validate_url(trimmed)
        except ValidationError as exc:
            result.invalid_entries.append(
                InvalidUrlEntry(
                    line_number=idx,
                    raw_value=trimmed,
                    reason=exc.user_message,
                )
            )
            continue

        if normalized in seen:
            result.duplicate_urls.append(normalized)
        else:
            seen.add(normalized)
            result.valid_urls.append(normalized)

    return result


def sanitize_filename(name: str, max_length: int = 180) -> str:
    """Sanitize an untrusted remote title or filename into a safe Windows filename."""
    if not name or not name.strip():
        return "download"

    # Strip any path traversal or directory separators first
    cleaned = name.replace("\\", "_").replace("/", "_")
    while ".." in cleaned:
        cleaned = cleaned.replace("..", "_")

    return truncate_safe_filename(cleaned, max_length=max_length)


def validate_filename_template(template: str) -> str:
    """Validate a yt-dlp filename output template to prevent path traversal or unsafe names."""
    if template is None:
        raise ValidationError(
            "Filename template cannot be empty.",
            remedy="Use a template such as '%(title).150B [%(id)s].%(ext)s'.",
        )

    cleaned = template.strip()
    if not cleaned:
        raise ValidationError(
            "Filename template cannot be empty.",
            remedy="Use a template such as '%(title).150B [%(id)s].%(ext)s'.",
        )

    if len(cleaned) > 250:
        raise ValidationError(
            "Filename template is too long (maximum 250 characters).",
            remedy="Shorten the filename template.",
        )

    if "/" in cleaned or "\\" in cleaned or ".." in cleaned or ":" in cleaned:
        raise ValidationError(
            "Filename template must not contain path separators ('/', '\\'), ':', or '..'.",
            remedy="Specify only a filename pattern without folder traversal.",
        )

    if _UNSAFE_TEMPLATE_CHARS_RE.search(cleaned):
        raise ValidationError(
            "Filename template contains invalid or unsafe characters.",
            remedy="Remove characters such as < > \" | ? * ; & ` $ from the template.",
        )

    stem_candidate = cleaned.split(".")[0].strip().upper()
    if stem_candidate in WINDOWS_RESERVED_NAMES:
        raise ValidationError(
            f"'{stem_candidate}' is a reserved Windows device name.",
            remedy="Choose a different filename template.",
        )

    if "%(ext)s" not in cleaned:
        raise ValidationError(
            "Filename template must include '%(ext)s' so downloaded files have the proper extension.",
            remedy="End your template with '.%(ext)s', e.g., '%(title).150B [%(id)s].%(ext)s'.",
        )

    return cleaned


def validate_output_directory(path_value: str | Path, create_if_missing: bool = True) -> Path:
    """Validate that an output directory is safe and writable."""
    return ensure_directory_writable(path_value, create_if_missing=create_if_missing)


def validate_playlist_indices(indices: list[int] | None) -> list[int]:
    """Validate and deduplicate 1-based playlist item selection indices."""
    if not indices:
        raise ValidationError(
            "No playlist items were selected.",
            remedy="Select at least one playlist entry (>= 1) or choose Full Playlist mode.",
        )

    cleaned: set[int] = set()
    for raw in indices:
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
            raise ValidationError(
                f"Invalid playlist item index '{raw}'. Indices must be positive integers starting at 1.",
                remedy="Select valid playlist item numbers.",
            )
        cleaned.add(raw)

    return sorted(cleaned)

