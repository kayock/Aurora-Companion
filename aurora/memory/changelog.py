"""Memory changelog: inspect and roll back consolidation runs.

Consolidation actions (merges, archivals, supersedes) are journaled so
the dashboard can show what changed and undo a bad run. Rollback is
best-effort and per-action: merges re-activate the archived duplicate,
archivals restore the fact, supersedes re-activate the old fact and
archive its replacement.
"""

from __future__ import annotations

from ..clock import Clock
from ..storage import Database
from .store import FactStore


class MemoryChangelog:
    def __init__(self, db: Database, store: FactStore, clock: Clock) -> None:
        self.db = db
        self.store = store
        self.clock = clock

    def entries(self) -> list[dict]:
        return self.db.get_meta_json("memory.changelog", [])

    def _action_log(self) -> list[dict]:
        return self.db.get_meta_json("memory.changelog_actions", [])

    def record_actions(self, actions: list[dict]) -> None:
        log = self._action_log()
        log.insert(0, {"ts": self.clock.now(), "actions": actions, "rolled_back": False})
        self.db.set_meta_json("memory.changelog_actions", log[:50])

    def rollback(self, ts: float) -> dict:
        """Undo all actions recorded at (approximately) the given run ts."""
        undone = {"merged": 0, "archived": 0, "superseded": 0, "missing": 0}
        log = self._action_log()
        for entry in log:
            if entry.get("rolled_back") or abs(entry.get("ts", 0) - ts) > 1.0:
                continue
            for action in entry.get("actions", []):
                kind = action.get("kind")
                fact_id = action.get("fact_id")
                fact = self.store.get_fact(fact_id) if fact_id else None
                if fact is None:
                    undone["missing"] += 1
                    continue
                if kind == "merge":
                    self.store.restore_fact(fact_id)
                    undone["merged"] += 1
                elif kind == "archive":
                    self.store.restore_fact(fact_id)
                    undone["archived"] += 1
                elif kind == "supersede":
                    old_id, new_id = fact_id, action.get("new_id")
                    old = self.store.get_fact(old_id)
                    if old is not None:
                        self.db.execute(
                            "UPDATE facts SET superseded_by=NULL, superseded_at=NULL WHERE id=?",
                            (old_id,),
                        )
                    if new_id:
                        self.store.archive_fact(new_id, "rollback")
                    undone["superseded"] += 1
            entry["rolled_back"] = True
        self.db.set_meta_json("memory.changelog_actions", log)
        return undone
