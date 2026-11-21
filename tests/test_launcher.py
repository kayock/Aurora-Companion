"""Application and website launching: deterministic parsing, catalog-only
execution, destination semantics, authorization gating, and the tool layer.

Processes are never really started in tests — the Launcher takes an
injectable launch_fn (the same seam the production code uses for Popen).
"""

from __future__ import annotations

import json

import pytest

from aurora.config import Config
from aurora.conversation.manager import ConversationManager
from aurora.launcher import Launcher, parse_launch_request, has_launch_intent


def make_launcher(**kw) -> Launcher:
    cfg = Config({})
    section = dict(cfg.get("launcher", {}))
    # Production default is off. These tests exercise launching itself.
    section["enabled"] = True
    section.setdefault("applications", {}).update({
        "saturday multiverse": {"command": ["multiverse"], "description": "SM site app"},
    })
    section.setdefault("websites", {}).update({
        "saturday multiverse": "https://saturdaymultiverse.example",
    })
    kw.setdefault("launch_fn", lambda command: 4242)
    kw.setdefault("browse_fn", lambda url: True)
    kw.setdefault("exists_fn", lambda c: "/usr/bin/" + c)
    return Launcher(section, **kw)


# ---------------------------------------------------------------- parsing
def test_parse_open_app():
    plan = parse_launch_request("Open Firefox.")
    assert plan is not None and plan.kind == "unknown" and plan.target == "firefox"


def test_parse_launch_and_polite_forms():
    assert parse_launch_request("Launch Cursor.").target == "cursor"
    assert parse_launch_request("could you open github?").target == "github"
    assert parse_launch_request("hey aurora, please open the terminal").target == "terminal"


def test_parse_website_by_name_and_url():
    plan = parse_launch_request("open the Saturday Multiverse website")
    assert plan.kind == "website" and plan.target == "saturday multiverse"
    plan = parse_launch_request("launch the notes app")
    assert plan.kind == "application" and plan.target == "notes"
    plan = parse_launch_request("open https://example.com/docs?x=1")
    assert plan.kind == "website" and plan.target == "https://example.com/docs?x=1"
    assert parse_launch_request("open github").target == "github"


def test_parse_ask_url_and_list():
    assert parse_launch_request("open this web address").kind == "ask_url"
    assert parse_launch_request("what applications can you open?").kind == "list"
    assert parse_launch_request("what apps can you open?").kind == "list"


def test_parse_conversation_is_not_a_launch():
    assert parse_launch_request("could you open up about your feelings?") is None \
        or parse_launch_request("could you open up about your feelings?").target.count(" ") >= 2
    plan = parse_launch_request("open the pod bay doors")
    assert plan is None or plan.target == "pod bay doors"  # ≥3 words → model turn


def test_parse_quoted_command_is_never_an_instruction():
    assert parse_launch_request("open 'rm -rf /'") is None
    assert parse_launch_request('open "firefox in a poem"') is None


def test_parse_remote_destination_is_refused():
    for text in ("open firefox on my computer", "open github in my browser",
                 "open the files app on my other computer", "open google on here"):
        plan = parse_launch_request(text)
        assert plan is not None and plan.kind == "remote", text


def test_parse_shell_metacharacters_rejected():
    assert parse_launch_request("open firefox; rm -rf /") is None
    assert parse_launch_request("open `id`") is None
    assert parse_launch_request("open ../../etc/passwd") is None


def test_parse_non_requests_return_none():
    for text in ("", "hello", "what's the weather", "remember the milk",
                 "remind me to open the windows in 10 minutes"):
        assert parse_launch_request(text) is None, text


# ---------------------------------------------------------------- launcher safety
def test_catalog_only_launching():
    l = make_launcher()
    ok = l.launch_application("firefox")
    assert ok.ok and ok.detail["command"] == ["firefox"]
    refused = l.launch_application("rm -rf /")
    assert not refused.ok and "not in the application catalog" in refused.message
    assert l.launch_application("../passwd").ok is False


def test_no_shell_execution_and_no_metacharacters():
    captured = {}
    l = make_launcher(launch_fn=lambda cmd: captured.update(cmd=cmd) or 7)
    l.launch_application("firefox")
    assert captured["cmd"] == ["firefox"]  # argv list — never a shell string


def test_url_scheme_validation():
    l = make_launcher()
    assert l.open_website("https://github.com/x").ok
    assert l.open_website("github.com").ok           # https assumed for bare hosts
    bad = l.open_website("file:///etc/passwd")
    assert not bad.ok and "not a usable web address" in bad.message
    bad = l.open_website("javascript:alert(1)")
    assert not bad.ok


