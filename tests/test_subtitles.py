"""Automated tests for Subtitle Discovery, Export (SRT/VTT/TXT), Captions-Only Mode, and Soft-Subtitle Embedding (Sections 11.3, 11.4, 11.5)."""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from src.core.events import EventBus, EventType
from src.core.exceptions import (
    DownloadCancelledError,
    DownloadError,
    FFmpegMissingError,
    ValidationError,
)
from src.core.models import (
    CaptionSource,
    DependencyStatus,
    DownloadJob,
    DownloadMode,
    JobStatus,
    OverwriteBehavior,
    PlaylistMode,
    SubtitleFormat,
    VideoFormat,
)
from src.core.validators import validate_subtitle_languages
from src.database.db import DatabaseManager
from src.database.repositories import HistoryRepository
from src.services.download_service import DownloadService
from src.services.ffmpeg_service import FFmpegService
from src.services.media_info_service import MediaInfoService
from src.services.queue_service import QueueService
from src.services.subtitle_service import (
    SubtitleCue,
    SubtitleService,
    clean_cue_text,
    convert_subtitle_content,
    convert_subtitle_file,
    export_cues_to_srt,
    export_cues_to_txt,
    export_cues_to_vtt,
    extract_caption_tracks,
    parse_subtitle_content,
    resolve_language_name,
)

SAMPLE_VTT_MANUAL_EN = """WEBVTT
Kind: captions
Language: en

NOTE Manual English subtitle track

00:00:01.000 --> 00:00:03.500 align:start position:0%
Welcome to <b>YouTube Downloader Studio</b>!

00:00:04.000 --> 00:00:06.250
Here we test &amp; verify subtitle parsing.
"""

SAMPLE_VTT_AUTO_ROLLING = """WEBVTT
Kind: captions
Language: en

00:00:01.000 --> 00:00:02.500
Hello<00:00:01.500><c> world</c><c.colorE5E5E5> from auto captions</c>

00:00:02.500 --> 00:00:04.000
Hello world from auto captions
Second line of rolling speech

00:00:04.000 --> 00:00:06.000
Second line of rolling speech
Final concluding sentence
"""

SAMPLE_SRT_ES = """1
00:00:01,200 --> 00:00:03,800
¡Hola mundo!

2
00:00:04,100 --> 00:00:06,500
Bienvenido al estudio de descargas.
"""

SAMPLE_JSON3 = json.dumps(
    {
        "events": [
            {
                "tStartMs": 1500,
                "dDurationMs": 2000,
                "segs": [{"utf8": "First JSON3 "}, {"utf8": "caption line"}],
            },
            {
                "tStartMs": 4000,
                "dDurationMs": 2500,
                "segs": [{"utf8": "Second JSON3 caption line"}],
            },
        ]
    }
)

SAMPLE_SRV3_XML = """<?xml version="1.0" encoding="utf-8" ?>
<timedtext format="3">
<body>
<p t="1000" d="2500">XML caption line one</p>
<p t="4000" d="2000">XML caption &amp; line two</p>
</body>
</timedtext>
"""


