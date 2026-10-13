"""Comprehensive unit tests for DownloadService, FFmpegService, and MediaInfoService."""

from __future__ import annotations

import errno
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
    MetadataError,
)
from src.core.models import (
    AudioFormat,
    AudioQuality,
    DependencyStatus,
    DownloadJob,
    DownloadMode,
    JobStatus,
    OverwriteBehavior,
    PlaylistMode,
    VideoFormat,
    VideoQuality,
)
from src.services.download_service import DownloadService
from src.services.ffmpeg_service import FFmpegService
from src.services.media_info_service import MediaInfoService


class StubFFmpegService(FFmpegService):
    """Test double for FFmpegService with controllable FFmpeg and FFprobe availability."""

    def __init__(
        self,
        available: bool = True,
        path: str | None = r"C:\ffmpeg\bin\ffmpeg.exe",
        ffprobe_available: bool | None = None,
    ) -> None:
        super().__init__()
        self._available = available
        self._path = path if available else None
        self._ffprobe_available = available if ffprobe_available is None else ffprobe_available

    def check_dependencies(self, force_refresh: bool = False) -> DependencyStatus:
        ffprobe_path = (
            self._path.replace("ffmpeg.exe", "ffprobe.exe")
            if (self._ffprobe_available and self._path)
            else None
        )
        return DependencyStatus(
            python_version="3.13.14",
            yt_dlp_available=True,
            yt_dlp_version="2026.8.19",
            ffmpeg_available=self._available,
            ffmpeg_path=self._path,
            ffmpeg_version="ffmpeg version 7.1" if self._available else None,
            ffprobe_available=self._ffprobe_available,
            ffprobe_path=ffprobe_path,
            ffprobe_version="ffprobe version 7.1" if self._ffprobe_available else None,
        )


# ----------------------------------------------------------------------
# Section 5: Video Download Option Construction & Execution
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("quality", "expected_snippet"),
    [
        (VideoQuality.BEST, "bestvideo"),
        (VideoQuality.Q2160P, "height<=2160"),
        (VideoQuality.Q1440P, "height<=1440"),
        (VideoQuality.Q1080P, "height<=1080"),
        (VideoQuality.Q720P, "height<=720"),
        (VideoQuality.Q480P, "height<=480"),
        (VideoQuality.Q360P, "height<=360"),
        (VideoQuality.LOWEST, "worstvideo+worstaudio/worst"),
    ],
)
def test_video_quality_selectors_with_ffmpeg(
    tmp_path: Path,
    quality: VideoQuality,
    expected_snippet: str,
) -> None:
    dl_service = DownloadService(ffmpeg_service=StubFFmpegService(available=True))
    job = DownloadJob(
        url="https://www.youtube.com/watch?v=abc123XYZ",
        destination_dir=tmp_path,
        mode=DownloadMode.VIDEO,
        video_quality=quality,
        video_format=VideoFormat.MP4,
    )
    opts = dl_service.build_ydl_options(job)
    assert expected_snippet in opts["format"]
    assert opts["continuedl"] is True
    assert opts["nopart"] is False


@pytest.mark.parametrize(
    "container",
    [VideoFormat.MP4, VideoFormat.MKV, VideoFormat.WEBM, VideoFormat.AUTO],
)
def test_video_container_options(tmp_path: Path, container: VideoFormat) -> None:
    dl_service = DownloadService(ffmpeg_service=StubFFmpegService(available=True))
    job = DownloadJob(
        url="https://www.youtube.com/watch?v=abc123XYZ",
        destination_dir=tmp_path,
        mode=DownloadMode.VIDEO,
        video_quality=VideoQuality.Q1080P,
        video_format=container,
    )
    opts = dl_service.build_ydl_options(job)
    if container == VideoFormat.AUTO:
        assert "merge_output_format" not in opts
    else:
        assert opts["merge_output_format"] == container.value


