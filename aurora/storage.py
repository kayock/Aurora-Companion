"""One authoritative SQLite store for the whole companion.

Everything persistent lives in a single SQLite database (WAL mode):
messages, facts, reminders, proactive log, personality changelog, lessons,
people, mood/affinity state and internal counters. One store means one
conversation history, no duplicate or competing memory systems, and
restart persistence by construction.

Schema design adapts:
* Luna's factstore (retire-don't-delete supersession chains, bi-temporal
  valid_from/valid_to, forgotten tombstones, FTS5 index),
* Shikigami's long-term facts (weight, category, evidence pointers to the
  messages that justify a fact, versions, archive/merge soft deletes).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One conversation history. `person` attributes the turn (owner/guest/...),
-- `source` says what produced an assistant turn (chat | proactive |
-- reminder | system). `meta` holds JSON (tool calls, voice flags...).
CREATE TABLE IF NOT EXISTS messages (
    id       INTEGER PRIMARY KEY,
    role     TEXT NOT NULL,             -- user | assistant | system
    content  TEXT NOT NULL,
    ts       REAL NOT NULL,
    person   TEXT NOT NULL DEFAULT 'owner',
    sender   TEXT NOT NULL DEFAULT '',  -- display name for user turns
    source   TEXT NOT NULL DEFAULT 'user',  -- user | verify | chat | proactive | …
    meta     TEXT NOT NULL DEFAULT '{}',
    archived INTEGER NOT NULL DEFAULT 0,   -- soft-hide from active context
    archive_reason TEXT,
    archived_at REAL
);
CREATE INDEX IF NOT EXISTS messages_ts ON messages (ts);
CREATE INDEX IF NOT EXISTS messages_person ON messages (person);
-- messages_active (archived, id) is created in _migrate after the
-- soft-archive columns exist on upgraded databases.

-- Long-term facts (the single memory system).
CREATE TABLE IF NOT EXISTS facts (
    id             TEXT PRIMARY KEY,     -- f_XXXXXXXX
    content        TEXT NOT NULL,
    subject        TEXT NOT NULL DEFAULT 'owner',   -- whose fact (person name)
    category       TEXT NOT NULL DEFAULT 'other',
    kind           TEXT NOT NULL DEFAULT '',        -- '' | stable | address | event | temporary
    event_date     TEXT,                 -- YYYY-MM-DD for kind=event
    expires_at     REAL,                 -- epoch for kind=temporary
    weight         REAL NOT NULL DEFAULT 1.0,
    source         TEXT NOT NULL DEFAULT 'auto',    -- manual | auto | consolidation | user_command
    trust          TEXT NOT NULL DEFAULT 'confirmed',
    pinned         INTEGER NOT NULL DEFAULT 0,
    archived       INTEGER NOT NULL DEFAULT 0,
    archive_reason TEXT,
    merged_into    TEXT,
    superseded_by  TEXT,
    superseded_at  REAL,
    valid_from     TEXT,                 -- bi-temporal real-world validity
    valid_to       TEXT,
    evidence       TEXT NOT NULL DEFAULT '[]',      -- JSON [{message_id, ts, quote}]
    versions       TEXT NOT NULL DEFAULT '[]',      -- JSON snapshots
    emotional_note TEXT NOT NULL DEFAULT '',
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL,
    embedding      BLOB
);
CREATE INDEX IF NOT EXISTS facts_active ON facts (archived, superseded_by);

-- FTS5 index over active fact text; triggers keep it in sync (Luna's approach).
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    content, subject, content='facts', content_rowid='rowid',
    tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, content, subject) VALUES (new.rowid, new.content, new.subject);
END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, content, subject) VALUES ('delete', old.rowid, old.content, old.subject);
END;
CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE OF content, subject, archived, superseded_by ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, content, subject) VALUES ('delete', old.rowid, old.content, old.subject);
    INSERT INTO facts_fts(rowid, content, subject) VALUES (new.rowid, new.content, new.subject);
END;

-- "Forget it" tombstones: hash of salient words (Luna's forgetting design).
CREATE TABLE IF NOT EXISTS forgotten (
    marks TEXT PRIMARY KEY,
    at    REAL NOT NULL
);

-- General-purpose reminders.
CREATE TABLE IF NOT EXISTS reminders (
    id            TEXT PRIMARY KEY,     -- r_XXXXXXXX
    text          TEXT NOT NULL,
    due_at        REAL NOT NULL,        -- epoch seconds
    created_at    REAL NOT NULL,
    repeat        TEXT,                 -- JSON: {"kind":"interval","seconds":N} | {"kind":"daily","hour":H,"minute":M} | {"kind":"weekly","weekday":0-6,"hour":H,"minute":M}
    follow_up     TEXT,                 -- JSON {"every_seconds":N,"max":K}
    status        TEXT NOT NULL DEFAULT 'scheduled',
                  -- scheduled | delivered | waiting_ack | snoozed | done | cancelled | missed
    ack           INTEGER NOT NULL DEFAULT 0,
    attempts      INTEGER NOT NULL DEFAULT 0,
    snooze_count  INTEGER NOT NULL DEFAULT 0,
    last_fired    REAL,
    snoozed_until REAL,
    project       TEXT NOT NULL DEFAULT '',
    person        TEXT NOT NULL DEFAULT 'owner',
    source        TEXT NOT NULL DEFAULT 'user',
    kind          TEXT NOT NULL DEFAULT '',  -- '' | 'timer' (timer interaction flow)
    duration      INTEGER                    -- intended timer length in seconds (timers only)
);
CREATE INDEX IF NOT EXISTS reminders_due ON reminders (status, due_at);

-- Every proactive message ever initiated (dedup + dashboard + honesty).
CREATE TABLE IF NOT EXISTS proactive_log (
    id        INTEGER PRIMARY KEY,
    ts        REAL NOT NULL,
    kind      TEXT NOT NULL,            -- morning_greeting | check_in | topic_share | work_break | memory_resurfacing | reminder_delivery
    text      TEXT NOT NULL,
    topic_ref TEXT NOT NULL DEFAULT '',
    urgency   REAL NOT NULL DEFAULT 0.0,
    trigger   TEXT NOT NULL DEFAULT ''
);

-- Persona evolution changelog with full before/after (rollback source).
CREATE TABLE IF NOT EXISTS personality_log (
    id              INTEGER PRIMARY KEY,
    ts              REAL NOT NULL,
    version         INTEGER NOT NULL,
    change_summary  TEXT NOT NULL DEFAULT '',
    reason          TEXT NOT NULL DEFAULT '',
    base_prompt     TEXT NOT NULL,
    style           TEXT NOT NULL DEFAULT '',
    core_anchor     TEXT NOT NULL DEFAULT '',
    baseline_review INTEGER NOT NULL DEFAULT 0
);

-- Lessons learned during consolidation (Luna's "lessons from her own mistakes").
CREATE TABLE IF NOT EXISTS lessons (
    id         TEXT PRIMARY KEY,
    text       TEXT NOT NULL,
    created_at REAL NOT NULL,
    source     TEXT NOT NULL DEFAULT 'consolidation',
    hits       INTEGER NOT NULL DEFAULT 0
);

-- Day summaries written by the consolidation daemon.
CREATE TABLE IF NOT EXISTS day_summaries (
    date          TEXT PRIMARY KEY,     -- YYYY-MM-DD
    summary       TEXT NOT NULL,
    generated_at  REAL NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0
);

-- People in the house (per-person memory separation; Luna's model).
CREATE TABLE IF NOT EXISTS people (
    name        TEXT PRIMARY KEY,
    pronouns    TEXT NOT NULL DEFAULT '',
    is_owner    INTEGER NOT NULL DEFAULT 0,
    notes       TEXT NOT NULL DEFAULT '',
    voiceprints TEXT NOT NULL DEFAULT '[]'   -- JSON list of float vectors
);

-- Mood + affinity state (one row, id=0).
CREATE TABLE IF NOT EXISTS mood_state (
    id         INTEGER PRIMARY KEY CHECK (id = 0),
    energy     REAL NOT NULL DEFAULT 0.6,
    warmth     REAL NOT NULL DEFAULT 0.45,
    desire     REAL NOT NULL DEFAULT 0.0,
    grudge_until REAL NOT NULL DEFAULT 0,
    affinity   REAL NOT NULL DEFAULT 0.0,
    updated_at REAL NOT NULL DEFAULT 0
);

-- Optional embedding cache for semantic recall (key = provider+model+text hash).
CREATE TABLE IF NOT EXISTS embeddings (
    key  TEXT PRIMARY KEY,
    dim  INTEGER NOT NULL,
    data BLOB NOT NULL
);
"""


