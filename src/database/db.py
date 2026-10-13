"""SQLite connection management, schema initialization, and transaction helpers."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from src.core.exceptions import DatabaseError
from src.utils.logging_config import get_logger

logger = get_logger("database")

SCHEMA_VERSION = 1

_INIT_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS download_history (
    job_id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    mode TEXT NOT NULL,
    requested_quality TEXT NOT NULL,
    output_path TEXT,
    destination_dir TEXT NOT NULL,
    status TEXT NOT NULL,
    file_size_bytes INTEGER,
    error_message TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    video_quality TEXT NOT NULL DEFAULT 'best',
    video_format TEXT NOT NULL DEFAULT 'mp4',
    audio_format TEXT NOT NULL DEFAULT 'mp3',
    playlist_mode TEXT NOT NULL DEFAULT 'single'
);

CREATE INDEX IF NOT EXISTS idx_history_status ON download_history(status);
CREATE INDEX IF NOT EXISTS idx_history_completed_at ON download_history(completed_at DESC);
CREATE INDEX IF NOT EXISTS idx_history_created_at ON download_history(created_at DESC);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class DatabaseManager:
    """Thread-safe SQLite database manager supporting transactions and schema initialization."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self._lock = threading.RLock()
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        """Create a new configured SQLite connection."""
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(self.db_path),
                timeout=15.0,
                detect_types=sqlite3.PARSE_DECLTYPES,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            return conn
        except sqlite3.Error as exc:
            logger.error("Failed to connect to SQLite database at %s: %s", self.db_path, exc)
            raise DatabaseError(
                f"Could not open database at '{self.db_path}': {exc}",
                remedy="Ensure the application data directory is writable and not locked.",
            ) from exc

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a read/query connection and close it cleanly."""
        with self._lock:
            conn = self._connect()
            try:
                yield conn
            except sqlite3.Error as exc:
                logger.error("SQLite query error: %s", exc)
                raise DatabaseError(
                    f"Database operation failed: {exc}",
                    remedy="Check application logs for database details.",
                ) from exc
            finally:
                conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Yield a transactional SQLite connection that commits on success or rolls back on error."""
        with self._lock:
            conn = self._connect()
            try:
                yield conn
                conn.commit()
            except sqlite3.Error as exc:
                conn.rollback()
                logger.error("SQLite transaction rolled back due to error: %s", exc)
                raise DatabaseError(
                    f"Database transaction failed: {exc}",
                    remedy="Verify available disk space and database permissions.",
                ) from exc
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def initialize(self) -> None:
        """Create tables, indexes, and record the schema version if not present."""
        with self.transaction() as conn:
            conn.executescript(_INIT_SQL)
            row = conn.execute(
                "SELECT MAX(version) AS ver FROM schema_migrations"
            ).fetchone()
            current_ver = row["ver"] if row and row["ver"] is not None else 0
            if current_ver < SCHEMA_VERSION:
                conn.execute(
                    "INSERT OR REPLACE INTO schema_migrations (version) VALUES (?)",
                    (SCHEMA_VERSION,),
                )
                logger.info(
                    "Database initialized at %s (schema version %d)",
                    self.db_path,
                    SCHEMA_VERSION,
                )
