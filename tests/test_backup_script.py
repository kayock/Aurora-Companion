"""deploy/backup.sh takes a consistent SQLite snapshot and gzips it.

The previous script joined sqlite3.iterdump() (str) with a bytes separator,
so it crashed on Python 3 and never used the online backup API the header
described. These tests run the real script against a disposable database.
"""

from __future__ import annotations

import gzip
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKUP = ROOT / "deploy" / "backup.sh"


def _make_db(path: Path, rows: int = 3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    conn.executemany("INSERT INTO notes (body) VALUES (?)", [(f"row-{i}",) for i in range(rows)])
    conn.commit()
    conn.close()


def _run_backup(data_dir: Path, dest: Path, keep: int = 14) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(BACKUP), "--keep", str(keep), "--dest", str(dest)],
        capture_output=True, text=True,
        env={"AURORA_DATA": str(data_dir), "PATH": "/usr/bin:/bin"},
        check=False,
    )


def _restore(gz_path: Path, dest_db: Path) -> sqlite3.Connection:
    dest_db.write_bytes(gzip.decompress(gz_path.read_bytes()))
    conn = sqlite3.connect(dest_db)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    return conn


def test_backup_roundtrip_preserves_rows(tmp_path):
    data = tmp_path / "data"
    _make_db(data / "aurora.db", rows=4)
    dest = tmp_path / "backups"
    result = _run_backup(data, dest)
    assert result.returncode == 0, result.stderr
    gz = list(dest.glob("aurora-*.db.gz"))
    assert len(gz) == 1
    assert gz[0].stat().st_mode & 0o777 == 0o600
    assert not list(dest.glob("*.partial*"))
    conn = _restore(gz[0], tmp_path / "restored.db")
    try:
        bodies = [r[0] for r in conn.execute("SELECT body FROM notes ORDER BY id")]
    finally:
        conn.close()
    assert bodies == ["row-0", "row-1", "row-2", "row-3"]
    assert "gunzip -c" in result.stdout
    assert "iterdump" not in BACKUP.read_text()


def test_backup_is_consistent_while_a_writer_is_active(tmp_path):
    data = tmp_path / "data"
    db_path = data / "aurora.db"
    _make_db(db_path, rows=1)
    stop = threading.Event()

    def writer():
        conn = sqlite3.connect(db_path)
        n = 0
        while not stop.is_set():
            conn.execute("INSERT INTO notes (body) VALUES (?)", (f"live-{n}",))
            conn.commit()
            n += 1
            time.sleep(0.002)
        conn.close()

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        time.sleep(0.05)
        result = _run_backup(data, tmp_path / "backups")
    finally:
        stop.set()
        thread.join(timeout=5)
    assert result.returncode == 0, result.stderr
    gz = next((tmp_path / "backups").glob("aurora-*.db.gz"))
    conn = _restore(gz, tmp_path / "restored.db")
    try:
        seed = conn.execute("SELECT COUNT(*) FROM notes WHERE body='row-0'").fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    finally:
        conn.close()
    assert seed == 1
    assert total >= 1


def test_missing_database_exits_nonzero_and_writes_nothing(tmp_path):
    dest = tmp_path / "backups"
    result = _run_backup(tmp_path / "empty", dest)
    assert result.returncode != 0
    assert "not found" in result.stderr
    assert not dest.exists() or list(dest.glob("aurora-*.db.gz")) == []


def test_retention_keeps_newest(tmp_path):
    data = tmp_path / "data"
    _make_db(data / "aurora.db", rows=1)
    dest = tmp_path / "backups"
    dest.mkdir()
    # Older names sort first under ls -1t only by mtime, so write them first
    # and push their mtimes into the past.
    for i, stamp in enumerate(("20000101-000001", "20000101-000002", "20000101-000003")):
        path = dest / f"aurora-{stamp}.db.gz"
        path.write_bytes(b"old")
        ts = 1_000_000_000 + i
        path.touch()
        import os
        os.utime(path, (ts, ts))
    result = _run_backup(data, dest, keep=2)
    assert result.returncode == 0, result.stderr
    names = sorted(p.name for p in dest.glob("aurora-*.db.gz"))
    assert len(names) == 2
    assert all(not name.startswith("aurora-2000") or name.endswith("000003.db.gz") for name in names)
    assert any(not name.startswith("aurora-2000") for name in names)
