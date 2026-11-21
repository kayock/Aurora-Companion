"""Tool registry + implementations.

Tools follow the OpenAI function-calling dialect (Luna's @tool registry
pattern). The reminder and memory tools are the important ones: they let
the model create/manage reminders and inspect its own memory mid-conversation,
while every mutation still flows through the same store the dashboard uses.

When the active provider cannot do tool calls, the conversation manager
skips the loop entirely and the user can still do everything through slash
commands and the dashboard — nothing depends on tools.
"""

from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable

from ..providers.base import ToolSpec
from ..launcher import has_launch_intent, LAUNCH_UNAUTHORIZED_MSG
from ..reminders.intent import UNAUTHORIZED_SCHEDULE_MSG, has_scheduling_intent
from ..timeparse import describe_repeat, friendly, parse_repeat, parse_when

ToolHandler = Callable[[dict, dict], Awaitable[str]]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, tuple[ToolSpec, ToolHandler]] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        self._tools[spec.name] = (spec, handler)

    def specs(self, names: list[str] | None = None) -> list[ToolSpec]:
        if names is None:
            return [spec for spec, _ in self._tools.values()]
        return [self._tools[n][0] for n in names if n in self._tools]

    def names(self) -> list[str]:
        return list(self._tools)

    async def call(self, name: str, arguments: dict, ctx: dict) -> str:
        entry = self._tools.get(name)
        if entry is None:
            return f"error: unknown tool {name}"
        spec, handler = entry
        try:
            return await handler(arguments, ctx)
        except Exception as exc:  # tool errors go back to the model, not the user
            return f"error: {exc}"

    def manifest_text(self) -> str:
        lines = [f"- {s.name}: {s.description}" for s in self.specs()]
        return "Tools you may call this turn:\n" + "\n".join(lines)


def _spec(name: str, description: str, params: dict) -> ToolSpec:
    return ToolSpec(name=name, description=description, parameters=params)


