"""Conversation focus helpers — close reminder loops and follow the latest topic.

Small local models often re-ask for reminder confirmation after a delivery
or an unauthorized schedule attempt, even when the owner has moved on or
said no. These detectors let ConversationManager short-circuit that loop
deterministically and frame the prompt around the current user turn.
"""

from __future__ import annotations

import re

# Bare / short rejections after a confirmation ask.
REJECT_RE = re.compile(
    r"(?i)^\s*(?:"
    r"no|nope|nah|no\s*thanks|no\s*thank\s*you|"
    r"don'?t|do\s+not|stop|cancel(?:\s+it)?|"
    r"never\s*mind|forget\s*it|not\s+now|please\s+don'?t"
    r")\s*[.!]?\s*$"
)

# Owner explaining a reminder/action was only a test / verification.
TEST_CONTEXT_RE = re.compile(
    r"(?i)\b(?:"
    r"(?:(?:just|only)\s+)?(?:a\s+)?test(?:\s+reminder)?\b|"
    r"test(?:ing)?\s+(?:to\s+see|whether|if)\b|"
    r"that\s+was\s+(?:just\s+)?(?:a\s+)?test\b|"
    r"to\s+see\s+if\s+you\s+worked\b|"
    r"you\s+worked\s+fine\b"
    r")"
)

# Assistant still fishing for schedule confirmation.
CONFIRM_ASK_RE = re.compile(
    r"(?i)(?:"
    r"confirm\s+whether|"
    r"(?:can|could)\s+you\s+please\s+confirm|"
    r"would\s+you\s+like\s+(?:me\s+)?to\s+(?:set|schedule|remind)|"
    r"shall\s+i\s+(?:set|schedule|remind)|"
    r"want\s+me\s+to\s+(?:set|schedule|remind)|"
    r"before\s+(?:setting|scheduling)\s+the\s+reminder|"
    r"ask(?:ed|ing)?\s+for\s+confirmation\s+before"
    r")"
)

# User is talking about reminders/timers (stay in that topic).
REMINDER_TOPIC_RE = re.compile(
    r"(?i)\b(?:remind(?:er|ers|ing)?|timer|alarm|schedule|snooze)\b"
)


def is_rejection(text: str) -> bool:
    return bool(REJECT_RE.match((text or "").strip()))


def is_test_context(text: str) -> bool:
    return bool(TEST_CONTEXT_RE.search(text or ""))


def is_confirm_ask(text: str) -> bool:
    return bool(CONFIRM_ASK_RE.search(text or ""))


def mentions_reminder_topic(text: str) -> bool:
    return bool(REMINDER_TOPIC_RE.search(text or ""))


def should_close_schedule_loop(
    user_text: str,
    *,
    assistant_asking_confirm: bool,
    recent_reminder_delivery: bool,
    pending_confirm: bool,
) -> bool:
    """True when we should *deterministically* end a confirmation side-quest.

    Substantive topic changes after a delivery are handled by prompt focus
    (not a canned reply) so the model can still engage with the new subject.
    """
    text = (user_text or "").strip()
    if not text:
        return False
    if not (assistant_asking_confirm or recent_reminder_delivery or pending_confirm):
        return False
    return is_rejection(text) or is_test_context(text)


def topic_change_away_from_schedule(
    user_text: str,
    *,
    recent_reminder_delivery: bool,
    assistant_asking_confirm: bool = False,
) -> bool:
    """Owner moved on — clear loop state, keep a normal model reply."""
    text = (user_text or "").strip()
    if not text or not (recent_reminder_delivery or assistant_asking_confirm):
        return False
    if mentions_reminder_topic(text):
        return False
    if is_rejection(text) or is_test_context(text):
        return False
    return len(text) >= 24


def close_loop_reply(user_text: str) -> str:
    """Short, natural closure — no second confirmation ask."""
    text = user_text or ""
    if is_test_context(text) and re.search(r"(?i)\bthank", text):
        return ("Got it — that was just a check, and I'm glad it worked. "
                "I'm here whenever you want to talk about your AI projects "
                "or anything else on your day off.")
    if is_test_context(text):
        return ("Understood — that was only a test. I won't keep asking about it. "
                "What would you like to dig into?")
    if is_rejection(text):
        return ("Understood — I won't set or chase that reminder. "
                "What would you like to talk about?")
    # Topic change after a delivery announcement.
    return ("Sounds good — I'll leave the reminder alone. "
            "I'm with you on getting things working so personality can evolve "
            "alongside your AI RPG platform and other projects. "
            "Where do you want to start?")


def focus_instruction(user_text: str, *, after_reminder_delivery: bool = False) -> str:
    """Extra system note so the model answers THIS turn, not a prior delivery."""
    lines = [
        "Turn focus:",
        f"- The owner's latest message is: {user_text.strip()[:500]}",
        "- Answer that message directly. Do not reopen a closed reminder "
        "confirmation, invent a pending schedule, or ask again after a no/test.",
        "- Treat thanks, agreement, frustration, humor, and subject changes "
        "as social signals — acknowledge briefly and follow the new topic.",
        "- Casual conversation is not an instruction to schedule anything.",
    ]
    if after_reminder_delivery and not mentions_reminder_topic(user_text):
        lines.append(
            "- A reminder was just announced. The owner has moved on — do not "
            "ask to confirm, reschedule, or recreate it unless they ask."
        )
    return "\n".join(lines)