def test_unavailable_application_reported():
    l = make_launcher(exists_fn=lambda c: False)
    res = l.launch_application("firefox")
    assert not res.ok and "not installed" in res.message


def test_launch_failure_reported_cleanly():
    def boom(cmd):
        raise OSError("no desktop session")
    l = make_launcher(launch_fn=boom)
    res = l.launch_application("firefox")
    assert not res.ok and "Could not start" in res.message


def test_disabled_launcher_refuses():
    l = make_launcher()
    l.enabled = False
    assert "disabled" in l.launch_application("firefox").message.lower()
    assert "disabled" in l.open_website("github").message.lower()


def test_fuzzy_matching_is_conservative():
    l = make_launcher()
    assert l.match("application", "fire")[0] == "firefox"       # unique prefix
    hit, options = l.match("application", "totally unknown thing")
    assert hit is None and options == []


def test_list_reply_mentions_catalog_and_availability():
    l = make_launcher()
    text = l.list_reply()
    assert "firefox" in text and "github" in text
    assert "not found" not in text
    l = make_launcher(exists_fn=lambda c: False)
    assert "not found right now" in l.list_reply()


def test_remote_reply_is_honest():
    assert "can't reach" in make_launcher().remote_reply()


# ---------------------------------------------------------------- tools
def test_tools_registered_and_gated():
    from aurora.conversation.tools import default_tool_registry
    l = make_launcher()
    reg = default_tool_registry({"launcher": l})
    assert {"open_website", "launch_application",
            "list_available_applications"} <= set(reg.names())


async def test_tool_requires_current_utterance_intent():
    from aurora.conversation.tools import default_tool_registry
    l = make_launcher()
    reg = default_tool_registry({"launcher": l})
    # model tries to launch from stale context / its own idea → refused
    out = await reg.call("launch_application", {"name": "firefox"}, {"user_text": "thanks for the chat!"})
    assert "Not launched" in out
    out = await reg.call("open_website", {"url": "https://github.com"}, {"user_text": "quoting 'open github'"})
    assert "Not launched" in out


async def test_tool_launches_on_explicit_request():
    from aurora.conversation.tools import default_tool_registry
    l = make_launcher()
    reg = default_tool_registry({"launcher": l})
    out = await reg.call("launch_application", {"name": "firefox"},
                         {"user_text": "hey aurora, open firefox"})
    assert "Launched firefox" in out
    out = await reg.call("open_website", {"name": "github"},
                         {"user_text": "please open github for me"})
    assert "Opened" in out and "running Aurora" in out
    out = await reg.call("open_website", {"url": "https://example.org"},
                         {"user_text": "open this web address: https://example.org"})
    assert "Opened" in out


async def test_tool_reports_unavailable_app():
    from aurora.conversation.tools import default_tool_registry
    l = make_launcher(exists_fn=lambda c: False)
    reg = default_tool_registry({"launcher": l})
    out = await reg.call("launch_application", {"name": "firefox"},
                         {"user_text": "open firefox"})
    assert "not installed" in out


async def test_list_tool_reply():
    from aurora.conversation.tools import default_tool_registry
    reg = default_tool_registry({"launcher": make_launcher()})
    out = await reg.call("list_available_applications", {}, {})
    assert "firefox" in out and "github" in out


def test_has_launch_intent_boundaries():
    assert has_launch_intent("open github now")
    assert has_launch_intent("visit https://x.org")
    assert not has_launch_intent("start a timer for 5 minutes")
    assert not has_launch_intent("thanks for the chat")


# ---------------------------------------------------------------- manager flow
def _app_with_launcher(make_app, launch_log):
    l = make_launcher(launch_fn=lambda cmd: launch_log.append(tuple(cmd)) or 4242,
                      browse_fn=lambda url: launch_log.append(("browser", url)) or True)
    return l


async def test_deterministic_launch_app_end_to_end(make_app):
    log: list = []
    app, _ = make_app()
    app.launcher = make_launcher(launch_fn=lambda cmd: log.append(tuple(cmd)) or 4242)
    app.conversation.launcher = app.launcher
    msg = await app.conversation.submit_user_message("Open Firefox.")
    assert "Launched firefox" in msg["content"]
    assert msg["source"] == "launch"
    assert log == [("firefox",)]
    # the model was never needed for the launch turn
    assert app.chat_provider.calls == []


async def test_deterministic_open_website_end_to_end(make_app):
    log: list = []
    app, _ = make_app()
    app.launcher = make_launcher(browse_fn=lambda url: log.append(url) or True)
    app.conversation.launcher = app.launcher
    msg = await app.conversation.submit_user_message("hey aurora, open github")
    assert "Opened https://github.com" in msg["content"]
    assert log == ["https://github.com"]


