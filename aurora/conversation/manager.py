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
from ..timeparse import describe_repeat, friendly, parse_repeat, parse_when
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

        # timer follow-up answers ("repeat", "five more minutes", "dismiss")
        # are matched first — they only count while a decision is pending
        decision = await self._try_timer_decision(text)
        if decision is not None:
            return await self._finish_system_reply(decision, person, source="timer")

        # natural-language reminders/timers — deterministic, model-free, so
        # they execute reliably even with no model or a JSON-happy model
        try:
            nl = await self._try_parse_reminder_request(text, person)
        except ValueError:
            nl = None  # trivial text (e.g. "hi") — not a reminder; model's turn
        if nl is not None:
            return await self._finish_system_reply(nl, person, source="reminder_set")

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
        state = self._software_state_text()
        if state:
            system += "\n\n" + state
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
                    if specs:
                        # server choked on tools: rebuild clean prompt and retry once without them
                        messages = await self._build_messages(text, person, proactive_context)
                        result = await self.chat_provider.chat(messages, tools=None, on_delta=on_delta)
                        specs = None
                    else:
                        raise
                if not result.tool_calls:
                    # Some models answer with the tool call as plain JSON text
                    # instead of using the tool_calls field. Detect that shape
                    # and EXECUTE it — a reminder request must actually run,
                    # never surface as JSON in the conversation.
                    embedded = self._extract_embedded_tool_call(result.text) if specs else None
                    if embedded is not None:
                        name, raw_args = embedded
                        args = parse_tool_arguments(raw_args)
                        output = await self.tools.call(name, args,
                                                       {"person": person, "tz_offset_minutes": 0})
                        await self._emit({"type": "tool", "name": name, "arguments": args,
                                          "output": output, "recovered_from_text": True})
                        messages.append({"role": "assistant", "content": result.text})
                        messages.append({"role": "user",
                                         "content": f"[tool {name} result]\n{output}"})
                        text = "continue with the tool results"
                        continue
                    return prefix + self._clean(result.text)
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
                    args = parse_tool_arguments(call.arguments)
                    ctx = {"person": person, "tz_offset_minutes": 0}
                    output = await self.tools.call(call.name, args, ctx)
                    await self._emit({"type": "tool", "name": call.name, "arguments": args,
                                      "output": output})
                    messages.append({"role": "tool",
                                     "tool_call_id": f"call_{round_no}_{i}",
                                     "content": str(output)})
                text = "continue with the tool results"
            return prefix + self._clean(result.text)
        finally:
            self._streaming = False

    @staticmethod
    def _extract_embedded_tool_call(text: str) -> "tuple[str, Any] | None":
        """Find a tool call that a model emitted as plain text, e.g.
        '{"name": "set_reminder", "arguments": {...}}' (with or without a
        ```json fence). Returns (name, raw_arguments) when the name matches a
        registered tool, else None."""
        raw = (text or "").strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I).strip()
        if not raw.startswith("{") or not raw.endswith("}"):
            return None
        try:
            data = json.loads(raw)
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        name = data.get("name") or data.get("tool") or data.get("tool_name")
        if not isinstance(name, str) or not name.strip():
            return None
        raw_args = data.get("arguments", data.get("parameters", {}))
        if not isinstance(raw_args, str):  # parse_tool_arguments expects JSON text
            raw_args = json.dumps(raw_args)
        return name.strip(), raw_args

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

    async def _try_parse_reminder_request(self, text: str, person: str) -> str | None:
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
                                 source="user", kind=kind,
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
                    return
            except ProviderError:
                pass
        text = f"⏰ Reminder: {reminder.text}" + (f" ({note})" if note else "")
        await self._finish_system_reply(text, reminder.person, source="reminder")
        await self._announce(text)  # voice announcement, when enabled (spoken once)
        self._log_proactive("reminder_delivery", text, reminder.id)

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
