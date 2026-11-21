"""Scheduling-intent detection for reminder creation.

A model inventing a valid ``set_reminder`` payload is not authorization.
Only the *current user request* may authorize creating a reminder/timer
via the assistant tool path. Dashboard forms, slash commands, and the
deterministic NL reminder parser already embody explicit user intent and
bypass this gate by calling ``ReminderStore.add`` directly with
``source="user"``.
"""

from __future__ import annotations

import re

# Clear ask-to-schedule language in the current user turn.
_INTENT_RE = re.compile(
    r"(?i)(?:"
    r"\bremind(?:\s+me)?\b|"
    r"\bremember\s+to\b|"  # "remember to check…" is a schedule ask, not a memory fact
    r"\bset\s+(?:an?\s+|the\s+)?(?:timer|reminder|alarm)\b|"
    r"\bstart\s+(?:an?\s+|the\s+)?timer\b|"
    r"\b(?:start|set)\b.{0,48}\btimer\b|"  # "start my kitchen timer"
    r"\bschedule\s+(?:an?\s+|the\s+)?(?:reminder|timer|alarm)\b|"
    r"\bdon'?t\s+forget\s+to\b|"
    r"\bmake\s+(?:me\s+)?(?:an?\s+)?(?:timer|reminder)\b|"
    r"\b(?:can|could|would)\s+you\s+(?:please\s+)?remind\b"
    r")"
)

# Quoted spans — "he said 'remind me tomorrow'" must not authorize.
_QUOTE_RE = re.compile(r"([\"'])(?:\\.|(?!\1).)*\1")


def _strip_quotes(text: str) -> str:
    return _QUOTE_RE.sub(" ", text)


_NEGATED_RE = re.compile(
    r"(?i)\b(?:don'?t|do\s+not|never|stop|cancel)\s+(?:(?:trying\s+to|asking\s+to)\s+)?remind\b"
)


def has_scheduling_intent(user_text: str | None) -> bool:
    """True when the current user utterance clearly asks to schedule something."""
    text = (user_text or "").strip()
    if not text:
        return False
    # Prefer unquoted body so quoted task phrases cannot authorize.
    body = _strip_quotes(text)
    if _NEGATED_RE.search(body):
        return False
    if _INTENT_RE.search(body):
        return True
    # If stripping quotes removed the only match, there is no intent.
    return False


UNAUTHORIZED_SCHEDULE_MSG = (
    "error: cannot schedule a reminder unless the user clearly asked to be "
    "reminded or to set a timer in this message. Do not ask them to confirm "
    "or rephrase a schedule offer — continue the conversation on their "
    "current topic instead."
)
