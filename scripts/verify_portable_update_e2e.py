#!/usr/bin/env python3
"""Reproduces the Portable updater path as faithfully as this sandbox allows.

What Portable really does (and what we reproduce here):
- the service user CANNOT write the application tree (ProtectSystem=strict;
  reproduced here by removing owner-write at the DAC level — the effect for
  the running process is the same: every create/rename inside the app tree
  fails);
- the service cannot elevate (NoNewPrivileges=true — no sudo), so the
  dashboard update goes through aurora-update.service (polkit-gated start of
  a fixed root helper). This sandbox has no systemd/polkit, so the HELPER IS
  EXECUTED DIRECTLY after re-granting write access — that reproduces the
  helper's swap logic but NOT the privilege separation (root + polkit), which
  is covered structurally by the manifest-validation tests.

Verified end to end: read-only apply → staged manifest (nothing touched) →
helper install with venv/.git/config/aurora.env carried → restart → new
version verified with memories intact → rollback (read-only apply) → helper
rollback with venv usable → restart → old version, data intact.

Usage: python scripts/verify_portable_update_e2e.py
"""

from __future__ import annotations

import io
import json
import os
import signal
import stat
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

APP_PORT = 8794
GH_PORT = 8793
BASE = f"http://127.0.0.1:{APP_PORT}"
TMP = Path("/tmp/aurora-portable-e2e")
APP = TMP / "opt" / "aurora-companion"        # parent read-only, like /opt
DATA = TMP / "var" / "lib" / "aurora"
INSTALLED_VERSION = "0.2.0"
RELEASE_VERSION = "0.3.0"
HELPER = ROOT / "deploy" / "aurora-update-helper"

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


def lock_app_tree(writable: bool) -> None:
    """DAC-level reproduction of ProtectSystem=strict on the app tree."""
    for path in [APP.parent, APP, *APP.rglob("*")]:
        mode = path.stat().st_mode
        if writable:
            path.chmod(mode | stat.S_IWUSR)
        else:
            path.chmod(mode & ~stat.S_IWUSR & ~stat.S_IWGRP & ~stat.S_IWOTH)


def restart_server(proc_holder: list) -> None:
    """What `systemctl restart` does: stop the process, start it again."""
    old = proc_holder[0]
    old.send_signal(signal.SIGTERM)
    try:
        old.wait(timeout=10)
    except Exception:
        old.kill()
    log = open(TMP / "server.log", "ab", buffering=0)
    proc_holder[0] = subprocess.Popen(
        [sys.executable, str(APP / "run.py"), "--config", str(DATA.parent / "etc" / "config.yaml")],
        cwd=str(APP), stdout=log, stderr=log)
    wait_up()


class FakeGitHub(BaseHTTPRequestHandler):
    release_zip: bytes = b""

    def log_message(self, *a):
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/repos/test/aurora/releases/latest":
            release = {"tag_name": f"v{RELEASE_VERSION}", "prerelease": False,
                       "body": "- portable hardening",
                       "assets": [{"name": "release.zip", "size": len(self.release_zip),
                                   "browser_download_url": f"http://127.0.0.1:{GH_PORT}/download/release.zip"}]}
            self._send(200, json.dumps(release).encode())
        elif self.path == "/download/release.zip":
            self._send(200, self.release_zip, "application/zip")
        else:
            self._send(404, b"{}")


def build_release_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in APP.rglob("*"):
            rel = p.relative_to(APP)
            if any(part in {"venv", ".git", "data", "__pycache__"} for part in rel.parts):
                continue
            zf.write(p, str(Path("aurora-companion") / rel))
        init = APP / "aurora" / "__init__.py"
        zf.writestr("aurora-companion/aurora/__init__.py",
                    init.read_text(encoding="utf-8").replace(
                        f'__version__ = "{INSTALLED_VERSION}"',
                        f'__version__ = "{RELEASE_VERSION}"'))
    return buf.getvalue()


