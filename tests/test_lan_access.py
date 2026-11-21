"""LAN accessibility controls: token-gated API, public dashboard page,
SSE query-token support. Binding and network reachability themselves are
deployment concerns (see docs/PORTABLE_DEPLOY.md) and are not asserted
in the sandbox."""

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request as StarletteRequest

from aurora.server.app import _request_authorized
from tests.conftest import standard_rules

TOKEN = "aurora-test-token-0123456789"


def _request(path: str, headers: dict | None = None, query: str = "") -> StarletteRequest:
    scope = {"type": "http", "method": "GET", "path": path,
             "query_string": query.encode(),
             "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]}
    return StarletteRequest(scope)


@pytest.fixture
def guarded_client(make_app):
    app, _ = make_app()
    app.config.set("server.auth_token", TOKEN)
    return TestClient(create_server(app))


@pytest.fixture
def open_client(make_app):
    app, _ = make_app()
    return TestClient(create_server(app))


def create_server(app):
    from aurora.server.app import create_server as _cs
    return _cs(app)


def test_open_when_no_token_configured(open_client):
    assert open_client.get("/api/status").status_code == 200


def test_api_requires_token(guarded_client):
    r = guarded_client.get("/api/status")
    assert r.status_code == 401
    assert "token" in r.json()["detail"].lower()


def test_api_accepts_bearer_header(guarded_client):
    r = guarded_client.get("/api/status", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200


def test_api_accepts_custom_header(guarded_client):
    r = guarded_client.get("/api/status", headers={"X-Aurora-Token": TOKEN})
    assert r.status_code == 200


def test_wrong_token_rejected(guarded_client):
    assert guarded_client.get("/api/status",
                              headers={"X-Aurora-Token": "wrong"}).status_code == 401


def test_static_page_public_but_api_gated(guarded_client):
    """The dashboard HTML stays reachable so the browser can prompt for the
    token; every /api/* call stays gated."""
    assert guarded_client.get("/").status_code == 200
    assert guarded_client.get("/api/memory/facts").status_code == 401


def test_chat_post_gated(guarded_client):
    r = guarded_client.post("/api/chat", json={"message": "hi"})
    assert r.status_code == 401
    r = guarded_client.post("/api/chat", json={"message": "hi"},
                            headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200


def test_query_token_only_for_event_stream():
    """The SSE endpoint accepts ?token= (EventSource cannot set headers);
    other endpoints must not be unlocked by a query parameter."""
    assert _authorized_on("/api/events", query_token=TOKEN) is True
    assert _authorized_on("/api/events", query_token="nope") is False
    assert _authorized_on("/api/events") is False
    assert _authorized_on("/api/status", query_token=TOKEN) is False


def _authorized_on(path: str, query_token: str | None = None) -> bool:
    request = _request(path, query=f"token={query_token}" if query_token else "")
    return _request_authorized(request, TOKEN)


def test_header_token_on_other_endpoints():
    req = _request("/api/status", headers={"Authorization": f"Bearer {TOKEN}"})
    assert _request_authorized(req, TOKEN) is True
    req = _request("/api/status", headers={"X-Aurora-Token": TOKEN})
    assert _request_authorized(req, TOKEN) is True
    req = _request("/api/status")
    assert _request_authorized(req, TOKEN) is False