@pytest.mark.parametrize(
    "quality",
    [
        VideoQuality.BEST,
        VideoQuality.Q1080P,
        VideoQuality.Q720P,
        VideoQuality.Q480P,
        VideoQuality.LOWEST,
    ],
)
def test_video_options_when_ffmpeg_unavailable_uses_progressive(
    tmp_path: Path,
    quality: VideoQuality,
) -> None:
    dl_service = DownloadService(ffmpeg_service=StubFFmpegService(available=False))
    job = DownloadJob(
        url="https://www.youtube.com/watch?v=abc123XYZ",
        destination_dir=tmp_path,
        mode=DownloadMode.VIDEO,
        video_quality=quality,
        video_format=VideoFormat.MP4,
    )
    opts = dl_service.build_ydl_options(job)
    assert "+" not in opts["format"]
    assert "vcodec!=none" in opts["format"]
    assert "acodec!=none" in opts["format"]
    assert "merge_output_format" not in opts


# ----------------------------------------------------------------------
# Section 6: Audio Download & Conversion Tests
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "audio_fmt",
    [AudioFormat.MP3, AudioFormat.WAV, AudioFormat.OPUS, AudioFormat.FLAC],
)
@pytest.mark.parametrize(
    "bitrate",
    [AudioQuality.BEST, AudioQuality.K320, AudioQuality.K256, AudioQuality.K192, AudioQuality.K128],
)
def test_audio_conversion_formats_and_bitrates(
    tmp_path: Path,
    audio_fmt: AudioFormat,
    bitrate: AudioQuality,
) -> None:
    dl_ok = DownloadService(ffmpeg_service=StubFFmpegService(available=True))
    job = DownloadJob(
        url="https://www.youtube.com/watch?v=abc123XYZ",
        destination_dir=tmp_path,
        mode=DownloadMode.AUDIO,
        audio_format=audio_fmt,
        audio_quality=bitrate,
    )
    opts = dl_ok.build_ydl_options(job)
    assert opts["format"] == "bestaudio/best"
    assert opts["postprocessors"][0]["key"] == "FFmpegExtractAudio"
    assert opts["postprocessors"][0]["preferredcodec"] == audio_fmt.value
    assert opts["postprocessors"][0]["preferredquality"] == bitrate.value

    # When FFmpeg is unavailable, all conversion formats must raise FFmpegMissingError
    dl_missing = DownloadService(ffmpeg_service=StubFFmpegService(available=False))
    with pytest.raises(FFmpegMissingError):
        dl_missing.build_ydl_options(job)


def test_audio_native_best_and_m4a_without_ffmpeg(tmp_path: Path) -> None:
    dl_missing = DownloadService(ffmpeg_service=StubFFmpegService(available=False))
    for fmt in (AudioFormat.BEST, AudioFormat.M4A):
        job = DownloadJob(
            url="https://www.youtube.com/watch?v=abc123XYZ",
            destination_dir=tmp_path,
            mode=DownloadMode.AUDIO,
            audio_format=fmt,
        )
        opts = dl_missing.build_ydl_options(job)
        assert "postprocessors" not in opts


def test_audio_conversion_failure_does_not_falsely_report_mp3_success(tmp_path: Path) -> None:
    """Regression test: if FFmpeg fails to produce .mp3 and leaves .webm, job must FAIL."""
    unconverted_webm = tmp_path / "Song [abc123XYZ].webm"

    class FakeFailedConversionYDL:
        def __init__(self, params: dict[str, Any]) -> None:
            self.params = params

        def __enter__(self) -> FakeFailedConversionYDL:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def extract_info(self, url: str, download: bool = False) -> dict[str, Any]:
            return {"id": "abc123XYZ", "title": "Song", "ext": "webm"}

        def prepare_filename(self, info: dict[str, Any]) -> str:
            return str(unconverted_webm)

        def process_ie_result(self, info: dict[str, Any], download: bool = True) -> dict[str, Any]:
            unconverted_webm.write_bytes(b"raw webm audio bytes")
            for hook in self.params.get("progress_hooks", []):
                hook({"status": "finished", "filename": str(unconverted_webm)})
            # Notice: no .mp3 file is ever created!
            return info

    dl_service = DownloadService(
        ffmpeg_service=StubFFmpegService(True),
        ydl_factory=FakeFailedConversionYDL,
    )
    job = DownloadJob(
        url="https://www.youtube.com/watch?v=abc123XYZ",
        destination_dir=tmp_path,
        mode=DownloadMode.AUDIO,
        audio_format=AudioFormat.MP3,
    )
    with pytest.raises(DownloadError):
        dl_service.execute_job(job)

    assert job.status == JobStatus.FAILED


