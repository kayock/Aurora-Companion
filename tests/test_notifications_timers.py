"""Phase 2+3 upgrades: reliable reminders, notification flow, and the timer
repeat/snooze/dismiss interaction — tested on the virtual clock."""

from __future__ import annotations

import asyncio

import pytest

from tests.conftest import SEED, T0, Clock, CompanionApp, Config, mock_provider, standard_rules
from tests.test_voice_selection import _adapter_stt, _adapter_tts


def _build(tmp_path, rules=None, overrides=None, clock=None):
    provider = mock_provider(rules if rules is not None else standard_rules())
    cfg = Config({"data_dir": str(tmp_path / "data")})
    for k, v in (overrides or {}).items():
        cfg.set(k, v)
    app = CompanionApp(cfg, clock=clock or Clock(T0), seed_path=SEED,
                       chat_provider=provider, background_provider=provider)
    return app, provider


class RecordingVoice:
    def __init__(self):
        self.spoken: list[str] = []

    async def speak(self, text):
        self.spoken.append(text)


async def test_nl_reminder_creates_and_confirms(tmp_path):
    app, _ = _build(tmp_path)
    reply = await app.conversation.submit_user_message(
        "Remind me to check the servers in 15 minutes")
    rems = app.reminders.list(active_only=True)
    assert len(rems) == 1
    assert "check the servers" in rems[0].text.lower()
    assert 14.5 * 60 <= rems[0].due_at - rems[0].created_at <= 15.5 * 60
    assert "Got it" in reply["content"] and "check the servers" in reply["content"]


async def test_timer_completion_asks_once_with_buttons(tmp_path):
    """Fired timer: the exact prompt, decision buttons in meta, voice spoken
    exactly once — and no endless re-prompting afterwards."""
    app, _ = _build(tmp_path)
    voice = RecordingVoice()
    app.conversation._voice = voice
    msg = await app.conversation.submit_user_message("Set a timer for 5 minutes")
    rem = app.reminders.list(active_only=True)[0]

    app.clock.advance(301)
    await app.reminder_scheduler.fire_due()

    assert len(app.conversation.pending_timers) == 1
    assert voice.spoken and voice.spoken[-1].startswith("⏱️ Your timer is up")
    # visible chat message with the same prompt + decision buttons
    history = app.conversation.history(limit=5)
    timer_msgs = [m for m in history if m["meta"].get("timer_decision")]
    assert timer_msgs and "Would you like me to repeat it?" in timer_msgs[-1]["content"]
    assert timer_msgs[-1]["meta"]["timer_decision"]["id"] == rem.id
    assert timer_msgs[-1]["meta"]["timer_decision"]["duration"] == 300

    # time passes again: the question is NOT repeated (no re-fire — one-shot
    # delivered status), the pending decision stays visible in the bell
    app.clock.advance(600)
    await app.reminder_scheduler.fire_due()
    prompts = [s for s in voice.spoken if "Would you like me to repeat it" in s]
    assert len(prompts) == 1  # spoken once, never looped


async def test_timer_repeat_uses_original_duration(tmp_path):
    app, _ = _build(tmp_path)
    await app.conversation.submit_user_message("Set a timer for 5 minutes")
    app.clock.advance(301)
    await app.reminder_scheduler.fire_due()
    timer_id = next(iter(app.conversation.pending_timers))

    result = await app.conversation.timer_repeat(timer_id)
    assert result["ok"]
    now = app.clock.now()
    rems = [r for r in app.reminders.list(active_only=True) if r.kind == "timer"]
    assert len(rems) == 1
    assert 299 <= rems[0].due_at - now <= 301  # original duration restored
    assert timer_id not in app.conversation.pending_timers


async def test_timer_snooze_then_refire(tmp_path):
    app, _ = _build(tmp_path)
    await app.conversation.submit_user_message("Set a timer for 2 minutes")
    app.clock.advance(121)
    await app.reminder_scheduler.fire_due()
    timer_id = next(iter(app.conversation.pending_timers))

    result = await app.conversation.timer_snooze(timer_id, 60)
    assert result["ok"]
    rem = app.reminders.get(timer_id)
    assert rem.status == "snoozed"
    # pending decision kept (duration preserved for a later repeat)
    assert timer_id in app.conversation.pending_timers
    assert app.conversation.pending_timers[timer_id]["duration"] == 120

    app.clock.advance(61)
    await app.reminder_scheduler.fire_due()
    assert timer_id in app.conversation.pending_timers  # asks again after snooze
    prompts = [m for m in app.conversation.history(limit=10)
               if m["meta"].get("timer_decision")]
    assert len(prompts) == 2  # once per fire, not more


async def test_timer_dismiss(tmp_path):
    app, _ = _build(tmp_path)
    await app.conversation.submit_user_message("Set a timer for 2 minutes")
    app.clock.advance(121)
    await app.reminder_scheduler.fire_due()
    timer_id = next(iter(app.conversation.pending_timers))

    result = await app.conversation.timer_dismiss(timer_id)
    assert result["ok"]
    assert timer_id not in app.conversation.pending_timers
    assert app.reminders.get(timer_id).status in ("done", "delivered")
    # dismissing again is an honest no
    assert not (await app.conversation.timer_dismiss(timer_id))["ok"]


