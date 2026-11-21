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
import re
import time as _time
from typing import Any

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
from ..timeparse import parse_repeat, parse_when, describe_repeat
from .prompt import PromptBuilder
from .tools import ToolRegistry

# "remember this …" / "don't forget …" / "keep in mind …" — anchored so
# questions like "what do you remember about X?" are NOT treated as commands.
REMEMBER_RE = re.compile(
    r"^(?:ok,?\s*|hey,?\s*|please\s+)*(remember|don'?t forget|keep in mind)\b(?:\s+(?:that|this))?[,: ]+(.{3,200})",
    re.I)


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
    def history(self, limit: int = 50, before_id: int | None = None) -> list[dict]:
        sql = "SELECT * FROM messages"
        params: list[Any] = []
        if before_id is not None:
            sql += " WHERE id < ?"
            params.append(before_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self.db.query(sql, tuple(params))
        return [self._msg_dict(r) for r in reversed(rows)]

    def _msg_dict(self, row) -> dict:
        return {
            "id": row["id"], "role": row["role"], "content": row["content"],
            "ts": row["ts"], "person": row["person"], "sender": row["sender"],
            "source": row["source"], "meta": json.loads(row["meta"] or "{}"),
        }

    def _window(self) -> list[dict]:
        limit = int(self.config.get("conversation.max_history_turns", 24))
        rows = self.db.query(
            "SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [{"role": r["role"], "content": r["content"], "id": r["id"]}
                for r in reversed(rows) if r["role"] in ("user", "assistant")]

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
            "SELECT COUNT(*) AS n, MIN(id) AS first_id, MAX(id) AS last_id FROM messages"
        )
        if total is None or total["n"] < trigger:
            return
        keep = int(self.config.get("conversation.max_history_turns", 24))
        summarized_until = int(self.db.get_meta("conversation.summary_until_id") or 0)
        window_start = total["last_id"] - keep
        pending_end = max(window_start, summarized_until)
        rows = self.db.query(
            "SELECT * FROM messages WHERE id <= ? AND id > ? ORDER BY id ASC",
            (pending_end, summarized_until),
        )
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
        cur = self.db.execute(
            "INSERT INTO messages (role, content, ts, person, sender, source) VALUES (?,?,?,?,?,?)",
            ("user", text, now, person, sender, "user"),
        )
        user_msg_id = cur.lastrowid

        # wake the activity-aware services
        if self._proactive_cb is not None:
            self._proactive_cb()
        await self._emit({"type": "message", "message": {
            "id": user_msg_id, "role": "user", "content": text, "ts": now,
            "person": person, "sender": sender, "source": "user", "meta": {}}})

        if text.startswith("/"):
            reply = self._handle_command(text)
            return await self._finish_system_reply(reply, person, source="chat")

        # explicit "remember this" → immediate durable fact
        m = REMEMBER_RE.search(text)
        if m and self.config.get("memory.enabled", True):
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
        return await self._finish_assistant_reply(reply_text, person, user_msg_id,
                                                  source="chat", meta=degraded)

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

    # ------------------------------------------------------------------ generation
    async def _build_messages(self, text: str, person: str,
                              proactive_context: str | None = None,
                              include_user_text: bool = True) -> list[dict]:
        recent_user = " ".join(
            m["content"] for m in self._window()[-4:] if m["role"] == "user") or text
        system = await self.prompt_builder.build_system(
            now_epoch=self.clock.now(), query=recent_user, subject=person,
            extra_blocks=[self._summary_block()] if self._summary_block() else None,
            proactive_context=proactive_context,
        )
        if self.tools.names():
            system += "\n\n" + self.tools.manifest_text()
        messages = [{"role": "system", "content": system}]
        summary_until = int(self.db.get_meta("conversation.summary_until_id") or 0)
        window = [m for m in self._window() if m["id"] > summary_until]
        messages += [{"role": m["role"], "content": m["content"]} for m in window]
        # For system-driven turns (reminder delivery) the instruction text is
        # not in the persisted window — send it as the final user message so
        # the model actually sees it.
        if include_user_text and text and (not window or window[-1]["content"] != text):
            messages.append({"role": "user", "content": text})
        return messages

    async def _generate_reply(self, text: str, person: str, prefix: str = "",
                              proactive_context: str | None = None) -> str:
        if self.chat_provider is None:
            return (prefix + "No chat model is configured. Set one under Settings → Models in the "
                    "dashboard (any OpenAI-compatible endpoint: LM Studio, llama.cpp, "
                    "Ollama). Reminders and memory keep working without it.")
        if self.compute_guard is not None:
            self.compute_guard.note_foreground()  # user turns preempt background AI
        messages = await self._build_messages(text, person, proactive_context)
        max_rounds = int(self.config.get("conversation.max_tool_rounds", 3))
        on_delta = self._make_delta_emitter()
        self._streaming = True
        try:
            for round_no in range(max_rounds):
                specs = self.tools.specs() if self.chat_provider.supports_tools() else None
                try:
                    result = await self.chat_provider.chat(messages, tools=specs, on_delta=on_delta)
                except ProviderError:
                    if specs and round_no > 0:
                        # server choked on tools: retry once without them
                        result = await self.chat_provider.chat(messages, tools=None, on_delta=on_delta)
                    else:
                        raise
                if not result.tool_calls:
                    return prefix + self._clean(result.text)
                messages.append({"role": "assistant", "content": result.text,
                                 "tool_calls": [{"name": c.name, "arguments": c.arguments} for c in result.tool_calls]})
                for call in result.tool_calls:
                    args = parse_tool_arguments(call.arguments)
                    ctx = {"person": person, "tz_offset_minutes": 0}
                    output = await self.tools.call(call.name, args, ctx)
                    await self._emit({"type": "tool", "name": call.name, "arguments": args,
                                      "output": output})
                    messages.append({"role": "user",
                                     "content": f"[tool {call.name} result]\n{output}"})
                text = "continue with the tool results"
            return prefix + self._clean(result.text)
        finally:
            self._streaming = False

    @staticmethod
    def _clean(text: str) -> str:
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I)
        text = re.sub(r"^<think>.*", "", text, flags=re.S | re.I)
        return text.strip()

    def _make_delta_emitter(self):
        async def on_delta(piece: str):
            await self._emit({"type": "delta", "text": piece})
        return lambda piece: asyncio.ensure_future(on_delta(piece))

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
            asyncio.ensure_future(self._voice.speak(text))
        return msg

    async def _finish_system_reply(self, text: str, person: str, source: str) -> dict:
        now = self.clock.now()
        cur = self.db.execute(
            "INSERT INTO messages (role, content, ts, person, source) VALUES (?,?,?,?,?)",
            ("assistant", text, now, person, source),
        )
        msg = {"id": cur.lastrowid, "role": "assistant", "content": text, "ts": now,
               "person": person, "sender": "", "source": source, "meta": {}}
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

    # ------------------------------------------------------------------ reminder delivery
    async def deliver_reminder(self, reminder: Reminder, note: str = "") -> None:
        """ReminderScheduler callback. Delivered even with no model (plain
        text); phrased naturally by the persona when the model is up."""
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
                    return
            except ProviderError:
                pass
        text = f"⏰ Reminder: {reminder.text}" + (f" ({note})" if note else "")
        await self._finish_system_reply(text, reminder.person, source="reminder")
        self._log_proactive("reminder_delivery", text, reminder.id)

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
            "at most one question. Do not repeat anything you have already said recently."
        )
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
