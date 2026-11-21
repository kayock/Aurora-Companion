"""Consolidation: idle-triggered summaries, dedup, lessons, changelog."""

import pytest

from aurora.memory.consolidation import Consolidator


async def test_tick_requires_idle(make_app):
    app, _ = make_app()
    cons = app.consolidator
    cons.touch()
    result = await cons.tick()  # not idle yet
    assert result is None


async def test_idle_summary_written(make_app):
    app, _ = make_app()
    db = app.db
    for i, text in enumerate(["hello there", "hi! how are you", "planning my garden", "nice, tomatoes?",
                              "yes and peppers too", "lovely"]):
        role = "user" if i % 2 == 0 else "assistant"
        db.execute("INSERT INTO messages (role, content, ts, person, source) VALUES (?,?,?,?, 'user')",
                   (role, text, 1000.0 + i, "owner"))
    cons = app.consolidator
    cons.touch()
    cons._last_activity = app.clock.now() - 3600  # idle one hour
    result = await cons.tick()
    assert result is not None
    assert result["summary"] is not None
    row = db.query_one("SELECT * FROM day_summaries")
    assert row is not None and "conversation" in row["summary"].lower()


async def test_lexical_duplicate_merge(make_app):
    app, _ = make_app()
    a = app.store.add_fact("Alex is learning to play the guitar")
    # A duplicate that predates dedup (e.g. imported from an older store):
    # insert directly so the store's dedup can't refuse it.
    import json as _json
    now = app.clock.now()
    app.db.execute(
        "INSERT INTO facts (id, content, subject, category, weight, source, trust, evidence,"
        " versions, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("f_legacy1", "Alex is learning playing the guitar", "owner", "other", 1.0, "auto",
         "confirmed", "[]", "[]", now, now),
    )
    cons = app.consolidator
    cons._last_activity = app.clock.now() - 3600
    result = await cons.tick()
    assert result["merged"] >= 1
    assert not app.store.get_fact("f_legacy1").active
    assert app.store.get_fact(a.id).active


async def test_lessons_from_corrections(make_app):
    app, _ = make_app()
    app.db.execute("INSERT INTO messages (role, content, ts, person, source) VALUES ('user', 'no, that is wrong, my name is Sam not Alex', 100.0, 'owner', 'user')")
    cons = app.consolidator
    cons._last_activity = app.clock.now() - 3600
    result = await cons.tick()
    assert result["lessons"] >= 1
    rows = app.db.query("SELECT * FROM lessons")
    assert any("correct" in r["text"] for r in rows)


async def test_changelog_recorded(make_app):
    app, _ = make_app()
    app.store.add_fact("Alex likes tea with honey in the morning")
    # legacy duplicate inserted directly (bypassing store dedup)
    now = app.clock.now()
    app.db.execute(
        "INSERT INTO facts (id, content, subject, category, weight, source, trust, evidence,"
        " versions, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("f_legacy2", "Alex likes tea with honey mornings", "owner", "other", 1.0, "auto",
         "confirmed", "[]", "[]", now, now),
    )
    cons = app.consolidator
    cons._last_activity = app.clock.now() - 3600
    await cons.tick()
    changelog = app.db.get_meta_json("memory.changelog", [])
    assert changelog and changelog[0]["merged"] >= 1


async def test_consolidation_uses_llm_review(make_app):
    """The mock review returns '[]' so no fact changes, but the LLM path runs."""
    app, mock = make_app()
    contents = ["Alex prefers winter over summer", "The project uses PostgreSQL",
                "Sarah's choir meets on Tuesdays", "Alex is allergic to shellfish",
                "The cottage has a wood stove", "Alex cycles to work daily"]
    for content in contents:
        app.store.add_fact(content)
    cons = app.consolidator
    cons._last_activity = app.clock.now() - 3600
    result = await cons.tick()
    assert result is not None
    assert result["superseded"] == 0  # mock returns []
    # the review prompt really reached the model
    assert any("curating an AI companion" in c["user"] for c in mock.calls)


async def test_interrupt_guard(make_app):
    app, _ = make_app()
    cons = app.consolidator
    cons.touch()
    assert await cons.tick() is None