def default_tool_registry(services: dict[str, Any]) -> ToolRegistry:
    """Build the standard toolset. `services` carries the app's subsystems:
    reminders (store), clock, config, memory (store), user_name."""
    registry = ToolRegistry()
    reminders: Any = services.get("reminders")
    clock = services.get("clock")
    facts: Any = services.get("memory")
    config = services.get("config")
    min_repeat = int((config.get("reminders.min_repeat_seconds", 60) if config else 60))

    # ---- time ----
    async def get_time(_args, _ctx):
        from datetime import datetime, timezone

        return datetime.fromtimestamp(clock.now(), tz=timezone.utc).strftime(
            "%A %Y-%m-%d %H:%M UTC")

    registry.register(_spec("get_time", "Get the current date and time.", {"type": "object", "properties": {}}), get_time)

    # ---- reminders ----
    if reminders is not None:
        async def set_reminder(args, ctx):
            text = str(args.get("text", "")).strip()
            when_words = str(args.get("when", "")).strip()
            person = ctx.get("person", "owner")
            # Authorization: a well-formed payload is NOT enough. The current
            # user utterance must clearly ask to schedule. Past chat, memory,
            # and proactive suggestions cannot authorize creation.
            user_text = ctx.get("user_text")
            if ctx.get("allow_schedule") is False or not has_scheduling_intent(
                    user_text if user_text is not None else ""):
                return UNAUTHORIZED_SCHEDULE_MSG
            if not text:
                return "error: reminder text required"
            if not when_words:
                return "error: when is required — use the user's timing words"
            due = parse_when(when_words, clock.now(), tz_offset_minutes=ctx.get("tz_offset_minutes", 0))
            if due is None:
                return (f"error: could not understand the time \"{when_words}\". "
                        "Ask the user to rephrase, e.g. 'in 20 minutes' or 'tomorrow at 9am'.")
            if due <= clock.now() - 5:
                return "error: that time is already in the past"
            repeat = parse_repeat(when_words)
            follow_up = bool(args.get("follow_up", False))
            kind = "timer" if "timer" in (text + " " + when_words).lower() else ""
            try:
                rem = reminders.add(
                    text, due, repeat=repeat, follow_up=follow_up,
                    project=str(args.get("project", "")), person=person, source="assistant_tool",
                    kind=kind,
                    duration=int(round(due - clock.now())) if kind == "timer" else None,
                )
            except ValueError as exc:
                return f"error: {exc}"
            ctx.get("on_change", (lambda: None))()
            rep = f", repeating {describe_repeat(repeat)}" if repeat else ""
            return f"scheduled reminder {rem.id}: \"{rem.text}\" for {friendly(due, clock.now())}{rep}"

        registry.register(_spec(
            "set_reminder",
            "Schedule a reminder ONLY when the user's CURRENT message clearly asks "
            "to be reminded or to set a timer (e.g. 'remind me…', 'set a timer…'). "
            "Never call this for greetings, check-ins, break suggestions, quoted "
            "phrases, past topics, or your own ideas — even if you remember a task. "
            "`text` must be an actionable task and `when` must be the user's timing "
            "words (e.g. 'in 20 minutes', 'tomorrow at 9am', 'every Monday at 9am').",
            {"type": "object",
             "properties": {
                 "text": {"type": "string"},
                 "when": {"type": "string"},
                 "project": {"type": "string"},
                 "follow_up": {"type": "boolean", "description": "repeat until acknowledged"},
             },
             "required": ["text", "when"]}), set_reminder)

        async def list_reminders(args, ctx):
            person = ctx.get("person", "owner")
            items = reminders.list(active_only=True, person=person)
            if not items:
                return "no active reminders"
            return "\n".join(
                f"[{r.id}] \"{r.text}\" due {friendly(r.due_at, clock.now())}"
                + (f" (recurring)" if r.recurring else "")
                + (f" (follow-up {r.attempts})" if r.attempts else "")
                for r in items)

        registry.register(_spec("list_reminders", "List the user's active reminders.",
                                {"type": "object", "properties": {}}), list_reminders)

        async def cancel_reminder(args, ctx):
            rem = reminders.cancel(str(args.get("id", "")))
            ctx.get("on_change", (lambda: None))()
            return f"cancelled {rem.id}" if rem else "reminder not found"

        registry.register(_spec("cancel_reminder", "Cancel a reminder by id.",
                                {"type": "object", "properties": {"id": {"type": "string"}},
                                 "required": ["id"]}), cancel_reminder)

        async def snooze_reminder(args, ctx):
            seconds = int(args.get("seconds", 600))
            rem = reminders.snooze(str(args.get("id", "")), seconds)
            ctx.get("on_change", (lambda: None))()
            return f"snoozed {rem.id} by {seconds}s" if rem else "reminder not found"

        registry.register(_spec("snooze_reminder", "Snooze a reminder by id for N seconds.",
                                {"type": "object",
                                 "properties": {"id": {"type": "string"}, "seconds": {"type": "integer"}},
                                 "required": ["id"]}), snooze_reminder)

    # ---- memory ----
    if facts is not None:
        async def remember_fact(args, ctx):
            import os
            person = ctx.get("person", "owner")
            user_text = str(ctx.get("user_text") or "")
            if (os.environ.get("AURORA_TURN_SOURCE") or "").strip().lower() in (
                "verify", "dev_test",
            ):
                return ("error: verification turns do not write durable owner facts "
                        "(set AURORA_TURN_SOURCE only for disposable harnesses)")
            # Do not let the model invent durable "user asked me to…" facts
            # from ordinary chat — require an explicit remember request.
            if not re.search(r"(?i)\b(remember|don'?t forget|keep in mind)\b", user_text):
                return ("error: only store a durable fact when the user asked you "
                        "to remember it in this message")
            fact = facts.add_fact(
                str(args.get("content", "")), subject=person,
                category=str(args.get("category", "other")) or "other",
                source="user_command",
                evidence=ctx.get("evidence") or [],
                weight=1.5, pinned=bool(args.get("pinned", False)),
            )
            return f"remembered (fact {fact.id}): {fact.content}"

        registry.register(_spec(
            "remember_fact",
            "Store a durable long-term fact ONLY when the user asked you to remember "
            "something in the current message (e.g. 'remember that…'). Never invent "
            "facts from greetings, reminders, or your own suggestions.",
            {"type": "object",
             "properties": {"content": {"type": "string"}, "category": {"type": "string"},
                            "pinned": {"type": "boolean"}},
             "required": ["content"]}), remember_fact)

        async def recall_facts(args, ctx):
            person = ctx.get("person", "owner")
            hits = facts.facts_fts(str(args.get("query", "")), subject=person, limit=8)
            if not hits:
                return "nothing found in memory"
            return "\n".join(f"[{f.id}] {f.content}" for f in hits)

        registry.register(_spec("recall_facts", "Search your long-term memory.",
                                {"type": "object", "properties": {"query": {"type": "string"}},
                                 "required": ["query"]}), recall_facts)

        async def forget_fact(args, ctx):
            person = ctx.get("person", "owner")
            fact_id = str(args.get("id", ""))
            fact = facts.get_fact(fact_id)
            if fact is None or fact.subject != person:
                return "fact not found"
            facts.archive_fact(fact_id, "user_request")
            return f"forgot (archived) {fact_id}"

        registry.register(_spec("forget_fact", "Retire a fact from memory by id (user asked to forget).",
                                {"type": "object", "properties": {"id": {"type": "string"}},
                                 "required": ["id"]}), forget_fact)

        async def update_fact(args, ctx):
            fact_id = str(args.get("id", ""))
            fact = facts.get_fact(fact_id)
            if fact is None:
                return "fact not found"
            new = facts.supersede_fact(fact_id, str(args.get("content", "")), source="user_command")
            return f"updated: {new.id} replaces {fact_id}"

        registry.register(_spec("update_fact", "Correct/replace an outdated fact with new content.",
                                {"type": "object",
                                 "properties": {"id": {"type": "string"}, "content": {"type": "string"}},
                                 "required": ["id", "content"]}), update_fact)

    # ---- launching (model names a catalog entry; the app executes it) ----
    launcher = services.get("launcher")
    if launcher is not None:
        def _live_launcher(ctx):
            current = ctx.get("launcher")
            return current if current is not None else launcher

        async def open_website(args, ctx):
            # Authorization mirrors the reminder tools: the CURRENT user
            # utterance must clearly ask to open something. Past chat, memory
            # or the model's own ideas never authorize a launch.
            if not has_launch_intent(str(ctx.get("user_text") or "")):
                return LAUNCH_UNAUTHORIZED_MSG
            target = str(args.get("url", "")).strip() or str(args.get("name", "")).strip()
            if not target:
                return "error: provide a url or a website name from the catalog"
            res = _live_launcher(ctx).open_website(target)
            return res.message

        registry.register(_spec(
            "open_website",
            "Open a website in the browser ON THE MACHINE RUNNING AURORA "
            "(not on the user's other computer). Use ONLY for the user's "
            "current explicit request — a URL, or a site name from the "
            "catalog. Never call this for quoted text or past topics.",
            {"type": "object",
             "properties": {
                 "url": {"type": "string", "description": "full http(s) URL"},
                 "name": {"type": "string", "description": "site name from the catalog"},
             }}), open_website)

        async def launch_application(args, ctx):
            if not has_launch_intent(str(ctx.get("user_text") or "")):
                return LAUNCH_UNAUTHORIZED_MSG
            name = str(args.get("name", "")).strip()
            if not name:
                return "error: name is required (a catalog application name)"
            res = _live_launcher(ctx).launch_application(name)
            return res.message

        registry.register(_spec(
            "launch_application",
            "Launch a desktop application ON THE MACHINE RUNNING AURORA "
            "(not on the user's other computer). Only names from the "
            "application catalog are valid; call list_available_applications "
            "first if unsure. Only for the user's current explicit request.",
            {"type": "object",
             "properties": {"name": {"type": "string"}},
             "required": ["name"]}), launch_application)

        async def list_available_applications(_args, ctx):
            return _live_launcher(ctx).list_reply()

        registry.register(_spec(
            "list_available_applications",
            "List the applications and websites you can open on the machine "
            "running Aurora, with availability.",
            {"type": "object", "properties": {}}), list_available_applications)

    return registry
