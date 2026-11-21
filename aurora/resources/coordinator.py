"""Resource coordination — Aurora yields compute gracefully.

Two pieces:

* :class:`ResourceCoordinator` — an optional, read-only client for the
  host's existing GPU Traffic Light service (port 8791). It only ever
  issues GET probes; it never unloads, terminates, reconfigures or
  restarts another application's models. Because the traffic light's
  exact protocol must be verified on the target machine first, the
  client is generic: it probes ``/health``, ``/status`` and ``/`` and
  applies configurable busy-marker substrings to the response. If the
  probe cannot be understood or the service is down, the state is
  ``unknown`` and Aurora deliberately does NOT block itself (a broken
  traffic light must not take the companion down).
* :class:`ComputeGuard` — Aurora's own gate for nonessential background
  AI work (mood appraisal, memory extraction, persona evolution,
  consolidation, rolling summaries, proactive initiations). A background
  task runs only when ALL of these hold:
    1. ``resources.background_enabled`` (config kill-switch),
    2. the model endpoint is healthy and the configured model is
       actually loaded (cheap cached probe),
    3. no foreground inference is in flight or within the foreground
       grace window (user chat always wins),
    4. the traffic light, when configured, does not report busy.

  Foreground chat never consults the guard: when the model is down the
  conversation manager replies with an honest degraded message while
  reminders, memory and the dashboard keep working.
"""

from __future__ import annotations

import httpx

DEFAULT_BUSY_MARKERS = ["busy", "occupied", "loading", "inferencing"]
_PROBE_PATHS = ("/health", "/status", "/")


def _flatten(value, depth: int = 0) -> str:
    """Serialize an arbitrary JSON structure into one lowercase string for
    marker matching (keys AND values, so {"state":"busy"} matches 'busy')."""
    if depth > 4:
        return ""
    if isinstance(value, dict):
        return " ".join(
            f"{_flatten(k, depth + 1)} {_flatten(v, depth + 1)}"
            for k, v in value.items()
        )
    if isinstance(value, (list, tuple)):
        return " ".join(_flatten(v, depth + 1) for v in value)
    return str(value).lower()


class ResourceCoordinator:
    """Read-only probe of the existing GPU Traffic Light (optional)."""

    name = "traffic_light"

    def __init__(self, config, clock=None, timeout: float = 3.0) -> None:
        self.url = str(config.get("resources.traffic_light_url", "") or "").strip().rstrip("/")
        self.probe_seconds = float(config.get("resources.probe_seconds", 30))
        markers = config.get("resources.busy_markers", DEFAULT_BUSY_MARKERS) or []
        self.busy_markers = [str(m).lower() for m in markers]
        self._clock = clock
        self._client = httpx.AsyncClient(timeout=timeout) if self.url else None
        self._cache: dict = {
            "state": "disabled" if not self.url else "unknown",
            "ts": 0.0,
            "detail": {"url": self.url} if self.url else {"url": ""},
        }

    def _now(self) -> float:
        return self._clock.now() if self._clock is not None else 0.0

    async def probe(self, force: bool = False) -> dict:
        """Return {"state": disabled|free|busy|unknown, "ts", "detail"}."""
        if self._client is None:
            return self._cache
        now = self._now()
        if not force and now - self._cache.get("ts", 0.0) < self.probe_seconds \
                and self._cache.get("state") != "unknown":
            return self._cache
        result: dict = {"state": "unknown", "ts": now,
                        "detail": {"url": self.url, "path": None, "keys": None}}
        for path in _PROBE_PATHS:
            try:
                resp = await self._client.get(self.url + path, follow_redirects=True)
            except httpx.HTTPError:
                continue
            if resp.status_code >= 500:
                continue
            flat: str
            keys = None
            try:
                data = resp.json()
                flat = _flatten(data)
                if isinstance(data, dict):
                    keys = sorted(str(k) for k in data.keys())[:16]
            except ValueError:
                flat = resp.text.lower()[:4000]
            busy = any(marker in flat for marker in self.busy_markers)
            result = {"state": "busy" if busy else "free", "ts": now,
                      "detail": {"url": self.url, "path": path, "keys": keys}}
            break
        self._cache = result
        return result

    async def is_busy(self) -> bool:
        """True only when the light *positively* reports busy; unknown/down
        returns False so a broken coordinator never blocks Aurora."""
        return (await self.probe()).get("state") == "busy"

    def snapshot(self) -> dict:
        return dict(self._cache)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()


class ComputeGuard:
    """Gate for nonessential background AI tasks (see module docstring)."""

    def __init__(self, config, clock, coordinator: ResourceCoordinator | None = None) -> None:
        self.config = config
        self.clock = clock
        self.coordinator = coordinator
        self._model_ok: bool | None = None
        self._model_checked_at: float = -1e18
        self._foreground_until: float = 0.0
        self.last_block: dict | None = None

    # -- model health ---------------------------------------------------------
    async def model_available(self, provider, ttl: float | None = None) -> bool:
        """Cached health probe; also detects 'server up but model not loaded'
        when the provider can answer that (e.g. /v1/models listing)."""
        if provider is None:
            return False
        ttl = float(ttl if ttl is not None else
                    self.config.get("resources.model_health_ttl_seconds", 60))
        now = self.clock.now()
        if self._model_ok is not None and now - self._model_checked_at < ttl:
            return self._model_ok
        ok = False
        try:
            ok = bool(await provider.healthcheck())
        except Exception:
            ok = False
        if ok:
            loaded = getattr(provider, "model_loaded", None)
            if callable(loaded):
                try:
                    if await loaded() is False:
                        ok = False
                except Exception:
                    pass
        self._model_ok = ok
        self._model_checked_at = now
        return ok

    def note_model_down(self) -> None:
        """Called when a foreground call fails so background work backs off
        immediately instead of waiting for the next probe."""
        self._model_ok = False
        self._model_checked_at = self.clock.now()

    # -- foreground tracking ----------------------------------------------------
    def note_foreground(self, seconds: float | None = None) -> None:
        """Foreground inference started (or finished): hold background tasks
        off for a grace window so the user's turn keeps the machine."""
        if seconds is None:
            seconds = float(self.config.get("resources.foreground_grace_seconds", 120))
        self._foreground_until = max(self._foreground_until, self.clock.now() + seconds)

    # -- the gate ---------------------------------------------------------------
    async def background_allowed(self, provider=None, check_foreground: bool = True) -> tuple[bool, str]:
        """(allowed, reason). `check_foreground=False` is for the post-turn
        hooks: they belong to the user's own turn (mood appraisal, extraction,
        evolution) and must not be deferred by the foreground grace window —
        they only yield to model health and external compute pressure."""
        if not bool(self.config.get("resources.background_enabled", True)):
            return False, "background AI disabled (resources.background_enabled)"
        if check_foreground and self.clock.now() < self._foreground_until:
            return False, "foreground inference active"
        if provider is not None and not await self.model_available(provider):
            return False, "model unavailable or unloaded"
        if self.coordinator is not None:
            busy = await self.coordinator.is_busy()
            if busy:
                return False, "traffic light reports busy"
        return True, ""

    def snapshot(self) -> dict:
        return {
            "background_enabled": bool(self.config.get("resources.background_enabled", True)),
            "model_available": self._model_ok,
            "model_checked_at": self._model_checked_at,
            "foreground_until": self._foreground_until,
            "foreground_now": self.clock.now() < self._foreground_until,
            "coordinator": self.coordinator.snapshot() if self.coordinator else None,
            "last_block": self.last_block,
        }

    async def close(self) -> None:
        if self.coordinator is not None:
            await self.coordinator.close()
