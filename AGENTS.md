# Project Instructions

## Project
youtube-downloader-studio — a Windows desktop application written in Python.

## Technology
- Python 3.11 or newer
- CustomTkinter for the desktop interface
- yt-dlp for supported media extraction and downloads
- FFmpeg and FFprobe for media processing
- SQLite for download history and persistent settings
- pytest for automated tests
- PyInstaller for Windows executable packaging

## Architecture
- Keep the UI separate from download logic.
- Keep long-running tasks off the UI thread.
- Communicate worker progress and errors safely to the UI.
- Use type hints and clear function names.
- Use pathlib for filesystem paths.
- Keep settings, history, and queue logic modular.
- Avoid unnecessary global state.

## Safety and reliability
- Support downloads only where the user has authorization.
- Do not bypass DRM, paywalls, authentication restrictions, or access controls.
- Never store passwords, browser cookies, or tokens in source control.
- Validate user-provided URLs and file paths.
- Never execute downloaded media as code.
- Handle network failures, cancellation, retries, and unavailable FFmpeg gracefully.
- Do not silently overwrite existing files.
- Keep the interface responsive during downloads.

## Development workflow
- Inspect existing files before modifying them.
- Implement one coherent phase at a time.
- Run relevant tests after each phase.
- Do not claim tests passed unless they were actually run.
- Update README.md and requirements.txt as dependencies or behavior change.
- Keep changes small enough to review and commit.
- Ask before adding unnecessary dependencies or destructive operations.

## Completion criteria
The application should support video and audio modes, playlists, a download queue,
progress reporting, cancellation, download history, settings, dark/light themes,
automated tests, and a documented Windows EXE build process.
