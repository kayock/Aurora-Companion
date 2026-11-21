"""Objective, rubric-based scoring for the Intelligence Lab.

Grading is deterministic wherever the scenario allows it (facts, numbers,
corrections, tool behavior) and explicitly heuristic where taste is involved
(humor, warmth) — those checks are marked as judgment criteria in the report
so nobody mistakes a heuristic for a human-quality verdict. No LLM-judge is
used: a local judge would be one of the very models under test.

Empty or reasoning-only answers score 0 by construction: graders run against
the VISIBLE text only.
"""

from __future__ import annotations

import difflib
import re

from .models import Check, CheckResult

_UNCERTAINTY_MARKERS = [
    "not sure", "don't know", "do not know", "don't recall", "can't recall",
    "cannot recall", "no way to know", "cannot know", "can't know",
    "i have no idea", "no idea", "cannot verify", "can't verify",
    "don't have that", "do not have that", "no record", "not mentioned",
    "didn't tell", "did not tell", "haven't told", "have not told",
    "haven't mentioned", "have not mentioned", "couldn't say", "could not say",
    "can't tell", "cannot tell", "unknown to me", "i'm afraid i don't",
]

_REFUSAL_MARKERS = [
    "won't", "will not", "can't", "cannot", "not able", "refuse",
    "not going to", "shouldn't", "should not", "no tool for", "not something i can",
    "i'm not able", "i am not able", "i don't have a way", "i do not have a way",
]

_CLARIFY_MARKERS = [
    "what do you mean", "which", "clarify", "could you tell me more",
    "more detail", "a bit more", "what would you like", "what exactly",
    "can you specify", "specify", "what kind", "make what better",
]

_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F600-\U0001F64F"
    "\U0001F900-\U0001F9FF\U00002700-\U000027BF\U0001F680-\U0001F6FF]")


def _low(text: str) -> str:
    return (text or "").lower()


def grade_check(check: Check, visible: str, prior_replies: list[str],
                tool_calls: list[dict], tool_results: dict[str, str]) -> CheckResult:
    text = _low(visible)
    p = check.params

    def done(passed: bool, observed: str = "") -> CheckResult:
        return CheckResult(kind=check.kind, note=check.note, passed=bool(passed),
                           observed=observed[:200])

    if check.kind == "contains_any":
        needles = [n.lower() for n in p.get("words", [])]
        hit = next((n for n in needles if n in text), None)
        return done(hit is not None, f"looked for any of {needles}; found {hit!r}")
    if check.kind == "contains_all":
        needles = [n.lower() for n in p.get("words", [])]
        missing = [n for n in needles if n not in text]
        return done(not missing, f"missing: {missing}" if missing else "all present")
    if check.kind == "not_contains":
        banned = [n.lower() for n in p.get("words", [])]
        found = [n for n in banned if n in text]
        return done(not found, f"found banned {found}" if found else "clean")
    if check.kind == "regex":
        m = re.search(p["pattern"], visible, re.I | re.S)
        return done(m is not None, m.group(0)[:120] if m else "no match")
    if check.kind == "min_len":
        return done(len(visible.strip()) >= int(p.get("chars", 1)),
                    f"{len(visible.strip())} chars")
    if check.kind == "max_len":
        return done(len(visible.strip()) <= int(p.get("chars", 100000)),
                    f"{len(visible.strip())} chars")
    if check.kind == "answer_number":
        wanted = [str(w).lower() for w in p.get("accepted", [])]
        hit = next((w for w in wanted if w in text), None)
        return done(hit is not None, f"accepted {wanted}; found {hit!r}")
    if check.kind == "uncertainty_expressed":
        hit = next((m for m in _UNCERTAINTY_MARKERS if m in text), None)
        return done(hit is not None, f"marker {hit!r}" if hit else "no uncertainty marker")
    if check.kind == "refusal_present":
        hit = next((m for m in _REFUSAL_MARKERS if m in text), None)
        return done(hit is not None, f"marker {hit!r}" if hit else "no refusal marker")
    if check.kind == "clarify_or_ask":
        asked = visible.count("?") >= 1 or next(
            (m for m in _CLARIFY_MARKERS if m in text), None) is not None
        return done(asked, "asked/clarified" if asked else "no clarifying question")
    if check.kind == "max_questions":
        n = visible.count("?")
        return done(n <= int(p.get("count", 1)), f"{n} question(s)")
    if check.kind == "no_emoji":
        return done(not _EMOJI_RE.search(visible), "emoji scan")
    if check.kind == "no_repetition":
        threshold = float(p.get("max_similarity", 0.75))
        worst = 0.0
        for prev in prior_replies:
            if prev.strip():
                worst = max(worst, difflib.SequenceMatcher(
                    None, _low(prev), text).ratio())
        return done(worst < threshold, f"max similarity {worst:.2f}")
    if check.kind == "tool_called":
        wanted = p.get("tool")
        names = [c.get("name") for c in tool_calls]
        if wanted is None:
            return done(bool(names), f"calls: {names}")
        return done(wanted in names, f"calls: {names}")
    if check.kind == "tool_not_called":
        return done(not tool_calls, f"calls: {[c.get('name') for c in tool_calls]}")
    if check.kind == "tool_args_valid":
        wanted = p.get("tool")
        required = list(p.get("required", []))
        for c in tool_calls:
            if wanted and c.get("name") != wanted:
                continue
            try:
                import json
                args = json.loads(c.get("arguments") or "{}")
            except Exception:
                continue
            missing = [k for k in required
                       if not str(args.get(k, "")).strip()]
            if not missing:
                return done(True, f"args {args}")
        return done(False, f"no call with valid {required}")
    if check.kind == "tool_result_used":
        marker = str(p.get("marker", "")).lower()
        if not marker:
            return done(True, "no marker configured")
        used = marker in text
        return done(used, f"tool result marker {marker!r} "
                          f"{'used' if used else 'absent from the reply'}")
    if check.kind == "attribute_to_user":
        markers = [m.lower() for m in p.get("markers", ["you"])]
        hit = next((m for m in markers if m in text), None)
        return done(hit is not None, f"attribution marker {hit!r}")
    if check.kind == "style_bullet_points":
        return done(bool(re.search(r"(?m)^\s*[-•*]\s+", visible)), "bullet scan")
    if check.kind == "max_words":
        n = len(visible.split())
        return done(n <= int(p.get("words", 100000)), f"{n} words")
    return CheckResult(kind=check.kind, note=check.note or "unknown check",
                       passed=False, observed="unrecognized check kind")


def grade_turn(checks: list[Check], visible: str, prior_replies: list[str],
               tool_calls: list[dict], tool_results: dict[str, str],
               ) -> tuple[float, list[CheckResult]]:
    results = [grade_check(c, visible, prior_replies, tool_calls, tool_results)
               for c in checks]
    if not results:
        return 1.0, results  # ungraded turn: completion itself is the criterion
    return sum(r.passed for r in results) / len(results), results


JUDGMENT_KINDS = {"contains_any", "on_topic"}  # flagged as heuristic in reports
