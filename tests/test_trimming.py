"""Automated unit and integration tests for Video/Audio Trimming (Sections 11.1 & 11.2)."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from src.core.events import EventBus
from src.core.exceptions import (
    DownloadCancelledError,
    DownloadError,
    FFmpegMissingError,
    ValidationError,
)
from src.core.models import (
    AudioFormat,
    AudioQuality,
    DependencyStatus,
    DownloadJob,
    DownloadMode,
    JobStatus,
    LocalMediaInfo,
    OverwriteBehavior,
    SourceType,
    VideoFormat,
    VideoQuality,
)
from src.core.validators import (
    format_timestamp,
    parse_timestamp,
    validate_local_media_path,
    validate_trim_range,
)
from src.services.download_service import DownloadService
from src.services.ffmpeg_service import FFmpegService


# ---------------------------------------------------------------------------
# Section 11.1 — Timestamp & Trim Validation Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw_input", "expected_seconds"),
    [
        ("0", 0.0),
        ("45", 45.0),
        ("45.5", 45.5),
        (90, 90.0),
        (12.345, 12.345),
        ("00:00", 0.0),
        ("01:05", 65.0),
        ("12:34.250", 754.25),
        ("02:30,500", 150.5),
        ("00:00:00", 0.0),
        ("01:02:03", 3723.0),
        ("02:15:30.5", 8130.5),
    ],
)
def test_parse_timestamp_valid_inputs(raw_input: str | float | int, expected_seconds: float) -> None:
    assert parse_timestamp(raw_input) == pytest.approx(expected_seconds)


@pytest.mark.parametrize(
    "invalid_input",
    [
        "",
        "   ",
        None,
        True,
        False,
        "-1",
        "-00:10",
        -0.01,
        float("nan"),
        float("inf"),
        "abc",
        "01:02:03:04",
        "1.5:30",
        "01:60",
        "00:60.0",
        "01:60:00",
        "01:00:60",
        "10::20",
    ],
)
def test_parse_timestamp_rejects_invalid_and_negative_inputs(invalid_input: Any) -> None:
    with pytest.raises(ValidationError):
        parse_timestamp(invalid_input)


def test_format_timestamp_representations() -> None:
    assert format_timestamp(0) == "00:00"
    assert format_timestamp(65) == "01:05"
    assert format_timestamp(3661) == "01:01:01"
    assert format_timestamp(65, force_hours=True) == "00:01:05"
    assert format_timestamp(65.25, include_millis=True) == "01:05.250"
    assert format_timestamp(65.25, include_millis=True, force_hours=True, srt_comma=True) == "00:01:05,250"


def test_validate_trim_range_valid_and_invalid_cases() -> None:
    start_s, end_s = validate_trim_range("00:15", "01:45", media_duration=120.0)
    assert start_s == 15.0
    assert end_s == 105.0

    # Rounding tolerance clamping at the end of media duration
    s_tol, e_tol = validate_trim_range(10.0, 120.03, media_duration=120.0)
    assert s_tol == 10.0
    assert e_tol == 120.0

    # Start == End rejected
    with pytest.raises(ValidationError, match="greater than Start Time"):
        validate_trim_range("01:00", "01:00")

    # Start > End rejected
    with pytest.raises(ValidationError, match="greater than Start Time"):
        validate_trim_range("02:00", "01:00")

    # Shorter than minimum clip length rejected
    with pytest.raises(ValidationError, match="shorter than the minimum clip length"):
        validate_trim_range("00:10.0", "00:10.2", min_clip_seconds=0.5)

    # Start >= media_duration rejected
    with pytest.raises(ValidationError, match="exceeds the media duration"):
        validate_trim_range("02:00", "02:30", media_duration=120.0)

    # End > media_duration rejected
    with pytest.raises(ValidationError, match="exceeds the media duration"):
        validate_trim_range("00:10", "02:05", media_duration=120.0)


def test_validate_local_media_path_checks_existence_extension_and_size(tmp_path: Path) -> None:
    valid_mp4 = tmp_path / "sample.mp4"
    valid_mp4.write_bytes(b"fake-mp4-content")
    assert validate_local_media_path(valid_mp4) == valid_mp4.resolve()

    valid_mp3 = tmp_path / "sample.mp3"
    valid_mp3.write_bytes(b"fake-mp3-content")
    assert validate_local_media_path(str(valid_mp3)) == valid_mp3.resolve()

    # Empty path
    with pytest.raises(ValidationError):
        validate_local_media_path("")

    # Missing file
    with pytest.raises(ValidationError, match="does not exist"):
        validate_local_media_path(tmp_path / "missing.mp4")

    # Directory instead of file
    with pytest.raises(ValidationError, match="folder"):
        validate_local_media_path(tmp_path)

    # Unsupported extension
    bad_ext = tmp_path / "script.exe"
    bad_ext.write_bytes(b"MZ")
    with pytest.raises(ValidationError, match="Unsupported media file extension"):
        validate_local_media_path(bad_ext)

    # Empty 0-byte file
    empty_mp4 = tmp_path / "empty.mp4"
    empty_mp4.write_bytes(b"")
    with pytest.raises(ValidationError, match="empty"):
        validate_local_media_path(empty_mp4)


# ---------------------------------------------------------------------------
# Section 11.2 — Trimming Workflow Tests (YouTube URL & Local Media)
# ---------------------------------------------------------------------------


class FakeTrimFFmpegService(FFmpegService):
    """Controllable FFmpegService for deterministic offline trimming tests."""

    def __init__(
        self,
        ffmpeg_available: bool = True,
        ffprobe_available: bool = True,
        probed_duration: float = 120.0,
        has_video: bool = True,
        has_audio: bool = True,
    ) -> None:
        super().__init__()
        self._ffmpeg_available = ffmpeg_available
        self._ffprobe_available = ffprobe_available
        self.probed_duration = probed_duration
        self.has_video = has_video
        self.has_audio = has_audio
        self.trim_calls: list[dict[str, Any]] = []

    def check_dependencies(self, force_refresh: bool = False) -> DependencyStatus:
        return DependencyStatus(
            python_version="3.13",
            yt_dlp_available=True,
            yt_dlp_version="2026.02.04",
            ffmpeg_available=self._ffmpeg_available,
            ffprobe_available=self._ffprobe_available,
            ffmpeg_path="C:/ffmpeg/bin/ffmpeg.exe" if self._ffmpeg_available else None,
            ffprobe_path="C:/ffmpeg/bin/ffprobe.exe" if self._ffprobe_available else None,
            ffmpeg_version="ffmpeg 7.0" if self._ffmpeg_available else None,
            ffprobe_version="ffprobe 7.0" if self._ffprobe_available else None,
        )

    def probe_local_media(self, file_path: str | Path) -> LocalMediaInfo:
        resolved = Path(file_path).resolve()
        return LocalMediaInfo(
            file_path=resolved,
            duration_seconds=self.probed_duration,
            has_video=self.has_video,
            has_audio=self.has_audio,
            video_codec="h264" if self.has_video else None,
            audio_codec="aac" if self.has_audio else None,
            subtitle_streams=0,
            container_format=resolved.suffix.lstrip(".").lower(),
        )

    def trim_media_file(
        self,
        source_path: Path | str,
        output_path: Path | str,
        start_seconds: float,
        end_seconds: float,
        accurate_trim: bool = True,
        audio_only_output: bool = False,
        audio_format: AudioFormat = AudioFormat.MP3,
        audio_quality: AudioQuality = AudioQuality.K192,
        cancel_event: threading.Event | None = None,
        probed_info: LocalMediaInfo | None = None,
        audio_only: bool | None = None,
    ) -> Path:
        if audio_only is not None:
            audio_only_output = audio_only
        if not self._ffmpeg_available:
            raise FFmpegMissingError("FFmpeg is required for trimming.")
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelledError()

        src_info = probed_info or self.probe_local_media(source_path)
        if audio_only_output and not src_info.has_audio:
            raise DownloadError(
                f"The selected file '{Path(source_path).name}' does not contain an audio stream."
            )

        out = Path(output_path).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"trimmed-media-payload-" + f"{start_seconds}-{end_seconds}".encode("ascii"))
        self.trim_calls.append(
            {
                "source_path": Path(source_path).resolve(),
                "output_path": out,
                "start_seconds": start_seconds,
                "end_seconds": end_seconds,
                "audio_only": audio_only_output,
                "accurate_trim": accurate_trim,
            }
        )
        # Subsequent probe on trimmed file returns the expected trimmed clip length
        self.probed_duration = round(end_seconds - start_seconds, 3)
        return out


def test_youtube_video_and_audio_trim_ydl_options_and_execution(tmp_path: Path) -> None:
    ffmpeg_svc = FakeTrimFFmpegService(probed_duration=180.0)
    bus = EventBus()

    out_file = tmp_path / "Trimmed Lecture [vidtrim1].mp4"

    def ydl_factory(opts: dict[str, Any]) -> Any:
        class _FakeYDL:
            def __init__(self, ydl_opts: dict[str, Any]) -> None:
                self.params = ydl_opts

            def __enter__(self) -> _FakeYDL:
                return self

            def __exit__(self, *args: object) -> None:
                pass

            def extract_info(self, url: str, download: bool = False) -> dict[str, Any]:
                return {
                    "id": "vidtrim1",
                    "title": "Trimmed Lecture",
                    "duration": 180,
                    "ext": "mp4",
                }

            def prepare_filename(self, info: dict[str, Any]) -> str:
                return str(out_file)

            def process_ie_result(self, info: dict[str, Any], download: bool = True) -> dict[str, Any]:
                out_file.write_bytes(b"initial-full-download-stream")
                for hook in self.params.get("progress_hooks", []):
                    hook(
                        {
                            "status": "downloading",
                            "downloaded_bytes": 500,
                            "total_bytes": 1000,
                            "speed": 2000.0,
                            "eta": 1,
                            "filename": str(out_file),
                        }
                    )
                    hook({"status": "finished", "filename": str(out_file)})
                info["requested_downloads"] = [{"filepath": str(out_file)}]
                return info

        return _FakeYDL(opts)

    dl_service = DownloadService(
        ffmpeg_service=ffmpeg_svc,
        event_bus=bus,
        ydl_factory=ydl_factory,
    )

    job = DownloadJob(
        url="https://www.youtube.com/watch?v=vidtrim1",
        destination_dir=tmp_path,
        mode=DownloadMode.VIDEO,
        video_quality=VideoQuality.Q1080P,
        video_format=VideoFormat.MP4,
        trim_enabled=True,
        trim_start_seconds=10.0,
        trim_end_seconds=40.0,
        trim_accurate=True,
    )

    opts = dl_service.build_ydl_options(job)
    assert callable(opts.get("download_ranges"))
    assert opts.get("force_keyframes_at_cuts") is True
    ranges = list(opts["download_ranges"]({"duration": 180}, None))
    assert ranges == [{"start_time": 10.0, "end_time": 40.0}]

    completed = dl_service.execute_job(job)
    assert completed.status == JobStatus.COMPLETED
    assert completed.output_path is not None and completed.output_path.exists()
    # Because FakeYDL produced a 180s file instead of a 30s clip, _ensure_url_download_trimmed
    # automatically ran FFmpegService.trim_media_file to guarantee the 30s output clip!
    assert len(ffmpeg_svc.trim_calls) == 1
    assert ffmpeg_svc.trim_calls[0]["start_seconds"] == 10.0
    assert ffmpeg_svc.trim_calls[0]["end_seconds"] == 40.0


def test_youtube_trim_rejects_range_exceeding_remote_video_duration(tmp_path: Path) -> None:
    ffmpeg_svc = FakeTrimFFmpegService(probed_duration=60.0)

    def ydl_factory(opts: dict[str, Any]) -> Any:
        class _ShortVideoYDL:
            def __enter__(self) -> _ShortVideoYDL:
                return self

            def __exit__(self, *args: object) -> None:
                pass

            def extract_info(self, url: str, download: bool = True) -> dict[str, Any]:
                # Trigger download_ranges callback with 60s duration while job asks for 10..90s
                if callable(opts.get("download_ranges")):
                    opts["download_ranges"]({"duration": 60}, self)
                return {"id": "short1", "title": "Short Clip", "duration": 60}

        return _ShortVideoYDL()

    dl_service = DownloadService(ffmpeg_service=ffmpeg_svc, ydl_factory=ydl_factory)
    job = DownloadJob(
        url="https://www.youtube.com/watch?v=short1",
        destination_dir=tmp_path,
        mode=DownloadMode.AUDIO,
        audio_format=AudioFormat.MP3,
        trim_enabled=True,
        trim_start_seconds=10.0,
        trim_end_seconds=90.0,
    )

    with pytest.raises((ValidationError, DownloadError)):
        dl_service.execute_job(job)
    assert job.status == JobStatus.FAILED


def test_local_video_and_audio_trimming_preserves_original_file(tmp_path: Path) -> None:
    source_video = tmp_path / "original_recording.mp4"
    original_bytes = b"original-untouched-video-stream-bytes-1234567890"
    source_video.write_bytes(original_bytes)

    out_dir = tmp_path / "exports"
    ffmpeg_svc = FakeTrimFFmpegService(probed_duration=120.0, has_video=True, has_audio=True)
    dl_service = DownloadService(ffmpeg_service=ffmpeg_svc, event_bus=EventBus())

    job = DownloadJob(
        url=str(source_video),
        source_type=SourceType.LOCAL_FILE,
        local_source_path=source_video,
        destination_dir=out_dir,
        mode=DownloadMode.VIDEO,
        video_format=VideoFormat.MP4,
        trim_enabled=True,
        trim_start_seconds=5.0,
        trim_end_seconds=25.0,
        trim_accurate=True,
    )

    result = dl_service.execute_job(job)
    assert result.status == JobStatus.COMPLETED
    assert result.output_path is not None and result.output_path.exists()
    assert result.output_path != source_video.resolve()
    assert "_trimmed_" in result.output_path.name
    # Original source file must remain 100% untouched
    assert source_video.read_bytes() == original_bytes


def test_local_video_only_file_handles_video_and_audio_modes_gracefully(tmp_path: Path) -> None:
    silent_video = tmp_path / "silent_screen_capture.mp4"
    silent_video.write_bytes(b"video-only-stream")

    ffmpeg_svc = FakeTrimFFmpegService(probed_duration=60.0, has_video=True, has_audio=False)
    dl_service = DownloadService(ffmpeg_service=ffmpeg_svc)

    # 1. Trimming as video succeeds even without an audio stream
    v_job = DownloadJob(
        url=str(silent_video),
        source_type=SourceType.LOCAL_FILE,
        local_source_path=silent_video,
        destination_dir=tmp_path,
        mode=DownloadMode.VIDEO,
        trim_enabled=True,
        trim_start_seconds=2.0,
        trim_end_seconds=12.0,
    )
    completed = dl_service.execute_job(v_job)
    assert completed.status == JobStatus.COMPLETED
    assert completed.output_path is not None and completed.output_path.exists()

    # 2. Requesting Audio Only extraction from a silent video-only file fails clearly
    ffmpeg_svc_2 = FakeTrimFFmpegService(probed_duration=60.0, has_video=True, has_audio=False)
    dl_service_2 = DownloadService(ffmpeg_service=ffmpeg_svc_2)
    a_job = DownloadJob(
        url=str(silent_video),
        source_type=SourceType.LOCAL_FILE,
        local_source_path=silent_video,
        destination_dir=tmp_path,
        mode=DownloadMode.AUDIO,
        audio_format=AudioFormat.MP3,
        trim_enabled=True,
        trim_start_seconds=2.0,
        trim_end_seconds=12.0,
    )
    with pytest.raises(DownloadError, match="does not contain an audio stream"):
        dl_service_2.execute_job(a_job)
    assert a_job.status == JobStatus.FAILED


def test_local_trim_cancellation_cleans_up_and_marks_job_cancelled(tmp_path: Path) -> None:
    source_audio = tmp_path / "podcast.mp3"
    source_audio.write_bytes(b"podcast-audio-bytes")

    ffmpeg_svc = FakeTrimFFmpegService(probed_duration=300.0, has_video=False, has_audio=True)
    dl_service = DownloadService(ffmpeg_service=ffmpeg_svc)

    cancel_event = threading.Event()
    cancel_event.set()

    job = DownloadJob(
        url=str(source_audio),
        source_type=SourceType.LOCAL_FILE,
        local_source_path=source_audio,
        destination_dir=tmp_path,
        mode=DownloadMode.AUDIO,
        audio_format=AudioFormat.MP3,
        trim_enabled=True,
        trim_start_seconds=10.0,
        trim_end_seconds=30.0,
    )

    with pytest.raises(DownloadCancelledError):
        dl_service.execute_job(job, cancel_event=cancel_event)
    assert job.status == JobStatus.CANCELLED


def test_ffmpeg_stderr_probe_fallback_when_ffprobe_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify probe_local_media parses duration and streams from ffmpeg -i stderr when ffprobe is unavailable."""
    sample = tmp_path / "clip.mp4"
    sample.write_bytes(b"fake-media-bytes")

    svc = FFmpegService()
    monkeypatch.setattr(
        svc,
        "check_dependencies",
        lambda force_refresh=False: DependencyStatus(
            python_version="3.13",
            yt_dlp_available=True,
            yt_dlp_version="2026.02.04",
            ffmpeg_available=True,
            ffprobe_available=False,
            ffmpeg_path="C:/ffmpeg/bin/ffmpeg.exe",
            ffprobe_path=None,
        ),
    )

    fake_stderr = (
        "Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'clip.mp4':\n"
        "  Duration: 00:02:15.50, start: 0.000000, bitrate: 1280 kb/s\n"
        "  Stream #0:0[0x1](und): Video: h264 (High), yuv420p, 1920x1080, 30 fps\n"
        "  Stream #0:1[0x2](eng): Audio: aac (LC), 48000 Hz, stereo, fltp, 128 kb/s\n"
    )
    monkeypatch.setattr(
        svc,
        "run_cancellable_command",
        lambda *a, **kw: subprocess.CompletedProcess(args=a[0], returncode=1, stdout="", stderr=fake_stderr),
    )

    info = svc.probe_local_media(sample)
    assert info.duration_seconds == pytest.approx(135.5)
    assert info.has_video is True
    assert info.has_audio is True
    assert info.video_codec == "h264"
    assert info.audio_codec == "aac"
    assert info.container_format == "mp4"