def prep() -> subprocess.Popen:
    import shutil

    if TMP.exists():  # previous runs leave read-only trees (by design)
        subprocess.run(["chmod", "-R", "u+rwx", str(TMP)], capture_output=True)
        shutil.rmtree(TMP, ignore_errors=True)
    APP.mkdir(parents=True)
    (DATA / "update").mkdir(parents=True)
    for item in ["aurora", "personas", "run.py", "pytest.ini", "requirements.txt"]:
        src = ROOT / item
        if src.is_dir():
            shutil.copytree(src, APP / item,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "data"))
        else:
            shutil.copy2(src, APP / item)
    (APP / "aurora" / "__init__.py").write_text(
        (APP / "aurora" / "__init__.py").read_text(encoding="utf-8").replace(
            '__version__ = "0.2.0"', f'__version__ = "{INSTALLED_VERSION}"'), encoding="utf-8")
    # dev-style local state that must survive everything
    (APP / "venv" / "bin").mkdir(parents=True)
    (APP / "venv" / "bin" / "python").write_text("#!/bin/sh\n")
    (APP / "venv" / "pyvenv.cfg").write_text("version = 3.12\n")
    (APP / ".git" / "refs" / "heads").mkdir(parents=True)
    (APP / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (APP / ".git" / "refs" / "heads" / "main").write_text("abc123\n")
    (APP / "config.yaml").write_text("data_dir: /x\n")
    (APP / "aurora.env").write_text("AURORA_PORT=8794\n")
    etc = DATA.parent / "etc"
    etc.mkdir(parents=True, exist_ok=True)
    (etc / "config.yaml").write_text(
        f"data_dir: {DATA}\nport: {APP_PORT}\nhost: 127.0.0.1\n"
        "update:\n  repo: test/aurora\n"
        f"  api_base: http://127.0.0.1:{GH_PORT}\n  asset: release.zip\n")
    lock_app_tree(writable=False)  # Portable: the app tree is read-only
    log = open(TMP / "server.log", "wb", buffering=0)
    return subprocess.Popen(
        [sys.executable, str(APP / "run.py"), "--config", str(etc / "config.yaml")],
        cwd=str(APP), stdout=log, stderr=log)


def main() -> int:
    holder = [prep()]
    FakeGitHub.release_zip = build_release_zip()  # built from the prepared tree
    gh = ThreadingHTTPServer(("127.0.0.1", GH_PORT), FakeGitHub)
    threading.Thread(target=gh.serve_forever, daemon=True).start()
    try:
        wait_up()
        check("read-only app tree is real", not os.access(APP / "run.py", os.W_OK))
        st = api("/api/status")
        check("installed version reported", st["version"] == INSTALLED_VERSION, st["version"])
        api("/api/memory/facts", {"content": "Rollback must keep the coffee fact"})
        api("/api/chat", {"message": "Remind me to water the tomatoes tomorrow at 9am"})

        # ---- 1. update under read-only restrictions ------------------------
        c = api("/api/update/check", {})
        check("check finds newer release", c.get("available") is True)
        a = api("/api/update/apply", {})
        run_py_before = (APP / "run.py").read_text(encoding="utf-8")
        check("apply stages instead of touching the read-only tree",
              a.get("status") == "staged_manual", json.dumps(a)[:200])
        check("manual command offered honestly",
              "aurora-update-helper" in a.get("manual_command", ""))
        check("app tree untouched by apply",
              (APP / "run.py").read_text(encoding="utf-8") == run_py_before)
        manifest = json.loads(Path(a["manifest"]).read_text())
        check("manifest points inside the data dir",
              manifest["staging"].startswith(str(DATA)) and manifest["backup"].startswith(str(DATA)))
        # the helper's authority is pinned to root-controlled values; in this
        # reproduction they are provided via the test-only override (a real
        # deployment writes /etc/aurora-update-authorized.json instead)
        auth_file = TMP / "authorized.json"
        auth_file.write_text(json.dumps({
            "app_dir": str(APP), "data_dir": str(DATA),
            "unit": "aurora-companion.service",
            "config_path": str(DATA.parent / "etc" / "config.yaml")}), encoding="utf-8")
        os.chmod(auth_file, 0o600)
        henv = dict(os.environ, AURORA_UPDATE_AUTHORIZED=str(auth_file))
        # a manifest claiming a foreign app dir must be refused outright
        rogue = dict(manifest, app_dir="/etc")
        rogue_path = DATA / "update" / "rogue-manifest.json"
        rogue_path.write_text(json.dumps(rogue))
        rv = subprocess.run([sys.executable, str(HELPER), "validate", str(rogue_path)],
                            capture_output=True, text=True, env=henv)
        check("helper refuses a foreign app_dir in the manifest", rv.returncode != 0)
        rogue_path.unlink()
        v = subprocess.run([sys.executable, str(HELPER), "validate", a["manifest"]],
                           capture_output=True, text=True, env=henv)
        check("helper accepts the staged manifest", v.returncode == 0, v.stderr[-200:])

        # ---- 2. the helper (as root, via aurora-update.service) installs ---
        lock_app_tree(writable=True)   # the root helper CAN write /opt
        h = subprocess.run([sys.executable, str(HELPER), "apply", str(DATA / "update")],
                           capture_output=True, text=True, env=henv)
        check("helper install succeeds", h.returncode == 0, h.stdout + h.stderr)
        check("new tree installed",
              (APP / "aurora" / "__init__.py").read_text().count(RELEASE_VERSION) == 1)
        check("venv carried across", (APP / "venv" / "bin" / "python").exists())
        check("git history carried across", (APP / ".git" / "HEAD").is_file())
        check("config.yaml carried across",
              (APP / "config.yaml").read_text() == "data_dir: /x\n")
        check("aurora.env carried across", (APP / "aurora.env").is_file())
        lock_app_tree(writable=False)  # back to read-only service reality

        # ---- 3. restart (what systemd does) + verification -----------------
        restart_server(holder)
        st = api("/api/status")
        check("restarted into the new version", st["version"] == RELEASE_VERSION, st["version"])
        hist = api("/api/history?limit=10")["messages"]
        check('"I\'m back!" announcement', any("I'm back!" in m["content"] for m in hist))
        facts = api("/api/memory/facts")["facts"]
        check("memories survived", any("coffee fact" in f["content"] for f in facts))
        rems = api("/api/reminders")["reminders"]
        check("reminders survived", any("tomatoes" in r["text"] for r in rems))

        # ---- 4. rollback under the same restrictions -----------------------
        lock_app_tree(writable=False)
        r = api("/api/update/rollback", {})
        check("rollback stages instead of touching the read-only tree",
              r.get("status") == "staged_manual", json.dumps(r)[:200])
        lock_app_tree(writable=True)
        h = subprocess.run([sys.executable, str(HELPER), "apply", str(DATA / "update")],
                           capture_output=True, text=True, env=henv)
        check("helper rollback succeeds", h.returncode == 0, h.stdout + h.stderr)
        check("previous tree restored",
              (APP / "aurora" / "__init__.py").read_text().count(INSTALLED_VERSION) == 1)
        check("venv survived the rollback", (APP / "venv" / "bin" / "python").exists()
              and (APP / "venv" / "pyvenv.cfg").is_file())
        check("git survived the rollback", (APP / ".git" / "refs" / "heads" / "main").is_file())
        check("config survived the rollback",
              (APP / "config.yaml").read_text() == "data_dir: /x\n")
        check("aurora.env survived the rollback", (APP / "aurora.env").is_file())
        lock_app_tree(writable=False)

        # ---- 5. restart after rollback + data still intact -----------------
        restart_server(holder)
        st = api("/api/status")
        check("restarted into the previous version", st["version"] == INSTALLED_VERSION,
              st["version"])
        facts = api("/api/memory/facts")["facts"]
        check("memories survived the rollback", any("coffee fact" in f["content"] for f in facts))
        us = api("/api/update/status")
        check("history records the rollback",
              any(h2.get("result") == "rolled_back" for h2 in us["history"]))
    finally:
        holder[0].send_signal(signal.SIGTERM)
        try:
            holder[0].wait(timeout=10)
        except Exception:
            holder[0].kill()
        gh.shutdown()

    print()
    failed = [r for r in results if not r[1]]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
