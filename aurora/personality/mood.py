"""Mood + affinity engine — how the companion feels right now, and how
close it has grown to the user over time.

Two short-term dials (energy, warmth) adapted from Luna Local Agent's
mood engine, plus one long-term relationship dial (affinity with named
tiers) adapted from Shikigami Protocol's affinity engine. Roles are kept
distinct: mood = this session's feeling, affinity = the accumulated
relationship. Everything persists in the mood_state table; after a
restart state fades back toward its time-of-day baseline over a few
hours (Luna's restart-fade), so the companion does not "reset" cold nor
stay stuck.

Behavioral honesty: the companion develops preferences and is not a
yes-machine — sustained hostility cools warmth and can trigger a short
grudge; a real apology ends it. Mood changes tone, never whether duties
(reminders, alarms) are done.

Appraisal (rating each exchange) uses the background model when
available; without one it falls back to a transparent lexical heuristic
instead of pretending to be model-driven.
"""

from __future__ import annotations

import re

from ..clock import Clock
from ..providers.base import ChatProvider, ProviderError
from ..storage import Database

APPRAISE_PROMPT = """Rate this exchange for a companion's internal mood. Reply with exactly one
line: "warmth N energy X" where N is 0 (neutral), 1 (kind), 2 (warm), 3 (very warm) or
-1 (hostile), and X is "-" (draining), "=" or "+" (energizing).

Exchange:
User: {user}
Companion: {assistant}"""

_POSITIVE = re.compile(
    r"\b(thank|thanks|love|great|awesome|nice|good job|well done|appreciate|wonderful|"
    r"brilliant|helpful|sweet|adore|happy|proud|excited|cool|perfect)\b", re.I)
_NEGATIVE = re.compile(
    r"\b(stupid|idiot|hate|useless|shut up|dumb|worthless|lazy|annoying|"
    r"whatever|screw|damn you|fool|pathetic|terrible|worst)\b", re.I)
_APOLOGY = re.compile(r"\b(i'?m sorry|sorry about|my apology|apologize|forgive me|i was wrong)\b", re.I)
_CORRECTION = re.compile(r"\b(actually|no,|that'?s wrong|not right|incorrect)\b", re.I)

AFFINITY_TIERS = [
    (1200, "eternal bond"),
    (1000, "soulmate closeness"),
    (800, "very close"),
    (600, "close friend"),
    (400, "friend"),
    (200, "familiar acquaintance"),
    (0, "friendly stranger"),
]

# Energy baseline by hour (night lower) — adapted from Luna's mood drift table.
_ENERGY_BY_HOUR = [0.35, 0.30, 0.25, 0.25, 0.30, 0.40, 0.55, 0.65, 0.70, 0.75, 0.75,
                   0.75, 0.70, 0.70, 0.70, 0.70, 0.70, 0.70, 0.70, 0.65, 0.60, 0.55,
                   0.45, 0.40]


