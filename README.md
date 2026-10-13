# YouTube Downloader Studio

**YouTube Downloader Studio** is a Windows desktop application written in Python for managing authorized video, audio, and playlist downloads. It pairs a modern **CustomTkinter** desktop interface with **yt-dlp**, **FFmpeg/FFprobe**, and **SQLite** persistence.

---

## Features

- **Three First-Class Download Modes (`Video`, `Audio Only`, `Captions Only`)**:
  - **Video Mode**: Quality preferences (`Best Available`, `2160p (4K)`, `1440p (2K)`, `1080p`, `720p`, `480p`, `360p`, `Lowest Available`) across `MP4`, `MKV`, `WebM`, or `Auto` containers.
  - **Audio Only Mode**: Audio extraction & conversion (`Best Audio Stream (No Re-encode)`, `M4A`, `MP3`, `WAV`, `Opus`, `FLAC`) with selectable target bitrates (`320k`, `256k`, `192k`, `128k`).
  - **Standalone `Captions Only` Mode**: Downloads manual subtitles and/or auto-generated captions **without** downloading video or audio streams and **without** requiring FFmpeg.
- **Video & Audio Trimming (YouTube URLs & Local Media Files)**:
  - Switch **Input Source** between **YouTube URL** and **Local Media File** (`.mp4`, `.mkv`, `.webm`, `.mov`, `.mp3`, `.m4a`, `.wav`, `.opus`, `.flac`).
  - **Synchronized Visual Timeline & Timestamp Inputs**: Enter `Start Time` and `End Time` in `SS`, `MM:SS`, or `HH:MM:SS(.mmm)` format or drag the interactive start/end timeline handles with live clip duration calculation and validation.
  - **Non-Destructive Local Trimming**: Local files are trimmed into a new `<stem>_trimmed_<start>-<end>.<ext>` file; the original source file is never overwritten.
- **Subtitle Discovery, Export (`SRT`, `VTT`, `TXT`), & Soft-Subtitle Embedding**:
  - **Manual vs Auto-Generated Track Discovery**: Metadata inspection lists creator-uploaded subtitles (`[Manual]`) and YouTube speech-recognition captions (`[Auto]`) with human-readable language labels (`English (en)`, `Spanish (es)`, etc.) and multi-language batch support (`en, es` or `all`).
  - **Structured Subtitle Export**: Exports clean, UTF-8 **SubRip (`.srt`)**, **WebVTT (`.vtt`)**, or readable plain-text **Transcript (`.txt`)** (automatically stripping cue numbers, VTT `<c>` markup, and rolling auto-caption duplicate lines, with an optional **TXT Timestamps** toggle).
  - **Optional Soft-Subtitle Embedding**: Muxes selectable subtitle tracks into downloaded `MP4` (`mov_text`), `MKV` (`srt`), or `WebM` (`webvtt`) videos without re-encoding video or audio streams, while optionally retaining separate subtitle files alongside the video.
- **Metadata Preview & Playlist Support**:
  - Background metadata inspection showing video title, channel/uploader, duration, caption track counts, thumbnail preview, and available resolutions without blocking the UI.
  - Playlist detection with options to process a **Single Video Only**, **Full Playlist**, or **Select Individual Playlist Items** (supported across Video, Audio Only, and Captions Only modes).
- **Multi-Item Download Queue & Batch Input**:
  - Paste a single URL or multiple newline-separated URLs with automatic whitespace trimming, duplicate detection, and line-by-line validation.
  - Bounded concurrent worker pool (configurable from `1` to `4` simultaneous jobs).
  - Real-time progress bars, stage detail indicators, transfer speed (`MB/s`), estimated time remaining (`ETA`), cooperative **Pause & Resume** (preserving `.part` files via `continuedl=True`), cooperative **Cancellation** (cleaning up temporary `.part`/`.ytdl`/subtitle files), and one-click **Retry**.
- **SQLite Download History**:
  - Persistent history across application restarts, with automatic recovery of unfinished/paused jobs on launch.
  - Live search by title or URL, status filtering (`All`, `Completed`, `Paused`, `Failed`, `Cancelled`), and date sorting (`Newest First`, `Oldest First`).
  - Safe `Open File` and `Open Folder` integration and one-click retry/re-download.
- **Persistent Settings & Themes**:
  - Instant **Dark**, **Light**, and **System** appearance modes.
  - Configurable default download folder, default mode (`Video`, `Audio Only`, `Captions Only`), quality/format defaults, subtitle export defaults, output filename template, file collision behavior (`rename`, `ask`, `skip`, `overwrite`), custom FFmpeg path, and log verbosity.
- **Diagnostics & Structured Logging**:
  - Built-in **Diagnostics & FFmpeg** modal displaying runtime versions, paths, and FFmpeg installation guidance.
  - Rotating log files with automatic sensitive-token redaction.

---

## Screenshots

> _Screenshots placeholder — launch the application (`python run.py`) to view the Downloader & Queue, Interactive Trim Timeline, Captions Only Controls, Download History, Settings, and Diagnostics views in Dark or Light mode._

---

## Requirements

- **Operating System**: Windows 10 or Windows 11 (64-bit)
- **Python**: Python 3.11 or newer (tested on Python 3.13)
- **FFmpeg & FFprobe** _(recommended)_: Required for merging separate high-resolution video/audio streams (1080p/4K), converting audio to MP3/WAV/Opus/FLAC, trimming media segments, and embedding soft subtitles into video containers. Standalone **Captions Only** mode and progressive video downloads work without FFmpeg.

---

## Installation & Setup (Windows PowerShell)

Open Windows PowerShell and run:

```powershell
cd D:\Projects\youtube-downloader-studio
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

For running automated tests and building the Windows executable, also install the development dependencies:

```powershell
pip install -r requirements-dev.txt
```

> **Note on PowerShell Execution Policy**:
> If `.venv\Scripts\Activate.ps1` is blocked by your system execution policy, enable script activation for the current PowerShell session only before activating:
> ```powershell
> Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process
> .\.venv\Scripts\Activate.ps1
> ```

---

## FFmpeg Installation & Configuration

While **YouTube Downloader Studio** launches and supports progressive streams and native audio without FFmpeg, installing **FFmpeg** (which includes `ffmpeg.exe` and `ffprobe.exe`) unlocks 1080p/1440p/4K stream merging and MP3/WAV/Opus/FLAC audio conversion.

### Trusted Installation Methods on Windows

1. **Via WinGet (Recommended)**:
   ```powershell
   winget install Gyan.FFmpeg
   ```
2. **Manual Installation**:
   - Download the release essentials build from [https://www.gyan.dev/ffmpeg/builds/](https://www.gyan.dev/ffmpeg/builds/).
   - Extract `ffmpeg.exe` and `ffprobe.exe` into `C:\ffmpeg\bin` and add `C:\ffmpeg\bin` to your Windows `PATH`.
3. **Custom Path in Settings**:
   - Open **YouTube Downloader Studio** -> **Settings** -> **Custom FFmpeg Path**, click **Browse...** to select `ffmpeg.exe`, and click **Test FFmpeg**.

---

## Launching the Application

From an activated virtual environment in the project root:

```powershell
python run.py
```

Or without activating the environment first:

```powershell
.\.venv\Scripts\python.exe run.py
```

To run a non-interactive environment and dependency check (optionally using an isolated `--data-dir`):

```powershell
.\.venv\Scripts\python.exe run.py --check-env
```

---

## Running Automated Tests

All default automated tests run 100% offline using mocked `yt-dlp` and isolated temporary directories (never touching real user data or performing real network downloads):

```powershell
.\.venv\Scripts\python.exe -m pytest -v
.\.venv\Scripts\python.exe -m pytest --cov=src --cov-report=term-missing
```

Opt-in live network download integration tests can be executed when authorized test URLs are provided via environment variables (`YDS_TEST_VIDEO_URL`, `YDS_TEST_AUDIO_URL`, `YDS_TEST_PLAYLIST_URL`, `YDS_TEST_FAIL_URL`):

```powershell
.\.venv\Scripts\python.exe -m pytest --run-live -m live -v
```

See [`docs/TESTING.md`](docs/TESTING.md) for full details on test coverage.

---

## Building the Windows Executable (`.exe`)

A repeatable PowerShell packaging script (`build_exe.ps1`) is provided using **PyInstaller**.

### 1. Recommended One-Folder Build

```powershell
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
```

- Output executable: `dist\YouTubeDownloaderStudio\YouTubeDownloaderStudio.exe`
- Verify the packaged executable from PowerShell:
  ```powershell
  .\dist\YouTubeDownloaderStudio\YouTubeDownloaderStudio.exe --check-env
  .\dist\YouTubeDownloaderStudio\YouTubeDownloaderStudio.exe
  ```

### 2. Optional Single-File Build

```powershell
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1 -OneFile
```

- Output executable: `dist\YouTubeDownloaderStudio.exe`

---

## Where Application Data Is Stored

Both in development mode and when running as a packaged `.exe`, writable application data is stored outside the installation folder:

- **Application Data Directory**: `%LOCALAPPDATA%\YouTubeDownloaderStudio` (e.g., `C:\Users\<Username>\AppData\Local\YouTubeDownloaderStudio`)
  - Override via CLI flag `--data-dir <path>` or environment variable `YTD_STUDIO_DATA_DIR`
- **SQLite Database (History & Settings)**: `%LOCALAPPDATA%\YouTubeDownloaderStudio\history.db`
- **JSON Settings Mirror**: `%LOCALAPPDATA%\YouTubeDownloaderStudio\settings.json`
- **Rotating Log Files**: `%LOCALAPPDATA%\YouTubeDownloaderStudio\logs\app.log`
- **Default Downloads Folder**: `%USERPROFILE%\Downloads\YouTubeDownloaderStudio` (configurable in the UI and Settings)

---

## Troubleshooting

See [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) for solutions to common issues (missing FFmpeg, HTTP 429 rate limits, unavailable playlist items, antivirus false positives on PyInstaller builds, and log inspection).

---

## Responsible Use Statement

**YouTube Downloader Studio** is intended strictly for personal use with content you own, content licensed under Creative Commons or open licenses, or media you have explicit permission or legal authorization to download.

- The application does **not** bypass Digital Rights Management (DRM), paywalls, private video restrictions, authentication barriers, or access controls.
- The application never extracts browser cookies, passwords, or private credentials.
- Users are solely responsible for complying with applicable copyright laws, platform Terms of Service, and content-owner permissions.

---

## Known Limitations

- **Pause / Resume**: Pausing an active download stops the worker cooperatively at the current transfer progress while keeping `.part` files on disk so **Resume** can continue from the partial download (`continuedl=True`). If the remote CDN expires the stream URL after a long pause, yt-dlp re-initiates or continues the transfer automatically.
- **Supported Domains**: URL validation intentionally restricts input to official YouTube domains (`youtube.com`, `youtu.be`, `music.youtube.com`, `youtube-nocookie.com`) over `http`/`https`.
- **Publicly Accessible Content Only**: Private, members-only, age-gated/sign-in-required, and DRM-protected streams are not supported.