async def test_voice_answers_repeat_snooze_dismiss(tmp_path):
    app, mock = _build(tmp_path)
    await app.conversation.submit_user_message("Set a timer for 5 minutes")
    app.clock.advance(301)
    await app.reminder_scheduler.fire_due()

    reply = await app.conversation.submit_user_message("repeat")
    rems = [r for r in app.reminders.list(active_only=True) if r.kind == "timer"]
    assert len(rems) == 1 and "restarted" in reply["content"].lower()
    assert mock.calls == []  # answered model-free

    app.clock.advance(301)
    await app.reminder_scheduler.fire_due()
    reply = await app.conversation.submit_user_message("five more minutes")
    rem = app.reminders.get(next(iter(app.conversation.pending_timers)))
    assert rem.status == "snoozed"

    app.clock.advance(301)
    await app.reminder_scheduler.fire_due()
    reply = await app.conversation.submit_user_message("dismiss")
    assert "dismiss" in reply["content"].lower()
    assert app.conversation.pending_timers == {}


async def test_voice_answers_without_pending_timer_go_to_model(tmp_path):
    app, mock = _build(tmp_path)
    await app.conversation.submit_user_message("repeat")
    assert mock.calls, "with no pending timer, 'repeat' is a normal chat turn"


async def test_multiple_timers_independent(tmp_path):
    app, _ = _build(tmp_path)
    await app.conversation.submit_user_message("Set a timer for 5 minutes")
    await app.conversation.submit_user_message("Set a timer for 10 minutes")
    app.clock.advance(601)
    await app.reminder_scheduler.fire_due()
    assert len(app.conversation.pending_timers) == 2

    # voice answer targets the most recently fired timer only
    await app.conversation.submit_user_message("repeat")
    active_timers = [r for r in app.reminders.list(active_only=True) if r.kind == "timer"]
    assert len(active_timers) == 1  # the fresh repeated timer (scheduled, not yet fired)
    # the untouched 10-minute timer keeps its pending decision; the repeated
    # one only asks again when IT fires later
    assert len(app.conversation.pending_timers) == 1


async def test_pending_timer_survives_restart(tmp_path):
    app, _ = _build(tmp_path)
    await app.conversation.submit_user_message("Set a timer for 5 minutes")
    app.clock.advance(301)
    await app.reminder_scheduler.fire_due()
    timer_id = next(iter(app.conversation.pending_timers))
    app.db.close()

    provider = mock_provider(standard_rules())
    cfg = Config({"data_dir": str(tmp_path / "data")})
    app2 = CompanionApp(cfg, clock=Clock(T0 + 400), seed_path=SEED,
                        chat_provider=provider, background_provider=provider)
    assert timer_id in app2.conversation.pending_timers  # question survived
    assert app2.conversation.pending_timers[timer_id]["duration"] == 300


async def test_plain_reminder_voice_announcement_once(tmp_path):
    """With a model: the reminder is announced through Aurora's voice exactly
    once (the model phrases it; here the mock's reply stands in)."""
    app, _ = _build(tmp_path, overrides={"notifications.voice_announce": True,
                                         "voice.server_speak": True})
    voice = RecordingVoice()
    app.conversation._voice = voice
    app.reminders.add("Check the bread", T0 + 30, source="user")
    app.clock.advance(31)
    await app.reminder_scheduler.fire_due()
    assert len(voice.spoken) == 1
    assert voice.spoken[0]


async def test_plain_reminder_announces_verbatim_without_model(tmp_path):
    """No model at all: the delivery still happens — plain text message with
    the reminder itself, spoken once through the wired voice."""
    app, _ = _build(tmp_path)
    app.chat_provider = None
    app.conversation.chat_provider = None
    app.conversation.background_provider = None
    voice = RecordingVoice()
    app.conversation._voice = voice
    app.reminders.add("Check the bread", T0 + 30, source="user")
    app.clock.advance(31)
    await app.reminder_scheduler.fire_due()
    assert voice.spoken == ["⏰ Reminder: Check the bread"]
    history = app.conversation.history(limit=3)
    assert any("Check the bread" in m["content"] for m in history)


async def test_alerts_endpoint_counts(tmp_path):
    from fastapi.testclient import TestClient
    from aurora.server.app import create_server

    app, _ = _build(tmp_path)
    app.reminders.add("Water the tomatoes", app.clock.now() + 600, source="user")
    await app.conversation.submit_user_message("Set a timer for 2 minutes")
    app.clock.advance(121)
    await app.reminder_scheduler.fire_due()

    client = TestClient(create_server(app))
    data = client.get("/api/alerts").json()
    assert data["count"] == 2  # 1 active reminder + 1 pending timer decision
    assert any(r["text"] == "Water the tomatoes" for r in data["active"])
    assert len(data["pending_timers"]) == 1

    tid = next(iter(data["pending_timers"]))
    assert client.post(f"/api/timers/{tid}/dismiss").json()["ok"]
    assert client.get("/api/alerts").json()["count"] == 1
    assert client.post(f"/api/timers/{tid}/repeat").status_code == 404  # honest no


