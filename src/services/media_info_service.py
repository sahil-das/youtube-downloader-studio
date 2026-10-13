"""Media and playlist metadata extraction service built on yt-dlp."""

from __future__ import annotations

import threading
import urllib.request
from typing import Any
from urllib.parse import urlparse

import yt_dlp  # type: ignore[import-untyped]

from src.core.exceptions import (
    DownloadCancelledError,
    MetadataError,
    ValidationError,
)
from src.core.models import MediaMetadata, PlaylistItemInfo
from src.core.validators import validate_url
from src.utils.logging_config import get_logger

logger = get_logger("media_info_service")

MAX_PLAYLIST_PREVIEW_ITEMS = 200
MAX_THUMBNAIL_BYTES = 3 * 1024 * 1024  # 3 MB
ALLOWED_THUMBNAIL_HOSTS = frozenset(
    {
        "i.ytimg.com",
        "i9.ytimg.com",
        "img.youtube.com",
        "yt3.ggpht.com",
        "yt3.googleusercontent.com",
    }
)


class _SilentYtdlpLogger:
    """Internal logger adapter for yt-dlp to prevent noisy stdout/stderr output."""

    def debug(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        logger.debug("yt-dlp metadata warning: %s", msg)

    def error(self, msg: str) -> None:
        logger.warning("yt-dlp metadata error: %s", msg)


class MediaInfoService:
    """Extracts video and playlist metadata safely without downloading media files."""

    def __init__(self, ydl_factory: type[ Any ] | None = None) -> None:
        self._ydl_cls = ydl_factory or yt_dlp.YoutubeDL

    def fetch_metadata(
        self,
        url: str,
        allow_playlist: bool = True,
        cancel_event: threading.Event | None = None,
    ) -> MediaMetadata:
        """Fetch metadata for a validated YouTube URL.

        Raises ValidationError, MetadataError, or DownloadCancelledError.
        """
        normalized_url = validate_url(url)

        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelledError("Metadata retrieval was cancelled.")

        ydl_opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": "in_playlist",
            "playlistend": MAX_PLAYLIST_PREVIEW_ITEMS,
            "noplaylist": not allow_playlist,
            "socket_timeout": 15,
            "retries": 2,
            "js_runtimes": {"deno": {}, "node": {}, "bun": {}, "quickjs": {}},
            "logger": _SilentYtdlpLogger(),
        }

        logger.info("Fetching metadata for URL: %s (allow_playlist=%s)", normalized_url, allow_playlist)
        try:
            with self._ydl_cls(ydl_opts) as ydl:
                raw_info = ydl.extract_info(normalized_url, download=False)
        except DownloadCancelledError:
            raise
        except Exception as exc:
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelledError("Metadata retrieval was cancelled.") from exc
            raise self._map_metadata_exception(exc) from exc

        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelledError("Metadata retrieval was cancelled.")

        if not raw_info or not isinstance(raw_info, dict):
            raise MetadataError(
                "No metadata was returned for this URL.",
                remedy="Check that the video or playlist is public and available.",
            )

        return self._parse_info_dict(normalized_url, raw_info)

    def _parse_info_dict(self, original_url: str, info: dict[str, Any]) -> MediaMetadata:
        """Transform a yt-dlp info dictionary into a structured MediaMetadata instance."""
        entry_type = str(info.get("_type", "video")).lower()
        entries_raw = info.get("entries")
        is_playlist = entry_type in {"playlist", "multi_video"} or entries_raw is not None

        if is_playlist:
            playlist_items: list[PlaylistItemInfo] = []
            entries_iter = entries_raw or []
            for idx, entry in enumerate(entries_iter, start=1):
                if idx > MAX_PLAYLIST_PREVIEW_ITEMS:
                    break
                if not entry or not isinstance(entry, dict):
                    playlist_items.append(
                        PlaylistItemInfo(
                            index=idx,
                            video_id=f"unavailable_{idx}",
                            title="[Unavailable Video]",
                            url="",
                            available=False,
                        )
                    )
                    continue

                vid_id = str(entry.get("id") or f"item_{idx}")
                raw_title = str(entry.get("title") or f"Track {idx}").strip()
                is_unavail = raw_title.lower() in {
                    "[private video]",
                    "[deleted video]",
                    "[unavailable video]",
                } or entry.get("availability") in {"private", "Premium_only", "subscriber_only"}

                item_url = str(
                    entry.get("url")
                    or entry.get("webpage_url")
                    or (f"https://www.youtube.com/watch?v={vid_id}" if vid_id else "")
                )
                if item_url and not item_url.startswith("http"):
                    item_url = f"https://www.youtube.com/watch?v={vid_id}"

                duration = self._safe_int(entry.get("duration"))
                uploader = str(entry.get("uploader") or entry.get("channel") or "").strip() or None

                playlist_items.append(
                    PlaylistItemInfo(
                        index=idx,
                        video_id=vid_id,
                        title=raw_title,
                        url=item_url,
                        duration_seconds=duration,
                        uploader=uploader,
                        available=not is_unavail,
                    )
                )

            pl_title = str(info.get("title") or info.get("playlist_title") or "YouTube Playlist").strip()
            pl_count = self._safe_int(info.get("playlist_count")) or len(playlist_items)
            uploader = str(
                info.get("uploader") or info.get("channel") or "YouTube Playlist"
            ).strip()
            thumb = self._select_safe_thumbnail(info)

            return MediaMetadata(
                url=original_url,
                webpage_url=str(info.get("webpage_url") or original_url),
                video_id=str(info.get("id") or "playlist"),
                title=pl_title,
                uploader=uploader,
                duration_seconds=None,
                thumbnail_url=thumb,
                is_playlist=True,
                playlist_title=pl_title,
                playlist_count=pl_count,
                playlist_items=playlist_items,
                available_heights=[2160, 1440, 1080, 720, 480, 360],
                available_video_exts=["mp4", "mkv", "webm"],
                available_audio_exts=["m4a", "webm"],
                has_separate_streams=True,
                has_muxed_streams=True,
            )

        formats = info.get("formats") or []
        heights: set[int] = set()
        video_exts: set[str] = set()
        audio_exts: set[str] = set()
        has_separate = False
        has_muxed = False

        if isinstance(formats, list):
            for fmt in formats:
                if not isinstance(fmt, dict):
                    continue
                vcodec = str(fmt.get("vcodec") or "none")
                acodec = str(fmt.get("acodec") or "none")
                ext = str(fmt.get("ext") or "").lower()
                height = self._safe_int(fmt.get("height"))

                has_video = vcodec != "none"
                has_audio = acodec != "none"

                if has_video and height and height > 0:
                    heights.add(height)
                if has_video and ext:
                    video_exts.add(ext)
                if has_audio and not has_video and ext:
                    audio_exts.add(ext)
                if has_video and has_audio:
                    has_muxed = True
                elif has_video and not has_audio:
                    has_separate = True

        sorted_heights = sorted(heights, reverse=True)
        title = str(info.get("title") or "Untitled Video").strip()
        uploader = str(info.get("uploader") or info.get("channel") or "Unknown Channel").strip()
        duration = self._safe_int(info.get("duration"))
        thumb = self._select_safe_thumbnail(info)

        return MediaMetadata(
            url=original_url,
            webpage_url=str(info.get("webpage_url") or original_url),
            video_id=str(info.get("id") or "video"),
            title=title,
            uploader=uploader,
            duration_seconds=duration,
            thumbnail_url=thumb,
            is_playlist=False,
            playlist_title=None,
            playlist_count=0,
            playlist_items=[],
            available_heights=sorted_heights,
            available_video_exts=sorted(video_exts),
            available_audio_exts=sorted(audio_exts),
            has_separate_streams=has_separate,
            has_muxed_streams=has_muxed or not has_separate,
        )

    @staticmethod
    def _safe_int(value: Any) -> int | None:
        """Convert a numeric value to int or return None."""
        if value is None:
            return None
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _select_safe_thumbnail(info: dict[str, Any]) -> str | None:
        """Return an HTTPS thumbnail URL from a known YouTube image host if available."""
        candidates: list[str] = []
        direct = info.get("thumbnail")
        if isinstance(direct, str) and direct.strip():
            candidates.append(direct.strip())

        thumbs = info.get("thumbnails")
        if isinstance(thumbs, list):
            for item in reversed(thumbs):
                if isinstance(item, dict) and isinstance(item.get("url"), str):
                    candidates.append(item["url"].strip())

        for url in candidates:
            try:
                parsed = urlparse(url)
                host = (parsed.hostname or "").lower()
                if parsed.scheme == "https" and (
                    host in ALLOWED_THUMBNAIL_HOSTS or host.endswith(".ytimg.com")
                ):
                    return url
            except ValueError:
                continue
        return None

    @staticmethod
    def fetch_thumbnail_bytes(thumbnail_url: str, timeout: float = 6.0) -> bytes | None:
        """Safely download thumbnail image bytes with host verification and size limit."""
        if not thumbnail_url:
            return None
        try:
            parsed = urlparse(thumbnail_url)
            host = (parsed.hostname or "").lower()
            if parsed.scheme != "https" or not (
                host in ALLOWED_THUMBNAIL_HOSTS or host.endswith(".ytimg.com")
            ):
                return None

            req = urllib.request.Request(
                thumbnail_url,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read(MAX_THUMBNAIL_BYTES + 1)
                if len(data) > MAX_THUMBNAIL_BYTES:
                    logger.warning("Thumbnail exceeded size limit (%d bytes); skipping.", len(data))
                    return None
                return data
        except Exception as exc:
            logger.debug("Could not fetch thumbnail '%s': %s", thumbnail_url, exc)
            return None

    @staticmethod
    def _map_metadata_exception(exc: Exception) -> MetadataError:
        """Translate yt-dlp or network exceptions into user-friendly MetadataError objects."""
        raw_msg = str(exc)
        lowered = raw_msg.lower()

        if "private video" in lowered or "sign in" in lowered or "members-only" in lowered:
            return MetadataError(
                "This media is private, restricted, or requires sign-in.",
                remedy="YouTube Downloader Studio only supports publicly accessible media you are authorized to download.",
            )
        if "video unavailable" in lowered or "has been removed" in lowered or "account associated" in lowered:
            return MetadataError(
                "This video or playlist is unavailable or has been removed.",
                remedy="Verify the link opens in your browser and is still available.",
            )
        if "429" in lowered or "too many requests" in lowered or "rate limit" in lowered:
            return MetadataError(
                "YouTube temporarily rate-limited metadata requests.",
                remedy="Wait a minute before trying again.",
            )
        if (
            "getaddrinfo failed" in lowered
            or "network is unreachable" in lowered
            or "timed out" in lowered
            or "connection" in lowered
            or "urlopen error" in lowered
        ):
            return MetadataError(
                "Could not connect to YouTube to retrieve media information.",
                remedy="Check your internet connection and firewall settings, then retry.",
            )
        if "drm" in lowered or "encrypted" in lowered:
            return MetadataError(
                "This content is DRM-protected and cannot be downloaded.",
                remedy="DRM-protected content is not supported.",
            )

        return MetadataError(
            "Could not retrieve metadata for the provided URL.",
            remedy="Confirm the URL is a valid, public YouTube link and try again.",
        )
