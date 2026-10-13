"""Repositories for download history and SQLite-backed settings persistence."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from src.core.models import DownloadJob, DownloadMode, HistoryEntry, JobStatus
from src.database.db import DatabaseManager
from src.utils.logging_config import get_logger

logger = get_logger("repositories")


class HistoryRepository:
    """Data access layer for download history records using parameterized SQL."""

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    @staticmethod
    def from_job(job: DownloadJob) -> HistoryEntry:
        """Convert a DownloadJob domain object into a HistoryEntry."""
        return HistoryEntry(
            job_id=job.job_id,
            url=job.url,
            title=job.display_title,
            mode=job.mode,
            requested_quality=job.requested_format_summary,
            output_path=str(job.output_path) if job.output_path else None,
            destination_dir=str(job.destination_dir),
            status=job.status,
            file_size_bytes=job.file_size_bytes,
            error_message=job.error_message,
            created_at=job.created_at.isoformat(),
            completed_at=job.completed_at.isoformat() if job.completed_at else None,
            video_quality=job.video_quality.value,
            video_format=job.video_format.value,
            audio_format=job.audio_format.value,
            playlist_mode=job.playlist_mode.value,
        )

    @staticmethod
    def _row_to_entry(row: Any) -> HistoryEntry:
        """Map a sqlite3.Row to a HistoryEntry instance."""
        try:
            mode = DownloadMode(row["mode"])
        except ValueError:
            mode = DownloadMode.VIDEO

        try:
            status = JobStatus(row["status"])
        except ValueError:
            status = JobStatus.FAILED

        return HistoryEntry(
            job_id=row["job_id"],
            url=row["url"],
            title=row["title"],
            mode=mode,
            requested_quality=row["requested_quality"],
            output_path=row["output_path"],
            destination_dir=row["destination_dir"],
            status=status,
            file_size_bytes=row["file_size_bytes"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            completed_at=row["completed_at"],
            video_quality=row["video_quality"] or "best",
            video_format=row["video_format"] or "mp4",
            audio_format=row["audio_format"] or "mp3",
            playlist_mode=row["playlist_mode"] or "single",
        )

    def upsert(self, entry: HistoryEntry) -> None:
        """Insert or update a download history entry."""
        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO download_history (
                    job_id, url, title, mode, requested_quality,
                    output_path, destination_dir, status, file_size_bytes,
                    error_message, created_at, completed_at,
                    video_quality, video_format, audio_format, playlist_mode
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    url = excluded.url,
                    title = excluded.title,
                    mode = excluded.mode,
                    requested_quality = excluded.requested_quality,
                    output_path = excluded.output_path,
                    destination_dir = excluded.destination_dir,
                    status = excluded.status,
                    file_size_bytes = excluded.file_size_bytes,
                    error_message = excluded.error_message,
                    completed_at = excluded.completed_at,
                    video_quality = excluded.video_quality,
                    video_format = excluded.video_format,
                    audio_format = excluded.audio_format,
                    playlist_mode = excluded.playlist_mode
                """,
                (
                    entry.job_id,
                    entry.url,
                    entry.title,
                    entry.mode.value,
                    entry.requested_quality,
                    entry.output_path,
                    entry.destination_dir,
                    entry.status.value,
                    entry.file_size_bytes,
                    entry.error_message,
                    entry.created_at,
                    entry.completed_at,
                    entry.video_quality,
                    entry.video_format,
                    entry.audio_format,
                    entry.playlist_mode,
                ),
            )

    def record_job(self, job: DownloadJob) -> None:
        """Persist the current state of a DownloadJob into SQLite history."""
        self.upsert(self.from_job(job))

    def update_status(
        self,
        job_id: str,
        status: JobStatus,
        output_path: str | None = None,
        file_size_bytes: int | None = None,
        error_message: str | None = None,
        completed_at: str | None = None,
    ) -> bool:
        """Update status and completion fields for an existing history entry."""
        if completed_at is None and status.is_terminal:
            completed_at = datetime.now(timezone.utc).isoformat()

        with self._db.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE download_history
                SET status = ?,
                    output_path = COALESCE(?, output_path),
                    file_size_bytes = COALESCE(?, file_size_bytes),
                    error_message = ?,
                    completed_at = COALESCE(?, completed_at)
                WHERE job_id = ?
                """,
                (
                    status.value,
                    output_path,
                    file_size_bytes,
                    error_message,
                    completed_at,
                    job_id,
                ),
            )
            return cursor.rowcount > 0

    def get_by_id(self, job_id: str) -> HistoryEntry | None:
        """Return a single history record by job_id or None if not found."""
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM download_history WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            return self._row_to_entry(row) if row else None

    def list_entries(
        self,
        search_query: str = "",
        status_filter: JobStatus | str | None = None,
        sort_descending: bool = True,
        limit: int = 500,
    ) -> list[HistoryEntry]:
        """List history records with optional search, status filtering, and date sorting."""
        clauses: list[str] = []
        params: list[Any] = []

        if search_query and search_query.strip():
            like_pattern = f"%{search_query.strip()}%"
            clauses.append("(title LIKE ? COLLATE NOCASE OR url LIKE ? COLLATE NOCASE)")
            params.extend([like_pattern, like_pattern])

        if status_filter is not None:
            status_val = (
                status_filter.value
                if isinstance(status_filter, JobStatus)
                else str(status_filter).strip().lower()
            )
            if status_val and status_val != "all":
                clauses.append("status = ?")
                params.append(status_val)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order_dir = "DESC" if sort_descending else "ASC"
        sql = f"""
            SELECT * FROM download_history
            {where_sql}
            ORDER BY COALESCE(completed_at, created_at) {order_dir}, rowid {order_dir}
            LIMIT ?
        """
        params.append(max(1, int(limit)))

        with self._db.connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [self._row_to_entry(row) for row in rows]

    def delete_entry(self, job_id: str) -> bool:
        """Delete a single history record by job_id (never deletes media files on disk)."""
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM download_history WHERE job_id = ?",
                (job_id,),
            )
            return cursor.rowcount > 0

    def clear_all(self) -> int:
        """Delete all history records and return the number of removed entries."""
        with self._db.transaction() as conn:
            cursor = conn.execute("DELETE FROM download_history")
            return cursor.rowcount


class SettingsRepository:
    """SQLite-backed key-value storage for persistent settings."""

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    def get_all(self) -> dict[str, Any]:
        """Load all stored settings key-value pairs from SQLite."""
        result: dict[str, Any] = {}
        with self._db.connection() as conn:
            rows = conn.execute("SELECT key, value_json FROM app_settings").fetchall()
            for row in rows:
                try:
                    result[row["key"]] = json.loads(row["value_json"])
                except json.JSONDecodeError:
                    logger.warning("Ignoring malformed JSON setting in SQLite for key '%s'", row["key"])
        return result

    def save_all(self, data: dict[str, Any]) -> None:
        """Save all settings key-value pairs in a single transaction."""
        with self._db.transaction() as conn:
            for key, value in data.items():
                serialized = json.dumps(value)
                conn.execute(
                    """
                    INSERT INTO app_settings (key, value_json, updated_at)
                    VALUES (?, ?, datetime('now'))
                    ON CONFLICT(key) DO UPDATE SET
                        value_json = excluded.value_json,
                        updated_at = datetime('now')
                    """,
                    (str(key), serialized),
                )