def test_real_ffmpeg_local_media_probe_and_trim_integration(tmp_path: Path) -> None:
    """Use the real installed FFmpeg binary (if present) to generate, probe, and trim a media clip."""
    svc = FFmpegService()
    dep = svc.check_dependencies(force_refresh=True)
    if not dep.ffmpeg_available or not dep.ffmpeg_path:
        pytest.skip("FFmpeg binary not available on this host.")

    source_mp4 = tmp_path / "synthetic_source.mp4"
    # Generate a 4-second synthetic video+audio MP4 using lavfi testsrc + sine
    gen_cmd = [
        dep.ffmpeg_path,
        "-hide_banner",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc=duration=4:size=320x240:rate=15",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=4",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(source_mp4),
    ]
    res = subprocess.run(gen_cmd, capture_output=True, text=True, timeout=30, check=False)
    assert res.returncode == 0 and source_mp4.exists()
    original_size = source_mp4.stat().st_size

    probed_src = svc.probe_local_media(source_mp4)
    assert probed_src.duration_seconds == pytest.approx(4.0, abs=0.5)
    assert probed_src.has_video is True
    assert probed_src.has_audio is True

    trimmed_mp4 = tmp_path / "trimmed_clip.mp4"
    out_path = svc.trim_media_file(
        source_path=source_mp4,
        output_path=trimmed_mp4,
        start_seconds=1.0,
        end_seconds=3.0,
        audio_only=False,
        accurate_trim=True,
    )
    assert out_path.exists() and out_path.stat().st_size > 0
    assert source_mp4.stat().st_size == original_size

    probed_trim = svc.probe_local_media(out_path)
    assert probed_trim.duration_seconds == pytest.approx(2.0, abs=0.5)
    assert probed_trim.has_video is True
    assert probed_trim.has_audio is True
