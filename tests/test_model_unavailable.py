"""Model-unavailable behavior: chat degrades honestly, voice-independent
subsystems keep working, and background AI defers instead of erroring."""

import httpx
import pytest
from fastapi.testclient import TestClient

from aurora.app import CompanionApp
from aurora.clock import Clock
from aurora.config import Config
from aurora.providers.base import ChatProvider, ChatResult, ProviderError
from aurora.server.app import create_server
from tests.conftest import SEED, T0, make_test_config, mock_provider, standard_rules


class DownProvider(ChatProvider):
    """A model server that is up enough to accept nothing: healthcheck
    fails and every chat call errors (the ProviderError path)."""
    name = "down"
    model = "down-model"

    async def chat(self, messages, tools=None, temperature=None, max_tokens=None,
                   on_delta=None) -> ChatResult:
        raise ProviderError("[LLM Error: connection refused]")

    async def healthcheck(self) -> bool:
        return False


async def test_degraded_reply_is_honest_and_marked(make_app):
    app, _ = make_app()
    down = DownProvider()
    app.conversation.chat_provider = down
    app.conversation.background_provider = down
    msg = await app.conversation.submit_user_message("hello?")
    assert msg["role"] == "assistant"
    assert "couldn't reach my language model" in msg["content"]
    assert msg["meta"]["degraded"] == "model_unreachable"
    # the turn stays in the one history (user + assistant)
    hist = app.conversation.history()
    assert [m["role"] for m in hist[-2:]] == ["user", "assistant"]


async def test_reminders_still_created_and_delivered_when_model_down(make_app):
    app, _ = make_app()
    down = DownProvider()
    app.conversation.chat_provider = down
    app.conversation.background_provider = down
    reply = await app.conversation.submit_user_message(
        "/remind in 5 minutes to stretch your legs")
    assert "Scheduled reminder" in reply["content"]
    active = app.reminders.list(active_only=True)
    assert len(active) == 1 and "stretch" in active[0].text

    app.clock.advance(301)
    fired = await app.reminder_scheduler.fire_due()
    assert fired and "stretch" in fired[0].text
    # delivery falls back to plain text (no model), still in the history
    hist = app.conversation.history()
    assert any(m["source"] == "reminder" and "stretch" in m["content"]
               for m in hist if m["role"] == "assistant")


async def test_background_ai_defers_when_model_down(make_app):
    app, _ = make_app()
    down = DownProvider()
    app.conversation.chat_provider = down
    app.conversation.background_provider = down
    warmth_before = app.mood.state()["warmth"]
    msg = await app.conversation.submit_user_message("hello there, model-less world")
    assert msg["meta"]["degraded"] == "model_unreachable"
    import asyncio
    await asyncio.sleep(0.2)  # post-turn hooks are fire-and-forget
    # mood appraisal (an LLM call) must have been deferred, not failed loudly
    assert app.mood.state()["warmth"] == warmth_before
    deferral = app.db.get_meta_json("resources.last_deferral", None)
    assert deferral and "model unavailable or unloaded" in deferral["reason"]


async def test_status_reports_model_unreachable(make_app):
    app, _ = make_app()
    down = DownProvider()
    app.chat_provider = down                 # what /api/status probes
    app.background_provider = down
    app.conversation.chat_provider = down
    app.conversation.background_provider = down
    c = TestClient(create_server(app))
    body = c.get("/api/status").json()
    assert body["model_reachable"] is False


async def test_no_provider_configured_is_honest(make_app):
    app, _ = make_app()
    app.conversation.chat_provider = None
    app.conversation.background_provider = None
    msg = await app.conversation.submit_user_message("ping")
    assert "No chat model is configured" in msg["content"]


async def test_unloaded_model_detected_and_deferred(make_app):
    """Server up (healthcheck OK) but configured model not in /v1/models:
    the guard treats it as unavailable and background AI defers."""
    from aurora.providers.openai_compat import OpenAICompatProvider

    app, _ = make_app()
    provider = OpenAICompatProvider("http://127.0.0.1:1234/v1", model="aurora-chat")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "other-model"}]})

    provider._client = httpx.AsyncClient(
        base_url=provider.base_url,
        transport=httpx.MockTransport(handler))
    app.conversation.background_provider = provider
    guard = app.guard
    assert await guard.model_available(provider) is False
    ok, reason = await guard.background_allowed(provider)
    assert not ok and "unavailable or unloaded" in reason
    await provider.close()


async def test_loaded_model_passes_guard(make_app):
    from aurora.providers.openai_compat import OpenAICompatProvider

    app, _ = make_app()
    provider = OpenAICompatProvider("http://127.0.0.1:1234/v1", model="aurora-chat")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "aurora-chat"}]})

    provider._client = httpx.AsyncClient(
        base_url=provider.base_url,
        transport=httpx.MockTransport(handler))
    try:
        assert await app.guard.model_available(provider) is True
    finally:
        await provider.close()