class FakeSubtitleYDL:
    """Configurable offline yt-dlp stub for subtitle discovery and Captions-Only execution tests."""

    info_response: dict[str, Any] = {}
    entry_responses: dict[str, dict[str, Any]] = {}
    recorded_init_opts: list[dict[str, Any]] = []
    recorded_download_urls: list[list[str]] = []
    cancel_during_download: threading.Event | None = None
    write_raw_files_on_download: bool = True

    def __init__(self, opts: dict[str, Any]) -> None:
        self.opts = opts
        self.params = opts
        FakeSubtitleYDL.recorded_init_opts.append(opts)

    def __enter__(self) -> FakeSubtitleYDL:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        return None

    def extract_info(self, url: str, download: bool = False) -> dict[str, Any]:
        if url in self.entry_responses:
            return dict(self.entry_responses[url])
        return dict(self.info_response)

    def prepare_filename(self, info: dict[str, Any]) -> str:
        outtmpl = self.opts.get("outtmpl", {})
        tmpl_str = outtmpl.get("default") if isinstance(outtmpl, dict) else str(outtmpl)
        tmpl_path = Path(tmpl_str) if tmpl_str else Path("test.%(ext)s")
        stem = f"{info.get('title', 'video')} [{info.get('id', '1')}]"
        return str(tmpl_path.parent / f"{stem}.mp4")

    def process_ie_result(self, info: dict[str, Any], download: bool = True) -> dict[str, Any]:
        return info

    def download(self, urls: list[str]) -> int:
        FakeSubtitleYDL.recorded_download_urls.append(list(urls))
        if self.cancel_during_download is not None:
            self.cancel_during_download.set()

        if not self.write_raw_files_on_download:
            return 0

        outtmpl = self.opts.get("outtmpl", {})
        tmpl_str = outtmpl.get("default") if isinstance(outtmpl, dict) else str(outtmpl)
        if not tmpl_str:
            return 0

        base_no_ext = tmpl_str.replace(".%(ext)s", "")
        langs: list[str] = list(self.opts.get("subtitleslangs") or [])
        target_url = urls[0] if urls else ""
        info = self.entry_responses.get(target_url, self.info_response)

        subs_dict = info.get("subtitles") or {}
        auto_dict = info.get("automatic_captions") or {}

        for lang in langs:
            written = False
            if self.opts.get("writesubtitles") and lang in subs_dict:
                fmt_list = subs_dict[lang]
                content = fmt_list[0].get("data", SAMPLE_VTT_MANUAL_EN) if fmt_list else SAMPLE_VTT_MANUAL_EN
                ext = fmt_list[0].get("ext", "vtt") if fmt_list else "vtt"
                Path(f"{base_no_ext}.{lang}.{ext}").write_text(content, encoding="utf-8")
                written = True
            if not written and self.opts.get("writeautomaticsub") and lang in auto_dict:
                fmt_list = auto_dict[lang]
                content = fmt_list[0].get("data", SAMPLE_VTT_AUTO_ROLLING) if fmt_list else SAMPLE_VTT_AUTO_ROLLING
                ext = fmt_list[0].get("ext", "vtt") if fmt_list else "vtt"
                Path(f"{base_no_ext}.{lang}.{ext}").write_text(content, encoding="utf-8")

        return 0


@pytest.fixture(autouse=True)
def reset_fake_subtitle_ydl() -> None:
    FakeSubtitleYDL.info_response = {}
    FakeSubtitleYDL.entry_responses = {}
    FakeSubtitleYDL.recorded_init_opts = []
    FakeSubtitleYDL.recorded_download_urls = []
    FakeSubtitleYDL.cancel_during_download = None
    FakeSubtitleYDL.write_raw_files_on_download = True


# ============================================================================
# Section 11.3 — Subtitle Discovery, Structured Parsing, and Format Conversion
# ============================================================================


def test_resolve_language_name_handles_known_regional_and_unknown_codes() -> None:
    assert resolve_language_name("en") == "English"
    assert resolve_language_name("en-US") == "English (US)"
    assert resolve_language_name("es-419") == "Spanish (Latin America)"
    assert resolve_language_name("fr-CA") == "French (fr-CA)"
    assert resolve_language_name("pt", raw_name="Português (Brasil)") == "Português (Brasil)"
    assert resolve_language_name("xyz") == "xyz"


def test_extract_caption_tracks_distinguishes_manual_and_auto_captions() -> None:
    info = {
        "id": "vid123",
        "title": "Caption Discovery Test",
        "subtitles": {
            "en": [{"ext": "vtt"}, {"ext": "srt"}],
            "es": [{"ext": "vtt", "name": "Spanish (Manual)"}],
            "live_chat": [{"ext": "json"}],  # Must be ignored
        },
        "automatic_captions": {
            "en": [{"ext": "vtt"}, {"ext": "srv3"}],
            "ja": [{"ext": "vtt"}],
        },
    }

    tracks = extract_caption_tracks(info)
    manual_tracks = [t for t in tracks if not t.is_automatic]
    auto_tracks = [t for t in tracks if t.is_automatic]

    assert [t.lang_code for t in manual_tracks] == ["en", "es"]
    assert [t.lang_code for t in auto_tracks] == ["en", "ja"]
    assert all(t.lang_code != "live_chat" for t in tracks)

    en_manual = manual_tracks[0]
    assert en_manual.source == CaptionSource.MANUAL
    assert en_manual.display_label == "English (en) [Manual]"
    assert "vtt" in en_manual.available_exts

    ja_auto = [t for t in auto_tracks if t.lang_code == "ja"][0]
    assert ja_auto.source == CaptionSource.AUTOMATIC
    assert ja_auto.display_label == "Japanese (ja) [Auto-generated]"


