"""The fact store — Aurora's single long-term memory.

One memory system for the whole app (no competing stores). Facts are
never hard-deleted by normal flows: corrections *supersede* (Luna's
retire-don't-delete chain), curation *archives* or *merges* (Shikigami's
soft deletes), and explicit "forget" requests tombstone the content so it
stays forgotten. Every fact carries evidence pointers to the messages
that justify it, a version list, bi-temporal validity and a weight.

Schema and ideas adapted from Luna Local Agent's factstore.py and
Shikigami Protocol's long_term_store.py (both AGPL-3.0).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..clock import Clock
from ..storage import Database
from ..util import jaccard, new_id, salient_words

CATEGORIES = ("habit", "preference", "relationship", "project", "event", "fact", "other")
ARCHIVE_REASONS = ("merged", "low_value", "obsolete", "cold_memory", "unsupported", "user_request")


@dataclass
class Fact:
    id: str
    content: str
    subject: str = "owner"
    category: str = "other"
    kind: str = ""
    event_date: str | None = None
    expires_at: float | None = None
    weight: float = 1.0
    source: str = "auto"
    trust: str = "confirmed"
    pinned: bool = False
    archived: bool = False
    archive_reason: str | None = None
    merged_into: str | None = None
    superseded_by: str | None = None
    superseded_at: float | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    evidence: list[dict] = field(default_factory=list)
    versions: list[dict] = field(default_factory=list)
    emotional_note: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id, "content": self.content, "subject": self.subject,
            "category": self.category, "kind": self.kind, "event_date": self.event_date,
            "expires_at": self.expires_at, "weight": self.weight, "source": self.source,
            "trust": self.trust, "pinned": self.pinned, "archived": self.archived,
            "archive_reason": self.archive_reason, "merged_into": self.merged_into,
            "superseded_by": self.superseded_by, "superseded_at": self.superseded_at,
            "valid_from": self.valid_from, "valid_to": self.valid_to,
            "evidence": self.evidence, "versions": self.versions,
            "emotional_note": self.emotional_note,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    @property
    def active(self) -> bool:
        expired = self.expires_at is not None and self.expires_at <= (self.updated_at or 0)
        return not (self.archived or self.superseded_by or expired)

    def score(self, now: float) -> float:
        """Prompt-selection score (Shikigami's formula): weight matters more
        than recency, but freshness keeps a fact in play."""
        hours = max(0.0, (now - (self.updated_at or now)) / 3600.0)
        recency = 1.0 / (1.0 + _log1p(hours))
        return self.weight * 0.7 + recency * 0.3


def _log1p(x: float) -> float:
    import math

    return math.log1p(x)


def _row_to_fact(row) -> Fact:
    return Fact(
        id=row["id"], content=row["content"], subject=row["subject"],
        category=row["category"], kind=row["kind"], event_date=row["event_date"],
        expires_at=row["expires_at"], weight=row["weight"], source=row["source"],
        trust=row["trust"], pinned=bool(row["pinned"]), archived=bool(row["archived"]),
        archive_reason=row["archive_reason"], merged_into=row["merged_into"],
        superseded_by=row["superseded_by"], superseded_at=row["superseded_at"],
        valid_from=row["valid_from"], valid_to=row["valid_to"],
        evidence=json.loads(row["evidence"] or "[]"),
        versions=json.loads(row["versions"] or "[]"),
        emotional_note=row["emotional_note"] or "",
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


class DuplicateFact(Exception):
    def __init__(self, existing: Fact) -> None:
        super().__init__(f"duplicate of {existing.id}")
        self.existing = existing


class ForgottenFact(Exception):
    """The content matches something the user asked the companion to forget."""


class FactStore:
    def __init__(self, db: Database, clock: Clock, duplicate_threshold: float = 0.8) -> None:
        self.db = db
        self.clock = clock
        self.duplicate_threshold = duplicate_threshold

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _norm_date(value: str | None) -> str | None:
        if not value:
            return None
        try:
            datetime.strptime(value[:10], "%Y-%m-%d")
            return value[:10]
        except ValueError:
            return None

    # -- tombstones ------------------------------------------------------
    def _tombstones(self) -> list[dict]:
        return self.db.get_meta_json("forget.tombstones", [])

    def is_forgotten(self, text: str) -> bool:
        words = set(salient_words(text))
        if not words:
            return False
        for tomb in self._tombstones():
            twords = set(tomb.get("words", []))
            if not twords:
                continue
            if len(twords) <= 2 and twords.issubset(words):
                return True
            if jaccard(words, twords) >= 0.5:
                return True
        return False

    def forget_text(self, text: str) -> dict:
        """Tombstone a phrase (explicit 'forget X' request): future facts
        matching it are refused, and callers scrub active facts."""
        tomb = {
            "text": text,
            "words": salient_words(text),
            "at": self.clock.now(),
        }
        tombs = self._tombstones()
        tombs.append(tomb)
        self.db.set_meta_json("forget.tombstones", tombs)
        return tomb

    # -- CRUD ------------------------------------------------------------
    def add_fact(
        self,
        content: str,
        subject: str = "owner",
        category: str = "other",
        source: str = "auto",
        evidence: list[dict] | None = None,
        kind: str = "",
        event_date: str | None = None,
        expires_at: float | None = None,
        weight: float = 1.0,
        trust: str = "confirmed",
        emotional_note: str = "",
        pinned: bool = False,
        allow_duplicate: bool = False,
    ) -> Fact:
        content = content.strip()
        if not content:
            raise ValueError("empty fact")
        now = self.clock.now()
        if self.is_forgotten(content):
            raise ForgottenFact(content)

        dup = self.find_duplicate(content, subject)
        if dup is not None and not allow_duplicate:
            if dup.weight < weight or dup.trust == "unconfirmed":
                self._touch(dup.id, weight=max(dup.weight, weight), trust=trust)
            raise DuplicateFact(self.get_fact(dup.id))

        fact_id = new_id("f")
        ts = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
        self.db.execute(
            """INSERT INTO facts (id, content, subject, category, kind, event_date, expires_at,
                weight, source, trust, pinned, evidence, versions, emotional_note, valid_from,
                created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                fact_id, content, subject, category if category in CATEGORIES else "other",
                kind, self._norm_date(event_date), expires_at, float(weight), source,
                trust if trust in ("confirmed", "unconfirmed") else "confirmed",
                1 if pinned else 0, json.dumps(evidence or [], ensure_ascii=False), "[]",
                emotional_note[:64], ts, now, now,
            ),
        )
        return self.get_fact(fact_id)

    def find_duplicate(self, content: str, subject: str = "owner") -> Fact | None:
        new_words = salient_words(content)
        if not new_words:
            return None
        rows = self.db.query(
            "SELECT * FROM facts WHERE subject=? AND archived=0 AND superseded_by IS NULL",
            (subject,),
        )
        for row in rows:
            if jaccard(new_words, salient_words(row["content"])) >= self.duplicate_threshold:
                return _row_to_fact(row)
        return None

    def get_fact(self, fact_id: str) -> Fact | None:
        row = self.db.query_one("SELECT * FROM facts WHERE id=?", (fact_id,))
        return _row_to_fact(row) if row else None

    def _touch(self, fact_id: str, weight: float | None = None, trust: str | None = None) -> None:
        sets, params = ["updated_at=?"], [self.clock.now()]
        if weight is not None:
            sets.append("weight=?")
            params.append(float(weight))
        if trust:
            sets.append("trust=?")
            params.append(trust)
        params.append(fact_id)
        self.db.execute(f"UPDATE facts SET {', '.join(sets)} WHERE id=?", tuple(params))

    def update_fact(self, fact_id: str, new_content: str, evidence: list[dict] | None = None,
                    note: str | None = None) -> Fact:
        fact = self.get_fact(fact_id)
        if fact is None:
            raise KeyError(fact_id)
        versions = list(fact.versions)
        versions.append({"content": fact.content, "weight": fact.weight, "changed_at": self.clock.now()})
        self.db.execute(
            "UPDATE facts SET content=?, versions=?, emotional_note=COALESCE(?, emotional_note), updated_at=? WHERE id=?",
            (new_content.strip(), json.dumps(versions, ensure_ascii=False), note, self.clock.now(), fact_id),
        )
        if evidence:
            self.attach_evidence(fact_id, evidence)
        return self.get_fact(fact_id)

    def attach_evidence(self, fact_id: str, evidence: list[dict]) -> None:
        fact = self.get_fact(fact_id)
        if fact is None:
            raise KeyError(fact_id)
        merged = {json.dumps(e, sort_keys=True) for e in fact.evidence}
        for e in evidence:
            merged.add(json.dumps(e, sort_keys=True))
        self.db.execute(
            "UPDATE facts SET evidence=?, updated_at=? WHERE id=?",
            (json.dumps([json.loads(m) for m in merged], ensure_ascii=False), self.clock.now(), fact_id),
        )

    def supersede_fact(self, old_id: str, new_content: str, evidence: list[dict] | None = None,
                       source: str = "consolidation") -> Fact:
        """Replace an outdated/contradicted fact: the old one stays, marked
        superseded_by the new one (Luna's chain), the new one is added."""
        old = self.get_fact(old_id)
        if old is None:
            raise KeyError(old_id)
        new = self.add_fact(
            new_content, subject=old.subject, category=old.category, source=source,
            evidence=evidence or old.evidence, kind=old.kind, event_date=old.event_date,
            weight=old.weight, trust=old.trust, allow_duplicate=True,
        )
        self.db.execute(
            "UPDATE facts SET superseded_by=?, superseded_at=?, updated_at=? WHERE id=?",
            (new.id, self.clock.now(), self.clock.now(), old_id),
        )
        return new

    def archive_fact(self, fact_id: str, reason: str) -> Fact:
        fact = self.get_fact(fact_id)
        if fact is None:
            raise KeyError(fact_id)
        if fact.pinned and reason != "user_request":
            raise ValueError("pinned facts are protected; unpin first")
        self.db.execute(
            "UPDATE facts SET archived=1, archive_reason=?, updated_at=? WHERE id=?",
            (reason if reason in ARCHIVE_REASONS else "low_value", self.clock.now(), fact_id),
        )
        return self.get_fact(fact_id)

    def restore_fact(self, fact_id: str) -> Fact:
        self.db.execute(
            "UPDATE facts SET archived=0, archive_reason=NULL, updated_at=? WHERE id=?",
            (self.clock.now(), fact_id),
        )
        return self.get_fact(fact_id)

    def merge_fact(self, duplicate_id: str, target_id: str) -> Fact:
        """Soft-merge a duplicate into its target: the duplicate is archived
        with reason 'merged' and stays resolvable via merged_into."""
        dup = self.get_fact(duplicate_id)
        target = self.get_fact(target_id)
        if dup is None or target is None:
            raise KeyError(duplicate_id if dup is None else target_id)
        self.db.execute(
            "UPDATE facts SET archived=1, archive_reason='merged', merged_into=?, updated_at=? WHERE id=?",
            (target_id, self.clock.now(), duplicate_id),
        )
        if dup.evidence:
            self.attach_evidence(target_id, dup.evidence)
        self._touch(target_id, weight=min(2.0, target.weight + 0.1))
        return self.get_fact(target_id)

    def delete_fact(self, fact_id: str) -> None:
        """Hard delete (dashboard 'delete' button, export/GDPR flows)."""
        self.db.execute("DELETE FROM facts WHERE id=?", (fact_id,))

    def confirm_fact(self, fact_id: str) -> Fact:
        self._touch(fact_id, trust="confirmed")
        return self.get_fact(fact_id)

    def set_pinned(self, fact_id: str, pinned: bool) -> Fact:
        self.db.execute("UPDATE facts SET pinned=? WHERE id=?", (1 if pinned else 0, fact_id))
        return self.get_fact(fact_id)

    # -- queries ---------------------------------------------------------
    def active_facts(self, subject: str | None = None) -> list[Fact]:
        if subject is None:
            rows = self.db.query(
                "SELECT * FROM facts WHERE archived=0 AND superseded_by IS NULL "
                "AND (expires_at IS NULL OR expires_at > ?) ORDER BY weight DESC, updated_at DESC",
                (self.clock.now(),),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM facts WHERE subject=? AND archived=0 AND superseded_by IS NULL "
                "AND (expires_at IS NULL OR expires_at > ?) ORDER BY weight DESC, updated_at DESC",
                (subject, self.clock.now()),
            )
        return [_row_to_fact(r) for r in rows]

    def facts_fts(self, query: str, subject: str | None = None, limit: int = 20) -> list[Fact]:
        """Full-text search (graceful degradation mirrors Luna's fallback:
        if FTS5 misbehaves we fall back to substring matching)."""
        safe = "".join(ch for ch in query if ch.isalnum() or ch.isspace()).strip()
        if not safe:
            return []
        where = "WHERE facts_fts MATCH ? AND archived=0 AND superseded_by IS NULL"
        params: list[Any] = [" OR ".join(f'"{w}"' for w in safe.split())]
        if subject:
            where += " AND subject=?"
            params.append(subject)
        try:
            rows = self.db.query(
                f"SELECT facts.* FROM facts_fts JOIN facts ON facts.rowid = facts_fts.rowid "
                f"{where} ORDER BY rank LIMIT {int(limit)}",
                tuple(params),
            )
        except Exception:
            rows = self.db.query(
                f"SELECT * FROM facts {where.replace('facts_fts MATCH', 'content LIKE')} "
                f"LIMIT {int(limit)}",
                tuple([f"%{safe}%"] + ([subject] if subject else [])),
            )
        return [_row_to_fact(r) for r in rows]

    def facts_about(self, text: str, subject: str | None = None, limit: int = 8) -> list[Fact]:
        """Facts lexically related to `text` — used for extraction context
        (so the extractor can supersede outdated ones)."""
        words = salient_words(text)
        if not words:
            return []
        candidates = self.active_facts(subject)
        scored = []
        for fact in candidates:
            overlap = jaccard(words, salient_words(fact.content))
            if overlap > 0.15:
                scored.append((overlap, fact))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [f for _, f in scored[:limit]]

    def recent_facts(self, days: int = 3, limit: int = 5) -> list[Fact]:
        cutoff = self.clock.now() - days * 86400
        rows = self.db.query(
            "SELECT * FROM facts WHERE archived=0 AND superseded_by IS NULL AND updated_at >= ? "
            "ORDER BY updated_at DESC LIMIT ?",
            (cutoff, limit),
        )
        return [_row_to_fact(r) for r in rows]

    def old_facts(self, older_than_days: float) -> list[Fact]:
        cutoff = self.clock.now() - older_than_days * 86400
        rows = self.db.query(
            "SELECT * FROM facts WHERE archived=0 AND superseded_by IS NULL AND created_at <= ? "
            "ORDER BY created_at ASC",
            (cutoff,),
        )
        return [_row_to_fact(r) for r in rows]

    def export(self) -> dict:
        rows = self.db.query("SELECT * FROM facts ORDER BY created_at")
        return {
            "exported_at": self.clock.now(),
            "facts": [_row_to_fact(r).to_dict() for r in rows],
            "tombstones": self._tombstones(),
        }

    def counts(self) -> dict:
        row = self.db.query_one(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN archived=0 AND superseded_by IS NULL THEN 1 ELSE 0 END) AS active "
            "FROM facts"
        )
        return {"total": row["total"] or 0, "active": row["active"] or 0}
