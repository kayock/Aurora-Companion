#!/usr/bin/env python3
"""Live end-to-end verification of the one-button updater, against the REAL
server (run.py) and a LOCAL fake-GitHub server (no network needed):

  check (versions differ) -> backup + download + verify + install (swap with
  venv carry) -> restart (os.execv, same argv) -> verify_after_restart posts
  "I'm back!" -> history records ok -> rollback restores the previous tree
  with user data intact.

Usage: python scripts/verify_update_e2e.py
"""

from __future__ import annotations

import io
import json
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

APP_PORT = 8798
GH_PORT = 8799
BASE = f"http://127.0.0.1:{APP_PORT}"
TMP = Path("/tmp/aurora-update-e2e")
APP = TMP / "app"
INSTALLED_VERSION = "0.9.0"
RELEASE_VERSION = "1.0.0"
RELEASE_NOTES = "### What's new\n- reminders and notification chime\n- timers with repeat/snooze/dismiss\n- update awareness"

results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail and not ok else ""))


def api(path: str, payload: dict | None = None):
    req = urllib.request.Request(BASE + path, method="POST" if payload is not None else "GET")
    body = None
    if payload is not None:
        req.add_header("Content-Type", "application/json")
        body = json.dumps(payload).encode()
    with urllib.request.urlopen(req, body, timeout=15) as r:
        return json.loads(r.read().decode())