async def test_reminder_kind_migration_preserves_existing_rows(tmp_path):
    """A pre-upgrade database (reminders without a `kind` column) opens and
    keeps working — additive migration, no data rewritten."""
    import sqlite3
    db_path = tmp_path / "data" / "aurora.db"
    db_path.parent.mkdir(parents=True)
    legacy = sqlite3.connect(str(db_path))
    legacy.executescript("""
        CREATE TABLE reminders (id TEXT PRIMARY KEY, text TEXT NOT NULL,
            due_at REAL NOT NULL, created_at REAL NOT NULL, repeat TEXT,
            follow_up TEXT, status TEXT NOT NULL DEFAULT 'scheduled',
            ack INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
            snooze_count INTEGER NOT NULL DEFAULT 0, last_fired REAL,
            snoozed_until REAL, project TEXT NOT NULL DEFAULT '',
            person TEXT NOT NULL DEFAULT 'owner', source TEXT NOT NULL DEFAULT 'user');
        INSERT INTO reminders (id, text, due_at, created_at)
            VALUES ('r_legacy1', 'Old reminder', 1795255200, 1795255000);
    """)
    legacy.commit()
    legacy.close()

    app, _ = _build(tmp_path)
    old = app.reminders.get("r_legacy1")
    assert old is not None and old.text == "Old reminder"  # survived
    app.reminders.add("New timer", T0 + 60, kind="timer")
    assert app.reminders.get("r_legacy1").text == "Old reminder"  # still there


async def test_timer_set_via_tool_call(tmp_path):
    """The model's set_reminder tool also tags timers (kind=timer) when the
    user asked for a timer, so the interaction flow applies there too."""
    import json as _json
    from aurora.providers.base import ChatResult, ToolCall

    rules = [
        (r"\[tool set_reminder result\]", "Done!"),
        (r"kitchen timer",
         ChatResult(text="", finish_reason="tool_calls", tool_calls=[ToolCall(
             name="set_reminder",
             arguments=_json.dumps({"text": "Kitchen timer", "when": "in 3 minutes"}))])),
    ]
    app, mock = _build(tmp_path, rules=rules)
    await app.conversation.submit_user_message("Start my kitchen timer, in 3 minutes")
    rems = app.reminders.list(active_only=True)
    assert len(rems) == 1 and rems[0].kind == "timer"
    assert rems[0].source == "assistant_tool"


# ------------------------------------------------------- steering fixes
async def test_bare_duration_in_chat_and_api(tmp_path):
    """'15 minutes' (no 'in') must parse everywhere, not only chat:
    dashboard/API path and the NL path both."""
    from aurora.timeparse import parse_when
    d = parse_when("15 minutes", T0)
    assert d is not None and 14.5 * 60 < d - T0 <= 15.5 * 60
    d2 = parse_when("for 5 minutes", T0)
    assert d2 is not None and 4.5 * 60 < d2 - T0 <= 5.5 * 60

    app, _ = _build(tmp_path)
    reply = await app.conversation.submit_user_message("Remind me to stretch, 15 minutes")
    rems = app.reminders.list(active_only=True)
    assert len(rems) == 1 and "stretch" in rems[0].text.lower()
    assert "Got it" in reply["content"]


async def test_junk_text_never_becomes_a_reminder(tmp_path):
    """The live run produced 'hi'-named reminders — greetings and test noise
    are now rejected at the store, whatever the entry point."""
    from fastapi.testclient import TestClient
    from aurora.server.app import create_server

    app, mock = _build(tmp_path)
    with pytest.raises(ValueError):
        app.reminders.add("hi", app.clock.now() + 60)
    with pytest.raises(ValueError):
        app.reminders.add("null", app.clock.now() + 60)
    with pytest.raises(ValueError):
        app.reminders.add("  ", app.clock.now() + 60)
    # a plain chat "hi" must not create reminders, and neither must the NL path
    await app.conversation.submit_user_message("hi")
    await app.conversation.submit_user_message("Remind me to hi in 5 minutes")
    assert app.reminders.list(active_only=True) == []

    client = TestClient(create_server(app))
    r = client.post("/api/reminders", json={"text": "hi", "when": "in 10 minutes"})
    assert r.status_code == 400  # dashboard/API path rejects it too


async def test_confirmation_only_after_execution(tmp_path):
    """Aurora's 'Got it' confirmation implies the reminder EXISTS: the store
    already holds it when the reply is produced (no phantom confirmations)."""
    app, _ = _build(tmp_path)
    reply = await app.conversation.submit_user_message("Remind me to check the servers in 15 minutes")
    assert "Got it" in reply["content"]
    rems = [r for r in app.reminders.list(active_only=True)
            if "check the servers" in r.text.lower()]
    assert len(rems) == 1  # executed before the confirmation was spoken
    assert rems[0].due_at > app.clock.now()
