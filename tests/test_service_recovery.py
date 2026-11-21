"""Service restart and recovery: model flapping, guard TTL recovery,
deferred work resuming, and state surviving a systemd-style restart."""

import asyncio

import httpx
import pytest
import yaml

from aurora.app import CompanionApp
from aurora.clock import Clock
from aurora.config import Config
from aurora.providers.base import ChatProvider, ChatResult, ProviderError
from tests.conftest import SEED, T0, make_test_config, mock_provider, standard_rules


class FlappingProvider(ChatProvider):
    """Healthy/unhealthy by flag — simulates a model server being unloaded
    and loaded again by other workloads on the shared machine."""
    name = "flapping"
    model = "shared-model"

    def __init__(self) -> None:
        self.healthy = True
        self.checks = 0

    async def chat(self, messages, tools=None, temperature=None, max_tokens=None,
                   on_delta=None) -> ChatResult:
        if not self.healthy:
            raise ProviderError("[LLM Error: model unloading]")
        return ChatResult(text="recovered reply")

    async def healthcheck(self) -> bool:
        self.checks += 1
        return self.healthy


def _flap_app(tmp_path, overrides=None):
    cfg = make_test_config(tmp_path, overrides)
    provider = FlappingProvider()
    app = CompanionApp(cfg, clock=Clock(T0), seed_path=SEED,
                       chat_provider=provider, background_provider=provider)
    return app, provider


async def test_chat_degrades_then_recovers(tmp_path):
    app, provider = _flap_app(tmp_path)
    provider.healthy = False
    bad = await app.conversation.submit_user_message("are you there?")
    assert "couldn't reach my language model" in bad["content"]
    assert bad["meta"]["degraded"] == "model_unreachable"

    provider.healthy = True
    good = await app.conversation.submit_user_message("are you there now?")
    assert good["content"] == "recovered reply"
    assert "degraded" not in good["meta"]


async def test_guard_backs_off_and_recovers_with_ttl(tmp_path):
    app, provider = _flap_app(tmp_path)
    app.config.set("resources.model_health_ttl_seconds", 600)
    guard = app.guard

    provider.healthy = False
    assert await guard.model_available(provider) is False
    ok, reason = await guard.background_allowed(provider)
    assert not ok and "model unavailable" in reason

    # server comes back but the TTL cache still holds the failure
    provider.healthy = True
    ok, _ = await guard.background_allowed(provider)
    assert not ok  # cached down-state
    app.clock.advance(601)
    ok, _ = await guard.background_allowed(provider)
    assert ok  # re-probed after TTL, healthy again


async def test_foreground_failure_marks_guard_down_immediately(tmp_path):
    app, provider = _flap_app(tmp_path)
    provider.healthy = False
    await app.conversation.submit_user_message("hello?")  # degraded reply path
    assert app.guard.snapshot()["model_available"] is False


async def test_deferred_proactive_fires_after_recovery(tmp_path):
    app, provider = _flap_app(tmp_path, overrides={
        "proactive.topic_share.enabled": False,
        "proactive.memory_resurfacing.enabled": False,
        "proactive.check_in.after_silence_minutes": 30,
    })
    pro = app.proactive
    pro._last_user_message = pro.clock.now() - 3600

    provider.healthy = False
    decision = await pro.tick()
    assert decision.get("deferred") is True
    assert app.db.query("SELECT * FROM proactive_log") == []

    provider.healthy = True
    app.clock.advance(601)  # past the health TTL
    decision = await pro.tick()
    assert decision.get("speak") is True
    rows = app.db.query("SELECT * FROM proactive_log")
    assert rows and rows[-1]["kind"] in ("check_in", "morning_greeting")


async def test_full_restart_preserves_voice_selection_reminders_history(tmp_path):
    data = {"data_dir": str(tmp_path / "data"),
            "voice": {"tts": "kayock", "stt": "kayock", "voice_id": "data"}}
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    cfg = Config.load(cfg_path)
    provider = mock_provider(standard_rules())
    app = CompanionApp(cfg, clock=Clock(T0), seed_path=SEED,
                       chat_provider=provider, background_provider=provider)
    try:
        await app.conversation.submit_user_message("Remember the greenhouse timer is at 6am")
        app.reminders.add("Greenhouse check", T0 + 3600)
        assert app.config.get("voice.voice_id") == "data"
        counts_before = app.store.counts()
        hist_before = app.conversation.history()
        reminders_before = len(app.reminders.list(active_only=True))
        app.db.close()

        app2 = CompanionApp(Config.load(cfg_path), clock=Clock(T0),
                            seed_path=SEED, chat_provider=mock_provider(standard_rules()),
                            background_provider=None)
        try:
            assert app2.voice.tts.name == "kayock"
            assert app2.voice.tts.voice == "data"           # voice selection survived
            assert app2.store.counts() == counts_before
            assert len(app2.conversation.history()) == len(hist_before)
            assert len(app2.reminders.list(active_only=True)) == reminders_before
            # model healthy on the rebuilt app: guard allows background work
            ok, reason = await app2.guard.background_allowed(app2.chat_provider)
            assert ok
        finally:
            app2.db.close()
    except Exception:
        app.db.close()
        raise


async def test_missed_reminder_recovery_after_crash_restart(tmp_path):
    app, _ = _flap_app(tmp_path)
    rem = app.reminders.add("Restart-proof reminder", T0 + 30)
    app.db.close()

    app2, provider2 = _flap_app(tmp_path)
    try:
        provider2.healthy = False  # model unavailable during recovery:
        app2.clock.advance(400)    # delivery must fall back to plain text
        app2.reminder_scheduler.start()
        await asyncio.sleep(0.1)
        assert app2.reminders.get(rem.id).status in ("delivered", "done")
        assert any("came due while Aurora was not running" in m["content"]
                   for m in app2.conversation.history() if m["role"] == "assistant")
    finally:
        app2.reminder_scheduler._running = False
