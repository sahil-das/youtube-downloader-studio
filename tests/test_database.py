"""Comprehensive unit tests for SQLite database, history repository, concurrency, and SQL safety."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.core.models import DownloadJob, DownloadMode, JobStatus, VideoFormat, VideoQuality
from src.database.db import DatabaseManager
from src.database.repositories import HistoryRepository, SettingsRepository


def test_database_init_repeated_and_reopen(tmp_path: Path) -> None:
    db_file = tmp_path / "test_history.db"
    db1 = DatabaseManager(db_file)
    # Repeated initialization is idempotent
    db1.initialize()
    repo1 = HistoryRepository(db1)

    media_file = tmp_path / "Sample Python Tutorial.mp4"
    media_file.write_bytes(b"video_content")

    job = DownloadJob(
        job_id="job_001",
        url="https://www.youtube.com/watch?v=abc123XYZ",
        title="Sample Python Tutorial",
        destination_dir=tmp_path,
        mode=DownloadMode.VIDEO,
        video_quality=VideoQuality.Q1080P,
        video_format=VideoFormat.MP4,
        status=JobStatus.COMPLETED,
        output_path=media_file,
        file_size_bytes=1048576,
    )
    repo1.record_job(job)

    # Reopen the database with a fresh manager instance
    db2 = DatabaseManager(db_file)
    repo2 = HistoryRepository(db2)
    loaded = repo2.get_by_id("job_001")
    assert loaded is not None
    assert loaded.title == "Sample Python Tutorial"
    assert loaded.status == JobStatus.COMPLETED
    assert loaded.file_size_bytes == 1048576

    # Deleting history record MUST NOT delete the media file on disk
    assert repo2.delete_entry("job_001") is True
    assert media_file.exists() is True


def test_history_statuses_search_filter_and_date_sorting(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path / "history.db")
    repo = HistoryRepository(db)

    base_time = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)

    job_a = DownloadJob(
        job_id="job_a",
        url="https://www.youtube.com/watch?v=aaaa1111",
        title="Rust Programming Deep Dive",
        destination_dir=tmp_path,
        mode=DownloadMode.VIDEO,
        status=JobStatus.QUEUED,
        created_at=base_time,
    )
    job_b = DownloadJob(
        job_id="job_b",
        url="https://www.youtube.com/watch?v=bbbb2222",
        title="Classical Piano Audio",
        destination_dir=tmp_path,
        mode=DownloadMode.AUDIO,
        status=JobStatus.FAILED,
        error_message="Network timeout",
        created_at=base_time + timedelta(minutes=5),
        completed_at=base_time + timedelta(minutes=6),
    )
    job_c = DownloadJob(
        job_id="job_c",
        url="https://www.youtube.com/watch?v=cccc3333",
        title="Paused Lecture Stream",
        destination_dir=tmp_path,
        mode=DownloadMode.VIDEO,
        status=JobStatus.PAUSED,
        created_at=base_time + timedelta(minutes=10),
    )
    job_d = DownloadJob(
        job_id="job_d",
        url="https://www.youtube.com/watch?v=dddd4444",
        title="Cancelled Documentary",
        destination_dir=tmp_path,
        mode=DownloadMode.VIDEO,
        status=JobStatus.CANCELLED,
        created_at=base_time + timedelta(minutes=15),
        completed_at=base_time + timedelta(minutes=16),
    )

    for j in (job_a, job_b, job_c, job_d):
        repo.record_job(j)

    # Update status of job_a to COMPLETED
    updated = repo.update_status(
        "job_a",
        JobStatus.COMPLETED,
        output_path=str(tmp_path / "rust.mp4"),
        file_size_bytes=2048,
        completed_at=(base_time + timedelta(minutes=1)).isoformat(),
    )
    assert updated is True

    # Date sorting: descending vs ascending
    desc_entries = repo.list_entries(sort_descending=True)
    assert [e.job_id for e in desc_entries] == ["job_d", "job_c", "job_b", "job_a"]

    asc_entries = repo.list_entries(sort_descending=False)
    assert [e.job_id for e in asc_entries] == ["job_a", "job_b", "job_c", "job_d"]

    # Search by title and URL
    rust_hits = repo.list_entries(search_query="Rust")
    assert len(rust_hits) == 1 and rust_hits[0].job_id == "job_a"

    url_hits = repo.list_entries(search_query="bbbb2222")
    assert len(url_hits) == 1 and url_hits[0].job_id == "job_b"

    # Filter by each status
    assert len(repo.list_entries(status_filter=JobStatus.COMPLETED)) == 1
    assert len(repo.list_entries(status_filter=JobStatus.FAILED)) == 1
    assert len(repo.list_entries(status_filter=JobStatus.PAUSED)) == 1
    assert len(repo.list_entries(status_filter=JobStatus.CANCELLED)) == 1


def test_unicode_long_titles_and_sql_injection_safety(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path / "sql_safety.db")
    repo = HistoryRepository(db)

    tricky_title = "日本語 🎵 ' OR '1'='1'; DROP TABLE download_history; -- " + ("X" * 600)
    job = DownloadJob(
        job_id="job_sql",
        url="https://www.youtube.com/watch?v=sql_safe_1",
        title=tricky_title,
        destination_dir=tmp_path,
        status=JobStatus.COMPLETED,
    )
    repo.record_job(job)

    # Search with SQL meta-characters
    hits = repo.list_entries(search_query="' OR '1'='1'")
    assert len(hits) == 1
    assert hits[0].title == tricky_title

    no_hits = repo.list_entries(search_query="non_existent_query_xyz")
    assert no_hits == []


def test_concurrent_multithreaded_database_writes(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path / "concurrent.db")
    repo = HistoryRepository(db)
    errors: list[Exception] = []

    def _writer(worker_idx: int) -> None:
        try:
            for item_idx in range(10):
                jid = f"w{worker_idx}_{item_idx}"
                job = DownloadJob(
                    job_id=jid,
                    url=f"https://www.youtube.com/watch?v={jid}",
                    title=f"Title {jid}",
                    destination_dir=tmp_path,
                    status=JobStatus.QUEUED,
                )
                repo.record_job(job)
                repo.update_status(jid, JobStatus.COMPLETED, file_size_bytes=1024)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_writer, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    assert errors == []
    all_records = repo.list_entries(limit=100)
    assert len(all_records) == 40


def test_transaction_rollback_on_exception(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path / "tx_test.db")
    settings_repo = SettingsRepository(db)
    settings_repo.save_all({"theme_mode": "dark"})

    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO app_settings (key, value_json) VALUES (?, ?)",
                ("theme_mode", '"light"'),
            )
            raise RuntimeError("Simulated crash before commit")

    assert settings_repo.get_all()["theme_mode"] == "dark"
