"""Idle-time memory consolidation.

While the user is away (no new user message for `idle_minutes`), a
background service consolidates long-term memory — the same idea as
Luna's reflect daemon and Shikigami's daily memory job, but continuous:

1. Episode summaries: new conversations since the last summary are
   summarized into day_summaries.
2. Fact tidy-up: near-duplicate facts are merged (pure lexical, no LLM);
   an LLM review may mark contradictions/outdated facts and replace or
   retire them (validated ops only, ids restricted to the reviewed set).
3. Lessons: repeated corrections or mistakes in the conversation become
   small lesson entries the companion can consult later.

Every run is recorded in the memory changelog so it can be inspected and
rolled back. If the user starts talking again mid-run, the run aborts
before each phase.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone

from ..clock import Clock
from ..providers.base import ChatProvider, ProviderError
from ..storage import Database
from ..util import jaccard, salient_words, new_id
from .store import FactStore

TIDY_PROMPT = """You are curating an AI companion's long-term memory. Below are its active
facts about {subject}. Identify problems:
- "merge": two facts say the same thing (return dup_id and target_id).
- "supersede": one fact is outdated or contradicted by a newer understanding
  (return target_id and new_content that replaces it).
- "archive": a fact is obsolete trivia with no future value (return target_id and reason).

Only use ids from the list. Change at most 5 facts; when in doubt, do nothing.
Reply with ONLY a JSON array like:
[{"action":"merge","dup_id":"f_..","target_id":"f_..","reason":"..."},
 {"action":"supersede","target_id":"f_..","new_content":"...","reason":"..."},
 {"action":"archive","target_id":"f_..","reason":"..."}]"""

SUMMARY_PROMPT = """Summarize this conversation excerpt for the companion's private diary in
2-4 sentences, first person ("we talked about..."). Note anything that seems important
to remember later. Reply with the summary only."""

CORRECTION_RE = re.compile(
    r"\b(no,?\s+(that'?s|it'?s|not)|actually,?|i told you|don'?t do|that'?s wrong|that is wrong|as i said)\b", re.I
)


class Consolidator:
    def __init__(self, db: Database, store: FactStore, provider: ChatProvider | None,
                 clock: Clock, config: dict | None = None, compute_guard=None) -> None:
        self.db = db
        self.store = store
        self.provider = provider
        self.clock = clock
        cfg = config or {}
        self.idle_minutes = float(cfg.get("consolidation_idle_minutes", 15))
        self.duplicate_threshold = float(cfg.get("tidy_duplicate_threshold", 0.75))
        # Resource-awareness: skip whole runs (including the lexical phase)
        # while compute is needed elsewhere; the 60s loop simply retries.
        self.compute_guard = compute_guard
        self.last_defer_reason: str | None = None
        self._last_activity = self.clock.now()
        self._task: asyncio.Task | None = None
        self._running = False
        self.last_run_result: dict | None = None

    # -- activity tracking -----------------------------------------------
    def touch(self) -> None:
        """Call on every incoming user message."""
        self._last_activity = self.clock.now()

    def idle_seconds(self) -> float:
        return self.clock.now() - self._last_activity

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._running = True
            self._task = asyncio.ensure_future(self.run_forever())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def run_forever(self) -> None:
        while self._running:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # consolidation must never crash the app
            await asyncio.sleep(60)

    async def tick(self, force: bool = False) -> dict | None:
        if not force and self.idle_seconds() < self.idle_minutes * 60:
            return None
        if self.compute_guard is not None and not force:
            ok, reason = await self.compute_guard.background_allowed(self.provider)
            if not ok:
                self.last_defer_reason = reason
                self.last_run_result = {"ts": self.clock.now(), "deferred": reason}
                return None
        self.last_defer_reason = None
        summary = {"ts": self.clock.now(), "summary": None, "merged": 0,
                   "superseded": 0, "archived": 0, "lessons": 0, "skipped": []}

        # Phase 1: episode/day summary.
        if await self._still_idle():
            done = await self._summarize_episode()
            if done is None:
                summary["skipped"].append("summary:no-provider-or-error")
            else:
                summary["summary"] = done

        # Phase 2: lexical duplicate merge (no model needed).
        summary["merged"] = self._merge_lexical_duplicates()

        # Phase 3: LLM fact review (contradictions/outdated facts).
        if self.provider is not None and await self._still_idle():
            review = await self._review_facts()
            summary["superseded"] = review.get("superseded", 0)
            summary["archived"] = review.get("archived", 0)
            summary["merged"] += review.get("merged", 0)
        elif self.provider is None:
            summary["skipped"].append("review:no-provider")

        # Phase 4: lessons from corrections.
        summary["lessons"] = await self._collect_lessons()

        if any([summary["summary"], summary["merged"], summary["superseded"],
                summary["archived"], summary["lessons"]]):
            self._record_changelog(summary)
        self.last_run_result = summary
        return summary

    async def _still_idle(self) -> bool:
        return self.idle_seconds() >= self.idle_minutes * 60

    # -- phase 1: summaries -------------------------------------------------
    def _last_summary_ts(self) -> float:
        row = self.db.query_one("SELECT MAX(generated_at) AS m FROM day_summaries")
        return (row["m"] or 0.0) if row else 0.0

    def _pending_messages(self, since_ts: float, limit: int = 60) -> list:
        return self.db.query(
            "SELECT id, role, content, ts FROM messages WHERE id > 0 AND ts > ? ORDER BY id ASC LIMIT ?",
            (since_ts, limit),
        )

    async def _summarize_episode(self) -> str | None:
        rows = self._pending_messages(self._last_summary_ts())
        rows = [r for r in rows if r["role"] in ("user", "assistant")]
        if len(rows) < 4:
            return None
        convo = "\n".join(f"{'User' if r['role'] == 'user' else 'Companion'}: {r['content'][:500]}" for r in rows)
        text = None
        if self.provider is not None:
            try:
                result = await self.provider.chat(
                    [{"role": "user", "content": SUMMARY_PROMPT + "\n\n" + convo}],
                    temperature=0.4, max_tokens=300,
                )
                text = result.text.strip() or None
            except ProviderError:
                text = None
        if text is None:
            # Honest heuristic fallback: no invented facts, just the range + topics.
            first = rows[0]["content"][:120]
            text = f"(heuristic summary) Conversation covering {len(rows)} messages, starting with: \"{first}\""
        now = self.clock.now()
        date = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
        self.db.execute(
            "INSERT INTO day_summaries (date, summary, generated_at, message_count) VALUES (?,?,?,?) "
            "ON CONFLICT(date) DO UPDATE SET summary=excluded.summary, generated_at=excluded.generated_at, "
            "message_count=excluded.message_count",
            (date, text, now, len(rows)),
        )
        return text

    # -- phase 2: lexical dedup ---------------------------------------------
    def _merge_lexical_duplicates(self) -> int:
        facts = self.store.active_facts()
        merged = 0
        dropped: set[str] = set()
        for i, a in enumerate(facts):
            if a.id in dropped:
                continue
            for b in facts[i + 1 :]:
                if b.id in dropped:
                    continue
                wa, wb = salient_words(a.content), salient_words(b.content)
                if wa and wb and jaccard(wa, wb) >= self.duplicate_threshold:
                    self.store.merge_fact(b.id, a.id)
                    dropped.add(b.id)
                    merged += 1
        return merged

    # -- phase 3: LLM review --------------------------------------------------
    async def _review_facts(self) -> dict:
        facts = self.store.active_facts()
        if len(facts) < 4:
            return {}
        listing = "\n".join(f"- {f.id}: {f.content}" for f in facts[:60])
        # .replace instead of .format: the prompt contains literal JSON braces.
        prompt = TIDY_PROMPT.replace("{subject}", "the owner") + "\n\nFacts:\n" + listing
        try:
            result = await self.provider.chat(
                [{"role": "user", "content": prompt}], temperature=0.2, max_tokens=800
            )
        except ProviderError:
            return {}
        raw = re.search(r"\[.*\]", result.text, re.S)
        if not raw:
            return {}
        try:
            actions = json.loads(raw.group(0))
        except ValueError:
            return {}
        valid_ids = {f.id for f in facts}
        stats = {"merged": 0, "superseded": 0, "archived": 0}
        for action in actions[:5]:
            if not isinstance(action, dict):
                continue
            act = action.get("action")
            if act == "merge":
                dup, target = action.get("dup_id"), action.get("target_id")
                if dup in valid_ids and target in valid_ids and dup != target:
                    self.store.merge_fact(dup, target)
                    stats["merged"] += 1
            elif act == "supersede":
                target = action.get("target_id")
                new_content = str(action.get("new_content", "")).strip()
                if target in valid_ids and new_content and len(new_content) <= 220:
                    self.store.supersede_fact(target, new_content, source="consolidation")
                    stats["superseded"] += 1
            elif act == "archive":
                target = action.get("target_id")
                if target in valid_ids:
                    try:
                        self.store.archive_fact(target, "low_value")
                        stats["archived"] += 1
                    except ValueError:
                        pass
        return stats

    # -- phase 4: lessons -------------------------------------------------------
    async def _collect_lessons(self) -> int:
        since = float(self.db.get_meta("lessons.scan_cursor") or 0)
        rows = self.db.query(
            "SELECT id, role, content FROM messages WHERE id > ? AND role='user' ORDER BY id ASC LIMIT 80",
            (since,),
        )
        if not rows:
            return 0
        self.db.set_meta("lessons.scan_cursor", str(rows[-1]["id"]))
        corrections = [r["content"] for r in rows if CORRECTION_RE.search(r["content"])]
        if not corrections:
            return 0
        added = 0
        existing = {r["text"] for r in self.db.query("SELECT text FROM lessons")}
        for text in corrections[-3:]:
            lesson = f"When the user says something like \"{text[:100]}\", they corrected the companion; follow their correction first."
            if lesson in existing:
                continue
            self.db.execute(
                "INSERT INTO lessons (id, text, created_at, source) VALUES (?,?,?,?)",
                (new_id("l"), lesson, self.clock.now(), "consolidation"),
            )
            existing.add(lesson)
            added += 1
        return added

    # -- changelog -----------------------------------------------------------
    def _record_changelog(self, summary: dict) -> None:
        log = self.db.get_meta_json("memory.changelog", [])
        log.insert(0, summary)
        self.db.set_meta_json("memory.changelog", log[:20])