def test_empty_zero_byte_output_file_is_rejected(tmp_path: Path) -> None:
    """Regression test: a 0-byte output file must not be marked COMPLETED."""
    empty_mp4 = tmp_path / "Empty [abc123XYZ].mp4"

    class FakeZeroByteYDL:
        def __init__(self, params: dict[str, Any]) -> None:
            self.params = params

        def __enter__(self) -> FakeZeroByteYDL:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def extract_info(self, url: str, download: bool = False) -> dict[str, Any]:
            return {"id": "abc123XYZ", "title": "Empty", "ext": "mp4"}

        def prepare_filename(self, info: dict[str, Any]) -> str:
            return str(empty_mp4)

        def process_ie_result(self, info: dict[str, Any], download: bool = True) -> dict[str, Any]:
            empty_mp4.write_bytes(b"")  # 0 bytes!
            for hook in self.params.get("progress_hooks", []):
                hook({"status": "finished", "filename": str(empty_mp4)})
            return info

    dl_service = DownloadService(
        ffmpeg_service=StubFFmpegService(True),
        ydl_factory=FakeZeroByteYDL,
    )
    job = DownloadJob(
        url="https://www.youtube.com/watch?v=abc123XYZ",
        destination_dir=tmp_path,
    )
    with pytest.raises(DownloadError) as exc_info:
        dl_service.execute_job(job)

    assert "empty" in exc_info.value.user_message.lower()
    assert job.status == JobStatus.FAILED


def test_existing_file_collision_renames_automatically(tmp_path: Path) -> None:
    existing_file = tmp_path / "Demo Title [abc123XYZ].mp4"
    existing_file.write_bytes(b"original content")
    captured_outtmpl: list[str] = []

    class FakeCollisionYDL:
        def __init__(self, params: dict[str, Any]) -> None:
            self.params = params

        def __enter__(self) -> FakeCollisionYDL:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def extract_info(self, url: str, download: bool = False) -> dict[str, Any]:
            return {"id": "abc123XYZ", "title": "Demo Title", "ext": "mp4"}

        def prepare_filename(self, info: dict[str, Any]) -> str:
            return str(existing_file)

        def process_ie_result(self, info: dict[str, Any], download: bool = True) -> dict[str, Any]:
            new_tmpl = self.params["outtmpl"]["default"]
            captured_outtmpl.append(new_tmpl)
            target_file = Path(new_tmpl.replace("%(ext)s", "mp4"))
            target_file.write_bytes(b"new video content")
            for hook in self.params.get("progress_hooks", []):
                hook({"status": "finished", "filename": str(target_file)})
            return info

    dl_service = DownloadService(
        ffmpeg_service=StubFFmpegService(True),
        ydl_factory=FakeCollisionYDL,
    )
    job = DownloadJob(
        url="https://www.youtube.com/watch?v=abc123XYZ",
        destination_dir=tmp_path,
        overwrite_behavior=OverwriteBehavior.RENAME,
    )
    completed = dl_service.execute_job(job)
    assert completed.status == JobStatus.COMPLETED
    assert completed.output_path is not None
    assert completed.output_path.name == "Demo Title [abc123XYZ] (1).mp4"
    assert existing_file.read_bytes() == b"original content"


# ----------------------------------------------------------------------
# Section 11: Progress Hook Edge Cases (Negative ETA, Out-of-order, Late events)
# ----------------------------------------------------------------------


def test_progress_hook_edge_cases_and_bounds(tmp_path: Path) -> None:
    bus = EventBus()
    dl_service = DownloadService(ffmpeg_service=StubFFmpegService(True), event_bus=bus)
    job = DownloadJob(
        url="https://www.youtube.com/watch?v=abc123XYZ",
        destination_dir=tmp_path,
    )
    job.transition_to(JobStatus.STARTING)
    tracked: list[Path] = []
    hook = dl_service._make_progress_hook(job, cancel_event=None, pause_event=None, tracked_files=tracked)

    # 1. Negative ETA and negative/NaN speed must be normalized to None
    hook(
        {
            "status": "downloading",
            "downloaded_bytes": 500,
            "total_bytes": 1000,
            "speed": -100.0,
            "eta": -5,
        }
    )
    assert job.progress_percent == 50.0
    assert job.speed_bps is None
    assert job.eta_seconds is None

    # 2. Out-of-order lower progress on the same item must not regress progress_percent
    hook(
        {
            "status": "downloading",
            "downloaded_bytes": 300,
            "total_bytes": 1000,
            "speed": float("nan"),
            "eta": 10,
        }
    )
    assert job.progress_percent == 50.0
    assert job.downloaded_bytes == 500
    assert job.speed_bps is None
    assert job.eta_seconds == 10

    # 3. Unknown total_bytes does not falsely report 100%
    job2 = DownloadJob(url="https://www.youtube.com/watch?v=abc123XYZ", destination_dir=tmp_path)
    job2.transition_to(JobStatus.STARTING)
    hook2 = dl_service._make_progress_hook(job2, None, None, [])
    hook2({"status": "downloading", "downloaded_bytes": 204800})
    assert job2.progress_percent == 0.0
    assert job2.total_bytes is None

    # 4. Late event after job is COMPLETED must not change status back to DOWNLOADING
    job.transition_to(JobStatus.COMPLETED)
    hook({"status": "downloading", "downloaded_bytes": 600, "total_bytes": 1000})
    assert job.status == JobStatus.COMPLETED
    assert job.progress_percent == 100.0