def _now() -> float:
    return time.time()


class Database:
    """Thin SQLite wrapper. One connection guarded by a lock (the app is
    mostly asyncio single-threaded, but the reminder and consolidation
    services touch the store from their own tasks)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            version = self.get_meta("schema_version")
            if version is None:
                self.set_meta("schema_version", str(SCHEMA_VERSION))
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Additive column migrations for databases created by older builds.

        Only adds columns that are missing (checked via PRAGMA table_info),
        so re-running is a no-op and existing rows keep their data — user
        memory and settings are never rewritten here.
        """
        wanted = {
            "reminders": {
                "kind": "TEXT NOT NULL DEFAULT ''",
                "duration": "INTEGER",
            },
            "messages": {
                "archived": "INTEGER NOT NULL DEFAULT 0",
                "archive_reason": "TEXT",
                "archived_at": "REAL",
            },
        }
        with self._lock:
            for table, columns in wanted.items():
                existing = {r["name"] for r in
                            self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
                if not existing:
                    continue  # table not created yet (fresh DB has it via _SCHEMA)
                for name, decl in columns.items():
                    if name not in existing:
                        self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            # Active-message index (idempotent).
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS messages_active ON messages (archived, id)"
            )
            version = self.get_meta("schema_version")
            if version is None or int(version) < SCHEMA_VERSION:
                self.set_meta("schema_version", str(SCHEMA_VERSION))

    # -- generic helpers -------------------------------------------------
    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    # -- meta / kv -------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        row = self.query_one("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def get_meta_json(self, key: str, default=None):
        import json

        raw = self.get_meta(key)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except ValueError:
            return default

    def set_meta_json(self, key: str, value) -> None:
        import json

        self.set_meta(key, json.dumps(value, ensure_ascii=False))

    def backup_to(self, path: Path) -> None:
        """Online, consistent snapshot of the whole store (sqlite backup API
        — safe while the app is running). Used by deploy/backup.sh-style
        flows and by the updater's pre-update backup."""
        with self._lock:
            dest = sqlite3.connect(str(path))
            try:
                self._conn.backup(dest)
            finally:
                dest.close()

    def close(self) -> None:
        with self._lock:
            self._conn.commit()
            self._conn.close()
