"""Shared fixtures: a CompanionApp on a virtual clock with a deterministic
mock provider. Tests advance virtual time instead of sleeping."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aurora.app import CompanionApp  # noqa: E402
from aurora.clock import Clock  # noqa: E402
from aurora.config import Config  # noqa: E402
from aurora.providers.mock import MockProvider  # noqa: E402

SEED = ROOT / "personas" / "default.json"

# Start time: 2026-11-21 10:00 UTC (a Saturday morning, outside quiet hours).
T0 = 1795255200.0


def make_test_config(tmp_path: Path, overrides: dict | None = None) -> Config:
    cfg = Config({"data_dir": str(tmp_path / "data")})
    if overrides:
        for k, v in overrides.items():
            cfg.set(k, v)
    return cfg


def mock_provider(rules=(), default="Hello, this is a mock reply.") -> MockProvider:
    return MockProvider(rules=list(rules), default=default)


APPRaise_RULE = (r"warmth\s+(-?\d+)\s+energy\s+([-+=])", None)  # not used; explicit below


def standard_rules() -> list:
    """Rules that make the mock behave plausibly for every background call."""
    return [
        # mood appraisal
        (r"warmth\s+(-?\d+)\s+energy\s+([-+=])", "warmth 2 energy +"),
        # anchor extraction
        (r"Extract 3-6 immutable core traits",
         '{"core_anchor": "warm but honest; never invents facts", "rationale": "core traits"}'),
        # persona evolution (callable reads the original persona from the prompt)
        (r"BASELINE REVIEW|REGULAR EVOLUTION", _evolution_responder),
        # memory extraction ops (callable reads message ids out of the prompt)
        (r"Conversation:", _extraction_responder),
        # consolidation summary
        (r"Summarize this conversation excerpt", "We had a friendly conversation and planned tomorrow."),
        # consolidation fact review
        (r"curating an AI companion's long-term memory", "[]"),
        # affinity adjust
        (r"how much closer this exchange", "5"),
        # rolling summary
        (r"Update the running summary", "Running summary of the conversation so far."),
    ]


def _evolution_responder(m, messages):
    prompt = messages[-1]["content"]
    anchor = re.search(r"CORE ANCHOR[^\n]*\n(.+)", prompt)
    original = re.search(r"ORIGINAL PERSONA[^\n]*:\n(.*?)\n\nCURRENT", prompt, re.S)
    base = original.group(1) if original else "## Identity\nYou are Aurora."
    evolved = base + "\n\n## Evolved taste\nYou have grown to enjoy discussing cooking together."
    return json.dumps({
        "base_prompt": evolved,
        "style": "unchanged style",
        "change_summary": "Added taste section from shared cooking conversations",
        "reason": "evidence: repeated cooking discussions in facts",
        "anchor_preserved": True,
    })


def _extraction_responder(m, messages):
    prompt = messages[-1]["content"]
    ids = [int(x) for x in re.findall(r"\[(\d+)\] (?:User|Companion):", prompt)]
    ops = []
    if ids:
        ops.append({
            "op": "add",
            "content": "The user is working on a garden project",
            "category": "project",
            "weight": 1.5,
            "evidence": [{"message_id": ids[-1], "quote": "garden"}],
        })
    return json.dumps(ops)


@pytest.fixture
def clock() -> Clock:
    return Clock(T0)


@pytest.fixture
def make_app(tmp_path):
    """Factory: make_app(rules=[...], config_overrides={...}) -> (app, mock)."""
    created = []

    def _make(rules=None, overrides=None, default="Hello, this is a mock reply."):
        provider = mock_provider(rules if rules is not None else standard_rules(), default=default)
        cfg = make_test_config(tmp_path, overrides)
        app = CompanionApp(cfg, clock=Clock(T0), seed_path=SEED,
                           chat_provider=provider, background_provider=provider)
        created.append(app)
        return app, provider

    yield _make

    for app in created:
        try:
            app.db.close()
        except Exception:
            pass
