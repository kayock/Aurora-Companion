"""Reminder store — one dependable reminder system for everything.

General-purpose (not medical, not app-specific): one-time and recurring
reminders, optional follow-up-until-acknowledged, snooze/dismiss/ack with
explicit state transitions, project/context tagging, and full persistence
in the shared SQLite store so every reminder survives restarts.

Scheduling itself lives in scheduler.py and never requires the language
model: if the model is down, reminders are still delivered as plain
messages. (Design adapted from Luna's reminders scanner and Shikigami's
durable dated-reminder store — Aurora extends both with recurrence and
follow-up semantics neither upstream had together.)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from ..clock import Clock
from ..storage import Database
from ..timeparse import next_occurrence, too_frequent
from ..util import new_id

# greetings / test noise that must never become a stored reminder (the user
# observed "hi"-named rows created in a live run — blocked at the source now)
_TRIVIAL_TEXT_RE = re.compile(
    r"^(h+i+|hello+|hey+|yo+|ok(?:ay)?|yes+|no+|thanks?|thank you|test(?: reminder|ing)?"
    r"|asdf+|qwerty+|null|undefined|none|nan|\.+)[\s!.?,:;~-]*$",
    re.I)

STATUS_SCHEDULED = "scheduled"
STATUS_DELIVERED = "delivered"
STATUS_WAITING_ACK = "waiting_ack"
STATUS_SNOOZED = "snoozed"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"
STATUS_MISSED = "missed"

ACTIVE_STATUSES = (STATUS_SCHEDULED, STATUS_WAITING_ACK, STATUS_SNOOZED)


@dataclass
class Reminder:
    id: str
    text: str
    due_at: float
    created_at: float
    repeat: dict | None = None
    follow_up: dict | None = None
    status: str = STATUS_SCHEDULED
    ack: bool = False
    attempts: int = 0
    snooze_count: int = 0
    last_fired: float | None = None
    snoozed_until: float | None = None
    project: str = ""
    person: str = "owner"
    source: str = "user"
    kind: str = ""  # "" | "timer" — timers get the repeat/snooze/dismiss flow
    duration: int | None = None  # intended timer length in seconds (timers only)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "text": self.text, "due_at": self.due_at,
            "created_at": self.created_at, "repeat": self.repeat,
            "follow_up": self.follow_up, "status": self.status, "ack": self.ack,
            "attempts": self.attempts, "snooze_count": self.snooze_count,
            "last_fired": self.last_fired, "snoozed_until": self.snoozed_until,
            "project": self.project, "person": self.person, "source": self.source,
            "kind": self.kind, "duration": self.duration,
        }

    @property
    def recurring(self) -> bool:
        return bool(self.repeat)


def _row_to_reminder(row) -> Reminder:
    keys = row.keys()
    kind = row["kind"] if "kind" in keys else ""
    duration = row["duration"] if "duration" in keys else None
    return Reminder(
        id=row["id"], text=row["text"], due_at=row["due_at"], created_at=row["created_at"],
        repeat=json.loads(row["repeat"]) if row["repeat"] else None,
        follow_up=json.loads(row["follow_up"]) if row["follow_up"] else None,
        status=row["status"], ack=bool(row["ack"]), attempts=row["attempts"],
        snooze_count=row["snooze_count"], last_fired=row["last_fired"],
        snoozed_until=row["snoozed_until"], project=row["project"],
        person=row["person"], source=row["source"], kind=kind, duration=duration,
    )


class ReminderStore:
    def __init__(self, db: Database, clock: Clock, config: dict | None = None) -> None:
        self.db = db
        self.clock = clock
        cfg = config or {}
        self.follow_up_default_seconds = int(cfg.get("follow_up_default_seconds", 600))
        self.follow_up_max = int(cfg.get("follow_up_max", 3))
        self.min_repeat_seconds = int(cfg.get("min_repeat_seconds", 60))

    # -- create ------------------------------------------------------------
    def add(
        self,
        text: str,
        due_at: float,
        repeat: dict | None = None,
        follow_up: bool = False,
        follow_up_seconds: int | None = None,
        follow_up_max: int | None = None,
        project: str = "",
        person: str = "owner",
        source: str = "user",
        kind: str = "",
        duration: int | None = None,
    ) -> Reminder:
        # single validation choke point: every creation path (dashboard form,
        # API, chat fast-path, model tool) flows through here, so junk never
        # reaches the store — no "hi"/"null"/blank reminder rows, ever.
        text = str(text or "").strip()
        project = str(project or "").strip()
        if len(text) < 2 or _TRIVIAL_TEXT_RE.match(text):
            raise ValueError(
                f"\"{text}\" is not an actionable reminder text — say what "
                "should be remembered, e.g. 'check the servers'")
        text = (text or "").strip()
        if not text:
            raise ValueError("reminder text is required")
        if repeat is not None and too_frequent(repeat, self.min_repeat_seconds):
            raise ValueError(f"repeat interval below minimum ({self.min_repeat_seconds}s)")
        now = self.clock.now()
        if follow_up and follow_up_seconds is None:
            follow_up_seconds = self.follow_up_default_seconds
        follow_up_spec = None
        if follow_up:
            follow_up_spec = {
                "every_seconds": max(60, int(follow_up_seconds or 600)),
                "max": int(follow_up_max if follow_up_max is not None else self.follow_up_max),
            }
        rem_id = new_id("r")
        self.db.execute(
            """INSERT INTO reminders (id, text, due_at, created_at, repeat, follow_up, status,
                project, person, source, kind, duration) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rem_id, text, float(due_at), now,
             json.dumps(repeat, ensure_ascii=False) if repeat else None,
             json.dumps(follow_up_spec, ensure_ascii=False) if follow_up_spec else None,
             STATUS_SCHEDULED, project, person, source, kind,
             int(duration) if duration else None),
        )
        return self.get(rem_id)

    # -- read --------------------------------------------------------------
    def get(self, rem_id: str) -> Reminder | None:
        row = self.db.query_one("SELECT * FROM reminders WHERE id=?", (rem_id,))
        return _row_to_reminder(row) if row else None

    def list(self, active_only: bool = False, person: str | None = None,
             project: str | None = None) -> list[Reminder]:
        sql, params = "SELECT * FROM reminders", []
        clauses = []
        if active_only:
            clauses.append(f"status IN ({','.join('?' * len(ACTIVE_STATUSES))})")
            params.extend(ACTIVE_STATUSES)
        if person:
            clauses.append("person=?")
            params.append(person)
        if project:
            clauses.append("project=?")
            params.append(project)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY due_at ASC"
        return [_row_to_reminder(r) for r in self.db.query(sql, tuple(params))]

    def due_now(self, now: float | None = None) -> list[Reminder]:
        now = now if now is not None else self.clock.now()
        rows = self.db.query(
            "SELECT * FROM reminders WHERE status IN (?,?,?) AND due_at <= ? ORDER BY due_at",
            (STATUS_SCHEDULED, STATUS_WAITING_ACK, STATUS_SNOOZED, now),
        )
        return [_row_to_reminder(r) for r in rows]

    # -- lifecycle transitions ------------------------------------------------
    def _update(self, rem_id: str, **fields) -> None:
        if not fields:
            return
        cols, params = [], []
        mapping = {
            "status": "status", "due_at": "due_at", "ack": "ack", "attempts": "attempts",
            "snooze_count": "snooze_count", "last_fired": "last_fired",
            "snoozed_until": "snoozed_until", "text": "text",
        }
        for key, value in fields.items():
            if key not in mapping:
                continue
            cols.append(f"{mapping[key]}=?")
            params.append((1 if value else 0) if key == "ack" else value)
        params.append(rem_id)
        self.db.execute(f"UPDATE reminders SET {', '.join(cols)} WHERE id=?", tuple(params))

    def mark_fired(self, rem: Reminder, waiting_ack: bool) -> None:
        """After a delivery attempt: recurring reminders are rescheduled to
        their next occurrence; one-shots wait for ack (if follow-up is on)
        or finish as delivered."""
        now = self.clock.now()
        fields: dict = {"attempts": rem.attempts + 1, "last_fired": now}
        if rem.recurring and rem.repeat:
            nxt = next_occurrence(rem.repeat, max(rem.due_at, now))
            if nxt is None:
                fields["status"] = STATUS_DONE
            else:
                fields["status"] = STATUS_SCHEDULED if not waiting_ack else STATUS_WAITING_ACK
                fields["due_at"] = nxt
        else:
            fields["status"] = STATUS_WAITING_ACK if waiting_ack else STATUS_DELIVERED
            if waiting_ack and rem.follow_up:
                fields["due_at"] = now + rem.follow_up["every_seconds"]
        self._update(rem.id, **fields)

    def follow_up_due(self, rem: Reminder) -> bool:
        return (rem.status == STATUS_WAITING_ACK and rem.follow_up is not None
                and rem.attempts < rem.follow_up["max"])

    def exhaust_follow_up(self, rem: Reminder) -> None:
        if rem.recurring:
            nxt = next_occurrence(rem.repeat, self.clock.now())
            self._update(rem.id, status=STATUS_SCHEDULED, due_at=nxt or (self.clock.now() + 3600))
        else:
            self._update(rem.id, status=STATUS_DONE)

    def acknowledge(self, rem_id: str) -> Reminder | None:
        rem = self.get(rem_id)
        if rem is None:
            return None
        if rem.recurring:
            nxt = next_occurrence(rem.repeat, self.clock.now())
            self._update(rem_id, ack=True, status=STATUS_SCHEDULED,
                         due_at=nxt or (self.clock.now() + 3600), attempts=0)
        else:
            self._update(rem_id, ack=True, status=STATUS_DONE)
        return self.get(rem_id)

    def dismiss(self, rem_id: str) -> Reminder | None:
        """User decides the reminder is no longer needed (no ack credit)."""
        rem = self.get(rem_id)
        if rem is None:
            return None
        if rem.recurring:
            self._update(rem_id, status=STATUS_CANCELLED)
        else:
            self._update(rem_id, status=STATUS_CANCELLED)
        return self.get(rem_id)

    def complete(self, rem_id: str) -> Reminder | None:
        return self.acknowledge(rem_id)

    def snooze(self, rem_id: str, seconds: int) -> Reminder | None:
        rem = self.get(rem_id)
        if rem is None or rem.status not in ACTIVE_STATUSES:
            return None
        now = self.clock.now()
        self._update(
            rem_id, status=STATUS_SNOOZED, due_at=now + seconds,
            snoozed_until=now + seconds, snooze_count=rem.snooze_count + 1,
        )
        return self.get(rem_id)

    def bring_back(self, rem_id: str, seconds: int) -> Reminder | None:
        """Timer snooze flow: bring a finished one-shot back into the future.
        Unlike plain snooze() this also works from `delivered` — a fired timer
        is delivered until the user answers — but never resurrects anything
        the user dismissed or that was cancelled."""
        rem = self.get(rem_id)
        if rem is None or rem.status in (STATUS_DONE, STATUS_CANCELLED, STATUS_MISSED):
            return None
        now = self.clock.now()
        self._update(
            rem_id, status=STATUS_SNOOZED, due_at=now + seconds,
            snoozed_until=now + seconds, snooze_count=rem.snooze_count + 1,
        )
        return self.get(rem_id)

    def cancel(self, rem_id: str) -> Reminder | None:
        return self.dismiss(rem_id)

    def mark_missed(self, rem: Reminder) -> None:
        self._update(rem.id, status=STATUS_MISSED)

    def reschedule_recurring(self, rem: Reminder, after: float | None = None) -> Reminder | None:
        nxt = next_occurrence(rem.repeat, after if after is not None else self.clock.now())
        if nxt is None:
            return None
        self._update(rem.id, status=STATUS_SCHEDULED, due_at=nxt, attempts=0)
        return self.get(rem.id)

    def clear_done(self) -> int:
        cur = self.db.execute("DELETE FROM reminders WHERE status IN (?,?,?)",
                              (STATUS_DONE, STATUS_CANCELLED, STATUS_MISSED))
        return cur.rowcount if cur.rowcount > 0 else 0