def wait_up(timeout: float = 40.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            api("/api/status")
            return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("server did not come up")


# ---------------------------------------------------------------- fake GitHub
class FakeGitHub(BaseHTTPRequestHandler):
    release_zip: bytes = b""

    def log_message(self, *a):  # silence
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/repos/test/aurora/releases/latest":
            release = {
                "tag_name": f"v{RELEASE_VERSION}", "prerelease": False,
                "body": RELEASE_NOTES, "html_url": "https://example.invalid/rel",
                "published_at": "2026-11-21T00:00:00Z",
                "assets": [{"name": "release.zip", "size": len(self.release_zip),
                            "browser_download_url": f"http://127.0.0.1:{GH_PORT}/download/release.zip"}],
            }
            self._send(200, json.dumps(release).encode())
        elif self.path == "/download/release.zip":
            self._send(200, self.release_zip, "application/zip")
        else:
            self._send(404, b"{}", "application/json")


def build_release_zip() -> bytes:
    """A release tree = the real app code, version stamped as the new release."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in ROOT.rglob("*"):
            rel = p.relative_to(ROOT)
            if any(part in {"data", ".git", "__pycache__", ".pytest_cache", "venv",
                            "data-demo", "node_modules", "tools"} for part in rel.parts):
                continue
            zf.write(p, str(Path("aurora-companion") / rel))
        # stamp the release version into the copied tree's __init__
        import re as _re
        init = ROOT / "aurora" / "__init__.py"
        zf.writestr("aurora-companion/aurora/__init__.py",
                    _re.sub(r'__version__ = "[^"]+"',
                            f'__version__ = "{RELEASE_VERSION}"',
                            init.read_text(encoding="utf-8")))
    return buf.getvalue()


def _stamp(text: str, version: str) -> str:
    import re as _re
    return _re.sub(r'__version__ = "[^"]+"', f'__version__ = "{version}"', text)


def prep_installation() -> None:
    shutil.rmtree(TMP, ignore_errors=True)
    APP.mkdir(parents=True)
    for item in ["aurora", "personas", "run.py", "pytest.ini", "requirements.txt",
                 "config.example.yaml", "tools" if False else "deploy"]:
        src = ROOT / item
        if src.exists():
            (APP / item).mkdir(parents=True, exist_ok=True) if src.is_dir() else None
            if src.is_dir():
                shutil.copytree(src, APP / item, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "data"))
            else:
                shutil.copy2(src, APP / item)
    # stamp the "installed" version
    init = APP / "aurora" / "__init__.py"
    init.write_text(_stamp(init.read_text(encoding="utf-8"), INSTALLED_VERSION),
                    encoding="utf-8")
    (APP / "config.yaml").write_text(
        f"data_dir: {TMP}/data\nport: {APP_PORT}\nhost: 127.0.0.1\n"
        "update:\n  repo: test/aurora\n"
        f"  api_base: http://127.0.0.1:{GH_PORT}\n  asset: release.zip\n",
        encoding="utf-8")


def main() -> int:
    FakeGitHub.release_zip = build_release_zip()
    gh = ThreadingHTTPServer(("127.0.0.1", GH_PORT), FakeGitHub)
    threading.Thread(target=gh.serve_forever, daemon=True).start()

    prep_installation()
    log = open(TMP / "server.log", "ab", buffering=0)
    proc = subprocess.Popen([sys.executable, "run.py", "--config", str(APP / "config.yaml")],
                            cwd=str(APP), stdout=log, stderr=log)
    try:
        wait_up()
        st = api("/api/status")
        check("installed version reported", st["version"] == INSTALLED_VERSION, st["version"])

        # user data created BEFORE the update must survive everything
        api("/api/memory/facts", {"content": "The trick to good coffee is a coarse grind"})
        api("/api/chat", {"message": "Remind me to water the tomatoes tomorrow at 9am"})

        # 1. check
        c = api("/api/update/check", {})
        check("check finds newer release", c.get("available") is True
              and c.get("latest_version") == RELEASE_VERSION, json.dumps(c)[:150])
        check("check carries release notes", "reminders" in (c.get("notes") or ""))

        # 2. apply (backup + download + verify + swap)
        a = api("/api/update/apply", {})
        check("install succeeded", a.get("status") == "installed", json.dumps(a)[:200])
        new_init = (APP / "aurora" / "__init__.py").read_text(encoding="utf-8")
        check("app tree swapped to release", f'"{RELEASE_VERSION}"' in new_init)
        backups = list((TMP / "data" / "update").glob("backup-*/app-tree.tar.gz"))
        check("pre-update backup exists", len(backups) == 1)
        pending = api("/api/update/status")
        check("pending verification recorded", pending["pending"] is not None)

        # 3. restart (os.execv inside the server process)
        api("/api/update/restart", {})
        deadline = time.time() + 60
        version = None
        while time.time() < deadline:
            try:
                version = api("/api/status")["version"]
                if version == RELEASE_VERSION:
                    break
            except Exception:
                pass
            time.sleep(1)
        check("restarted into the new version", version == RELEASE_VERSION, str(version))
        if version != RELEASE_VERSION:
            print("--- server.log tail ---")
            print((TMP / "server.log").read_text(errors="replace")[-1500:])

        # 4. post-restart verification + continuity
        hist = api("/api/history?limit=10")["messages"]
        check('"I\'m back!" announcement posted',
              any("I'm back!" in m["content"] for m in hist if m["role"] == "assistant"))
        us = api("/api/update/status")
        check("update history records ok",
              bool(us["history"]) and us["history"][-1].get("result") == "ok",
              json.dumps(us["history"]))
        facts = api("/api/memory/facts")["facts"]
        check("memories survived the update",
              any("coarse grind" in f["content"] for f in facts))
        rems = api("/api/reminders")["reminders"]
        check("reminders survived the update", any("tomatoes" in r["text"] for r in rems))
        persona_version = api("/api/status")["persona_version"]
        check("persona intact", persona_version >= 0)

        # 5. rollback
        api("/api/update/rollback", {})
        deadline = time.time() + 60
        version = None
        while time.time() < deadline:
            try:
                version = api("/api/status")["version"]
                if version == INSTALLED_VERSION:
                    break
            except Exception:
                pass
            time.sleep(1)
        check("rollback restored previous version", version == INSTALLED_VERSION, str(version))
        facts = api("/api/memory/facts")["facts"]
        check("memories survived the rollback", any("coarse grind" in f["content"] for f in facts))
        us = api("/api/update/status")
        check("history records the rollback",
              any(h.get("result") == "rolled_back" for h in us["history"]))

        # 6. corrupt download is refused, nothing changes
        FakeGitHub.release_zip = b"this is not a zip file"
        api("/api/update/check", {})
        r = urllib.request.Request(BASE + "/api/update/apply", method="POST")
        try:
            urllib.request.urlopen(r, b"", timeout=30)
            check("corrupt download refused", False, "apply returned 200")
        except urllib.error.HTTPError as e:
            body = json.loads(e.read().decode())
            check("corrupt download refused", e.code == 400 and "zip" in body.get("error", ""),
                  f"{e.code} {body}")
        check("corrupt download left the tree intact",
              f'"{INSTALLED_VERSION}"' in (APP / "aurora" / "__init__.py").read_text(encoding="utf-8"))
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        gh.shutdown()

    print()
    failed = [r for r in results if not r[1]]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
