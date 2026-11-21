"""Reminder Active/History views, Done/Cancel removal, permanent delete,
bulk cleanup, and phantom-prevention regressions.

These cover the failure paths observed on Portable: Done/Cancel left rows
visible because the dashboard listed every status; greetings and break
nudges were stored via assistant_tool; prompts exposed relative countdowns
that the model recited during unrelated chat.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aurora.conversation.prompt import PromptBuilder
from aurora.reminders.store import validate_reminder_text
from aurora.server.app import create_server


@pytest.fixture
def client(make_app):
    app, mock = make_app(overrides={"proactive.enabled": False})
    return TestClient(create_server(app)), app


def test_done_and_cancel_leave_active_view(client):
    c, app = client
    a = c.post("/api/reminders", json={"text": "Water the plants", "when": "in 20 minutes"}).json()
    b = c.post("/api/reminders", json={"text": "Call the dentist", "when": "in 40 minutes"}).json()

    r = c.post(f"/api/reminders/{a['id']}/done")
    assert r.status_code == 200 and r.json()["status"] == "done"
    r = c.post(f"/api/reminders/{b['id']}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"

    active = c.get("/api/reminders?view=active").json()["reminders"]
    assert active == []
    history_ids = {x["id"] for x in c.get("/api/reminders?view=history").json()["reminders"]}
    assert a["id"] in history_ids and b["id"] in history_ids


def test_permanent_delete_and_bulk_delete(client):
    c, app = client
    ids = []
    for label in ("Check the mail", "Feed the cat", "Buy milk"):
        rem = c.post("/api/reminders", json={"text": label, "when": "in 15 minutes"}).json()
        ids.append(rem["id"])
        c.post(f"/api/reminders/{rem['id']}/cancel")

    r = c.delete(f"/api/reminders/{ids[0]}")
    assert r.status_code == 200 and ids[0] in r.json()["deleted"]
    assert app.reminders.get(ids[0]) is None

    r = c.post("/api/reminders/bulk-delete", json={"ids": ids[1:]})
    assert r.status_code == 200 and r.json()["deleted_count"] == 2
    assert app.reminders.get(ids[1]) is None
    assert app.reminders.get(ids[2]) is None


def test_clear_history_preserves_active(client):
    c, app = client
    keep = c.post("/api/reminders", json={"text": "Keep this active", "when": "in 1 hour"}).json()
    gone = c.post("/api/reminders", json={"text": "History junk task", "when": "in 10 minutes"}).json()
    c.post(f"/api/reminders/{gone['id']}/done")

    r = c.post("/api/reminders/clear-history")
    assert r.status_code == 200 and r.json()["deleted_count"] >= 1
    assert app.reminders.get(keep["id"]) is not None
    assert app.reminders.get(keep["id"]).status == "scheduled"
    assert app.reminders.get(gone["id"]) is None


def test_phantom_greeting_and_break_texts_rejected(client):
    c, _ = client
    phantoms = [
        "hi", "Hey, Eric", "Good morning", "null", "Take a break",
        "morning check-in, Eric! It",
        "I can help you with reminders, facts, and other tasks.",
    ]
    for text in phantoms:
        with pytest.raises(ValueError):
            validate_reminder_text(text)
        r = c.post("/api/reminders", json={"text": text, "when": "in 10 minutes"})
        assert r.status_code == 400, text


@pytest.mark.asyncio
async def test_set_reminder_tool_rejects_phantoms(make_app):
    app, _ = make_app(overrides={"proactive.enabled": False})
    out = await app.tools.call(
        "set_reminder",
        {"text": "Hey, Eric", "when": "in 20 minutes"},
        {"person": "owner", "on_change": (lambda: None)},
    )
    assert out.startswith("error:")
    assert app.reminders.list(active_only=True) == []


@pytest.mark.asyncio
async def test_legitimate_reminder_still_schedules(make_app):
    app, _ = make_app(overrides={"proactive.enabled": False})
    out = await app.tools.call(
        "set_reminder",
        {"text": "check the servers", "when": "in 20 minutes"},
        {"person": "owner", "on_change": (lambda: None),
         "user_text": "Please remind me to check the servers in 20 minutes"},
    )
    assert out.startswith("scheduled reminder")
    active = app.reminders.list(active_only=True)
    assert len(active) == 1 and "servers" in active[0].text.lower()


@pytest.mark.asyncio
async def test_system_prompt_omits_relative_countdown(make_app):
    app, _ = make_app(overrides={"proactive.enabled": False})
    rem = app.reminders.add("check the servers", app.clock.now() + 20 * 60)
    builder = PromptBuilder(
        app.db, app.persona, app.mood, app.retriever, app.reminders, "Eric",
        people_registry=app.people,
    )
    system = await builder.build_system(app.clock.now(), "how's the weather", subject="owner")
    assert "check the servers" in system
    assert rem.id in system
    assert "in 20 minutes" not in system.lower()
    assert "do not recite or count down" in system.lower()
    assert "only call set_reminder when the user's current message clearly asks" in system.lower()


def test_dashboard_reminder_js_has_active_history_and_delete():
    """Static UI contract: Active/History select, Done/Cancel/Delete, bulk."""
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "aurora/server/static/index.html").read_text()
    assert 'id="rem-view"' in html
    assert 'value="active"' in html and 'value="history"' in html
    assert "remBulkDelete" in html and "remClearHistory" in html
    assert "/api/reminders/bulk-delete" in html
    assert "Permanently delete" in html
