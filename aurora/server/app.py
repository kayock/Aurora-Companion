"""FastAPI server: REST + SSE for the dashboard.

One app, one conversation, one store. Every dashboard panel (chat,
memory, reminders, personality, proactivity, permissions, people,
settings) reads/writes the same subsystems the background services use —
no parallel state.

LAN access: when `server.auth_token` is set, every /api/* call must
present it (Authorization: Bearer, X-Aurora-Token header, or ?token= for
the SSE stream); the static dashboard page stays public so the browser
can prompt for the token. Bind `host` to 0.0.0.0 for LAN access and keep
the token on — never port-forward Aurora to the internet.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, Response

from .. import __version__
from ..app import CompanionApp
from ..reminders.store import ACTIVE_STATUSES

STATIC_DIR = Path(__file__).parent / "static"


def _request_authorized(request: Request, token: str) -> bool:
    """Token check extracted for direct unit testing. Static pages are not
    token-gated; every /api/* call is. A query parameter is accepted only
    for the SSE stream (EventSource cannot set headers)."""
    supplied = None
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        supplied = auth[7:].strip()
    elif request.headers.get("x-aurora-token"):
        supplied = request.headers.get("x-aurora-token").strip()
    elif request.query_params.get("token") and request.url.path == "/api/events":
        supplied = request.query_params.get("token").strip()
    if supplied is None:
        return False
    return secrets.compare_digest(supplied, token)


def create_server(app: CompanionApp) -> FastAPI:
    api = FastAPI(title="Aurora", version=__version__)

    # ------------------------------------------------------------ access control
    @api.middleware("http")
    async def auth_middleware(request: Request, call_next):
        token = str(app.config.get("server.auth_token", "") or "")
        if token and request.url.path.startswith("/api/") \
                and not _request_authorized(request, token):
            return JSONResponse({"detail": "unauthorized: access token required"},
                                status_code=401)
        return await call_next(request)

    # ------------------------------------------------------------ static + events
    @api.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    @api.get("/api/events")
    async def events():
        queue = app.conversation.subscribe()

        async def gen():
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                app.conversation.unsubscribe(queue)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    # ------------------------------------------------------------ chat
    @api.post("/api/chat")
    async def chat(request: Request):
        body = await request.json()
        text = str(body.get("message", "")).strip()
        if not text:
            raise HTTPException(400, "message is required")
        person = str(body.get("person", "owner"))
        try:
            msg = await app.conversation.submit_user_message(text, person=person)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return JSONResponse(msg)

    @api.get("/api/history")
    async def history(limit: int = 60, before: int | None = None,
                      include_archived: bool = False):
        return {"messages": app.conversation.history(
            limit=limit, before_id=before, include_archived=include_archived,
        )}

    @api.get("/api/search")
    async def search(q: str, include_archived: bool = False):
        sql = (
            "SELECT id, role, content, ts, person, source, "
            "COALESCE(archived,0) AS archived FROM messages "
            "WHERE content LIKE ?"
        )
        params: list = [f"%{q}%"]
        if not include_archived:
            sql += " AND COALESCE(archived,0)=0"
        sql += " ORDER BY id DESC LIMIT 50"
        rows = app.db.query(sql, tuple(params))
        return {"messages": [dict(r) for r in rows]}

    @api.delete("/api/history")
    async def clear_history(all: bool = False):
        if all:
            app.db.execute("DELETE FROM messages")
            app.db.execute("DELETE FROM meta WHERE key IN ('conversation.summary','conversation.summary_until_id')")
        else:
            last = app.db.query_one("SELECT MAX(id) AS m FROM messages")
            if last and last["m"]:
                recent = app.db.query("SELECT id FROM messages ORDER BY id DESC LIMIT 2")
                keep_min = recent[-1]["id"] if recent else 0
                app.db.execute("DELETE FROM messages WHERE id >= ?", (keep_min,))
        return {"ok": True}

    # ------------------------------------------------------------ memory
    @api.get("/api/memory/facts")
    async def facts(view: str = "active", q: str = "", subject: str | None = None):
        if q:
            hits = app.store.facts_fts(q, subject=subject, limit=100)
            return {"facts": [f.to_dict() for f in hits]}
        if view == "active":
            items = app.store.active_facts(subject)
        elif view == "archived":
            rows = app.db.query("SELECT * FROM facts WHERE archived=1 OR superseded_by IS NOT NULL "
                                "ORDER BY updated_at DESC LIMIT 200")
            items = [app.store.get_fact(r["id"]) for r in rows]
            items = [f for f in items if f is not None]
        else:
            rows = app.db.query("SELECT * FROM facts ORDER BY updated_at DESC LIMIT 500")
            items = [app.store.get_fact(r["id"]) for r in rows]
            items = [f for f in items if f is not None]
        return {"facts": [f.to_dict() for f in items]}

    @api.post("/api/memory/facts")
    async def add_fact(request: Request):
        body = await request.json()
        try:
            fact = app.store.add_fact(
                str(body.get("content", "")), subject=str(body.get("subject", "owner")),
                category=str(body.get("category", "other")), source="manual",
                pinned=bool(body.get("pinned", False)),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except Exception as exc:
            raise HTTPException(409, str(exc))
        return fact.to_dict()

    @api.put("/api/memory/facts/{fact_id}")
    async def edit_fact(fact_id: str, request: Request):
        body = await request.json()
        fact = app.store.get_fact(fact_id)
        if fact is None:
            raise HTTPException(404, "fact not found")
        if "content" in body:
            fact = app.store.update_fact(fact_id, str(body["content"]))
        if "pinned" in body:
            fact = app.store.set_pinned(fact_id, bool(body["pinned"]))
        return fact.to_dict()

    @api.post("/api/memory/facts/{fact_id}/archive")
    async def archive_fact(fact_id: str):
        try:
            fact = app.store.archive_fact(fact_id, "user_request")
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        return fact.to_dict()

    @api.post("/api/memory/facts/{fact_id}/restore")
    async def restore_fact(fact_id: str):
        fact = app.store.restore_fact(fact_id)
        if fact is None:
            raise HTTPException(404, "fact not found")
        return fact.to_dict()

    @api.delete("/api/memory/facts/{fact_id}")
    async def delete_fact(fact_id: str):
        fact = app.store.get_fact(fact_id)
        if fact is None:
            raise HTTPException(404, "fact not found")
        app.store.delete_fact(fact_id)
        return {"ok": True}

    @api.get("/api/memory/export")
    async def export_memory():
        return app.store.export()

    @api.get("/api/memory/changelog")
    async def memory_changelog():
        entries = app.db.get_meta_json("memory.changelog", [])
        actions = app.db.get_meta_json("memory.changelog_actions", [])
        return {"runs": entries, "actions": actions}

    @api.post("/api/memory/changelog/rollback")
    async def memory_rollback(request: Request):
        body = await request.json()
        undone = _changelog_rollback(app, float(body.get("ts", 0)))
        return undone

    # ------------------------------------------------------------ reminders
    @api.get("/api/reminders")
    async def list_reminders(active_only: bool = False, view: str | None = None):
        """List reminders. Prefer `view=active|history|all`. `active_only`
        remains for older clients and maps to view=active."""
        effective = view or ("active" if active_only else "all")
        if effective not in ("active", "history", "all"):
            raise HTTPException(400, "view must be active, history, or all")
        items = app.reminders.list(view=effective)
        return {"reminders": [r.to_dict() for r in items], "view": effective}

    @api.post("/api/reminders")
    async def add_reminder(request: Request):
        from ..timeparse import parse_repeat, parse_when

        body = await request.json()
        text = str(body.get("text", "")).strip()
        when = str(body.get("when", "")).strip()
        if not text:
            raise HTTPException(400, "text is required")
        due = float(body["due_at"]) if body.get("due_at") else parse_when(when, app.clock.now())
        if due is None:
            raise HTTPException(400, f"could not parse time \"{when}\"; try 'in 10 minutes' or 'tomorrow at 9am'")
        kind = str(body.get("kind", ""))
        duration = int(round(due - app.clock.now())) if kind == "timer" else None
        try:
            rem = app.reminders.add(
                text, due,
                repeat=parse_repeat(when) if body.get("repeat") is None else body.get("repeat"),
                follow_up=bool(body.get("follow_up", False)),
                follow_up_seconds=body.get("follow_up_seconds"),
                follow_up_max=body.get("follow_up_max"),
                project=str(body.get("project", "")),
                person=str(body.get("person", "owner")),
                kind=kind, duration=duration,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        app.reminder_scheduler.notify_changed()
        return rem.to_dict()

    @api.post("/api/reminders/{rem_id}/{action}")
    async def reminder_action(rem_id: str, action: str, request: Request):
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        if action == "ack" or action == "done" or action == "complete":
            rem = app.reminders.acknowledge(rem_id)
        elif action == "cancel" or action == "dismiss":
            rem = app.reminders.dismiss(rem_id)
        elif action == "snooze":
            # Prefer bring_back so delivered one-shots (post-fire) can still
            # be deferred from the dashboard; falls back to snooze for active.
            rem = app.reminders.bring_back(rem_id, int(body.get("seconds", 600))) \
                or app.reminders.snooze(rem_id, int(body.get("seconds", 600)))
        elif action == "delete":
            if not app.reminders.delete(rem_id):
                raise HTTPException(404, "reminder not found")
            app.reminder_scheduler.notify_changed()
            return {"ok": True, "deleted": [rem_id]}
        else:
            raise HTTPException(400, f"unknown action {action}")
        if rem is None:
            raise HTTPException(404, "reminder not found")
        app.reminder_scheduler.notify_changed()
        return rem.to_dict()

    @api.delete("/api/reminders/{rem_id}")
    async def delete_reminder(rem_id: str):
        if not app.reminders.delete(rem_id):
            raise HTTPException(404, "reminder not found")
        app.reminder_scheduler.notify_changed()
        return {"ok": True, "deleted": [rem_id]}

    @api.post("/api/reminders/bulk-delete")
    async def bulk_delete_reminders(request: Request):
        body = await request.json()
        ids = body.get("ids") or []
        if not isinstance(ids, list) or not ids:
            raise HTTPException(400, "ids must be a non-empty list")
        if len(ids) > 500:
            raise HTTPException(400, "too many ids (max 500)")
        deleted = app.reminders.delete_many([str(i) for i in ids])
        app.reminder_scheduler.notify_changed()
        return {"ok": True, "deleted_count": deleted}

    @api.post("/api/reminders/clear-history")
    async def clear_reminder_history():
        """Permanently delete delivered/done/cancelled/missed rows only."""
        n = app.reminders.clear_history()
        app.reminder_scheduler.notify_changed()
        return {"ok": True, "deleted_count": n}

    @api.get("/api/reminders/deliveries")
    async def reminder_deliveries():
        return {"deliveries": app.reminder_scheduler.deliveries[-100:]}

    # ------------------------------------------------------------ notifications + timers
    @api.get("/api/alerts")
    async def alerts():
        """Notification bell payload: active reminders + timers awaiting a
        repeat/snooze/dismiss answer."""
        return app.conversation.alerts()

    @api.post("/api/timers/{timer_id}/{action}")
    async def timer_action(timer_id: str, action: str, request: Request):
        """Repeat / snooze / dismiss a fired timer. Timers are never restarted
        automatically — this endpoint is the user's explicit decision."""
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        if action == "repeat":
            result = await app.conversation.timer_repeat(timer_id)
        elif action == "snooze":
            result = await app.conversation.timer_snooze(timer_id, int(body.get("seconds", 300)))
        elif action == "dismiss":
            result = await app.conversation.timer_dismiss(timer_id)
        else:
            raise HTTPException(400, f"unknown timer action {action}")
        if not result.get("ok"):
            raise HTTPException(404, result.get("error", "timer not found"))
        app.reminder_scheduler.notify_changed()
        return result

    # ------------------------------------------------------------ software update
    @api.get("/api/update/status")
    async def update_status():
        return app.updater.public_state()

    @api.post("/api/update/check")
    async def update_check():
        return app.updater.check()

    @api.post("/api/update/apply")
    async def update_apply():
        """Back up, download, verify and install. Requires the user's
        explicit confirmation (dashboard button) — never automatic."""
        try:
            return app.updater.apply()
        except Exception as exc:
            return JSONResponse({"error": str(exc), "installed": False}, status_code=400)

    @api.post("/api/update/restart")
    async def update_restart():
        return app.updater.restart_soon()

    @api.post("/api/update/rollback")
    async def update_rollback():
        try:
            return app.updater.rollback()
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    # ------------------------------------------------------------ personality
    @api.get("/api/persona")
    async def persona():
        return {
            "state": app.persona.state.to_dict(),
            "history": app.persona.history(),
            "evolution_config": app.config.get("personality.evolution", {}),
        }

    @api.post("/api/persona/evolve")
    async def persona_evolve(request: Request):
        body = await request.json()
        facts_text = "\n".join(f"- {f.content}" for f in app.store.active_facts()[:30])
        convo_rows = app.db.query("SELECT role, content FROM messages ORDER BY id DESC LIMIT 30")
        convo_text = "\n".join(f"{r['role']}: {r['content'][:200]}" for r in reversed(convo_rows))
        result = await app.evolution.evolve(
            facts_text, convo_text,
            baseline_review=bool(body.get("baseline_review", False)), force=True,
        )
        return {"fired": result.fired, "version": result.version,
                "change_summary": result.change_summary, "reason": result.reason,
                "skipped_reason": result.skipped_reason}

    @api.post("/api/persona/rollback")
    async def persona_rollback(request: Request):
        body = await request.json()
        ok = app.persona.rollback(int(body.get("version", 0)))
        if not ok:
            raise HTTPException(404, "version not found")
        return app.persona.state.to_dict()

    @api.put("/api/persona")
    async def persona_edit(request: Request):
        body = await request.json()
        app.persona.set_manual(str(body.get("prompt", "")), str(body.get("style", "")))
        return app.persona.state.to_dict()

    @api.get("/api/mood")
    async def mood():
        return app.mood.state()

    # ------------------------------------------------------------ proactive
    @api.get("/api/proactive")
    async def proactive_info():
        state = app.db.get_meta_json("proactive.state", {})
        rows = app.db.query("SELECT * FROM proactive_log ORDER BY id DESC LIMIT 50")
        return {"state": state, "log": [dict(r) for r in rows],
                "last_decision": app.proactive.last_decision,
                "enabled": app.proactive.enabled}

    @api.post("/api/proactive/focus")
    async def focus(request: Request):
        body = await request.json()
        minutes = body.get("minutes")
        until = app.clock.now() + int(minutes) * 60 if minutes else None
        app.proactive.set_focus(until, enabled=bool(body.get("enabled", True)))
        return {"focus": app.proactive.focus_active(), "until": until}

    @api.post("/api/proactive/tick")
    async def proactive_tick():
        decision = await app.proactive.tick()
        return decision

    # ------------------------------------------------------------ permissions / people
    @api.get("/api/permissions")
    async def permissions():
        return app.permissions.snapshot()

    @api.post("/api/permissions")
    async def set_permission(request: Request):
        body = await request.json()
        name, value = str(body.get("name")), bool(body.get("value"))
        try:
            result = app.permissions.set(name, value)
        except KeyError:
            raise HTTPException(400, f"unknown permission {name}")
        return {"name": name, "value": result}

    @api.get("/api/people")
    async def people():
        return {"people": app.people.list()}

    @api.post("/api/people")
    async def add_person(request: Request):
        body = await request.json()
        return app.people.add(str(body.get("name", "")), str(body.get("pronouns", "")))

    @api.post("/api/people/{name}/voiceprint")
    async def voiceprint(name: str, request: Request):
        body = await request.json()
        vector = body.get("vector", [])
        if not isinstance(vector, list) or not vector:
            raise HTTPException(400, "vector must be a non-empty list of numbers")
        return app.people.enroll_voiceprint(name, [float(x) for x in vector])

    # ------------------------------------------------------------ settings
    @api.get("/api/settings")
    async def settings():
        snap = app.config.snapshot()
        for preset in (snap.get("provider", {}).get("presets", {}) or {}).values():
            if isinstance(preset, dict) and preset.get("api_key"):
                preset["api_key"] = "***set***"
        return snap

    @api.post("/api/settings")
    async def set_settings(request: Request):
        body = await request.json()
        for dotted, value in body.items():
            app.config.set(dotted, value)
        app.config.save()
        return {"ok": True}

    @api.post("/api/provider/test")
    async def provider_test():
        if app.chat_provider is None:
            return {"ok": False, "error": "no provider configured"}
        ok = await app.chat_provider.healthcheck()
        return {"ok": bool(ok), "type": getattr(app.chat_provider, "name", "?")}

    # ------------------------------------------------------------ voice + vision
    @api.post("/api/voice/stt")
    async def voice_stt(file: UploadFile = File(...)):
        data = await file.read()
        text = await app.voice.transcribe(data, filename=file.filename or None,
                                          content_type=file.content_type or None)
        body: dict = {"text": text}
        if not text:
            # honest failure detail for the dashboard (permission, service
            # down, empty recording) — voice trouble never fakes a transcript
            err = getattr(app.voice.stt, "last_error", None)
            if not app.permissions.get("microphone"):
                err = "microphone permission is off (Privacy panel)"
            elif err is None:
                err = "transcription returned no text"
            body["error"] = err
        return body

    @api.get("/api/voice/tts")
    async def voice_tts(text: str, voice: str | None = None):
        audio, media, error = await app.voice.speak_one(text, voice=voice or None)
        if not audio:
            return JSONResponse({"error": error or "tts unavailable or disabled"},
                                status_code=503)
        return Response(content=audio, media_type=media)

    @api.get("/api/voice/status")
    async def voice_status():
        return app.voice.status()

    @api.get("/api/voice/voices")
    async def voice_voices(refresh: bool = False):
        """Voice ids discovered live from the speech adapter (GET /v1/models
        there) plus Aurora's current selection."""
        voices, err = ([], None)
        if hasattr(app.voice.tts, "list_voices"):
            voices, err = await app.voice.tts.list_voices(force=refresh)
        selected = getattr(app.voice.tts, "voice", None) or \
            app.config.get("voice.voice_id", "")
        return {"tts": app.voice.tts.name, "voices": voices, "selected": selected,
                "source": "discovered" if voices else "none", "error": err}

    @api.post("/api/voice/voice")
    async def voice_select(request: Request):
        body = await request.json()
        try:
            return await app.voice.select_voice(str(body.get("voice", "")))
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @api.post("/api/voice/test")
    async def voice_test(request: Request):
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        text = str(body.get("text") or "Voice check: Aurora is ready.")
        voice = str(body.get("voice") or "").strip() or None
        audio, media, error = await app.voice.speak_one(text, voice=voice)
        if not audio:
            return JSONResponse({"error": error or "tts unavailable or disabled"},
                                status_code=503)
        return Response(content=audio, media_type=media)

    @api.post("/api/vision/screen")
    async def vision_screen(request: Request):
        body = await request.json()
        import base64 as _b64

        try:
            png = _b64.b64decode(str(body.get("image", "")))
        except Exception:
            raise HTTPException(400, "image must be base64 PNG")
        text = await app.senses.describe(app.chat_provider, png, str(body.get("question", "")))
        if text.startswith("error:"):
            return JSONResponse({"error": text}, status_code=400)
        return {"description": text}

    # ------------------------------------------------------------ status
    @api.get("/api/status")
    async def status():
        info = app.status()
        if app.chat_provider is not None:
            try:
                info["model_reachable"] = await app.chat_provider.healthcheck()
            except Exception:
                info["model_reachable"] = False
        info["streaming"] = app.conversation._streaming
        return info

    def _changelog_rollback(app_: CompanionApp, ts: float) -> dict:
        from ..memory.changelog import MemoryChangelog

        changelog = MemoryChangelog(app_.db, app_.store, app_.clock)
        return changelog.rollback(ts)

    return api
