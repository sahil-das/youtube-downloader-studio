# Architecture Overview

**YouTube Downloader Studio** follows a layered, modular architecture that strictly separates the CustomTkinter user interface from background media extraction, queue scheduling, and SQLite persistence.

---

## Layered Structure

```text
src/
├── main.py                     # Application bootstrap, CLI flags, dependency wiring
├── core/
│   ├── exceptions.py           # Typed exception hierarchy with user remedies
│   ├── models.py               # Dataclasses, enums, and state transition matrix
│   ├── events.py               # Thread-safe EventBus (queue.Queue) for UI updates
│   └── validators.py           # URL, batch input, filename template, and path validation
├── services/
│   ├── ffmpeg_service.py       # Runtime detection for yt-dlp, FFmpeg, and FFprobe
│   ├── media_info_service.py   # Background metadata & thumbnail extraction
│   ├── download_service.py     # Isolated per-job yt-dlp execution, hooks, cancellation
│   ├── queue_service.py        # Bounded worker pool, job lifecycle, retry & history sync
│   └── settings_service.py     # Settings validation and dual JSON + SQLite persistence
├── database/
│   ├── db.py                   # Thread-safe SQLite connection & transaction manager (WAL)
│   └── repositories.py         # Parameterized SQL repositories for history and settings
├── ui/
│   ├── main_window.py          # Main window, event loop polling, navigation
│   ├── components.py           # MetadataPreviewCard and human-readable formatters
│   ├── queue_view.py           # Scrollable queue view and per-job progress cards
│   ├── history_view.py         # Searchable, filterable SQLite history view
│   ├── settings_view.py        # Preferences editor and FFmpeg path tester
│   └── dialogs.py              # Error, confirmation, playlist item selector, diagnostics modals
└── utils/
    ├── paths.py                # PyInstaller-aware paths and collision-safe filenames
    ├── logging_config.py       # RotatingFileHandler setup and sensitive token redaction
    └── platform_utils.py       # Safe Windows file/folder launching (blocks executables)
```

---

## Threading & Concurrency Model

1. **Main UI Thread**:
   - Owns all CustomTkinter widgets.
   - Polls `EventBus.poll_events()` every `100ms` via `self.after(100, ...)` and updates UI widgets exclusively on the main thread.
2. **Metadata Worker Thread**:
   - Spawned on demand when the user clicks **Fetch Info / Preview**.
   - Uses a cooperative `threading.Event` cancellation token if the user clears the input or requests a new URL before the previous fetch completes.
   - Publishes `METADATA_FETCHED` or `METADATA_FAILED` events to `EventBus`.
3. **Bounded Download Worker Threads**:
   - Managed by `QueueService` up to `max_concurrent_downloads` (`1`–`4`, default `2`).
   - Each job uses its own isolated `yt_dlp.YoutubeDL` instance and per-job logger.
   - `progress_hooks` and `postprocessor_hooks` inspect the job's `cancel_event` and raise `DownloadCancelledError` for cooperative cancellation, cleaning up `.part`/`.ytdl` temporary files.

---

## Job State Machine

`DownloadJob` enforces valid status transitions via `VALID_STATUS_TRANSITIONS` in `src/core/models.py`:

- `QUEUED` -> `STARTING` / `FETCHING_METADATA` / `DOWNLOADING` / `CANCELLED` / `FAILED`
- `STARTING` -> `FETCHING_METADATA` / `DOWNLOADING` / `PROCESSING` / `COMPLETED` / `FAILED` / `CANCELLED`
- `FETCHING_METADATA` -> `DOWNLOADING` / `PROCESSING` / `COMPLETED` / `FAILED` / `CANCELLED`
- `DOWNLOADING` -> `DOWNLOADING` / `PROCESSING` / `COMPLETED` / `FAILED` / `CANCELLED`
- `PROCESSING` -> `COMPLETED` / `FAILED` / `CANCELLED`
- `FAILED` -> `QUEUED` (via Retry)
- `CANCELLED` -> `QUEUED` (via Retry)
- `COMPLETED` -> _(terminal state)_