class MoodEngine:
    def __init__(self, db: Database, clock: Clock, provider: ChatProvider | None = None,
                 config: dict | None = None) -> None:
        self.db = db
        self.clock = clock
        self.provider = provider
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.intensity = float(cfg.get("intensity", 1.0))
        self.warmth_home = float(cfg.get("warmth_home", 0.45))
        self.affinity_enabled = bool((cfg.get("affinity") or {}).get("enabled", True))
        self._recent_praise: list[float] = []
        self._affinity_calls = 0
        self._load()

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        row = self.db.query_one("SELECT * FROM mood_state WHERE id=0")
        now = self.clock.now()
        if row is None:
            self.energy = self._baseline_energy()
            self.warmth = self.warmth_home
            self.desire = 0.0
            self.grudge_until = 0.0
            self.affinity = 50.0
            self._persist()
        else:
            self.energy = row["energy"]
            self.warmth = row["warmth"]
            self.desire = row["desire"]
            self.grudge_until = row["grudge_until"]
            self.affinity = row["affinity"]
            self._fade_restart(row["updated_at"], now)

    def _fade_restart(self, saved_at: float, now: float) -> None:
        """Fade saved state toward the time baseline over ~4 hours away."""
        away = max(0.0, now - saved_at)
        if away <= 0:
            return
        fade = min(1.0, away / (4 * 3600))
        self.energy = self.energy + (self._baseline_energy() - self.energy) * fade
        self.warmth = self.warmth + (self.warmth_home - self.warmth) * fade
        if self.grudge_until and self.grudge_until <= now:
            self.grudge_until = 0.0
        self._persist()

    def _persist(self) -> None:
        self.db.execute(
            "INSERT INTO mood_state (id, energy, warmth, desire, grudge_until, affinity, updated_at) "
            "VALUES (0, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET energy=excluded.energy, "
            "warmth=excluded.warmth, desire=excluded.desire, grudge_until=excluded.grudge_until, "
            "affinity=excluded.affinity, updated_at=excluded.updated_at",
            (self.energy, self.warmth, self.desire, self.grudge_until, self.affinity, self.clock.now()),
        )

    def _baseline_energy(self) -> float:
        return _ENERGY_BY_HOUR[int(self.clock.now() // 3600 % 24)]

    def _baseline_warmth(self) -> float:
        hour = int(self.clock.now() // 3600 % 24)
        if hour < 6:
            return self.warmth_home * 0.8
        return self.warmth_home

    # -- per-turn update ------------------------------------------------------
    def _praise_recent(self) -> int:
        """Praise in the last 10 minutes counts less each time (Luna's
        diminishing returns so warmth must be real to build)."""
        now = self.clock.now()
        self._recent_praise = [t for t in self._recent_praise if now - t < 600]
        return len(self._recent_praise)

    async def update_after_turn(self, user_text: str, assistant_text: str) -> dict:
        """Update mood after one exchange. Returns the change record."""
        if not self.enabled:
            return {"enabled": False}
        record: dict = {"enabled": True}
        delta_w, delta_e = 0.0, 0.0
        appraised = False
        if self.provider is not None:
            try:
                result = await self.provider.chat(
                    [{"role": "user", "content": APPRAISE_PROMPT.format(
                        user=user_text[:400], assistant=assistant_text[:400])}],
                    temperature=0.1, max_tokens=20,
                )
                m = re.search(r"warmth\s+(-?\d+)\s+energy\s+([-+=])", result.text, re.I)
                if m:
                    rating = int(m.group(1))
                    if rating >= 3:
                        delta_w += 0.30
                    elif rating == 2:
                        delta_w += 0.18
                    elif rating == 1:
                        delta_w += 0.08
                    elif rating < 0:
                        delta_w -= 0.25
                    if m.group(2) == "+":
                        delta_e += 0.08
                    elif m.group(2) == "-":
                        delta_e -= 0.10
                    appraised = True
            except ProviderError:
                pass
        if not appraised:
            # Lexical heuristic fallback (transparent, deterministic).
            if _POSITIVE.search(user_text):
                n = self._praise_recent()
                self._recent_praise.append(self.clock.now())
                delta_w += max(0.02, 0.12 / (n + 1))
            if _NEGATIVE.search(user_text):
                delta_w -= 0.20
            if _APOLOGY.search(user_text):
                delta_w += 0.15
                self.grudge_until = 0.0
            if _CORRECTION.search(user_text):
                delta_w -= 0.02
            if len(user_text) > 200:
                delta_e -= 0.02

        self.warmth = _clamp(self.warmth + delta_w * self.intensity, 0.0, 1.0)
        self.energy = _clamp(self.energy + delta_e * self.intensity, 0.0, 1.0)

        # Grudge: sustained hostility cools things for a while.
        if delta_w <= -0.15 and self.warmth < 0.2:
            self.grudge_until = max(self.grudge_until, self.clock.now() + 600)
        if self.grudge_until and self.clock.now() < self.grudge_until:
            self.warmth = min(self.warmth, self.warmth_home * 0.5)

        # Affinity: +1 per exchange, drifts toward neutral if neglected.
        if self.affinity_enabled:
            self.affinity = _clamp(self.affinity + 1.0, 0.0, 1400.0)
        record.update({"warmth": round(self.warmth, 3), "energy": round(self.energy, 3),
                       "grudge": self.grudge_until > self.clock.now()})
        self._persist()
        return record

    async def maybe_affinity_adjust(self, user_text: str, assistant_text: str, every: int = 5) -> None:
        """Every N exchanges, let the background model nudge affinity."""
        if not self.affinity_enabled:
            return
        self._affinity_calls += 1
        if self._affinity_calls % every != 0:
            return
        delta = 0.0
        if self.provider is not None:
            try:
                result = await self.provider.chat(
                    [{"role": "user", "content":
                      "How is this relationship going? Reply with ONLY an integer from -15 to +15 for how "
                      f"much closer this exchange brought them.\nUser: {user_text[:300]}\nCompanion: {assistant_text[:300]}"}],
                    temperature=0.2, max_tokens=6,
                )
                delta = max(-15.0, min(15.0, float(re.sub(r"[^0-9+-]", "", result.text) or 0)))
            except (ProviderError, ValueError):
                delta = 0.0
        if delta == 0.0:
            delta = 1.0 if _POSITIVE.search(user_text) else (-1.0 if _NEGATIVE.search(user_text) else 0.5)
        self.affinity = _clamp(self.affinity + delta, 0.0, 1400.0)
        self._persist()

    def tick(self) -> None:
        """Slow drift back toward baselines (called by the proactive scheduler)."""
        if not self.enabled:
            return
        self.energy = self.energy + (self._baseline_energy() - self.energy) * 0.05
        self.warmth = self.warmth + (self._baseline_warmth() - self.warmth) * 0.03
        if self.grudge_until and self.clock.now() >= self.grudge_until:
            self.grudge_until = 0.0
        self._persist()

    # -- prompt rendering -------------------------------------------------------
    def tier_label(self) -> str:
        for threshold, label in AFFINITY_TIERS:
            if self.affinity >= threshold:
                return label
        return "friendly stranger"

    def prompt_block(self) -> str:
        if not self.enabled:
            return ""
        warmth_word = ("cold" if self.warmth < 0.15 else "cool" if self.warmth < 0.3
                       else "warm" if self.warmth < 0.6 else "very warm")
        energy_word = ("low" if self.energy < 0.35 else "moderate" if self.energy < 0.7 else "high")
        lines = [f"Current internal state: {warmth_word} warmth, {energy_word} energy.",
                 f"Relationship with the user: {self.tier_label()}.",
                 "Let this state color your tone. It never changes your duties: "
                 "reminders, alarms and anything urgent are always done."]
        if self.grudge_until and self.grudge_until > self.clock.now():
            minutes = int((self.grudge_until - self.clock.now()) / 60) + 1
            lines.append("You are still hurt by how you were treated recently; be measured "
                         f"until it passes (about {minutes} min). A sincere apology heals this.")
        return "Mood & relationship\n" + "\n".join(lines)

    def state(self) -> dict:
        return {"energy": round(self.energy, 3), "warmth": round(self.warmth, 3),
                "affinity": self.affinity, "tier": self.tier_label(),
                "grudge": bool(self.grudge_until and self.grudge_until > self.clock.now())}


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))