def test_media_info_service_populates_caption_tracks_in_metadata() -> None:
    FakeSubtitleYDL.info_response = {
        "id": "meta_sub_01",
        "title": "Lecture With Captions",
        "uploader": "University Channel",
        "duration": 600,
        "subtitles": {"en": [{"ext": "vtt"}]},
        "automatic_captions": {"de": [{"ext": "vtt"}]},
        "formats": [{"format_id": "18", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a", "height": 360}],
    }
    service = MediaInfoService(ydl_factory=FakeSubtitleYDL)
    meta = service.fetch_metadata("https://www.youtube.com/watch?v=meta_sub_01")

    assert len(meta.manual_caption_tracks) == 1
    assert meta.manual_caption_tracks[0].lang_code == "en"
    assert len(meta.automatic_caption_tracks) == 1
    assert meta.automatic_caption_tracks[0].lang_code == "de"
    assert meta.has_captions is True


def test_parse_vtt_srt_json3_and_xml_into_structured_cues() -> None:
    vtt_cues = parse_subtitle_content(SAMPLE_VTT_MANUAL_EN, source_ext="vtt")
    assert len(vtt_cues) == 2
    assert vtt_cues[0] == SubtitleCue(
        start_seconds=1.0,
        end_seconds=3.5,
        text="Welcome to YouTube Downloader Studio!",
    )
    assert vtt_cues[1].text == "Here we test & verify subtitle parsing."

    srt_cues = parse_subtitle_content(SAMPLE_SRT_ES, source_ext="srt")
    assert len(srt_cues) == 2
    assert srt_cues[0].start_seconds == pytest.approx(1.2)
    assert srt_cues[0].end_seconds == pytest.approx(3.8)
    assert srt_cues[0].text == "¡Hola mundo!"

    json_cues = parse_subtitle_content(SAMPLE_JSON3, source_ext="json3")
    assert len(json_cues) == 2
    assert json_cues[0].text == "First JSON3 caption line"
    assert json_cues[1].start_seconds == pytest.approx(4.0)

    xml_cues = parse_subtitle_content(SAMPLE_SRV3_XML, source_ext="srv3")
    assert len(xml_cues) == 2
    assert xml_cues[0].text == "XML caption line one"
    assert xml_cues[1].text == "XML caption & line two"


def test_convert_vtt_to_srt_and_srt_to_vtt_roundtrip() -> None:
    srt_output = convert_subtitle_content(
        SAMPLE_VTT_MANUAL_EN,
        target_format=SubtitleFormat.SRT,
        source_ext="vtt",
    )
    assert "1\n00:00:01,000 --> 00:00:03,500\nWelcome to YouTube Downloader Studio!" in srt_output
    assert "2\n00:00:04,000 --> 00:00:06,250\nHere we test & verify subtitle parsing." in srt_output
    assert "WEBVTT" not in srt_output

    vtt_output = convert_subtitle_content(
        SAMPLE_SRT_ES,
        target_format=SubtitleFormat.VTT,
        source_ext="srt",
    )
    assert vtt_output.startswith("WEBVTT\n\n")
    assert "00:00:01.200 --> 00:00:03.800\n¡Hola mundo!" in vtt_output


def test_export_clean_txt_without_and_with_timestamps_deduplicates_rolling_cues() -> None:
    clean_txt = convert_subtitle_content(
        SAMPLE_VTT_AUTO_ROLLING,
        target_format=SubtitleFormat.TXT,
        source_ext="vtt",
        txt_include_timestamps=False,
    )
    lines = [ln for ln in clean_txt.strip().splitlines() if ln.strip()]
    assert lines == [
        "Hello world from auto captions",
        "Second line of rolling speech",
        "Final concluding sentence",
    ]
    assert "-->" not in clean_txt
    assert "<c>" not in clean_txt
    assert "WEBVTT" not in clean_txt

    timestamped_txt = convert_subtitle_content(
        SAMPLE_VTT_AUTO_ROLLING,
        target_format=SubtitleFormat.TXT,
        source_ext="vtt",
        txt_include_timestamps=True,
    )
    ts_lines = [ln for ln in timestamped_txt.strip().splitlines() if ln.strip()]
    assert ts_lines == [
        "[00:01] Hello world from auto captions",
        "[00:02] Second line of rolling speech",
        "[00:04] Final concluding sentence",
    ]


def test_validate_subtitle_languages_parsing_and_rejection() -> None:
    assert validate_subtitle_languages("en, es-419, fr-CA , en") == ["en", "es-419", "fr-CA"]
    assert validate_subtitle_languages(["ja", "ko"]) == ["ja", "ko"]
    with pytest.raises(ValidationError):
        validate_subtitle_languages("   ")
    with pytest.raises(ValidationError):
        validate_subtitle_languages("en, ../bad_lang!")


# ============================================================================
# Section 11.4 — Mandatory Captions-Only Mode Tests
# ============================================================================


def test_captions_only_downloads_manual_srt_without_media_or_ffmpeg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify Captions Only mode exports manual SRT, creates zero media files, and never calls FFmpeg or build_ydl_options."""
    FakeSubtitleYDL.info_response = {
        "id": "cap_srt_01",
        "title": "Clean Architecture Talk",
        "webpage_url": "https://www.youtube.com/watch?v=cap_srt_01",
        "subtitles": {
            "en": [{"ext": "vtt", "data": SAMPLE_VTT_MANUAL_EN}],
        },
        "automatic_captions": {},
    }

    ffmpeg_service = FFmpegService()
    # Explicitly fail if any FFmpeg method is called during Captions Only mode
    monkeypatch.setattr(
        ffmpeg_service,
        "check_dependencies",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("FFmpeg check should not be called in Captions Only mode")),
    )
    monkeypatch.setattr(
        ffmpeg_service,
        "run_cancellable_command",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("FFmpeg subprocess should never run in Captions Only mode")),
    )

    bus = EventBus()
    sub_service = SubtitleService(event_bus=bus, ydl_factory=FakeSubtitleYDL)
    dl_service = DownloadService(
        ffmpeg_service=ffmpeg_service,
        event_bus=bus,
        ydl_factory=FakeSubtitleYDL,
        subtitle_service=sub_service,
    )
    monkeypatch.setattr(
        dl_service,
        "build_ydl_options",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("Media build_ydl_options must not be called in Captions Only mode")),
    )

    job = DownloadJob(
        url="https://www.youtube.com/watch?v=cap_srt_01",
        mode=DownloadMode.CAPTIONS,
        destination_dir=tmp_path,
        subtitle_languages=["en"],
        include_auto_captions=False,
        subtitle_format=SubtitleFormat.SRT,
    )

    result = dl_service.execute_job(job)

    assert result.status == JobStatus.COMPLETED
    assert len(result.exported_subtitle_paths) == 1
    srt_path = result.exported_subtitle_paths[0]
    assert srt_path.exists()
    assert srt_path.name == "Clean Architecture Talk [cap_srt_01].en.srt"
    content = srt_path.read_text(encoding="utf-8")
    assert "00:00:01,000 --> 00:00:03,500" in content
    assert "Welcome to YouTube Downloader Studio!" in content

    # Verify zero video/audio or temporary files exist in destination directory
    all_files = list(tmp_path.iterdir())
    assert all_files == [srt_path]
    assert all(opts.get("skip_download") is True for opts in FakeSubtitleYDL.recorded_init_opts)


def test_captions_only_downloads_auto_vtt_and_clean_or_timestamped_txt_when_ffmpeg_absent(
    tmp_path: Path,
) -> None:
    """Verify Captions Only succeeds for auto VTT, clean TXT, and timestamped TXT even when FFmpeg is unavailable."""
    FakeSubtitleYDL.info_response = {
        "id": "auto_cap_02",
        "title": "Keynote Speech",
        "webpage_url": "https://www.youtube.com/watch?v=auto_cap_02",
        "subtitles": {},
        "automatic_captions": {
            "en": [{"ext": "vtt", "data": SAMPLE_VTT_AUTO_ROLLING}],
        },
    }

    class MissingFFmpegService(FFmpegService):
        def check_dependencies(self, force_refresh: bool = False) -> DependencyStatus:
            return DependencyStatus(
                python_version="3.13",
                yt_dlp_available=True,
                yt_dlp_version="2026.02",
                ffmpeg_available=False,
                ffprobe_available=False,
            )

    sub_service = SubtitleService(ydl_factory=FakeSubtitleYDL)
    dl_service = DownloadService(
        ffmpeg_service=MissingFFmpegService(),
        ydl_factory=FakeSubtitleYDL,
        subtitle_service=sub_service,
    )

    # 1. Auto-caption VTT
    vtt_job = DownloadJob(
        url="https://www.youtube.com/watch?v=auto_cap_02",
        mode=DownloadMode.CAPTIONS,
        destination_dir=tmp_path,
        subtitle_languages=["en"],
        include_auto_captions=True,
        subtitle_format=SubtitleFormat.VTT,
    )
    dl_service.execute_job(vtt_job)
    assert vtt_job.status == JobStatus.COMPLETED
    assert vtt_job.output_path is not None
    assert vtt_job.output_path.name == "Keynote Speech [auto_cap_02].en.auto.vtt"
    assert vtt_job.output_path.read_text(encoding="utf-8").startswith("WEBVTT")

    # 2. Clean TXT without timestamps
    txt_job = DownloadJob(
        url="https://www.youtube.com/watch?v=auto_cap_02",
        mode=DownloadMode.CAPTIONS,
        destination_dir=tmp_path,
        subtitle_languages=["en"],
        include_auto_captions=True,
        subtitle_format=SubtitleFormat.TXT,
        txt_include_timestamps=False,
    )
    dl_service.execute_job(txt_job)
    assert txt_job.status == JobStatus.COMPLETED
    assert txt_job.output_path is not None
    txt_text = txt_job.output_path.read_text(encoding="utf-8")
    assert txt_text.strip().splitlines() == [
        "Hello world from auto captions",
        "Second line of rolling speech",
        "Final concluding sentence",
    ]

    # 3. TXT with timestamps enabled (RENAME overwrite behavior creates _1.txt)
    ts_txt_job = DownloadJob(
        url="https://www.youtube.com/watch?v=auto_cap_02",
        mode=DownloadMode.CAPTIONS,
        destination_dir=tmp_path,
        subtitle_languages=["en"],
        include_auto_captions=True,
        subtitle_format=SubtitleFormat.TXT,
        txt_include_timestamps=True,
        overwrite_behavior=OverwriteBehavior.RENAME,
    )
    dl_service.execute_job(ts_txt_job)
    assert ts_txt_job.status == JobStatus.COMPLETED
    assert ts_txt_job.output_path is not None
    assert ts_txt_job.output_path.name.endswith(" (1).txt")
    assert "[00:01] Hello world from auto captions" in ts_txt_job.output_path.read_text(encoding="utf-8")


def test_captions_only_multi_language_download_in_single_job(tmp_path: Path) -> None:
    FakeSubtitleYDL.info_response = {
        "id": "multi_lang_03",
        "title": "Multilingual Course",
        "webpage_url": "https://www.youtube.com/watch?v=multi_lang_03",
        "subtitles": {
            "en": [{"ext": "vtt", "data": SAMPLE_VTT_MANUAL_EN}],
            "es": [{"ext": "srt", "data": SAMPLE_SRT_ES}],
        },
        "automatic_captions": {
            "fr": [{"ext": "vtt", "data": SAMPLE_VTT_AUTO_ROLLING}],
        },
    }

    sub_service = SubtitleService(ydl_factory=FakeSubtitleYDL)
    job = DownloadJob(
        url="https://www.youtube.com/watch?v=multi_lang_03",
        mode=DownloadMode.CAPTIONS,
        destination_dir=tmp_path,
        subtitle_languages=["en", "es", "fr"],
        include_auto_captions=True,
        subtitle_format=SubtitleFormat.SRT,
    )

    sub_service.execute_captions_only_job(job)

    assert job.status == JobStatus.COMPLETED
    names = sorted(p.name for p in job.exported_subtitle_paths)
    assert names == [
        "Multilingual Course [multi_lang_03].en.srt",
        "Multilingual Course [multi_lang_03].es.srt",
        "Multilingual Course [multi_lang_03].fr.auto.srt",
    ]
    # When multiple files are exported, output_path points to destination directory
    assert job.output_path == tmp_path.resolve()


def test_captions_only_fails_clearly_when_no_captions_or_auto_disabled(tmp_path: Path) -> None:
    sub_service = SubtitleService(ydl_factory=FakeSubtitleYDL)

    # Case 1: Video has no captions at all
    FakeSubtitleYDL.info_response = {
        "id": "no_caps_01",
        "title": "Silent Instrumental",
        "subtitles": {},
        "automatic_captions": {},
    }
    job_none = DownloadJob(
        url="https://www.youtube.com/watch?v=no_caps_01",
        mode=DownloadMode.CAPTIONS,
        destination_dir=tmp_path,
        subtitle_languages=["en"],
        include_auto_captions=True,
    )
    with pytest.raises(DownloadError, match="No manual subtitles or automatic captions are available"):
        sub_service.execute_captions_only_job(job_none)
    assert job_none.status == JobStatus.FAILED

    # Case 2: Only auto-captions exist, but include_auto_captions is False
    FakeSubtitleYDL.info_response = {
        "id": "only_auto_02",
        "title": "Auto Captions Only Video",
        "subtitles": {},
        "automatic_captions": {"en": [{"ext": "vtt", "data": SAMPLE_VTT_AUTO_ROLLING}]},
    }
    job_auto_off = DownloadJob(
        url="https://www.youtube.com/watch?v=only_auto_02",
        mode=DownloadMode.CAPTIONS,
        destination_dir=tmp_path,
        subtitle_languages=["en"],
        include_auto_captions=False,
    )
    with pytest.raises(DownloadError, match="has no manual subtitles"):
        sub_service.execute_captions_only_job(job_auto_off)
    assert job_auto_off.status == JobStatus.FAILED


def test_captions_only_queue_execution_progress_stages_and_cancellation(tmp_path: Path) -> None:
    FakeSubtitleYDL.info_response = {
        "id": "q_cap_01",
        "title": "Queue Caption Video",
        "webpage_url": "https://www.youtube.com/watch?v=q_cap_01",
        "subtitles": {"en": [{"ext": "vtt", "data": SAMPLE_VTT_MANUAL_EN}]},
        "automatic_captions": {},
    }

    bus = EventBus()
    db = DatabaseManager(tmp_path / "history.db")
    history_repo = HistoryRepository(db)
    sub_service = SubtitleService(event_bus=bus, ydl_factory=FakeSubtitleYDL)
    dl_service = DownloadService(
        ffmpeg_service=FFmpegService(),
        event_bus=bus,
        ydl_factory=FakeSubtitleYDL,
        subtitle_service=sub_service,
    )
    queue_service = QueueService(
        download_service=dl_service,
        history_repository=history_repo,
        event_bus=bus,
        max_concurrent_downloads=1,
        auto_start=True,
    )

    job = queue_service.add_job(
        url="https://www.youtube.com/watch?v=q_cap_01",
        destination_dir=tmp_path,
        mode=DownloadMode.CAPTIONS,
        subtitle_languages=["en"],
        include_auto_captions=False,
        subtitle_format=SubtitleFormat.SRT,
    )
    assert queue_service.wait_for_idle(timeout=5.0) is True
    assert job.status == JobStatus.COMPLETED

    events = bus.poll_events()
    stage_messages = [
        ev.data["job"].stage_detail
        for ev in events
        if ev.data.get("job") is not None and ev.data["job"].stage_detail
    ]
    assert any("Completed" in msg for msg in stage_messages)

    # Test cancellation cleans up temporary subtitle files
    cancel_dir = tmp_path / "cancel_sub"
    cancel_dir.mkdir()
    cancel_event = threading.Event()
    FakeSubtitleYDL.cancel_during_download = cancel_event

    cancel_job = DownloadJob(
        url="https://www.youtube.com/watch?v=q_cap_01",
        mode=DownloadMode.CAPTIONS,
        destination_dir=cancel_dir,
        subtitle_languages=["en"],
    )
    with pytest.raises(DownloadCancelledError):
        sub_service.execute_captions_only_job(cancel_job, cancel_event=cancel_event)

    assert cancel_job.status == JobStatus.CANCELLED
    assert list(cancel_dir.glob(".ytd_sub_tmp_*")) == []


def test_captions_only_playlist_full_and_selected_items(tmp_path: Path) -> None:
    entry1_url = "https://www.youtube.com/watch?v=pl_item_1"
    entry2_url = "https://www.youtube.com/watch?v=pl_item_2"

    FakeSubtitleYDL.info_response = {
        "_type": "playlist",
        "id": "PL_caps_99",
        "title": "Caption Playlist Series",
        "entries": [
            {"id": "pl_item_1", "title": "Episode 1", "webpage_url": entry1_url},
            {"id": "pl_item_2", "title": "Episode 2", "webpage_url": entry2_url},
        ],
    }
    FakeSubtitleYDL.entry_responses = {
        entry1_url: {
            "id": "pl_item_1",
            "title": "Episode 1",
            "webpage_url": entry1_url,
            "subtitles": {"en": [{"ext": "vtt", "data": SAMPLE_VTT_MANUAL_EN}]},
            "automatic_captions": {},
        },
        entry2_url: {
            "id": "pl_item_2",
            "title": "Episode 2",
            "webpage_url": entry2_url,
            "subtitles": {"en": [{"ext": "srt", "data": SAMPLE_SRT_ES}]},
            "automatic_captions": {},
        },
    }

    sub_service = SubtitleService(ydl_factory=FakeSubtitleYDL)
    job = DownloadJob(
        url="https://www.youtube.com/playlist?list=PL_caps_99",
        mode=DownloadMode.CAPTIONS,
        destination_dir=tmp_path,
        playlist_mode=PlaylistMode.SELECTED_ITEMS,
        selected_playlist_indices=[1, 2],
        subtitle_languages=["en"],
        subtitle_format=SubtitleFormat.SRT,
    )

    sub_service.execute_captions_only_job(job)

    assert job.status == JobStatus.COMPLETED
    assert len(job.exported_subtitle_paths) == 2
    assert {p.name for p in job.exported_subtitle_paths} == {
        "Episode 1 [pl_item_1].en.srt",
        "Episode 2 [pl_item_2].en.srt",
    }
    assert any(opts.get("playlist_items") == "1,2" for opts in FakeSubtitleYDL.recorded_init_opts)


# ============================================================================
# Section 11.5 — Soft-Subtitle Embedding Tests
# ============================================================================


def test_ffmpeg_embed_soft_subtitles_builds_expected_codec_args_for_mp4_mkv_webm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify soft-subtitle muxing uses mov_text for MP4, srt for MKV, webvtt for WebM, and copies A/V streams."""
    ffmpeg = FFmpegService()
    monkeypatch.setattr(
        ffmpeg,
        "check_dependencies",
        lambda force_refresh=False: DependencyStatus(
            python_version="3.13",
            yt_dlp_available=True,
            yt_dlp_version="2026.02",
            ffmpeg_available=True,
            ffprobe_available=True,
            ffmpeg_path="C:/ffmpeg/bin/ffmpeg.exe",
            ffprobe_path="C:/ffmpeg/bin/ffprobe.exe",
        ),
    )

    recorded_cmds: list[list[str]] = []

    def fake_run_cancellable(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        recorded_cmds.append(cmd)
        out_path = Path(cmd[-1])
        out_path.write_bytes(b"muxed-video-with-soft-subs" * 20)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(ffmpeg, "run_cancellable_command", fake_run_cancellable)
    monkeypatch.setattr(ffmpeg, "verify_subtitle_streams", lambda _p, min_streams=1: True)

    srt_en = tmp_path / "sub.en.srt"
    srt_es = tmp_path / "sub.es.srt"
    srt_en.write_text(SAMPLE_SRT_ES, encoding="utf-8")
    srt_es.write_text(SAMPLE_SRT_ES, encoding="utf-8")

    for ext, expected_sub_codec in ((".mp4", "mov_text"), (".mkv", "srt"), (".webm", "webvtt")):
        recorded_cmds.clear()
        vid = tmp_path / f"movie{ext}"
        vid.write_bytes(b"original-video-bytes" * 20)
        ffmpeg.embed_soft_subtitles(
            video_path=vid,
            subtitle_tracks=[(srt_en, "en"), (srt_es, "es")],
        )
        assert len(recorded_cmds) == 1
        cmd = recorded_cmds[0]
        assert "-c:v" in cmd and cmd[cmd.index("-c:v") + 1] == "copy"
        assert "-c:a" in cmd and cmd[cmd.index("-c:a") + 1] == "copy"
        assert "-c:s" in cmd and cmd[cmd.index("-c:s") + 1] == expected_sub_codec
        assert "language=en" in cmd
        assert "language=es" in cmd


def test_ffmpeg_embed_soft_subtitles_rejects_unsupported_container_and_missing_ffmpeg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ffmpeg = FFmpegService()
    srt = tmp_path / "sub.en.srt"
    srt.write_text(SAMPLE_SRT_ES, encoding="utf-8")
    avi = tmp_path / "video.avi"
    avi.write_bytes(b"avi-bytes")

    # 1. Missing FFmpeg raises FFmpegMissingError
    monkeypatch.setattr(
        ffmpeg,
        "check_dependencies",
        lambda force_refresh=False: DependencyStatus(
            python_version="3.13",
            yt_dlp_available=True,
            yt_dlp_version="2026.02",
            ffmpeg_available=False,
            ffprobe_available=False,
        ),
    )
    with pytest.raises(FFmpegMissingError):
        ffmpeg.embed_soft_subtitles(video_path=avi, subtitle_tracks=[(srt, "en")])

    # 2. Unsupported .avi container raises DownloadError
    monkeypatch.setattr(
        ffmpeg,
        "check_dependencies",
        lambda force_refresh=False: DependencyStatus(
            python_version="3.13",
            yt_dlp_available=True,
            yt_dlp_version="2026.02",
            ffmpeg_available=True,
            ffprobe_available=True,
            ffmpeg_path="C:/ffmpeg/bin/ffmpeg.exe",
        ),
    )
    with pytest.raises(DownloadError, match="does not support soft-subtitle embedding"):
        ffmpeg.embed_soft_subtitles(video_path=avi, subtitle_tracks=[(srt, "en")])


def test_video_download_with_soft_subtitle_embedding_keeps_or_removes_sidecar_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify video download + embed_subtitles=True muxes subtitles and honors keep_subtitle_files."""

    class FakeVideoWithSubsYDL(FakeSubtitleYDL):
        def extract_info(self, url: str, download: bool = False) -> dict[str, Any]:
            info = {
                "id": "vid_embed_01",
                "title": "Embedded Subtitles Video",
                "webpage_url": url,
                "ext": "mp4",
                "subtitles": {"en": [{"ext": "vtt", "data": SAMPLE_VTT_MANUAL_EN}]},
                "automatic_captions": {},
            }
            return info

        def process_ie_result(self, info: dict[str, Any], download: bool = True) -> dict[str, Any]:
            if download:
                out_file = tmp_path / "Embedded Subtitles Video [vid_embed_01].mp4"
                out_file.write_bytes(b"video-stream-payload" * 50)
                info["requested_downloads"] = [{"filepath": str(out_file)}]
            return info

    ffmpeg = FFmpegService()
    monkeypatch.setattr(
        ffmpeg,
        "check_dependencies",
        lambda force_refresh=False: DependencyStatus(
            python_version="3.13",
            yt_dlp_available=True,
            yt_dlp_version="2026.02",
            ffmpeg_available=True,
            ffprobe_available=True,
            ffmpeg_path="C:/ffmpeg/bin/ffmpeg.exe",
            ffprobe_path="C:/ffmpeg/bin/ffprobe.exe",
        ),
    )

    embedded_calls: list[tuple[Path, list[tuple[Path, str]]]] = []

    def fake_embed(
        video_path: Path,
        subtitle_tracks: list[tuple[Path, str]],
        cancel_event: threading.Event | None = None,
    ) -> Path:
        embedded_calls.append((video_path, list(subtitle_tracks)))
        return video_path

    monkeypatch.setattr(ffmpeg, "embed_soft_subtitles", fake_embed)

    sub_service = SubtitleService(ydl_factory=FakeVideoWithSubsYDL)
    dl_service = DownloadService(
        ffmpeg_service=ffmpeg,
        ydl_factory=FakeVideoWithSubsYDL,
        subtitle_service=sub_service,
    )

    # Case A: embed_subtitles=True, keep_subtitle_files=False -> sidecar .srt removed after embedding
    job_remove_sidecar = DownloadJob(
        url="https://www.youtube.com/watch?v=vid_embed_01",
        mode=DownloadMode.VIDEO,
        video_format=VideoFormat.MP4,
        destination_dir=tmp_path,
        download_subtitles=True,
        embed_subtitles=True,
        keep_subtitle_files=False,
        subtitle_languages=["en"],
        subtitle_format=SubtitleFormat.SRT,
    )
    dl_service.execute_job(job_remove_sidecar)
    assert job_remove_sidecar.status == JobStatus.COMPLETED
    assert len(embedded_calls) == 1
    assert list(tmp_path.glob("*.srt")) == []

    # Case B: embed_subtitles=True, keep_subtitle_files=True -> sidecar .srt retained alongside .mp4
    job_keep_sidecar = DownloadJob(
        url="https://www.youtube.com/watch?v=vid_embed_01",
        mode=DownloadMode.VIDEO,
        video_format=VideoFormat.MP4,
        destination_dir=tmp_path,
        download_subtitles=True,
        embed_subtitles=True,
        keep_subtitle_files=True,
        subtitle_languages=["en"],
        subtitle_format=SubtitleFormat.SRT,
        overwrite_behavior=OverwriteBehavior.OVERWRITE,
    )
    dl_service.execute_job(job_keep_sidecar)
    assert job_keep_sidecar.status == JobStatus.COMPLETED
    retained_srts = list(tmp_path.glob("*.srt"))
    assert len(retained_srts) == 1
    assert retained_srts[0].exists()


def test_real_ffmpeg_soft_subtitle_embedding_and_stream_verification(tmp_path: Path) -> None:
    """Integration test with real FFmpeg binary: mux an SRT track into an MP4 and verify the embedded subtitle stream."""
    ffmpeg = FFmpegService()
    status = ffmpeg.check_dependencies(force_refresh=True)
    if not status.ffmpeg_available or not status.ffmpeg_path:
        pytest.skip("FFmpeg binary not available on system PATH")

    video_path = tmp_path / "sample_video.mp4"
    srt_path = tmp_path / "sample_video.en.srt"
    srt_path.write_text(SAMPLE_SRT_ES, encoding="utf-8")

    # Generate a 2-second synthetic MP4 video
    gen_cmd = [
        status.ffmpeg_path,
        "-hide_banner",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=darkgreen:s=160x120:d=2",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=2",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(video_path),
    ]
    subprocess.run(gen_cmd, check=True, capture_output=True, text=True, timeout=30)
    assert ffmpeg.verify_subtitle_streams(video_path, min_streams=1) is False

    muxed_path = ffmpeg.embed_soft_subtitles(
        video_path=video_path,
        subtitle_tracks=[(srt_path, "en")],
    )
    assert muxed_path == video_path.resolve()
    assert ffmpeg.verify_subtitle_streams(video_path, min_streams=1) is True
