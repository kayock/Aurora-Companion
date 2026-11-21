"""The one proactive scheduler — Aurora's single authority for initiating.

Every message Aurora starts on its own (greetings, check-ins, topic
shares, work-break nudges, memory resurfacing) comes from this loop.
Because there is exactly one scheduler and every initiation is gated and
journaled in the proactive_log, duplicate proactive messages from
competing subsystems are prevented by construction — the design flaw the
requirements call out.

Per-kind gates (adapted from Shikigami's ASE engine):
* silence gate: no initiation while the user is actively chatting
  (`min_silence_seconds` since the last user message);
* quiet hours: config window, tightened by the learned sleep window;
* focus mode: explicit user focus periods suppress everything (until ts);
* cooldown: `min_interval * (1 - urgency * 0.85)` — high urgency shortens
  the wait, never to zero;
* daily caps per kind and a global `max_per_day`;
* response consideration: after N consecutive proactive messages with no
  user reply, non-essential kinds back off;
* dedup: a topic ref surfaced in the last 24h is never re-surfaced.

Urgency per kind rises with silence, session length and pending thoughts;
the highest-urgency eligible kind wins the tick.
"""

from __future__ import annotations

import asyncio
import time as _time
from datetime import datetime, timezone

from ..clock import Clock
from ..memory.store import FactStore
from ..personality.mood import MoodEngine
from ..providers.base import ChatProvider
from ..storage import Database
from .sleepwindow import SleepWindow
from .topics import AiSelf, Icebreaker, MemoryResurfacing, TopicCandidate, UserLife, get_candidates

KINDS = ("morning_greeting", "check_in", "topic_share", "work_break", "memory_resurfacing")

KIND_DEFAULTS = {
    "morning_greeting": {"daily_cap": 1, "min_gap_minutes": 0},
    "check_in": {"daily_cap": 2, "min_gap_minutes": 180},
    "topic_share": {"daily_cap": 3, "min_gap_minutes": 240},
    "work_break": {"daily_cap": 2, "min_gap_minutes": 120},
    "memory_resurfacing": {"daily_cap": 1, "min_gap_minutes": 300},
}


def _parse_hhmm(value: str) -> float:
    hh, mm = value.split(":")
    return int(hh) + int(mm) / 60.0


