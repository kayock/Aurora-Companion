"""Restart persistence: build an app, work, close it, rebuild on the same
data dir and verify every kind of state survived."""

import json

import pytest

from aurora.app import CompanionApp
from aurora.clock import Clock
from tests.conftest import SEED, T0, standard_rules, mock_provider, make_test_config as cfg_factory


def _build(tmp_path, rules=None):
    provider = mock_provider(rules if rules is not None else standard_rules())
    cfg = cfg_factory(tmp_path)
    app = CompanionApp(cfg, clock=Clock(T0), seed_path=SEED,
                       chat_provider=provider, background_provider=provider)
    return app, provider


async def test_full_restart_persistence(tmp_path):
    app, mock = _build(tmp_path)

    # 1. a conversation turn
    msg = await app.conversation.submit_user_message("Remember that I take photos of lighthouse trails")
    # 2. an explicit fact
    app.store.add_fact("Backup day is Sunday", source="manual", pinned=True)
    # 3. reminders (one future one-time, one recurring, one with follow-up)
    r1 = app.reminders.add("Future one-time", T0 + 7200)
    r2 = app.reminders.add("Recurring chore", T0 + 60, repeat={"kind": "interval", "seconds": 3600})
    r3 = app.reminders.add("Nagging task", T0 + 60, follow_up=True, follow_up_seconds=300)
    # 4. mood change + persona evolution + proactive log entry
    await app.mood.update_after_turn("thanks, this is great", "mock reply")
    await app.evolution.evolve("facts", "conversation", force=True)
    app.conversation.log_proactive("topic_share", "test message", "ref1", 0.4)
    # 5. extraction via conversation cadence
    await app.conversation.extractor.extract_since_cursor()
    counts_before = app.store.counts()
    facts_before = [f.to_dict() for f in app.store.active_facts()]
    history_before = app.conversation.history()
    persona_before = app.persona.state.to_dict()
    mood_before = app.mood.state()
    deliv_before = len(app.reminders.list(active_only=True))

    app.db.close()

    # ---- rebuild (same data dir, new instances = the restart) ----
    app2, mock2 = _build(tmp_path)
    try:
        # conversation survived
        assert len(app2.conversation.history()) == len(history_before)
        assert any("lighthouse" in m["content"] for m in app2.conversation.history())
        # facts survived with pinned flag and evidence
        assert app2.store.counts() == counts_before
        pinned = [f for f in app2.store.active_facts() if f.pinned]
        assert any("Backup day" in f.content for f in pinned)
        for f_before in facts_before:
            now = app2.store.get_fact(f_before["id"])
            assert now is not None and now.content == f_before["content"]
        # reminders survived with repeat/follow-up specs
        assert len(app2.reminders.list(active_only=True)) == deliv_before
        got2 = app2.reminders.get(r2.id)
        assert got2.repeat == {"kind": "interval", "seconds": 3600}
        got3 = app2.reminders.get(r3.id)
        assert got3.follow_up["every_seconds"] == 300
        # persona evolution survived (version + evolved prompt + anchor)
        assert app2.persona.state.version == persona_before["version"] >= 1
        assert app2.persona.state.evolved_prompt == persona_before["evolved_prompt"]
        assert app2.persona.state.core_anchor
        assert len(app2.persona.history()) >= 1
        # mood survived
        assert app2.mood.state()["warmth"] == mood_before["warmth"]
        # proactive log survived
        rows = app2.db.query("SELECT * FROM proactive_log")
        assert any(r["topic_ref"] == "ref1" for r in rows)
        # extraction cursor survived (no re-extraction of old messages)
        cursor = app2.db.get_meta("memory.extraction_cursor")
        assert cursor is not None
    finally:
        app2.db.close()


async def test_mood_restarts_without_crash(tmp_path):
    app, _ = _build(tmp_path)
    app.mood.warmth = 0.8
    app.mood._persist()
    app.db.close()
    app2, _ = _build(tmp_path)
    assert 0.0 <= app2.mood.warmth <= 1.0
    app2.db.close()


async def test_corrupt_db_quarantined_or_clear_error(tmp_path):
    """A corrupt store must not silently lose everything without a trace:
    Aurora refuses to start on a corrupt DB (loud failure, no fake reset)."""
    data = tmp_path / "data"
    data.mkdir(parents=True)
    (data / "aurora.db").write_bytes(b"not a database at all" * 10)
    import sqlite3
    from aurora.storage import Database

    with pytest.raises((sqlite3.DatabaseError, Exception)):
        Database(data / "aurora.db")
