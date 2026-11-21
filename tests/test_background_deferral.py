"""Resource-aware continuous operation: ComputeGuard rules, traffic-light
coordination (mocked), and deferral of proactive/consolidation/post-turn
background AI when compute is needed elsewhere."""

import httpx
import pytest

from aurora.config import Config
from aurora.resources import ComputeGuard, ResourceCoordinator
from aurora.clock import Clock
from tests.conftest import T0, mock_provider, standard_rules

TL = "http://127.0.0.1:8791"  # documented GPU Traffic Light port (mocked here)


def _coordinator(state: str, busy_markers=None) -> ResourceCoordinator:
    def handler(request: httpx.Request) -> httpx.Response:
        if state == "down":
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json={"state": state, "jobs": 1 if state == "busy" else 0})

    cfg = Config({"resources": {"traffic_light_url": TL,
                                "probe_seconds": 0.0,
                                "busy_markers": busy_markers or ["busy", "occupied", "loading", "inferencing"]}})
    coord = ResourceCoordinator(cfg, Clock(T0))
    coord._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return coord


def _guard(state="free", markers=None, overrides=None) -> ComputeGuard:
    cfg = Config({"resources": {"traffic_light_url": TL, "probe_seconds": 0.0,
                                "busy_markers": markers or ["busy", "occupied", "loading", "inferencing"]}})
    if overrides:
        for k, v in overrides.items():
            cfg.set(k, v)
    return ComputeGuard(cfg, Clock(T0), coordinator=_coordinator(state, markers))


async def test_coordinator_detects_busy():
    snap = await _coordinator("busy").probe(force=True)
    assert snap["state"] == "busy"
    assert (await _coordinator("busy").is_busy()) is True


async def test_coordinator_detects_free():
    snap = await _coordinator("free").probe(force=True)
    assert snap["state"] == "free"


async def test_coordinator_down_is_unknown_not_busy():
    """A broken traffic light must never block Aurora (fail-open)."""
    snap = await _coordinator("down").probe(force=True)
    assert snap["state"] == "unknown"
    assert (await _coordinator("down").is_busy()) is False


async def test_coordinator_disabled_when_unconfigured():
    cfg = Config({"resources": {"traffic_light_url": ""}})
    coord = ResourceCoordinator(cfg, Clock(T0))
    assert (await coord.probe())["state"] == "disabled"
    assert (await coord.is_busy()) is False


async def test_coordinator_never_writes():
    """The coordinator only ever issues GET requests."""
    methods = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(200, json={"state": "free"})

    cfg = Config({"resources": {"traffic_light_url": TL}})
    coord = ResourceCoordinator(cfg, Clock(T0))
    coord._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await coord.probe(force=True)
    assert methods and all(m == "GET" for m in methods)


async def test_guard_blocks_when_traffic_light_busy():
    ok, reason = await _guard("busy").background_allowed(mock_provider())
    assert not ok and "traffic light reports busy" in reason


async def test_guard_allows_when_free_and_model_healthy():
    ok, reason = await _guard("free").background_allowed(mock_provider())
    assert ok and reason == ""


async def test_guard_blocks_when_model_unhealthy():
    class Sick:
        async def healthcheck(self):
            return False

    ok, reason = await _guard("free").background_allowed(Sick())
    assert not ok and "model unavailable" in reason


async def test_guard_blocks_on_foreground_grace():
    guard = _guard("free")
    provider = mock_provider()
    guard.note_foreground(seconds=3600)
    ok, reason = await guard.background_allowed(provider)  # full check
    assert not ok and "foreground" in reason
    # post-turn hooks skip the foreground window but still respect the light
    ok2, reason2 = await guard.background_allowed(provider, check_foreground=False)
    assert ok2


async def test_guard_respects_kill_switch():
    guard = _guard("free", overrides={"resources.background_enabled": False})
    ok, reason = await guard.background_allowed(mock_provider())
    assert not ok and "disabled" in reason


async def test_guard_model_health_cached_by_ttl():
    guard = _guard("free")
    provider = mock_provider()
    guard.config.set("resources.model_health_ttl_seconds", 600)
    clock = guard.clock
    assert await guard.model_available(provider) is True
    provider_down_called = {"n": 0}

    class Flaky:
        async def healthcheck(self):
            provider_down_called["n"] += 1
            return False

    # within TTL the cached True is reused, no re-probe
    assert await guard.model_available(Flaky()) is True
    assert provider_down_called["n"] == 0
    clock.advance(601)
    assert await guard.model_available(Flaky()) is False  # TTL expired: re-probed
    assert provider_down_called["n"] == 1


async def test_proactive_deferred_when_compute_busy(make_app):
    app, mock = make_app(overrides={
        "resources.traffic_light_url": TL,
        "resources.probe_seconds": 0.0,
        # keep only topic-free kinds eligible so the decision is deterministic
        "proactive.topic_share.enabled": False,
        "proactive.memory_resurfacing.enabled": False,
        "proactive.check_in.after_silence_minutes": 30,
    })
    # point the app's coordinator at a mocked busy light
    app.coordinator = _coordinator("busy")
    app.guard.coordinator = app.coordinator
    pro = app.proactive
    pro._last_user_message = pro.clock.now() - 3600  # silence gate satisfied
    decision = await pro.tick()
    assert decision.get("deferred") is True and decision["speak"] is False
    assert "traffic light" in decision["reason"]
    # nothing was initiated: no proactive log rows, no daily counters burned
    rows = app.db.query("SELECT * FROM proactive_log")
    assert rows == []
    state = app.db.get_meta_json("proactive.state", {})
    assert not state.get("day_total")

    # when the light goes free, the same situation proceeds
    app.coordinator = _coordinator("free")
    app.guard.coordinator = app.coordinator
    decision = await pro.tick()
    assert decision.get("speak") is True


async def test_proactive_not_deferred_on_manual_tick(make_app):
    """force=True (dashboard 'run one tick now') bypasses the guard."""
    app, mock = make_app(overrides={
        "resources.traffic_light_url": TL,
        "proactive.topic_share.enabled": False,
        "proactive.memory_resurfacing.enabled": False,
        "proactive.check_in.after_silence_minutes": 30,
    })
    app.coordinator = _coordinator("busy")
    app.guard.coordinator = app.coordinator
    pro = app.proactive
    pro._last_user_message = pro.clock.now() - 3600
    decision = await pro.tick(force=True)
    assert decision.get("speak") is True


async def test_consolidation_deferred_when_compute_busy(make_app):
    app, mock = make_app(overrides={"memory.consolidation_idle_minutes": 0,
                                    "resources.traffic_light_url": TL})
    app.coordinator = _coordinator("busy")
    app.guard.coordinator = app.coordinator
    result = await app.consolidator.tick()
    assert result is None  # nothing ran
    assert app.consolidator.last_run_result.get("deferred")
    assert "traffic light" in app.consolidator.last_defer_reason

    app.coordinator = _coordinator("free")
    app.guard.coordinator = app.coordinator
    result = await app.consolidator.tick()
    assert result is not None and "deferred" not in result


async def test_background_disabled_stops_proactive_entirely(make_app):
    app, mock = make_app(overrides={"resources.background_enabled": False,
                                    "resources.traffic_light_url": ""})
    pro = app.proactive
    pro._last_user_message = pro.clock.now() - 3600
    decision = await pro.tick()
    assert decision.get("deferred") is True
    assert "disabled" in decision["reason"]