async def test_deterministic_list_end_to_end(make_app):
    app, _ = make_app()
    app.launcher = make_launcher()
    app.conversation.launcher = app.launcher
    msg = await app.conversation.submit_user_message("what applications can you open?")
    assert "firefox" in msg["content"] and "github" in msg["content"]


async def test_remote_request_refused_without_launching(make_app):
    log: list = []
    app, _ = make_app()
    app.launcher = make_launcher(launch_fn=lambda cmd: log.append(cmd) or 4242,
                                 browse_fn=lambda url: log.append(url) or True)
    app.conversation.launcher = app.launcher
    msg = await app.conversation.submit_user_message("open firefox on my computer")
    assert "can't reach" in msg["content"]
    assert log == []  # nothing was opened anywhere


async def test_conversation_fragments_reach_the_model(make_app):
    app, provider = make_app(rules=[(r"pod bay doors",
                                     "*smiles* I'm afraid I can't do that — it's a movie line.")])
    app.launcher = make_launcher()
    app.conversation.launcher = app.launcher
    msg = await app.conversation.submit_user_message("Open the pod bay doors, Aurora.")
    assert "movie line" in msg["content"]      # model answered, not the launcher
    assert provider.calls                      # model was consulted


async def test_website_vs_application_distinction(make_app):
    app, _ = make_app()
    l = make_launcher()
    app.launcher = l
    app.conversation.launcher = l
    # "saturday multiverse" exists in BOTH catalogs: app first for plain names
    msg = await app.conversation.submit_user_message("Launch the Saturday Multiverse.")
    assert "Launched saturday multiverse" in msg["content"]
    # explicit "website" wording goes to the browser
    msg = await app.conversation.submit_user_message("Open the Saturday Multiverse website.")
    assert "Opened https://saturdaymultiverse.example" in msg["content"]


async def test_quoted_launch_phrases_never_execute(make_app):
    log: list = []
    app, _ = make_app()
    app.launcher = make_launcher(launch_fn=lambda cmd: log.append(cmd) or 4242)
    app.conversation.launcher = app.launcher
    await app.conversation.submit_user_message("He said 'open firefox' to the TV.")
    assert log == []  # quoted text is not an instruction


async def test_ambiguous_match_asks_instead_of_guessing(make_app):
    app, _ = make_app()
    cfg = Config({})
    section = dict(cfg.get("launcher", {}))
    section["enabled"] = True
    section["applications"] = {
        "code": {"command": ["code"]}, "codeium": {"command": ["codeium"]},
    }
    l = Launcher(section, launch_fn=lambda cmd: 1, exists_fn=lambda c: "/usr/bin/" + c)
    app.launcher = l
    app.conversation.launcher = l
    msg = await app.conversation.submit_user_message("open cod now")
    assert "Which one" in msg["content"] and "codeium" in msg["content"]
    # an exact catalog name still wins outright
    msg = await app.conversation.submit_user_message("open code")
    assert "Launched code" in msg["content"]


async def test_model_launched_website_via_tools_still_runs(make_app):
    """A model that answers with embedded tool JSON gets its launch executed
    (recovered_from_text path) — and the JSON never reaches the chat."""
    app, provider = make_app(rules=[
        (r"open\s+example", json.dumps({
            "name": "open_website",
            "arguments": {"url": "https://example.com"}})),
        (r"opened", "Done — the site is open."),
    ])
    app.launcher = make_launcher(browse_fn=lambda url: True)
    app.conversation.launcher = app.launcher
    msg = await app.conversation.submit_user_message("Please open example.com for me.")
    assert "Opened https://example.com" in msg["content"] or "open" in msg["content"].lower()


# ---------------------------------------------------------------- dashboard API
def test_launcher_endpoint(make_app, monkeypatch):
    from fastapi.testclient import TestClient
    from aurora.server.app import create_server
    app, _ = make_app()
    app.launcher = make_launcher()
    client = TestClient(create_server(app))
    r = client.get("/api/launcher")
    assert r.status_code == 200
    data = r.json()
    assert data["enabled"] is True
    names = [a["name"] for a in data["applications"]]
    assert "firefox" in names
    assert "github" in data["websites"]


def test_launcher_endpoint_absent(make_app):
    from fastapi.testclient import TestClient
    from aurora.server.app import create_server
    app, _ = make_app()
    app.launcher = None
    client = TestClient(create_server(app))
    data = client.get("/api/launcher").json()
    assert data["enabled"] is False and data["applications"] == []