# ----------------------------------------------------------------------
# Section 14 & 15: FFmpeg/FFprobe Permutations & Error Injection
# ----------------------------------------------------------------------


def test_ffmpeg_service_detection_permutations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dir_with_spaces = tmp_path / "Custom FFmpeg Folder"
    dir_with_spaces.mkdir()
    fake_ffmpeg = dir_with_spaces / ("ffmpeg.exe" if Path("C:/").exists() else "ffmpeg")
    fake_ffprobe = dir_with_spaces / ("ffprobe.exe" if Path("C:/").exists() else "ffprobe")
    fake_ffmpeg.write_bytes(b"exe")
    fake_ffprobe.write_bytes(b"exe")

    def _mock_run_ok(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        exe_name = Path(args[0]).stem.lower()
        return subprocess.CompletedProcess(args, 0, stdout=f"{exe_name} version 7.1-custom\n", stderr="")

    monkeypatch.setattr(subprocess, "run", _mock_run_ok)

    # 1. Both FFmpeg and FFprobe available in path with spaces
    svc = FFmpegService(custom_ffmpeg_path=str(dir_with_spaces))
    st = svc.check_dependencies(force_refresh=True)
    assert st.ffmpeg_available is True
    assert st.ffprobe_available is True
    assert st.full_media_processing_ready is True
    assert "FFmpeg + FFprobe Ready" in st.summary_text

    # 2. FFmpeg only (FFprobe missing) -> still ready for media processing
    fake_ffprobe.unlink()
    st_no_probe = svc.check_dependencies(force_refresh=True)
    assert st_no_probe.ffmpeg_available is True
    assert st_no_probe.ffprobe_available is False
    assert st_no_probe.full_media_processing_ready is True
    assert "FFprobe missing" in st_no_probe.summary_text

    # 3. FFmpeg returns nonzero exit code -> treated as unavailable
    def _mock_run_fail(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="corrupt binary")

    monkeypatch.setattr(subprocess, "run", _mock_run_fail)
    st_failed = svc.check_dependencies(force_refresh=True)
    assert st_failed.ffmpeg_available is False


@pytest.mark.parametrize(
    ("raw_exc", "expected_keyword"),
    [
        (OSError(errno.ENOSPC, "No space left on device"), "disk space"),
        (RuntimeError("HTTP Error 429: Too Many Requests"), "rate-limited"),
        (RuntimeError("HTTP Error 403: Forbidden"), "403"),
        (RuntimeError("HTTP Error 404: Not Found"), "404"),
        (RuntimeError("ERROR: Private video. Sign in"), "private"),
        (RuntimeError("ERROR: Video unavailable. This video has been removed"), "no longer available"),
        (RuntimeError("ERROR: Requested format is not available"), "format"),
        (RuntimeError("urlopen error [Errno 11001] getaddrinfo failed"), "network"),
        (RuntimeError("Permission denied: 'output.mp4'"), "permission denied"),
        (RuntimeError("ERROR: Postprocessing: ffprobe and ffmpeg not found"), "ffmpeg"),
    ],
)
def test_error_mapping_all_injected_failures(raw_exc: Exception, expected_keyword: str) -> None:
    mapped = DownloadService.map_download_exception(raw_exc)
    assert expected_keyword in mapped.user_message.lower()
    assert mapped.remedy
