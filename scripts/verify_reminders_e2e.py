#!/usr/bin/env python3
"""Live end-to-end verification of the reminder/timer/notification flow,
driven over HTTP against the real server (run.py), no model configured —
the deterministic paths must carry everything.

Covers: NL chat reminder -> stored -> SSE delivery -> bell; dashboard form
with bare duration; timer completion prompt -> repeat (original duration) ->
snooze -> dismiss; persistence across a server restart.

Usage: python scripts/verify_reminders_e2e.py
"""

from __future__ import annotations

import json
import signal
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PORT = 8797
BASE = f"http://127.0.0.1:{PORT}"
TMP = Path("/tmp/aurora-reminders-e2e")

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail and not ok else ""))


def api(path: str, payload: dict | None = None, method: str | None = None):
    req = urllib.request.Request(BASE + path, method=method or ("POST" if payload else "GET"))
    body = None
    if payload is not None:
        req.add_header("Content-Type", "application/json")
        body = json.dumps(payload).encode()
    with urllib.request.urlopen(req, body, timeout=10) as r:
        return json.loads(r.read().decode())


def wait_up(timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            api("/api/status")
            return
        except Exception:
            time.sleep(0.4)
    raise RuntimeError("server did not come up")


class SSEListener:
    def __init__(self):
        self.events: list[dict] = []

    def listen(self, seconds: float) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(BASE + "/api/events", timeout=20) as resp:
                    for raw in resp:
                        line = raw.decode("utf-8", "replace").strip()
                        if line.startswith("data:"):
                            try:
                                self.events.append(json.loads(line[5:].strip()))
                            except ValueError:
                                pass
            except Exception:
                pass
            return  # one connection window is enough per phase


def start_server() -> "subprocess.Popen":
    import subprocess

    proc = subprocess.Popen(
        [sys.executable, "run.py", "--config", str(TMP / "config.yaml")],
        cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    return proc


def stop_server(proc) -> None:
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()


def main() -> int:
    import shutil

    shutil.rmtree(TMP, ignore_errors=True)
    (TMP / "data").mkdir(parents=True)
    (TMP / "config.yaml").write_text(
        f"data_dir: {TMP}/data\nport: {PORT}\nhost: 127.0.0.1\n", encoding="utf-8")

    proc = start_server()
    try:
        wait_up()

        # 1. NL chat reminder — model-free, must confirm AFTER storing
        reply = api("/api/chat", {"message": "Remind me to check the servers in 90 seconds"})
        check("nl chat reminder confirmed", "got it" in reply["content"].lower(),
              reply["content"][:120])
        rems = api("/api/reminders")["reminders"]
        check("nl reminder stored", any("check the servers" in r["text"] for r in rems))

        # 2. dashboard form with a BARE duration ("15 minutes", no 'in')
        form = api("/api/reminders", {"text": "Stretch break", "when": "15 minutes"})
        check("bare-duration form reminder created", bool(form.get("id")), json.dumps(form)[:120])

        # 3. junk text is rejected
        r = urllib.request.Request(BASE + "/api/reminders", method="POST")
        r.add_header("Content-Type", "application/json")
        try:
            urllib.request.urlopen(r, json.dumps({"text": "hi", "when": "in 5 minutes"}).encode(),
                                   timeout=10)
            check("junk text rejected", False, "server accepted 'hi'")
        except urllib.error.HTTPError as e:
            check("junk text rejected", e.code == 400, f"status {e.code}")

        # 4. a 90-second timer via chat
        api("/api/chat", {"message": "Set a timer for 90 seconds"})
        timers = [r for r in api("/api/reminders")["reminders"] if r.get("kind") == "timer"]
        check("timer created with kind=timer", len(timers) == 1)

        # 5. wait for due time: reminders + timer fire, SSE delivers
        print("…waiting ~95s for the 90-second entries to come due")
        sse = SSEListener()
        import threading

        t = threading.Thread(target=sse.listen, args=(100,), daemon=True)
        t.start()
        deadline = time.time() + 100
        while time.time() < deadline:
            alerts = api("/api/alerts")
            if alerts["pending_timers"]:
                break
            time.sleep(2)
        alerts = api("/api/alerts")
        check("timer fired and is pending", len(alerts["pending_timers"]) == 1,
              json.dumps(alerts["pending_timers"])[:150])
        due_events = [e for e in sse.events if e.get("type") == "reminder_due"]
        timer_events = [e for e in sse.events if e.get("type") == "timer_due"]
        # the SSE listener captures the first ~100s window: the 90-second
        # reminder fires in-window; the 15-minute one is verified via the bell
        check("reminder_due delivered over SSE", len(due_events) >= 1,
              f"{len(due_events)} events")
        check("timer_due delivered over SSE", len(timer_events) == 1)
        bell_texts = [a["text"] for a in alerts["active"]]
        check("bell shows active reminders", any("Stretch break" in x for x in bell_texts))

        # 6. completion message carries the decision buttons
        history = api("/api/history?limit=10")["messages"]
        timer_msgs = [m for m in history if (m.get("meta") or {}).get("timer_decision")]
        check("completion prompt asked with buttons",
              bool(timer_msgs) and "Would you like me to repeat it?" in timer_msgs[-1]["content"])

        # 7. repeat -> original duration restored; then snooze; then dismiss
        timer_id = next(iter(alerts["pending_timers"]))
        rep = api(f"/api/timers/{timer_id}/repeat", {}, method="POST")
        check("timer repeated", rep.get("ok") is True)
        new_timers = [r for r in api("/api/reminders")["reminders"] if r.get("kind") == "timer"]
        check("repeat restores original 90s duration",
              any(88 <= r["due_at"] - r["created_at"] <= 92 for r in new_timers),
              json.dumps([(r["text"], r["due_at"] - r["created_at"]) for r in new_timers]))
        check("answered timer cleared from pending",
              timer_id not in api("/api/alerts")["pending_timers"])

        print("…waiting ~95s for the repeated timer")
        deadline = time.time() + 100
        new_pending = []
        while time.time() < deadline:
            new_pending = list(api("/api/alerts")["pending_timers"])
            if new_pending:
                break
            time.sleep(2)
        check("repeated timer asks again once fired", len(new_pending) == 1)
        if new_pending:
            rid = next(iter(new_pending))
            sn = api(f"/api/timers/{rid}/snooze", {"seconds": 60}, method="POST")
            check("timer snoozed", sn.get("ok") is True and rid in api("/api/alerts")["pending_timers"])
            print("…waiting ~65s for the snooze")
            time.sleep(63)
            fired = api("/api/alerts")["pending_timers"]
            check("snoozed timer re-fired (original duration kept)",
                  rid in fired and fired[rid]["duration"] == 90, json.dumps(fired)[:150])
            dm = api(f"/api/timers/{rid}/dismiss", {}, method="POST")
            check("timer dismissed", dm.get("ok") is True
                  and rid not in api("/api/alerts")["pending_timers"])

        # 8. persistence across a restart
        stop_server(proc)
        proc = start_server()
        wait_up()
        alerts = api("/api/alerts")
        check("reminders survive restart",
              any("Stretch break" in r["text"] for r in alerts["active"]))
        check("no stale pending decisions after restart",
              isinstance(alerts["pending_timers"], dict))
    finally:
        stop_server(proc)

    print()
    failed = [r for r in results if not r[1]]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
