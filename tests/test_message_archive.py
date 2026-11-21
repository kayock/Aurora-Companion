"""Soft-archive and verification provenance keep test chatter out of context."""

from __future__ import annotations

import pytest

from aurora.conversation.manager import VERIFY_TURN_SOURCES


@pytest.mark.asyncio
async def test_archived_messages_excluded_from_window_and_history(make_app):
    app, _ = make_app()
    await app.conversation.submit_user_message("Hello genuine owner turn")
    await app.conversation.submit_user_message(
        "ping latency check only — reply with the single word ok"
    )
    rows = app.db.query("SELECT id, role, content FROM messages ORDER BY id")
    first_assistant = next(r["id"] for r in rows if r["role"] == "assistant")
    to_archive = [r["id"] for r in rows if r["id"] > first_assistant]
    n = app.conversation.archive_messages(to_archive, "dev_test_probe")
    assert n == len(to_archive)

    window = app.conversation._window()
    assert all("ping latency" not in m["content"] for m in window)
    assert any("genuine owner" in m["content"] for m in window)

    hist = app.conversation.history(limit=50)
    assert all(m.get("archived", 0) == 0 for m in hist)
    assert all("ping latency" not in m["content"] for m in hist)

    full = app.conversation.history(limit=50, include_archived=True)
    assert any("ping latency" in m["content"] for m in full)


@pytest.mark.asyncio
async def test_verify_env_auto_archives_and_skips_memory(make_app, monkeypatch):
    app, _ = make_app()
    monkeypatch.setenv("AURORA_TURN_SOURCE", "verify")
    assert app.conversation._turn_source_from_env() in VERIFY_TURN_SOURCES

    result = await app.conversation.submit_user_message(
        "Remember that I like disposable verify astronomy facts."
    )
    assert result.get("archived") == 1
    assert "(Verification turn" in (result.get("content") or "")

    active = [
        f for f in app.store.active_facts()
        if "disposable verify astronomy" in f.content.lower()
    ]
    assert active == []

    hist = app.conversation.history(limit=20)
    assert all("disposable verify" not in m["content"] for m in hist)

    monkeypatch.delenv("AURORA_TURN_SOURCE", raising=False)
    await app.conversation.submit_user_message("Hi Aurora, this is a real hello.")
    window = app.conversation._window()
    assert any("real hello" in m["content"] for m in window)
    assert all("disposable verify" not in m["content"] for m in window)


@pytest.mark.asyncio
async def test_extraction_skips_archived_and_verify_rows(make_app, monkeypatch):
    app, _ = make_app()
    await app.conversation.submit_user_message("I collect rare orchids as a hobby.")
    monkeypatch.setenv("AURORA_TURN_SOURCE", "verify")
    await app.conversation.submit_user_message(
        "Remember that basil greenhouse is a verify fact."
    )
    monkeypatch.delenv("AURORA_TURN_SOURCE", raising=False)

    batch = app.conversation.extractor._batch_messages(0, limit=40)
    texts = " ".join(m["content"] for m in batch)
    assert "orchids" in texts
    assert "basil greenhouse" not in texts


def test_schema_migrates_message_archive_columns(tmp_path):
    from aurora.storage import Database, SCHEMA_VERSION

    db = Database(tmp_path / "t.db")
    cols = {r["name"] for r in db.query("PRAGMA table_info(messages)")}
    assert {"archived", "archive_reason", "archived_at"} <= cols
    assert int(db.get_meta("schema_version")) >= 2
    assert SCHEMA_VERSION >= 2
    db.close()
