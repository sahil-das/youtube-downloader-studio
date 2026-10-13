# Troubleshooting Guide

## 1. "FFmpeg Not Found (Limited mode)" or Audio Conversion Disabled

### Symptoms
- The status bar shows `FFmpeg Not Found (Limited mode)`.
- Audio formats such as `MP3`, `WAV`, `Opus`, and `FLAC` are unavailable, or high-resolution video downloads fall back to 720p progressive streams.

### Solution
1. Install FFmpeg using WinGet in Windows PowerShell:
   ```powershell
   winget install Gyan.FFmpeg
   ```
   Restart the application after installation so the updated `PATH` is detected.
2. Alternatively, if `ffmpeg.exe` is already on your computer (for example in `C:\ffmpeg\bin\ffmpeg.exe`):
   - Open the **Settings** tab in YouTube Downloader Studio.
   - Under **Custom FFmpeg Path**, click **Browse...** and select `ffmpeg.exe`.
   - Click **Test FFmpeg**, then click **Save Settings**.

---

## 2. "FFmpeg Ready (FFprobe missing)"

### Symptoms
- The status bar or Diagnostics window reports `FFmpeg Ready (FFprobe missing)`.

### Solution
- Stream merging (1080p/4K) and audio conversion (`MP3`/`WAV`) will still work using `ffmpeg.exe`.
- For complete media probing support, place `ffprobe.exe` (from the same FFmpeg release archive) in the same folder as `ffmpeg.exe` (e.g., `C:\ffmpeg\bin\ffprobe.exe`).

---

## 3. PowerShell Blocks `.venv\Scripts\Activate.ps1`

### Symptoms
- PowerShell displays: `Activate.ps1 cannot be loaded because running scripts is disabled on this system.`

### Solution
Allow local scripts for the current PowerShell process only:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process
.\.venv\Scripts\Activate.ps1
```

Or run Python directly from `.venv` without activating:

```powershell
.\.venv\Scripts\python.exe run.py
```

---

## 4. Metadata Fetch or Download Fails ("Video unavailable" / "Private video" / HTTP 429)

### Causes & Remedies
- **Private / Age-Restricted / Members-Only Videos**: By design, YouTube Downloader Studio does not use browser cookies, credentials, or access-control bypasses. Only publicly accessible videos and playlists are supported.
- **Outdated `yt-dlp`**: YouTube frequently updates its player scripts. Update `yt-dlp` inside your virtual environment:
  ```powershell
  .\.venv\Scripts\python.exe -m pip install --upgrade yt-dlp
  ```
- **HTTP 429 (Too Many Requests)**: YouTube has temporarily rate-limited your IP address. Wait several minutes and reduce **Max Concurrent Downloads** to `1` in **Settings**.

---

## 5. Video/Audio Trimming & Timestamp Validation

### Symptoms
- "Start time must be less than end time" or "End time exceeds total media duration".
- Trimming is blocked with "FFmpeg Required for Segment Trimming".

### Causes & Remedies
- **Timestamp Formats**: Enter timestamps as `SS` (e.g., `45`), `MM:SS` (e.g., `01:30`), or `HH:MM:SS` (e.g., `01:05:20`), with optional milliseconds (`.500`). Alternatively, drag the blue start/end handles on the interactive visual timeline.
- **Minimum Clip Duration**: The selected clip must be at least `0.5` seconds long and within the inspected duration of the video or local file.
- **FFmpeg Requirement**: Both YouTube URL segment downloads (`download_ranges`) and local media file trimming require `ffmpeg.exe`. If `ffprobe.exe` is missing, YouTube Downloader Studio automatically falls back to `ffmpeg -hide_banner -i` to inspect duration and stream metadata.

---

## 6. Subtitles, `Captions Only` Mode, and Soft-Subtitle Embedding

### Symptoms
- "No manual subtitles or automatic captions are available" or "Requested subtitle track(s) could not be found".
- Soft-subtitle embedding fails for an unsupported container.

### Causes & Remedies
- **Manual vs Auto-Generated Captions**: Many YouTube videos only provide speech-recognition captions (`[Auto]`) rather than creator-uploaded subtitles (`[Manual]`). Check the **Auto-Captions** box in the Captions/Subtitles row to include automatic captions.
- **No FFmpeg Required for `Captions Only`**: Standalone **Captions Only** mode downloads and converts `.srt`, `.vtt`, and clean `.txt` transcripts in pure Python without downloading any video/audio streams and without requiring FFmpeg.
- **Soft-Subtitle Container Support**: Soft-subtitle stream muxing (`Embed Soft Subtitles`) is supported for `MP4` (`mov_text`), `MKV` (`srt`), and `WebM` (`webvtt`) containers without re-encoding video or audio. Keep **Keep Subtitle File** checked if you also want the standalone `.srt`/`.vtt`/`.txt` file saved alongside the video.

---

## 7. Inspecting Application Logs

Click **Open Logs** in the top-right header of the application window, or open the folder directly in Windows Explorer:

```text
%LOCALAPPDATA%\YouTubeDownloaderStudio\logs\app.log
```

For more detailed logs while diagnosing an issue, go to **Settings** -> **Logging Verbosity**, select `DEBUG`, and click **Save Settings**.
