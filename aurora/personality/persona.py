"""Persona state: seed identity + evolved identity + core anchor.

The persona is the companion's *identity*. It starts from a user-authored
seed (personas/*.json — Aurora ships one original seed) and can evolve
through experience via the evolution engine. The seed is kept read-only
forever (Shikigami's design: evolution writes a parallel evolved copy),
every version is journaled in the personality_log table, and any version
can be rolled back. The core anchor lists traits that every evolution
must preserve, so identity stays stable across restarts and model
changes — the persona lives in Aurora's own store, never inside a model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..clock import Clock
from ..storage import Database
from ..util import read_json


@dataclass
class PersonaState:
    name: str = "Aurora"
    base_prompt: str = ""
    style: str = ""
    core_anchor: str = ""
    evolved_prompt: str = ""
    evolved_style: str = ""
    evolution_count: int = 0
    evolved_at: float = 0.0
    version: int = 0  # incremented on every evolution; 0 = pure seed

    def effective_prompt(self) -> str:
        return self.evolved_prompt or self.base_prompt

    def effective_style(self) -> str:
        return self.evolved_style or self.style

    def to_dict(self) -> dict:
        return {
            "name": self.name, "base_prompt": self.base_prompt, "style": self.style,
            "core_anchor": self.core_anchor, "evolved_prompt": self.evolved_prompt,
            "evolved_style": self.evolved_style, "evolution_count": self.evolution_count,
            "evolved_at": self.evolved_at, "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PersonaState":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


class Persona:
    def __init__(self, db: Database, clock: Clock, seed_path=None) -> None:
        self.db = db
        self.clock = clock
        raw = db.get_meta_json("persona.state", None)
        if raw is None:
            seed = self._load_seed(seed_path)
            self.state = PersonaState(**seed)
            self._save()
        else:
            self.state = PersonaState.from_dict(raw)

    def _load_seed(self, seed_path) -> dict:
        data = read_json(seed_path, {}) if seed_path else {}
        return {
            "name": data.get("name", "Aurora"),
            "base_prompt": data.get("base_prompt", self._fallback_prompt()),
            "style": data.get("style_constraint", ""),
            "core_anchor": data.get("core_anchor", ""),
        }

    @staticmethod
    def _fallback_prompt() -> str:
        return ("## Identity\nYou are Aurora, a locally-run AI companion.\n\n"
                "## Attitude\nYou are warm, curious and honest.")

    # -- persistence -------------------------------------------------------
    def _save(self) -> None:
        self.db.set_meta_json("persona.state", self.state.to_dict())

    # -- prompt rendering ---------------------------------------------------
    def prompt_block(self, user_name: str) -> str:
        s = self.state
        parts = [
            f"# Who you are\n{s.effective_prompt()}",
        ]
        if s.effective_style():
            parts.append(f"# Style\n{s.effective_style()}")
        if s.core_anchor:
            parts.append(
                "# Core identity (never betray these traits, regardless of events)\n" + s.core_anchor
            )
        parts.append(
            f"You are talking with {user_name}. You are one single companion — not a "
            "roleplay character, not an assistant persona to be re-derived each session."
        )
        return "\n\n".join(parts)

    # -- evolution -----------------------------------------------------------
    def apply_evolution(self, base_prompt: str, style: str, change_summary: str, reason: str,
                        anchor: str | None, baseline_review: bool) -> int:
        """Persist a new evolved version. Returns the new version number."""
        s = self.state
        if anchor:
            s.core_anchor = anchor
        s.evolved_prompt = base_prompt
        s.evolved_style = style
        s.evolution_count += 1
        s.evolved_at = self.clock.now()
        s.version += 1
        self.db.execute(
            """INSERT INTO personality_log (ts, version, change_summary, reason, base_prompt, style,
                core_anchor, baseline_review) VALUES (?,?,?,?,?,?,?,?)""",
            (self.clock.now(), s.version, change_summary, reason, base_prompt, style,
             s.core_anchor, 1 if baseline_review else 0),
        )
        self._save()
        return s.version

    def history(self) -> list[dict]:
        rows = self.db.query(
            "SELECT id, ts, version, change_summary, reason, base_prompt, style, core_anchor, "
            "baseline_review FROM personality_log ORDER BY version DESC"
        )
        return [dict(r) for r in rows]

    def rollback(self, version: int) -> bool:
        """Restore the evolved persona to the given version's snapshot.
        The seed itself is immutable; the core anchor is untouched."""
        row = self.db.query_one(
            "SELECT base_prompt, style, change_summary, reason FROM personality_log WHERE version=?",
            (version,),
        )
        if row is None:
            return False
        self.state.evolved_prompt = row["base_prompt"]
        self.state.evolved_style = row["style"]
        self.state.version = version
        self._save()
        return True

    def set_manual(self, prompt: str, style: str) -> None:
        """Direct user edit of the evolved persona (always allowed)."""
        self.state.evolved_prompt = prompt
        self.state.evolved_style = style
        self._save()

    def extract_anchor(self) -> str:
        return self.state.core_anchor
