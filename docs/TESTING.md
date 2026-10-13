# Testing Guide

**YouTube Downloader Studio** includes a comprehensive automated test suite built with `pytest` and `pytest-cov`. All default tests run 100% offline using isolated temporary directories (`YTD_STUDIO_DATA_DIR` / `tmp_path`) so your real `%LOCALAPPDATA%\YouTubeDownloaderStudio` data and Downloads folder are never modified.

---

## Running the Test Suite

From the repository root:

```powershell
# Run all offline automated unit, queue, pause/resume, cancellation, GUI, and EXE tests
.\.venv\Scripts\python.exe -m pytest -v

# Run with line coverage report across src/
.\.venv\Scripts\python.exe -m pytest --cov=src --cov-report=term-missing
```

To run a specific test module or marker:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_pause_resume.py -v
.\.venv\Scripts\python.exe -m pytest -m gui -v
.\.venv\Scripts\python.exe -m pytest -m packaging -v
```

### Opt-In Live Network Integration Tests

Live network tests (`@pytest.mark.live`) are skipped by default. To execute them against authorized public/Creative-Commons YouTube URLs:

```powershell
$env:YDS_TEST_VIDEO_URL = "https://www.youtube.com/watch?v=<authorized_video_id>"
$env:YDS_TEST_AUDIO_URL = "https://www.youtube.com/watch?v=<authorized_video_id>"
$env:YDS_TEST_PLAYLIST_URL = "https://www.youtube.com/playlist?list=<authorized_playlist_id>"
$env:YDS_TEST_FAIL_URL = "https://www.youtube.com/watch?v=invalid_id_000"

.\.venv\Scripts\python.exe -m pytest --run-live -m live -v
```

---

## Test Modules Overview

1. **`tests/test_validators.py`**:
   - Empty, whitespace, control-character, and shell-metacharacter URLs.
   - Valid YouTube watch, short (`youtu.be`), mobile, music, shorts, and playlist URLs with strict video/playlist ID slug validation.
   - Rejection of dangerous schemes (`file:`, `javascript:`, `data:`, `ftp:`), local paths, embedded credentials, and non-YouTube domains.
   - Multiline batch URL parsing, deduplication, and line-numbered error reporting.
   - Filename sanitization (stripping path traversal `..`, slashes, Windows reserved characters, reserved device names `CON`/`NUL`/`COM1`–`LPT9`, trailing dots/spaces, and bounding 300-character filenames).
   - Output filename template and playlist index validation.
2. **`tests/test_paths.py`**:
   - User data directory resolution (`custom_dir` and `YTD_STUDIO_DATA_DIR`), frozen PyInstaller bundle path resolution (`sys._MEIPASS`), directory writability verification, and non-destructive file collision resolution (`lecture (1).mp4`, `lecture (2).mp4`).
3. **`tests/test_database.py`**:
   - SQLite schema creation, WAL mode initialization, reopening across restarts, CRUD operations, search/filter/sort queries (including `Paused` status), thread-safe concurrent access, and corruption recovery.
4. **`tests/test_settings.py`**:
   - Default settings creation, validation, dual persistence (JSON + SQLite), automatic recovery when `settings.json` is malformed or missing keys, and rejection of invalid concurrency counts, unsafe filename templates, or missing custom FFmpeg paths.
5. **`tests/test_download_service.py`**:
   - Video and audio option construction (`continuedl=True`, `nopart=False`), FFmpeg present vs absent behavior, audio conversion verification (rejecting unconverted `.webm` when `.mp3` is requested), 0-byte output file rejection, progress hook normalization, error mapping, and `FFmpegService` detection.
6. **`tests/test_playlist_service.py`**:
   - Single-item vs full-playlist vs selected-items (`1,3,5`) option building, validation of invalid/empty playlist indices, unavailable/private playlist entry handling, and playlist output templates.
7. **`tests/test_queue_service.py`**:
   - State machine transition enforcement, single/multi-worker concurrency limits, batch URL submission, fault isolation when a worker raises `DownloadError` or `RuntimeError`, retry state reset, and a 30-job stress test verifying zero worker thread leaks.
8. **`tests/test_pause_resume.py`**:
   - Cooperative pause and resume (`Active -> Paused -> Queued -> Active -> Completed`), `.part` file preservation on pause vs cleanup on cancel, progress retention across resume, late progress event suppression, and unfinished job recovery across application restarts.
9. **`tests/test_cancellation.py`**:
   - Cancelling queued, metadata-fetching, downloading, playlist, and post-processing jobs; `.part`/`.part-Frag*`/`.ytdl` cleanup while preserving completed media files; and retrying cancelled jobs.
10. **`tests/test_ui_logic.py`**:
    - UI formatters, status badges, and full `MainWindow` workflow testing (tab navigation, Video/Audio mode switching, FFmpeg-missing option filtering, URL validation dialogs, queue Pause/Resume/Cancel/Retry, History search/filter, Settings save/reset, Dark/Light theme toggle, and clean shutdown).
11. **`tests/test_packaging.py`**:
    - Verification of `dist\YouTubeDownloaderStudio\YouTubeDownloaderStudio.exe`, bundled assets (`customtkinter`, `yt_dlp_ejs`, `app_icon.ico`), and isolated execution with `--check-env` and `--smoke-test-ui --data-dir`.
12. **`tests/integration/test_live_downloads.py`**:
    - Opt-in live download tests for metadata, video, audio/MP3, playlists, failure recovery, pause/resume, and cancellation.

---

## Smoke Testing the Application Entry Point

To verify that all services initialize and the CustomTkinter UI launches cleanly in an isolated data directory without touching user data:

```powershell
.\.venv\Scripts\python.exe run.py --check-env --data-dir "$env:TEMP\yds-smoke"
.\.venv\Scripts\python.exe run.py --smoke-test-ui --data-dir "$env:TEMP\yds-smoke"
```
