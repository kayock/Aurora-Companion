"""The one conversation manager.

Every assistant-visible turn — user chat, slash commands, reminder
deliveries, proactive initiations — flows through this single class, so
there is exactly one conversation history, one prompt builder, one tool
loop, and one event stream. Reminder scheduling and proactive initiation
are *services* that request turns; they never talk to the model on their
own. This is the structural answer to the requirement that proactive
messages must not come from competing subsystems.

Turn flow:
1. persist user message, bump counters, touch activity trackers
2. slash commands handled locally (no model needed)
3. build system prompt (persona/mood/memory/time/reminders/people)
4. provider call with optional tool loop (skipped if provider lacks tools)
5. persist assistant message, broadcast SSE events
6. fire-and-forget post-turn: mood appraisal, affinity, extraction cadence,
   evolution cadence, rolling-summary maintenance

The manager never invents content: if the model is missing or errors,
the turn fails loudly (no fake replies stored).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time as _time
from typing import Any

# User-turn provenance for development / live verification harnesses.
# Messages tagged this way stay in the DB for audit but are excluded from
# model context, rolling summaries, and memory extraction.
VERIFY_TURN_SOURCES = frozenset({"verify", "dev_test"})

from ..clock import Clock
from ..config import Config
from ..memory.extraction import MemoryExtractor
from ..memory.retrieval import MemoryRetriever
from ..memory.store import FactStore
from ..people import PeopleRegistry
from ..personality.evolution import PersonaEvolution
from ..personality.mood import MoodEngine
from ..personality.persona import Persona
from ..providers.base import ChatProvider, ProviderError
from ..providers import parse_tool_arguments
from ..reminders.scheduler import ReminderScheduler
from ..reminders.store import Reminder, ReminderStore
from ..storage import Database
from ..timeparse import describe_repeat, friendly, parse_repeat, parse_when
from .focus import (
    close_loop_reply,
    focus_instruction,
    is_confirm_ask,
    mentions_reminder_topic,
    should_close_schedule_loop,
    topic_change_away_from_schedule,
)
from .prompt import PromptBuilder
from .tools import ToolRegistry

# "remember this …" / "don't forget …" / "keep in mind …" — anchored so
# questions like "what do you remember about X?" are NOT treated as commands.
REMEMBER_RE = re.compile(
    r"^(?:ok,?\s*|hey,?\s*|please\s+)*(remember|don'?t forget|keep in mind)\b(?:\s+(?:that|this))?[,: ]+(.{3,200})",
    re.I)

# Natural-language reminders and timers ("Remind me to check the servers in
# 15 minutes.", "Set a timer for 5 minutes."). Handled deterministically,
# model-free, so the request always executes — even when the model is down
# or answers with tool-call JSON instead of calling its tools.
REMIND_NL_RE = re.compile(r"^remind me (?:to |about |that |)?(.{3,200})$", re.I)
TIMER_NL_RE = re.compile(r"^(?:set|start)(?: a| an| the)? timer (?:for |to |on |about )?(.{1,200})$", re.I)
# label/time split: the tail is either a when-keyword phrase ("in 15 minutes",
# "tomorrow at 9am") or a bare duration ("…, 15 minutes", "… for 90 seconds").
# The head may be short ("hi" → rejected as trivial by the store, never stored).
_WHEN_KEYWORD_TAIL = r"(?:in|after|at|every|on|next|tomorrow|each)\b.+"
_WHEN_BARE_TAIL = (
    r"\d+\s*(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|months?)"
    r"(?:\s*(?:,\s*|\band\s+)?\d+\s*(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|months?))*"
    r"|a\s+few\s+(?:seconds?|minutes?|mins?|hours?|days?)"
    r"|a\s+couple\s+of\s+(?:minutes?|mins?|hours?|days?)")
WHEN_TAIL_RE = re.compile(
    rf"^(.{{1,180}}?)[\s,;]+((?:{_WHEN_KEYWORD_TAIL})|(?:{_WHEN_BARE_TAIL}))$",
    re.I)
# Timer follow-up answers: "Repeat", "Five more minutes", "Dismiss", ...
TIMER_DECISION_RE = re.compile(
    r"^(?:ok,?\s*|well,?\s*)?(?:please\s+)?"
    r"(repeat(?: it)?|again|restart(?: it)?|go again|yes(?: please)?|sure|"
    r"(five|ten|fifteen|twenty|thirty|\d+)\s*more\s*minutes?|snooze(?: (?:it|for)?)?(?:\s+(\d+)\s*minutes?)?|"
    r"(\d+)\s*minutes?|dismiss(?: it)?|stop(?: it)?|cancel(?: it)?|done|no thanks|never mind|thanks)[.!]?$",
    re.I)
WORD_MINUTES = {"five": 5, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30}


class ConversationManager:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        config: Config,
        persona: Persona,
        mood: MoodEngine,
        retriever: MemoryRetriever,
        store: FactStore,
        reminder_store: ReminderStore,
        people: PeopleRegistry,
        chat_provider: ChatProvider | None,
        background_provider: ChatProvider | None,
        tools: ToolRegistry,
        evolution: PersonaEvolution | None = None,
        compute_guard=None,
    ) -> None:
        self.db = db
        self.clock = clock
        self.config = config
        self.persona = persona
        self.mood = mood
        self.retriever = retriever
        self.store = store
        self.reminders = reminder_store
        self.people = people
        self.chat_provider = chat_provider
        self.background_provider = background_provider or chat_provider
        self.tools = tools
        self.prompt_builder = PromptBuilder(
            db, persona, mood, retriever, reminder_store,
            user_name=config.get("user_name", "Alex"), people_registry=people,
        )
        self.extractor = MemoryExtractor(db, store, self.background_provider, clock)
        # Exactly one evolution engine lives in the app; the manager borrows it.
        self.evolution = evolution or PersonaEvolution(
            db, persona, self.background_provider, clock,
            affinity_label=mood.tier_label(),
            config=config.get("personality.evolution", {}),
        )
        self._lock = asyncio.Lock()
        self._subscribers: list[asyncio.Queue] = []
        self._proactive_cb = None
        self._voice = None  # set by the app when voice is enabled
        self._streaming = False
        self.last_timing: dict[str, Any] = {}
        # Timer interaction state: pending "Your timer is up — repeat it?"
        # decisions keyed by reminder id. Persisted in the store so a pending
        # question survives a restart; the prompt is asked exactly once per
        # fire (never repeated on a loop).
        self.pending_timers: dict[str, dict] = dict(
            self.db.get_meta_json("timers.pending", {}))
        # Optional software-state provider (wired by the app; keeps this
        # module decoupled from the updater). Returns a short prompt block
        # describing version / update availability / recent upgrade history.
        self.software_state_fn: Any = None
        # Resource-awareness: when a ComputeGuard is wired, nonessential
        # background AI (mood appraisal, extraction, evolution, summary)
        # is deferred while the model is busy/unloaded/unreachable or the
        # host's traffic light reports compute needed elsewhere.
        self.compute_guard = compute_guard

    # ------------------------------------------------------------------ events
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    async def _emit(self, event: dict) -> None:
        event = dict(event, ts=self.clock.now())
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    # ------------------------------------------------------------------ history
    @staticmethod
    def _turn_source_from_env() -> str:
        """Optional provenance for verification harnesses (AURORA_TURN_SOURCE)."""
        raw = (os.environ.get("AURORA_TURN_SOURCE") or "").strip().lower()
        if raw in VERIFY_TURN_SOURCES:
            return raw
        return "user"

    def history(self, limit: int = 50, before_id: int | None = None,
                include_archived: bool = False) -> list[dict]:
        clauses: list[str] = []
        params: list[Any] = []
        if not include_archived:
            clauses.append("COALESCE(archived, 0) = 0")
        if before_id is not None:
            clauses.append("id < ?")
            params.append(before_id)
        sql = "SELECT * FROM messages"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self.db.query(sql, tuple(params))
        return [self._msg_dict(r) for r in reversed(rows)]

    def archive_messages(self, ids: list[int], reason: str) -> int:
        """Soft-hide confirmed records from active context (recoverable)."""
        if not ids:
            return 0
        now = self.clock.now()
        reason = (reason or "archived").strip()[:200]
        n = 0
        for mid in ids:
            cur = self.db.execute(
                "UPDATE messages SET archived=1, archive_reason=?, archived_at=? "
                "WHERE id=? AND COALESCE(archived,0)=0",
                (reason, now, int(mid)),
            )
            n += cur.rowcount
        return n

    def _msg_dict(self, row) -> dict:
        keys = row.keys() if hasattr(row, "keys") else []
        archived = int(row["archived"]) if "archived" in keys else 0
        return {
            "id": row["id"], "role": row["role"], "content": row["content"],
            "ts": row["ts"], "person": row["person"], "sender": row["sender"],
            "source": row["source"], "meta": json.loads(row["meta"] or "{}"),
            "archived": archived,
            "archive_reason": row["archive_reason"] if "archive_reason" in keys else None,
        }

    def _context_eligible(self, row) -> bool:
        keys = row.keys() if hasattr(row, "keys") else []
        if "archived" in keys and int(row["archived"] or 0):
            return False
        source = (row["source"] or "") if "source" in keys else ""
        if source in VERIFY_TURN_SOURCES:
            return False
        meta_raw = row["meta"] if "meta" in keys else "{}"
        try:
            meta = json.loads(meta_raw or "{}")
        except ValueError:
            meta = {}
        if isinstance(meta, dict) and meta.get("verification"):
            return False
        return True

    def _window(self, current_text: str | None = None) -> list[dict]:
        limit = int(self.config.get("conversation.max_history_turns", 24))
        # Over-fetch so soft-archived / verify rows do not shrink the window.
        rows = self.db.query(
            "SELECT * FROM messages WHERE COALESCE(archived,0)=0 "
            "ORDER BY id DESC LIMIT ?",
            (max(limit * 4, limit),),
        )
        drop_reminder_noise = bool(
            current_text and current_text.strip()
            and not mentions_reminder_topic(current_text)
        )
        skipped_reminder_rows = 0
        out: list[dict] = []
        for r in rows:  # newest first
            if r["role"] not in ("user", "assistant"):
                continue
            if not self._context_eligible(r):
                continue
            # Never re-feed leaked tool-call JSON into the model context —
            # that is the main source of topic stickiness after a bad turn.
            if r["role"] == "assistant" and self._looks_like_tool_json(r["content"]):
                continue
            # When the owner has moved on, keep at most one recent reminder
            # delivery in context so it cannot dominate the reply.
            if (drop_reminder_noise and r["role"] == "assistant"
                    and (r["source"] or "") == "reminder"):
                skipped_reminder_rows += 1
                if skipped_reminder_rows > 1:
                    continue
            # Drop prior assistant "please confirm the reminder" loops when
            # the current turn is not about reminders.
            if (drop_reminder_noise and r["role"] == "assistant"
                    and is_confirm_ask(r["content"] or "")):
                continue
            out.append({"role": r["role"], "content": r["content"], "id": r["id"]})
            if len(out) >= limit:
                break
        return list(reversed(out))

    def _summary_block(self) -> str:
        summary = self.db.get_meta("conversation.summary")
        if not summary:
            return ""
        until = self.db.get_meta("conversation.summary_until_id") or 0
        return (f"Summary of earlier parts of this conversation:\n{summary}\n"
                f"(verbatim history continues after message #{until})")

    async def _maybe_roll_summary(self) -> None:
        """Fold old raw messages into a running summary (Luna's
        maybe_summarize / Shikigami's rolling summary, simplified)."""
        trigger = int(self.config.get("conversation.summary_trigger_messages", 60))
        total = self.db.query_one(
            "SELECT COUNT(*) AS n, MIN(id) AS first_id, MAX(id) AS last_id FROM messages "
            "WHERE COALESCE(archived,0)=0"
        )
        if total is None or total["n"] < trigger or total["last_id"] is None:
            return
        keep = int(self.config.get("conversation.max_history_turns", 24))
        summarized_until = int(self.db.get_meta("conversation.summary_until_id") or 0)
        window_start = total["last_id"] - keep
        pending_end = max(window_start, summarized_until)
        rows = self.db.query(
            "SELECT * FROM messages WHERE id <= ? AND id > ? AND COALESCE(archived,0)=0 "
            "ORDER BY id ASC",
            (pending_end, summarized_until),
        )
        rows = [r for r in rows if self._context_eligible(r)]
        if len(rows) < 8 or self.background_provider is None:
            return
        convo = "\n".join(f"{r['role']}: {r['content'][:400]}" for r in rows)
        prev = self.db.get_meta("conversation.summary") or "(none)"
        try:
            result = await self.background_provider.chat(
                [{"role": "user", "content":
                  "Update the running summary of this conversation. Previous summary:\n" + prev +
                  "\n\nNew messages:\n" + convo +
                  "\n\nReply with the updated summary only (max 200 words, factual, no inventions)."}],
                temperature=0.3, max_tokens=400,
            )
        except ProviderError:
            return
        text = result.text.strip()
        if text and "LLM Error" not in text:
            self.db.set_meta("conversation.summary", text)
            self.db.set_meta("conversation.summary_until_id", str(rows[-1]["id"]))

    # ------------------------------------------------------------------ user turns
    async def submit_user_message(self, text: str, person: str = "owner",
                                  sender_name: str | None = None) -> dict:
        async with self._lock:
            return await self._submit_locked(text, person, sender_name)

    async def _submit_locked(self, text: str, person: str, sender_name: str | None) -> dict:
        text = (text or "").strip()
        if not text:
            raise ValueError("empty message")
        now = self.clock.now()
        stats = self.db.get_meta_json("stats", {"user_turns": 0})
        stats["user_turns"] = stats.get("user_turns", 0) + 1
        self.db.set_meta_json("stats", stats)

        sender = sender_name or self.people.display_name(person)
        turn_source = self._turn_source_from_env()
        cur = self.db.execute(
            "INSERT INTO messages (role, content, ts, person, sender, source) VALUES (?,?,?,?,?,?)",
            ("user", text, now, person, sender, turn_source),
        )
        user_msg_id = cur.lastrowid

        # wake the activity-aware services
        if self._proactive_cb is not None:
            self._proactive_cb()
        await self._emit({"type": "message", "message": {
            "id": user_msg_id, "role": "user", "content": text, "ts": now,
            "person": person, "sender": sender, "source": turn_source, "meta": {}}})

        verify = turn_source in VERIFY_TURN_SOURCES

        if text.startswith("/"):
            reply = self._handle_command(text)
            result = await self._finish_system_reply(reply, person, source="chat")
            return self._seal_verify_turn(result, user_msg_id, turn_source)

        # timer follow-up answers ("repeat", "five more minutes", "dismiss")
        # are matched first — they only count while a decision is pending
        decision = await self._try_timer_decision(text)
        if decision is not None:
            result = await self._finish_system_reply(decision, person, source="timer")
            return self._seal_verify_turn(result, user_msg_id, turn_source)

        # Close reminder-confirmation / test-reminder loops without the model
        # when the owner rejects or says it was only a test.
        closed = await self._try_close_schedule_loop(text, person)
        if closed is not None:
            return self._seal_verify_turn(closed, user_msg_id, turn_source)
        # Topic change after a delivery: clear loop state, then let the model
        # answer the new subject (focus_instruction steers the prompt).
        self._maybe_clear_delivery_on_topic_change(text)

        # natural-language reminders/timers — deterministic, model-free, so
        # they execute reliably even with no model or a JSON-happy model
        try:
            nl = await self._try_parse_reminder_request(
                text, person, reminder_source=("verify" if verify else "user"),
            )
        except ValueError:
            nl = None  # trivial text (e.g. "hi") — not a reminder; model's turn
        if nl is not None:
            result = await self._finish_system_reply(nl, person, source="reminder_set")
            return self._seal_verify_turn(result, user_msg_id, turn_source)

        # explicit "remember this" → immediate durable fact (never from verify)
        m = REMEMBER_RE.search(text)
        if m and self.config.get("memory.enabled", True) and not verify:
            content = m.group(2).strip()
            try:
                fact = self.store.add_fact(
                    content, subject=person, source="user_command", weight=1.5,
                    evidence=[{"message_id": user_msg_id, "ts": now, "quote": content[:120]}],
                )
                pre = f"Noted for keeps: \"{fact.content}\".\n\n"
            except Exception as exc:
                pre = f"(I couldn't store that memory: {exc})\n\n"
            text_for_model = text  # still answered naturally below
        elif m and verify:
            pre = "(Verification turn — durable memory not written.)\n\n"
            text_for_model = text
        else:
            pre, text_for_model = "", text

        try:
            reply_text = await self._generate_reply(text_for_model, person, prefix=pre)
        except ProviderError as exc:
            # Honest failure: the reply visibly states the model is unreachable;
            # nothing is invented, and the message stays in the one history.
            # Any "remember this" confirmation survives the outage too.
            reply_text = (f"{pre}(I couldn't reach my language model just now — {exc} — "
                          "reminders and memory are unaffected. Try again shortly.)")
            if self.compute_guard is not None:
                self.compute_guard.note_model_down()
            degraded = {"degraded": "model_unreachable", "error": str(exc)[:200]}
        else:
            degraded = None
        if verify:
            degraded = dict(degraded or {})
            degraded["verification"] = True
        result = await self._finish_assistant_reply(
            reply_text, person, user_msg_id,
            source=("verify" if verify else "chat"), meta=degraded,
        )
        return self._seal_verify_turn(result, user_msg_id, turn_source)

    def _seal_verify_turn(self, result: dict, user_msg_id: int, turn_source: str) -> dict:
        """Soft-archive a verification harness turn so it cannot enter living context."""
        if turn_source not in VERIFY_TURN_SOURCES:
            return result
        ids = [user_msg_id]
        rid = result.get("id") if isinstance(result, dict) else None
        if rid:
            ids.append(int(rid))
        self.archive_messages(ids, f"auto:{turn_source}")
        if isinstance(result, dict):
            result = dict(result)
            result["archived"] = 1
            result["archive_reason"] = f"auto:{turn_source}"
        return result

    def _handle_command(self, text: str) -> str:
        """Slash commands: deterministic, model-free management of reminders
        and memory (both upstreams have these; Aurora keeps the essentials)."""
        parts = text[1:].split(None, 1)
        cmd = parts[0].lower() if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""
        person = "owner"
        if cmd in ("remind", "remindme"):
            m = re.match(r"(.*?)\s+(?:to|that)\s+(.+)$", rest)
            when_words, what = (m.group(1), m.group(2)) if m else (rest, rest)
            due = parse_when(when_words, self.clock.now())
            if due is None:
                return f"Could not understand the time \"{when_words}\". Try 'in 20 minutes' or 'tomorrow at 9am'."
            repeat = parse_repeat(when_words)
            rem = self.reminders.add(what, due, repeat=repeat, source="user")
            if self._proactive_cb is not None:
                self._proactive_cb()  # wake reminder scanner via notify (set in app wiring)
            rep = f" (repeats {describe_repeat(repeat)})" if repeat else ""
            return f"Scheduled reminder {rem.id}: \"{what}\" for {self._friendly(due)}{rep}."
        if cmd == "reminders":
            items = self.reminders.list(active_only=True, person=person)
            if not items:
                return "No active reminders."
            return "\n".join(f"[{r.id}] \"{r.text}\" {self._friendly(r.due_at)}"
                             + (" (recurring)" if r.recurring else "") for r in items)
        if cmd == "remember":
            if not rest:
                return "Usage: /remember <fact>"
            try:
                fact = self.store.add_fact(rest, subject=person, source="user_command", weight=1.5)
                return f"Remembered (fact {fact.id}): {fact.content}"
            except Exception as exc:
                return f"Could not store that: {exc}"
        if cmd == "forget":
            tombs = self.store.forget_text(rest)
            count = 0
            for fact in self.store.active_facts(person):
                import difflib
                ratio = difflib.SequenceMatcher(None, fact.content.lower(), rest.lower()).ratio()
                if ratio > 0.6:
                    self.store.archive_fact(fact.id, "user_request")
                    count += 1
            return f"Forgot. {count} matching fact(s) retired; future matches will be refused."
        if cmd == "facts":
            items = self.store.active_facts(person)
            if not items:
                return "No facts stored yet."
            return "\n".join(f"[{f.id}] {f.content}" for f in items[:40])
        if cmd == "help":
            return ("Commands: /remind <when> to <what> · /reminders · /remember <fact> · "
                    "/forget <text> · /facts · /help. Everything is also in the dashboard.")
        return f"Unknown command /{cmd}. Try /help."

    def _friendly(self, epoch: float) -> str:
        from ..timeparse import friendly as _f
        return _f(epoch, self.clock.now())

    def _recent_dialogue_rows(self, limit: int = 8) -> list:
        return self.db.query(
            "SELECT role, content, source FROM messages "
            "WHERE COALESCE(archived,0)=0 ORDER BY id DESC LIMIT ?",
            (limit,),
        )

    def _clear_schedule_confirm_state(self) -> None:
        self.db.execute(
            "DELETE FROM meta WHERE key IN "
            "('reminder.pending_confirm','reminder.just_delivered')"
        )

    def _note_reminder_delivered(self, reminder_id: str, text: str) -> None:
        self.db.set_meta_json("reminder.just_delivered", {
            "id": reminder_id, "text": text, "ts": self.clock.now(),
        })

    def _schedule_loop_flags(self) -> tuple[bool, bool, bool]:
        recent = self._recent_dialogue_rows(8)
        prior = recent[1:] if recent and recent[0]["role"] == "user" else recent
        last_asst = next((r for r in prior if r["role"] == "assistant"), None)
        assistant_asking = bool(last_asst and is_confirm_ask(last_asst["content"] or ""))
        recent_delivery = any(
            r["role"] == "assistant" and (r["source"] or "") == "reminder"
            for r in prior[:4]
        )
        pending = bool(self.db.get_meta("reminder.pending_confirm")
                       or self.db.get_meta("reminder.just_delivered"))
        return assistant_asking, recent_delivery, pending

    def _dismiss_open_deliveries(self, person: str) -> None:
        for rem in self.reminders.list(active_only=False, person=person)[:12]:
            if rem.status in ("delivered", "waiting_ack") and rem.kind != "timer":
                try:
                    self.reminders.acknowledge(rem.id)
                except Exception:
                    pass

    def _maybe_clear_delivery_on_topic_change(self, text: str) -> None:
        assistant_asking, recent_delivery, _ = self._schedule_loop_flags()
        if topic_change_away_from_schedule(
            text,
            recent_reminder_delivery=recent_delivery,
            assistant_asking_confirm=assistant_asking,
        ):
            self._clear_schedule_confirm_state()
            self._dismiss_open_deliveries("owner")

    async def _try_close_schedule_loop(self, text: str, person: str) -> dict | None:
        """End confirmation/test-reminder fixation without another model pass."""
        assistant_asking, recent_delivery, pending = self._schedule_loop_flags()
        if not should_close_schedule_loop(
            text,
            assistant_asking_confirm=assistant_asking,
            recent_reminder_delivery=recent_delivery,
            pending_confirm=pending,
        ):
            return None
        self._clear_schedule_confirm_state()
        self._dismiss_open_deliveries(person)
        reply = close_loop_reply(text)
        return await self._finish_system_reply(reply, person, source="chat")

    # ------------------------------------------------------------------ generation
    async def _build_messages(self, text: str, person: str,
                              proactive_context: str | None = None,
                              include_user_text: bool = True) -> list[dict]:
        # Memory retrieval must track THIS turn — concatenating recent user
        # turns caused Saturn/reminder bleed into unrelated requests.
        t_ret0 = _time.perf_counter()
        extras: list[str] = []
        summary = self._summary_block()
        if summary:
            extras.append(summary)
        just = self.db.get_meta_json("reminder.just_delivered")
        after_delivery = bool(just) and not mentions_reminder_topic(text or "")
        if text and include_user_text:
            extras.append(focus_instruction(
                text, after_reminder_delivery=after_delivery,
            ))
        system = await self.prompt_builder.build_system(
            now_epoch=self.clock.now(), query=text or "", subject=person,
            extra_blocks=extras or None,
            proactive_context=proactive_context,
        )
        retrieve_ms = (_time.perf_counter() - t_ret0) * 1000.0
        if self.tools.names():
            system += "\n\n" + self.tools.manifest_text()
        state = self._software_state_text()
        if state:
            system += "\n\n" + state
        messages = [{"role": "system", "content": system}]
        summary_until = int(self.db.get_meta("conversation.summary_until_id") or 0)
        window = [m for m in self._window(current_text=text) if m["id"] > summary_until]
        messages += [{"role": m["role"], "content": m["content"]} for m in window]
        # For system-driven turns (reminder delivery) the instruction text is
        # not in the persisted window — send it as the final user message so
        # the model actually sees it.
        if include_user_text and text and (not window or window[-1]["content"] != text):
            messages.append({"role": "user", "content": text})
        # Stash retrieval timing for the outer generator.
        self._last_retrieve_ms = round(retrieve_ms, 1)
        return messages

    def _software_state_text(self) -> str:
        """Operational software awareness (Phase: update awareness). The app
        wires a provider that describes version / update availability / the
        result of the last upgrade, so Aurora can talk about her own state
        naturally. Empty unless there is something worth saying."""
        if self.software_state_fn is None:
            return ""
        try:
            return str(self.software_state_fn() or "")
        except Exception:
            return ""

    async def _generate_reply(self, text: str, person: str, prefix: str = "",
                              proactive_context: str | None = None) -> str:
        if self.chat_provider is None:
            return (prefix + "No chat model is configured. Set one under Settings → Models in the "
                    "dashboard (any OpenAI-compatible endpoint: LM Studio, llama.cpp, "
                    "Ollama). Reminders and memory keep working without it.")
        if self.compute_guard is not None:
            self.compute_guard.note_foreground()  # user turns preempt background AI
        # Freeze the authorizing user utterance for the whole tool loop —
        # later rounds replace `text` with "continue with the tool results".
        user_request = text
        allow_schedule = True  # chat turns may schedule when intent is present
        if proactive_context:
            allow_schedule = False  # proactive initiations never create reminders
        self._last_retrieve_ms = 0.0
        t_build0 = _time.perf_counter()
        messages = await self._build_messages(text, person, proactive_context)
        build_ms = (_time.perf_counter() - t_build0) * 1000.0
        max_rounds = int(self.config.get("conversation.max_tool_rounds", 3))
        gate = self._DeltaGate(self._emit)
        on_delta = gate.as_callback()
        self._streaming = True
        self.last_timing = {
            "prompt_build_ms": round(build_ms, 1),
            "retrieve_ms": getattr(self, "_last_retrieve_ms", 0.0),
            "model_ms": [],
            "tool_ms": [],
        }
        # Owner moved on after a delivery — clear so the next turn is clean.
        if self.db.get_meta("reminder.just_delivered") and not mentions_reminder_topic(text):
            self.db.execute("DELETE FROM meta WHERE key='reminder.just_delivered'")

        def _tool_ctx() -> dict:
            return {
                "person": person,
                "tz_offset_minutes": 0,
                "user_text": user_request,
                "allow_schedule": allow_schedule,
                "on_change": (lambda: None),
            }

        try:
            for round_no in range(max_rounds):
                specs = self.tools.specs() if self.chat_provider.supports_tools() else None
                gate.reset_for_round(hold_possible_tools=bool(specs))
                t_model0 = _time.perf_counter()
                try:
                    result = await self.chat_provider.chat(messages, tools=specs, on_delta=on_delta)
                except ProviderError:
                    if specs:
                        # server choked on tools: rebuild clean prompt and retry once without them
                        messages = await self._build_messages(user_request, person, proactive_context)
                        gate.reset_for_round(hold_possible_tools=False)
                        result = await self.chat_provider.chat(messages, tools=None, on_delta=on_delta)
                        specs = None
                    else:
                        raise
                self.last_timing["model_ms"].append(
                    round((_time.perf_counter() - t_model0) * 1000.0, 1))
                if not result.tool_calls:
                    # Some models answer with the tool call as plain JSON text
                    # instead of using the tool_calls field. Detect that shape
                    # and EXECUTE it — a reminder request must actually run,
                    # never surface as JSON in the conversation.
                    known = set(self.tools.names())
                    embedded = (self._extract_embedded_tool_call(result.text, known)
                                if specs or known else None)
                    if embedded is None and self._looks_like_tool_json(result.text):
                        # Malformed tool JSON: never show it as a chat reply.
                        await gate.discard()
                        return (prefix + "I tried to use an internal tool just then but "
                                "couldn't complete it cleanly. Please say that another way.")
                    if embedded is not None:
                        await gate.discard()
                        name, raw_args = embedded
                        t_tool0 = _time.perf_counter()
                        args = parse_tool_arguments(raw_args)
                        output = await self.tools.call(name, args, _tool_ctx())
                        self.last_timing["tool_ms"].append(
                            round((_time.perf_counter() - t_tool0) * 1000.0, 1))
                        await self._emit({"type": "tool", "name": name, "arguments": args,
                                          "output": output, "recovered_from_text": True})
                        # OpenAI-shaped follow-up — do not re-inject the leaked JSON.
                        messages.append({
                            "role": "assistant", "content": "",
                            "tool_calls": [{
                                "id": f"call_{round_no}_0", "type": "function",
                                "function": {"name": name, "arguments": raw_args},
                            }],
                        })
                        messages.append({"role": "tool",
                                         "tool_call_id": f"call_{round_no}_0",
                                         "content": str(output)})
                        continue
                    await gate.flush_if_held()
                    cleaned = self._clean(result.text)
                    if self._looks_like_tool_json(cleaned):
                        return (prefix + "I tried to use an internal tool just then but "
                                "couldn't complete it cleanly. Please say that another way.")
                    return prefix + cleaned
                await gate.discard()
                oai_calls = []
                for i, c in enumerate(result.tool_calls):
                    args = c.arguments if isinstance(c.arguments, str) else json.dumps(c.arguments)
                    oai_calls.append({
                        "id": f"call_{round_no}_{i}",
                        "type": "function",
                        "function": {"name": c.name, "arguments": args},
                    })
                messages.append({"role": "assistant", "content": result.text or "",
                                 "tool_calls": oai_calls})
                for i, call in enumerate(result.tool_calls):
                    t_tool0 = _time.perf_counter()
                    args = parse_tool_arguments(call.arguments)
                    output = await self.tools.call(call.name, args, _tool_ctx())
                    self.last_timing["tool_ms"].append(
                        round((_time.perf_counter() - t_tool0) * 1000.0, 1))
                    await self._emit({"type": "tool", "name": call.name, "arguments": args,
                                      "output": output})
                    messages.append({"role": "tool",
                                     "tool_call_id": f"call_{round_no}_{i}",
                                     "content": str(output)})
            await gate.flush_if_held()
            cleaned = self._clean(result.text)
            if self._looks_like_tool_json(cleaned):
                return (prefix + "I tried to use an internal tool just then but "
                        "couldn't complete it cleanly. Please say that another way.")
            return prefix + cleaned
        finally:
            self._streaming = False

    class _DeltaGate:
        """Hold stream deltas that look like tool-call JSON so they never
        paint into the chat bubble; flush promptly for ordinary prose."""

        def __init__(self, emit) -> None:
            self._emit = emit
            self._buf: list[str] = []
            self._holding = False
            self._suppressed = False

        def reset_for_round(self, hold_possible_tools: bool) -> None:
            self._buf.clear()
            self._holding = hold_possible_tools
            self._suppressed = False

        def as_callback(self):
            def _cb(piece: str):
                asyncio.ensure_future(self.feed(piece))
            return _cb

        async def feed(self, piece: str) -> None:
            if self._suppressed or not piece:
                return
            if not self._holding:
                await self._emit({"type": "delta", "text": piece})
                return
            self._buf.append(piece)
            joined = "".join(self._buf).lstrip()
            if not joined:
                return
            # Tool JSON always starts with '{'; ordinary replies do not.
            if joined.startswith("{"):
                return
            self._holding = False
            for p in self._buf:
                await self._emit({"type": "delta", "text": p})
            self._buf.clear()

        async def discard(self) -> None:
            self._suppressed = True
            self._buf.clear()
            await self._emit({"type": "stream_reset"})

        async def flush_if_held(self) -> None:
            if self._suppressed or not self._buf:
                self._holding = False
                return
            self._holding = False
            for p in self._buf:
                await self._emit({"type": "delta", "text": p})
            self._buf.clear()

    @staticmethod
    def _repair_tool_json(text: str) -> str:
        raw = (text or "").strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I).strip()
        # llama3.2 often emits `"parameters{"text":...}` or `"parameters {"text"`
        # (opening brace before the key is closed) / missing colons.
        raw = re.sub(r'"(parameters|arguments)\s*\{\s*"', r'"\1": {"', raw)
        raw = re.sub(r'"(parameters|arguments)"\s*\{\s*"', r'"\1": {"', raw)
        raw = re.sub(r'"(parameters|arguments)"\s*\{', r'"\1": {', raw)
        raw = re.sub(r'"(parameters|arguments)"\s+"', r'"\1": "', raw)
        return raw

    @classmethod
    def _looks_like_tool_json(cls, text: str) -> bool:
        raw = cls._repair_tool_json(text)
        if not raw.startswith("{"):
            return False
        if re.search(r'"name"\s*:\s*"(set_reminder|cancel_reminder|snooze_reminder|'
                     r'list_reminders|recall_facts|remember_fact|forget_fact|'
                     r'update_fact|get_time)"', raw):
            return True
        if '"parameters"' in raw or '"arguments"' in raw:
            if any(k in raw for k in ("set_reminder", "recall_facts", "remember_fact",
                                      "list_reminders", "get_time", "cancel_reminder")):
                return True
        return False

    @classmethod
    def _extract_embedded_tool_call(
        cls, text: str, known_tools: "set[str] | None" = None,
    ) -> "tuple[str, Any] | None":
        """Find a tool call that a model emitted as plain text, e.g.
        '{"name": "set_reminder", "arguments": {...}}' (with or without a
        ```json fence). Returns (name, raw_arguments) when the name matches a
        registered tool, else None. Tolerates common llama3.2 malformations."""
        raw = cls._repair_tool_json(text)
        if not raw.startswith("{"):
            return None
        data = None
        try:
            data = json.loads(raw)
        except Exception:
            # Extract name + object args even when the blob is not strict JSON.
            m = re.search(
                r'"name"\s*:\s*"([a-zA-Z0-9_]+)"\s*,\s*"(?:arguments|parameters)"\s*:\s*(\{.*\})\s*\}?\s*$',
                raw, flags=re.S)
            if not m:
                return None
            name, args_blob = m.group(1), m.group(2)
            try:
                json.loads(args_blob)
            except Exception:
                return None
            if known_tools is not None and name not in known_tools:
                return None
            return name, args_blob
        if not isinstance(data, dict):
            return None
        name = data.get("name") or data.get("tool") or data.get("tool_name")
        if not isinstance(name, str) or not name.strip():
            return None
        name = name.strip()
        if known_tools is not None and name not in known_tools:
            return None
        raw_args = data.get("arguments", data.get("parameters", {}))
        if not isinstance(raw_args, str):  # parse_tool_arguments expects JSON text
            raw_args = json.dumps(raw_args)
        return name, raw_args

    @staticmethod
    def _clean(text: str) -> str:
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I)
        text = re.sub(r"^<think>.*", "", text, flags=re.S | re.I)
        return text.strip()

    # ------------------------------------------------------------------ finishing turns
    async def _finish_assistant_reply(self, text: str, person: str, user_msg_id: int,
                                      source: str, meta: dict | None = None) -> dict:
        now = self.clock.now()
        meta = dict(meta or {})
        cur = self.db.execute(
            "INSERT INTO messages (role, content, ts, person, source, meta) VALUES (?,?,?,?,?,?)",
            ("assistant", text, now, person, source, json.dumps(meta)),
        )
        msg = {"id": cur.lastrowid, "role": "assistant", "content": text, "ts": now,
               "person": person, "sender": "", "source": source, "meta": meta}
        await self._emit({"type": "message", "message": msg})
        asyncio.ensure_future(self._post_turn(text, person, user_msg_id))
        if self._voice is not None:
            # awaited so the utterance actually happens with the turn (and is
            # observable); speak failures never break the visible reply
            try:
                await self._voice.speak(text)
            except Exception:
                pass
        return msg

    async def _finish_system_reply(self, text: str, person: str, source: str,
                                   meta: dict | None = None) -> dict:
        now = self.clock.now()
        meta = dict(meta or {})
        cur = self.db.execute(
            "INSERT INTO messages (role, content, ts, person, source, meta) VALUES (?,?,?,?,?,?)",
            ("assistant", text, now, person, source, json.dumps(meta)),
        )
        msg = {"id": cur.lastrowid, "role": "assistant", "content": text, "ts": now,
               "person": person, "sender": "", "source": source, "meta": meta}
        await self._emit({"type": "message", "message": msg})
        return msg

    # ------------------------------------------------------------------ post-turn hooks
    async def _background_ai_gate(self) -> tuple[bool, str]:
        """(allowed, reason) for the post-turn AI hooks. These belong to the
        user's own turn, so the foreground grace window does not apply — but
        they still yield when the model is unavailable/unloaded or the host's
        traffic light reports compute needed elsewhere. With no guard wired
        (tests, minimal setups) everything proceeds as before."""
        if self.compute_guard is None:
            return True, ""
        return await self.compute_guard.background_allowed(
            self.background_provider, check_foreground=False)

    async def _post_turn(self, assistant_text: str, person: str, user_msg_id: int) -> None:
        try:
            user_text = ""
            row = self.db.query_one("SELECT content FROM messages WHERE id=?", (user_msg_id,))
            if row:
                user_text = row["content"]
            # The affinity label is bookkeeping, not AI — keep it fresh even
            # when the AI hooks are deferred.
            self.evolution.affinity_label = self.mood.tier_label() if self.mood else ""
            allowed, reason = await self._background_ai_gate()
            if not allowed:
                # Deferred, not dropped: these hooks are cadence-based and
                # simply run on a later turn. Record why for the dashboard.
                self.db.set_meta_json("resources.last_deferral",
                                      {"ts": self.clock.now(), "where": "post_turn",
                                       "reason": reason})
                return
            if self.mood is not None:
                await self.mood.update_after_turn(user_text, assistant_text)
                await self.mood.maybe_affinity_adjust(user_text, assistant_text)
            if self.config.get("memory.enabled", True):
                stats = self.db.get_meta_json("stats", {})
                n = int(stats.get("user_turns", 0))
                every = int(self.config.get("memory.extraction_every_n_user_turns", 4))
                if every > 0 and n % every == 0:
                    await self.extractor.extract_since_cursor()
                if self.evolution.maybe_evolve(n):
                    facts_text = "\n".join(
                        f"- {f.content}" for f in self.store.active_facts(person)[:30])
                    convo_rows = self.db.query(
                        "SELECT role, content FROM messages ORDER BY id DESC LIMIT 30")
                    convo_text = "\n".join(f"{r['role']}: {r['content'][:200]}"
                                           for r in reversed(convo_rows))
                    await self.evolution.evolve(facts_text, convo_text)
            await self._maybe_roll_summary()
        except Exception:
            pass  # post-turn hooks must never break the visible reply

    # ------------------------------------------------------------------ notifications, timers
    async def _announce(self, text: str) -> None:
        """Speak a notification through the wired voice service (one-shot).
        Awaited by the delivery paths so the utterance actually happens
        before the turn ends (and is observable in tests)."""
        if self._voice is not None:
            try:
                await self._voice.speak(text)
            except Exception:
                pass  # voice is an enhancement — never break the delivery

    def _persist_pending_timers(self) -> None:
        self.db.set_meta_json("timers.pending", self.pending_timers)

    def _register_pending_timer(self, timer: Reminder, duration: int | None) -> None:
        prev = self.pending_timers.get(timer.id)
        # a snoozed timer re-fires with a shifted due_at — keep the ORIGINAL
        # duration so "repeat" restarts the real length, not the snooze span
        keep_duration = prev["duration"] if (prev and prev.get("duration")) else duration
        self.pending_timers[timer.id] = {
            "text": timer.text, "duration": keep_duration,
            "fired_at": self.clock.now(), "person": timer.person,
        }
        self._persist_pending_timers()

    def _clear_pending_timer(self, timer_id: str) -> None:
        if self.pending_timers.pop(timer_id, None) is not None:
            self._persist_pending_timers()

    def _most_recent_pending_timer(self) -> str | None:
        if not self.pending_timers:
            return None
        return max(self.pending_timers, key=lambda k: self.pending_timers[k].get("fired_at", 0))

    def _timer_duration_hint(self, timer: Reminder) -> int | None:
        if timer.duration:
            return int(timer.duration)  # exact intended length, recorded at creation
        span = timer.due_at - timer.created_at
        return int(span) if 1 <= span <= 30 * 86400 else None

    def alerts(self) -> dict:
        """Notification-bell payload: active reminders (upcoming, snoozed,
        waiting for ack) plus timers awaiting a repeat/snooze/dismiss answer."""
        active = self.reminders.list(active_only=True)
        return {
            "active": [r.to_dict() for r in active],
            "pending_timers": dict(self.pending_timers),
            "count": len(active) + len(self.pending_timers),
        }

    async def _try_parse_reminder_request(
        self, text: str, person: str, reminder_source: str = "user",
    ) -> str | None:
        """Deterministic NL reminders/timers ("remind me to X in 15 minutes",
        "set a timer for 5 minutes"). Returns a confirmation reply, or None
        when the text is not a plainly parseable request (the model still
        gets its turn). Never needs the model — it must execute reliably."""
        kind = ""
        m = REMIND_NL_RE.match(text)
        if m is None:
            m = TIMER_NL_RE.match(text)
            kind = "timer"
        if m is None:
            return None
        payload = m.group(1).strip()
        now = self.clock.now()
        label, when_text = payload, payload
        tail = WHEN_TAIL_RE.match(payload)
        if tail is not None:
            label, when_text = tail.group(1).strip(), tail.group(2).strip()
            label = re.sub(r"\s+(?:for|in|at|on|after|every|and)$", "", label, flags=re.I)
        due = parse_when(when_text, now)
        if due is None and kind == "timer" and tail is None:
            # "set a timer for 5 minutes" — a bare duration is a timer length
            due = parse_when(f"in {when_text}", now)
        if due is None or due <= now - 5:
            return None  # not a parseable time → let the model handle it
        repeat = parse_repeat(when_text)
        if kind == "timer" and repeat:
            return None  # "a timer every day" is a recurring reminder
        if kind == "timer" and not tail:
            label = f"Timer ({when_text})"  # bare duration → generic label
        rem = self.reminders.add(label, due, repeat=repeat, person=person,
                                 source=reminder_source or "user", kind=kind,
                                 duration=int(round(due - now)) if kind == "timer" else None)
        rep = f", repeating {describe_repeat(repeat)}" if repeat else ""
        if kind == "timer":
            return f"Timer set — {friendly(rem.due_at, now)}{rep}."
        return f"Got it — I'll remind you to {label.rstrip('.')} {friendly(rem.due_at, now)}{rep}."

    async def _try_timer_decision(self, text: str) -> str | None:
        """Model-free follow-up answers to a fired timer ("Repeat", "Five
        more minutes", "Dismiss"). Targets the most recently fired pending
        timer; several pending timers keep their own buttons in the UI."""
        if not self.pending_timers:
            return None
        m = TIMER_DECISION_RE.match(text.strip())
        if m is None:
            return None
        token = (m.group(1) or "").lower().strip()
        target = self._most_recent_pending_timer()
        if target is None:
            return None
        info = self.pending_timers.get(target, {})
        person = info.get("person", "owner")

        if token.startswith(("dismiss", "stop", "cancel", "done", "no thanks", "never mind")):
            result = await self.timer_dismiss(target, person)
            return "Dismissed." if result.get("ok") else None
        if (token in ("again", "go again", "yes", "yes please", "sure")
                or token.startswith("repeat") or token.startswith("restart")):
            result = await self.timer_repeat(target, person)
            return result["message"] if result.get("ok") else None
        mm = re.search(r"(\d+|five|ten|fifteen|twenty|thirty)\s*(?:more\s*)?minutes?", token)
        if mm is not None:
            word = mm.group(1)
            minutes = WORD_MINUTES.get(word) if not word.isdigit() else int(word)
            result = await self.timer_snooze(target, (minutes or 5) * 60, person)
            return result["message"] if result.get("ok") else None
        if token.startswith("snooze"):
            result = await self.timer_snooze(target, 300, person)
            return result["message"] if result.get("ok") else None
        return None  # matched something ambiguous → let the model answer

    async def timer_repeat(self, timer_id: str, person: str | None = None) -> dict:
        """Restart a completed timer with its original duration. Never runs
        automatically — only on the user's word or button."""
        info = self.pending_timers.get(timer_id)
        if info is None:
            return {"ok": False, "error": "no pending timer decision for that id"}
        duration = int(info.get("duration") or 300)
        now = self.clock.now()
        person = person or info.get("person", "owner")
        rem = self.reminders.add(info.get("text") or "Timer", now + duration,
                                 person=person, source="user", kind="timer",
                                 duration=duration)
        self._clear_pending_timer(timer_id)
        await self._emit({"type": "timers_changed"})
        message = f"Timer restarted — {friendly(rem.due_at, now)}."
        await self._finish_system_reply(message, person, source="timer")
        return {"ok": True, "reminder": rem.to_dict(), "message": message}

    async def timer_snooze(self, timer_id: str, seconds: int, person: str | None = None) -> dict:
        """Bring a fired timer back after `seconds` (original duration kept
        for a later repeat)."""
        info = self.pending_timers.get(timer_id)
        if info is None:
            return {"ok": False, "error": "no pending timer decision for that id"}
        seconds = max(30, min(int(seconds), 86400))
        rem = self.reminders.bring_back(timer_id, seconds)
        if rem is None:
            return {"ok": False, "error": "reminder not found"}
        info["fired_at"] = self.clock.now()  # stays most-recent for voice answers
        self._persist_pending_timers()
        await self._emit({"type": "timers_changed"})
        person = person or info.get("person", "owner")
        message = (f"Snoozed — I'll bring it back "
                   f"{friendly(rem.snoozed_until, self.clock.now())}.")
        await self._finish_system_reply(message, person, source="timer")
        return {"ok": True, "reminder": rem.to_dict(), "message": message}

    async def timer_dismiss(self, timer_id: str, person: str | None = None) -> dict:
        info = self.pending_timers.get(timer_id)
        if info is None:
            return {"ok": False, "error": "no pending timer decision for that id"}
        self._clear_pending_timer(timer_id)
        done = self.reminders.acknowledge(timer_id) or self.reminders.cancel(timer_id)
        if done is None:
            return {"ok": False, "error": "reminder not found"}
        await self._emit({"type": "timers_changed"})
        return {"ok": True, "reminder": done.to_dict()}

    # ------------------------------------------------------------------ reminder delivery
    async def deliver_reminder(self, reminder: Reminder, note: str = "") -> None:
        """ReminderScheduler callback. Delivered even with no model (plain
        text); phrased naturally by the persona when the model is up.
        Timers get the dedicated repeat/snooze/dismiss flow instead."""
        if reminder.kind == "timer":
            await self._deliver_timer(reminder)
            return
        await self._emit({"type": "reminder_due", "id": reminder.id, "text": reminder.text})
        if self.chat_provider is not None and not self._streaming:
            try:
                if self.compute_guard is not None:
                    self.compute_guard.note_foreground()  # delivery is user-facing
                messages = await self._build_messages(
                    f"ANNOUNCE THIS REMINDER NOW. A reminder was scheduled earlier and it has just "
                    f"come due: \"{reminder.text}\".{(' ' + note) if note else ''} "
                    "Open with the reminder itself in one or two natural sentences, in your own voice. "
                    "Do not say you lack details about it, do not discuss other topics, "
                    "and do not claim it was never scheduled — it was.",
                    reminder.person,
                )
                result = await self.chat_provider.chat(messages, tools=None)
                text = self._clean(result.text)
                if text:
                    await self._finish_assistant_reply(text, reminder.person, 0, source="reminder")
                    self._log_proactive("reminder_delivery", text, reminder.id)
                    self._note_reminder_delivered(reminder.id, reminder.text)
                    return
            except ProviderError:
                pass
        text = f"⏰ Reminder: {reminder.text}" + (f" ({note})" if note else "")
        await self._finish_system_reply(text, reminder.person, source="reminder")
        await self._announce(text)  # voice announcement, when enabled (spoken once)
        self._log_proactive("reminder_delivery", text, reminder.id)
        self._note_reminder_delivered(reminder.id, reminder.text)

    async def _deliver_timer(self, timer: Reminder) -> None:
        """Timer completion: ask once about repeating, register the pending
        decision, and keep a visible notification. Never re-prompts on its
        own and never restarts the timer without permission."""
        duration = self._timer_duration_hint(timer)
        self._register_pending_timer(timer, duration)
        label = f" — {timer.text}" if timer.text and not timer.text.lower().startswith("timer") else ""
        text = f"⏱️ Your timer is up{label}. Would you like me to repeat it?"
        await self._emit({"type": "timer_due", "id": timer.id, "text": timer.text,
                          "duration": duration})
        await self._finish_system_reply(
            text, timer.person, source="timer",
            meta={"timer_decision": {"id": timer.id, "duration": duration, "text": timer.text}})
        await self._announce(text)  # spoken exactly once per fire
        self._log_proactive("timer_delivery", text, timer.id)

    # ------------------------------------------------------------------ proactive initiation
    def set_proactive_callback(self, touch_fn) -> None:
        self._proactive_cb = touch_fn

    def log_proactive(self, kind: str, text: str, topic_ref: str = "", urgency: float = 0.0) -> None:
        self._log_proactive(kind, text, topic_ref, urgency)

    def _log_proactive(self, kind: str, text: str, topic_ref: str = "", urgency: float = 0.0) -> None:
        self.db.execute(
            "INSERT INTO proactive_log (ts, kind, text, topic_ref, urgency, trigger) VALUES (?,?,?,?,?,?)",
            (self.clock.now(), kind, text[:400], topic_ref or "", urgency, kind),
        )

    async def send_proactive(self, kind: str, topic, urgency: float) -> dict:
        """ProactiveScheduler callback: the only path for companion-initiated
        conversation. Topic refs are journaled for dedup."""
        frame = (
            "You are initiating a conversation yourself (no user message this turn). "
            f"Kind of initiation: {kind}. One short, natural opening message — "
            "at most one question. Do not repeat anything you have already said recently. "
            "Do not schedule reminders or timers on this turn; speak only."
        )
        if kind == "work_break":
            frame += (
                " This is a gentle spoken check-in about taking a break — not a "
                "scheduled reminder. Vary the wording; do not nag."
            )
        if kind == "morning_greeting":
            frame += " Keep it a brief greeting; do not invent tasks or reminders."
        if topic is not None:
            frame += f"\nTopic to bring up: {topic.summary}"
        context = f"Proactive initiation\n{frame}"
        text = ""
        if self.chat_provider is not None and not self._streaming:
            try:
                messages = await self._build_messages("(initiate)", "owner", proactive_context=context)
                result = await self.chat_provider.chat(messages, tools=None)
                text = self._clean(result.text)
            except ProviderError:
                text = ""
        if not text:
            # Honest no-model fallback: deliver the topic material itself.
            text = topic.summary if topic is not None else \
                "Just checking in — how are you doing?"
        msg = await self._finish_assistant_reply(text, "owner", 0, source="proactive")
        self._log_proactive(kind, text, getattr(topic, "ref", ""), urgency)
        return msg