class ProactiveScheduler:
    def __init__(self, db: Database, clock: Clock, provider: ChatProvider | None,
                 store: FactStore, mood: MoodEngine | None = None, config: dict | None = None,
                 compute_guard=None) -> None:
        self.db = db
        self.clock = clock
        self.provider = provider
        self.store = store
        self.mood = mood
        cfg = config or {}
        self.cfg = cfg
        # Resource-awareness: a decided initiation is deferred (not dropped)
        # when compute is needed elsewhere or the model is unloaded; the
        # gates simply stay closed and the next tick retries.
        self.compute_guard = compute_guard
        self.enabled = bool(cfg.get("enabled", True))
        self.tick_seconds = float(cfg.get("tick_seconds", 20))
        self.quiet_start = cfg.get("quiet_start", "23:00")
        self.quiet_end = cfg.get("quiet_end", "07:30")
        self.min_silence = float(cfg.get("min_silence_seconds", 900))
        self.max_per_day = int(cfg.get("max_per_day", 6))
        self.sleep_window = SleepWindow(db, clock)
        self.sources = [
            MemoryResurfacing(store, float((cfg.get("memory_resurfacing") or {}).get("older_than_days", 14))),
            UserLife(store),
            AiSelf(db, mood),
            Icebreaker(),
        ]
        self._last_user_message: float = clock.now()
        self._session_start: float | None = None
        self._task: asyncio.Task | None = None
        self._running = False
        self.on_initiate = None  # async callback set by ConversationManager
        self.last_decision: dict | None = None
        self._candidates: list[TopicCandidate] = []

    # -- state ----------------------------------------------------------------
    def _state(self) -> dict:
        return self.db.get_meta_json("proactive.state", {})

    def _set_state(self, state: dict) -> None:
        self.db.set_meta_json("proactive.state", state)

    def touch_user_message(self) -> None:
        """Called by the conversation manager on every incoming user message."""
        now = self.clock.now()
        state = self._state()
        self._last_user_message = now
        session_start = state.get("session_start")
        # A gap > 30 min starts a new work session.
        if session_start is None or now - session_start > 1800:
            state["session_start"] = now
        state["consecutive_no_response"] = 0
        state["last_user_message"] = now
        self._set_state(state)
        self.sleep_window.observe()

    def set_focus(self, until_epoch: float | None, enabled: bool = True) -> None:
        state = self._state()
        state["focus_until"] = until_epoch if enabled else None
        state["focus_enabled"] = enabled
        self._set_state(state)

    def focus_active(self) -> bool:
        state = self._state()
        if not state.get("focus_enabled"):
            return False
        until = state.get("focus_until")
        return until is None or until > self.clock.now()

    # -- lifecycle ---------------------------------------------------------------
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._running = True
            self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run(self) -> None:
        while self._running:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(self.tick_seconds)

    # -- gating ------------------------------------------------------------------
    def in_quiet_hours(self) -> bool:
        return self.sleep_window.is_quiet(self.quiet_start, self.quiet_end)

    def in_quiet_hours_clock(self, now_epoch: float) -> bool:
        """Quiet-hours check against an arbitrary timestamp (tests)."""
        hour = (now_epoch / 3600.0) % 24
        start, end = _parse_hhmm(self.quiet_start), _parse_hhmm(self.quiet_end)
        return (hour >= start or hour < end) if start > end else (start <= hour < end)

    def _daily_count(self, state: dict, kind: str) -> int:
        today = datetime.fromtimestamp(self.clock.now(), tz=timezone.utc).strftime("%Y-%m-%d")
        counts = state.get("day_counts", {})
        if counts.get("date") != today:
            return 0
        return int(counts.get(kind, 0))

    def _bump_daily(self, state: dict, kind: str) -> None:
        today = datetime.fromtimestamp(self.clock.now(), tz=timezone.utc).strftime("%Y-%m-%d")
        counts = state.get("day_counts", {})
        if counts.get("date") != today:
            counts = {"date": today}
        counts[kind] = int(counts.get(kind, 0)) + 1
        state["day_counts"] = counts
        total = sum(v for k, v in counts.items() if k != "date")
        state["day_total"] = total
        self._set_state(state)

    def _kind_last_fired(self, state: dict, kind: str) -> float:
        return float(state.get("kind_last_fired", {}).get(kind, 0.0))

    def _topic_recently_used(self, ref: str, window_seconds: float = 86400) -> bool:
        cutoff = self.clock.now() - window_seconds
        row = self.db.query_one(
            "SELECT 1 FROM proactive_log WHERE topic_ref=? AND ts > ? LIMIT 1", (ref, cutoff)
        )
        return row is not None

    def _urgency(self, kind: str, state: dict, now: float) -> float:
        silence = now - self._last_user_message
        if kind == "check_in":
            after = float((self.cfg.get("check_in") or {}).get("after_silence_minutes", 90)) * 60
            return max(0.0, min(1.0, (silence - after) / 7200))
        if kind == "topic_share":
            return max(0.0, min(1.0, (silence - self.min_silence) / 7200))
        if kind == "work_break":
            start = state.get("session_start")
            if start is None:
                return 0.0
            length = now - start
            after = float((self.cfg.get("work_break") or {}).get("after_minutes", 90)) * 60
            return max(0.0, min(1.0, (length - after) / 3600)) if silence < 3600 else 0.0
        if kind == "morning_greeting":
            hour = (now / 3600.0) % 24
            window = (self.cfg.get("morning_greeting") or {}).get("window", "07:30-10:00")
            try:
                gs, ge = window.split("-")
                if _parse_hhmm(gs) <= hour <= _parse_hhmm(ge):
                    return 0.8
            except ValueError:
                return 0.0
            return 0.0
        if kind == "memory_resurfacing":
            return max(0.0, min(0.6, (silence - self.min_silence) / 7200 * 0.6))
        return 0.0

    # -- decision ---------------------------------------------------------------
    def decide(self, now: float | None = None) -> dict:
        """Evaluate all gates and return the decision for this tick
        (pure function of state — also used directly by tests)."""
        now = now if now is not None else self.clock.now()
        if not self.enabled:
            return {"speak": False, "reason": "disabled"}
        if self.focus_active():
            return {"speak": False, "reason": "focus period"}
        if self.in_quiet_hours_clock(now):
            return {"speak": False, "reason": "quiet hours"}
        silence = now - self._last_user_message
        if silence < self.min_silence:
            return {"speak": False, "reason": "user active recently", "silence": silence}
        state = self._state()
        if int(state.get("day_total", 0)) >= self.max_per_day:
            return {"speak": False, "reason": "daily proactive cap reached"}
        if int(state.get("consecutive_no_response", 0)) >= 3:
            return {"speak": False, "reason": "no response to recent proactive messages"}

        best: dict | None = None
        for kind in KINDS:
            kcfg = dict(KIND_DEFAULTS[kind])
            kcfg.update(self.cfg.get(kind) or {})
            if not kcfg.get("enabled", True):
                continue
            if self._daily_count(state, kind) >= int(kcfg["daily_cap"]):
                continue
            last = self._kind_last_fired(state, kind)
            urgency = self._urgency(kind, state, now)
            if urgency <= 0:
                continue
            min_gap = float(kcfg["min_gap_minutes"]) * 60
            cooldown = min_gap * (1 - urgency * 0.85)
            if last and now - last < cooldown:
                continue
            candidate = {"kind": kind, "urgency": urgency}
            if best is None or urgency > best["urgency"]:
                best = candidate
        if best is None:
            return {"speak": False, "reason": "no kind eligible"}
        # Topic selection for topic-ish kinds, with 24h dedup by ref.
        topic = None
        if best["kind"] in ("topic_share", "memory_resurfacing"):
            self._candidates = get_candidates(self.sources, {"now": now})
            fresh = [c for c in self._candidates if not self._topic_recently_used(c.ref)]
            if not fresh:
                return {"speak": False, "reason": "no fresh topics"}
            topic = fresh[0]
        best["topic"] = topic
        best["speak"] = True
        return best

    async def tick(self, force: bool = False) -> dict | None:
        decision = self.decide()
        self.last_decision = decision
        if not decision.get("speak") and not force:
            if self.mood is not None:
                self.mood.tick()
            return decision
        if self.compute_guard is not None and not force:
            ok, reason = await self.compute_guard.background_allowed(self.provider)
            if not ok:
                decision = {"speak": False, "deferred": True, "reason": f"deferred: {reason}"}
                self.last_decision = decision
                return decision  # state untouched: caps/cooldowns unaffected, retried next tick
        kind = decision["kind"]
        topic: TopicCandidate | None = decision.get("topic")
        if self.on_initiate is None:
            return decision
        await self.on_initiate(kind, topic, decision["urgency"])
        state = self._state()
        state.setdefault("kind_last_fired", {})[kind] = self.clock.now()
        state["last_kind"] = kind
        state["consecutive_no_response"] = int(state.get("consecutive_no_response", 0)) + 1
        self._bump_daily(state, kind)
        return decision
