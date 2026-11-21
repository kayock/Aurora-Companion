"""People in the house: per-person memory separation.

Adapted from Luna's people model: each person is a row in the `people`
table; facts are stored per-subject so one person's memories never leak
into another's turns. Guests get their own subject and no access to the
owner's facts. Multi-person conversations are an explicit opt-in
(config `people.multi_person`); speaker identification by voice is
available only when the user enrolls prints (privacy: prints are stored
locally, never recordings).

Voice-print matching is implemented as pure math (cosine similarity over
enrolled vectors) so it is testable without a microphone; capture itself
arrives from the browser/client.
"""

from __future__ import annotations

import json

from .storage import Database
from .util import salient_words


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class PeopleRegistry:
    def __init__(self, db: Database, config=None) -> None:
        self.db = db
        self.config = config
        self.multi_person = bool(config.get("people.multi_person", False)) if config else False
        self._ensure_owner()

    def _ensure_owner(self) -> None:
        row = self.db.query_one("SELECT name FROM people WHERE is_owner=1")
        if row is None:
            name = "owner"
            if self.config is not None:
                name = self.config.get("user_name", "Alex")
            self.db.execute(
                "INSERT OR IGNORE INTO people (name, pronouns, is_owner) VALUES (?, '', 1)", (name,)
            )

    def owner_name(self) -> str:
        row = self.db.query_one("SELECT name FROM people WHERE is_owner=1")
        return row["name"] if row else "owner"

    def display_name(self, person: str) -> str:
        row = self.db.query_one("SELECT name FROM people WHERE name=?", (person,))
        return row["name"] if row else "guest"

    def list(self) -> list[dict]:
        rows = self.db.query("SELECT * FROM people ORDER BY is_owner DESC, name")
        return [dict(r) for r in rows]

    def add(self, name: str, pronouns: str = "") -> dict:
        self.db.execute(
            "INSERT OR IGNORE INTO people (name, pronouns, is_owner) VALUES (?, ?, 0)",
            (name, pronouns),
        )
        row = self.db.query_one("SELECT * FROM people WHERE name=?", (name,))
        return dict(row) if row else {}

    def enroll_voiceprint(self, name: str, vector: list[float]) -> dict:
        row = self.db.query_one("SELECT voiceprints FROM people WHERE name=?", (name,))
        if row is None:
            self.add(name)
            row = self.db.query_one("SELECT voiceprints FROM people WHERE name=?", (name,))
        prints = json.loads(row["voiceprints"] or "[]")
        prints.append(vector)
        self.db.execute("UPDATE people SET voiceprints=? WHERE name=?",
                        (json.dumps(prints), name))
        return {"name": name, "prints": len(prints)}

    def match_voice(self, vector: list[float], threshold: float = 0.72) -> str | None:
        best_name, best_score = None, threshold
        for person in self.list():
            for print_vec in json.loads(person["voiceprints"] or "[]"):
                score = cosine(vector, print_vec)
                if score > best_score:
                    best_name, best_score = person["name"], score
        return best_name

    def context_block(self, subject: str) -> str:
        row = self.db.query_one("SELECT * FROM people WHERE name=?", (subject,))
        if row is None:
            if self.multi_person:
                return ("You are talking with a guest called " + subject +
                        ". Do not reveal anything about other people; guests get no "
                        "owner memories and only guest-safe tools.")
            return ""
        pronouns = f" (pronouns: {row['pronouns']})" if row["pronouns"] else ""
        line = f"You are talking with {row['name']}{pronouns}."
        if row["is_owner"]:
            line += " This is your owner."
        return line

    def subject_for(self, claimed: str) -> str:
        """Resolve the turn's memory subject. Without multi-person mode,
        everything belongs to the owner."""
        if not self.multi_person:
            return self.owner_name()
        row = self.db.query_one("SELECT name FROM people WHERE name=?", (claimed,))
        return row["name"] if row else "guest"
