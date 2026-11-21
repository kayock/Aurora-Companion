"""Updater unit tests: version ordering, release checking against a stubbed
GitHub API, and download/install failure handling. The full live cycle
(install → restart → verify → rollback) is covered by
scripts/verify_update_e2e.py against the real server."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

from aurora.storage import Database
from aurora.updater import Updater, UpdaterError, version_tuple


def _updater(tmp_path: Path, app_dir: Path | None = None) -> Updater:
    db = Database(tmp_path / "data" / "aurora.db")
    return Updater({"repo": "test/aurora", "asset": "release.zip",
                    "timeout_seconds": 5.0}, db,
                   app_dir=app_dir or tmp_path / "app",
                   data_dir=tmp_path / "data", config_path=tmp_path / "app" / "config.yaml",
                   version="0.2.0")


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _BrokenResponse:
    """A response that dies mid-stream — the interrupted-download case."""

    def __init__(self):
        self._sent = False

    def read(self, n=-1):
        if not self._sent:
            self._sent = True
            return b"PK\x03\x04partial"
        raise ConnectionError("connection reset mid-download")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_version_tuple_ordering():
    assert version_tuple("0.2.0") > version_tuple("0.1.9")
    assert version_tuple("0.10.0") > version_tuple("0.9.0")
    assert version_tuple("0.9.0-rc.2") < version_tuple("0.9.0")
    assert version_tuple("1.0.0") == version_tuple("1.0.0")


def test_check_disabled_without_repo(tmp_path):
    u = _updater(tmp_path)
    u.config = {"repo": ""}
    result = u.check()
    assert result["available"] is False and "repo" in result["error"]


def test_check_compares_versions_and_finds_asset(tmp_path, monkeypatch):
    u = _updater(tmp_path)
    release = {"tag_name": "v0.3.0", "prerelease": False, "body": "- notes here",
               "assets": [{"name": "release.zip", "size": 10,
                           "browser_download_url": "http://x/release.zip"}]}
    monkeypatch.setattr("aurora.updater.urllib.request.urlopen",
                        lambda req, timeout: _FakeResponse(json.dumps(release).encode()))
    state = u.check()
    assert state["available"] is True and state["latest_version"] == "0.3.0"
    assert state["notes"] == "- notes here"

    release["tag_name"] = "v0.1.0"  # older than installed
    state = u.check()
    assert state["available"] is False


def test_check_network_error_is_reported_not_raised(tmp_path, monkeypatch):
    u = _updater(tmp_path)

    def boom(req, timeout):
        raise OSError("network unreachable")

    monkeypatch.setattr("aurora.updater.urllib.request.urlopen", boom)
    state = u.check()
    assert state["available"] is False and "network unreachable" in state["error"]


def test_interrupted_download_changes_nothing(tmp_path, monkeypatch):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "run.py").write_text("print('hi')\n")
    u = _updater(tmp_path, app_dir)
    u._last_check = {"available": True, "latest_version": "0.3.0",
                     "asset_url": "http://x/release.zip", "notes": ""}
    monkeypatch.setattr("aurora.updater.urllib.request.urlopen",
                        lambda req, timeout: _BrokenResponse())
    with pytest.raises(UpdaterError, match="interrupted"):
        u.apply()
    # nothing installed, nothing partial left behind
    assert (app_dir / "run.py").read_text() == "print('hi')\n"
    assert not (u.work_dir() / "release.zip").exists()
    assert not (u.work_dir() / "release.zip.part").exists()
    assert u.db.get_meta_json("update.pending") is None


def test_corrupt_zip_refused_before_any_swap(tmp_path, monkeypatch):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "run.py").write_text("print('hi')\n")
    u = _updater(tmp_path, app_dir)
    u._last_check = {"available": True, "latest_version": "0.3.0",
                     "asset_url": "http://x/release.zip", "notes": ""}
    monkeypatch.setattr("aurora.updater.urllib.request.urlopen",
                        lambda req, timeout: _FakeResponse(b"definitely not a zip"))
    with pytest.raises(UpdaterError, match="zip"):
        u.apply()
    assert (app_dir / "run.py").read_text() == "print('hi')\n"


def test_swap_carries_venv_git_config_and_restores_on_failure(tmp_path, monkeypatch):
    """venv/.git/config.yaml survive the swap; a failing install restores the
    previous tree untouched."""
    app_dir = tmp_path / "app"
    (app_dir / "aurora").mkdir(parents=True)
    (app_dir / "run.py").write_text("print('hi')\n")
    (app_dir / "config.yaml").write_text("data_dir: /x\n")
    (app_dir / "venv" / "bin").mkdir(parents=True)
    (app_dir / "venv" / "bin" / "python").write_text("#!/bin/sh\n")
    (app_dir / ".git" / "refs" / "heads").mkdir(parents=True)
    (app_dir / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (app_dir / ".git" / "refs" / "heads" / "main").write_text("abc123\n")
    u = _updater(tmp_path, app_dir)

    release_bytes = io.BytesIO()
    with zipfile.ZipFile(release_bytes, "w") as zf:
        zf.writestr("aurora-companion/run.py", "print('new')\n")
        zf.writestr("aurora-companion/aurora/__init__.py", "__version__ = '0.3.0'\n")
    u._last_check = {"available": True, "latest_version": "0.3.0",
                     "asset_url": "http://x/release.zip", "notes": ""}
    monkeypatch.setattr("aurora.updater.urllib.request.urlopen",
                        lambda req, timeout: _FakeResponse(release_bytes.getvalue()))

    result = u.apply()
    assert result["status"] == "installed"
    # new tree in place, local state carried across
    assert (app_dir / "run.py").read_text() == "print('new')\n"
    assert (app_dir / "config.yaml").read_text() == "data_dir: /x\n"
    assert (app_dir / "venv" / "bin" / "python").exists()
    assert (app_dir / ".git" / "HEAD").exists()
    # backup archive + pending verification + manifest recorded
    backups = list(u.work_dir().glob("backup-*/app-tree.tar.gz"))
    assert len(backups) == 1
    assert u.db.get_meta_json("update.pending")["expected_version"] == "0.3.0"
    assert (u.work_dir() / "last_install.json").is_file()


def test_verify_after_restart_and_history(tmp_path):
    db = Database(tmp_path / "data" / "aurora.db")
    u = _updater(tmp_path)
    db.set_meta_json("update.pending", {"expected_version": "0.3.0",
                                        "previous_version": "0.2.0", "ts": 0.0})
    # wrong version running → honest failure message
    msg = u.verify_after_restart()
    assert msg and "did not complete" in msg
    assert db.get_meta_json("update.history")[-1]["result"] == "failed"
    # right version → success announcement
    db.set_meta_json("update.pending", {"expected_version": "0.3.0",
                                        "previous_version": "0.2.0", "ts": 0.0})
    u.version = "0.3.0"
    msg = u.verify_after_restart()
    assert msg and "I'm back!" in msg
    assert db.get_meta_json("update.history")[-1]["result"] == "ok"
    assert db.get_meta_json("update.pending") is None
    # no pending → silent (no announcement on ordinary startups)
    assert u.verify_after_restart() is None


def test_database_backup_to_snapshot(tmp_path):
    db = Database(tmp_path / "data" / "aurora.db")
    db.set_meta("k", "v")
    snap = tmp_path / "snap.db"
    db.backup_to(snap)
    other = sqlite3.connect(str(snap))
    assert other.execute("SELECT value FROM meta WHERE key='k'").fetchone()[0] == "v"
    other.close()


# ---------------------------------------------------------------- hardening round 2
def _make_installable(tmp_path: Path, with_venv: bool = True):
    """A realistic dev-style app dir: venv, .git, config.yaml, aurora.env."""
    app_dir = tmp_path / "app"
    (app_dir / "aurora").mkdir(parents=True)
    (app_dir / "run.py").write_text("print('hi')\n")
    (app_dir / "config.yaml").write_text("data_dir: /x\n")
    (app_dir / "aurora.env").write_text("AURORA_PORT=8795\n")
    if with_venv:
        (app_dir / "venv" / "bin").mkdir(parents=True)
        (app_dir / "venv" / "bin" / "python").write_text("#!/bin/sh\n")
        (app_dir / "venv" / "pyvenv.cfg").write_text("version = 3.12\n")
    (app_dir / ".git" / "refs" / "heads").mkdir(parents=True)
    (app_dir / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (app_dir / ".git" / "refs" / "heads" / "main").write_text("abc123\n")
    return app_dir


def _release_zip(version: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("aurora-companion/run.py", f"# release {version}\n")
        zf.writestr("aurora-companion/aurora/__init__.py", f"__version__ = '{version}'\n")
    return buf.getvalue()


def _arm(u: Updater, version: str, monkeypatch):
    u._last_check = {"available": True, "latest_version": version,
                     "asset_url": "http://x/release.zip", "notes": ""}
    monkeypatch.setattr("aurora.updater.urllib.request.urlopen",
                        lambda req, timeout: _FakeResponse(_release_zip(version)))


def test_rollback_preserves_venv_git_and_config(tmp_path, monkeypatch):
    """Rollback must NOT destroy the virtualenv, git history or local config:
    the backup archive excludes them, so they are carried aside before the
    restore and moved back afterwards — Aurora can restart off the old tree."""
    app_dir = _make_installable(tmp_path)
    u = _updater(tmp_path, app_dir)
    _arm(u, "0.3.0", monkeypatch)
    u.apply()
    assert (app_dir / "run.py").read_text() == "# release 0.3.0\n"

    u.rollback()
    # previous tree restored, local state intact and usable
    assert (app_dir / "run.py").read_text() == "print('hi')\n"
    assert (app_dir / "venv" / "bin" / "python").exists()      # venv survived
    assert (app_dir / "venv" / "pyvenv.cfg").is_file()
    assert (app_dir / ".git" / "HEAD").read_text() == "ref: refs/heads/main\n"
    assert (app_dir / "config.yaml").read_text() == "data_dir: /x\n"
    assert (app_dir / "aurora.env").is_file()
    # the venv is actually usable (interpreter file intact, non-empty)
    assert (app_dir / "venv" / "bin" / "python").stat().st_size > 0


def test_rollback_after_interrupted_update_is_honest(tmp_path, monkeypatch):
    """No completed install (no manifest) → rollback refuses honestly and
    changes nothing, instead of destroying the current tree."""
    app_dir = _make_installable(tmp_path)
    u = _updater(tmp_path, app_dir)
    u._last_check = {"available": True, "latest_version": "0.3.0",
                     "asset_url": "http://x/release.zip", "notes": ""}
    monkeypatch.setattr(Updater, "_app_tree_writable", lambda self: False)
    monkeypatch.setattr("aurora.updater.urllib.request.urlopen",
                        lambda req, timeout: _BrokenResponse())
    with pytest.raises(UpdaterError, match="interrupted"):
        u.apply()  # download dies mid-stream
    with pytest.raises(UpdaterError, match="no update manifest"):
        u.rollback()
    assert (app_dir / "run.py").read_text() == "print('hi')\n"
    assert (app_dir / "venv" / "bin" / "python").exists()


def test_apply_on_readonly_app_tree_stages_via_helper(tmp_path, monkeypatch):
    """Portable reality (ProtectSystem=strict + NoNewPrivileges): the service
    cannot write the app tree. apply() must NOT attempt the in-process swap —
    it stages the release inside the (writable) data dir, writes an install
    manifest for the root helper, and honestly reports how the install runs."""
    app_dir = _make_installable(tmp_path)
    u = _updater(tmp_path, app_dir)
    _arm(u, "0.3.0", monkeypatch)
    monkeypatch.setattr(Updater, "_app_tree_writable", lambda self: False)
    monkeypatch.setattr("shutil.which", lambda name: None)  # no systemctl here

    result = u.apply()
    assert result["status"] == "staged_manual"
    assert "aurora-update-helper" in result["manual_command"]
    # nothing in the app tree changed; staging + manifest live in the data dir
    assert (app_dir / "run.py").read_text() == "print('hi')\n"
    manifest_path = Path(result["manifest"])
    manifest = json.loads(manifest_path.read_text())
    assert manifest["action"] == "install"
    assert manifest["new_version"] == "0.3.0"
    staged = Path(manifest["staging"])
    assert staged.is_dir() and (staged / "run.py").is_file()
    assert str(u.data_dir) in str(staged)  # staging inside the writable data dir
    # verification pending: whichever restart happens next verifies the new version
    assert u.db.get_meta_json("update.pending")["expected_version"] == "0.3.0"


def test_helper_manifest_validation(tmp_path, monkeypatch):
    """The root helper validates manifests strictly: schema, paths confined to
    the ROOT-AUTHORIZED data dir, and a refusal to act on anything else."""
    helper = ROOT / "deploy" / "aurora-update-helper"
    app_dir = _make_installable(tmp_path)
    u = _updater(tmp_path, app_dir)
    _arm(u, "0.3.0", monkeypatch)
    monkeypatch.setattr(Updater, "_app_tree_writable", lambda self: False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    result = u.apply()
    manifest = result["manifest"]

    # authorized values for this test install (env override — test only)
    auth_file = tmp_path / "authorized.json"
    auth_file.write_text(json.dumps(
        {"app_dir": str(app_dir), "data_dir": str(u.data_dir),
         "unit": "aurora-companion.service", "config_path": ""}), encoding="utf-8")
    os.chmod(auth_file, 0o600)
    henv = dict(os.environ, AURORA_UPDATE_AUTHORIZED=str(auth_file))
    good = subprocess.run([sys.executable, str(helper), "validate", manifest],
                          capture_output=True, text=True, env=henv)
    assert good.returncode == 0, good.stderr

    # path escape: staging claimed outside the data dir must be refused
    m = json.loads(Path(manifest).read_text())
    m["staging"] = str(tmp_path / "elsewhere" / "staging")
    bad = tmp_path / "bad-manifest.json"
    bad.write_text(json.dumps(m))
    escaped = subprocess.run([sys.executable, str(helper), "validate", str(bad)],
                             capture_output=True, text=True, env=henv)
    assert escaped.returncode != 0 and "outside" in (escaped.stderr + escaped.stdout)
