"""Self-update for Aurora Companion — opt-in, user-approved, rollback-safe.

Checks the configured GitHub repository's latest stable release, downloads
the release zip asset, verifies it, backs up the current installation plus
configuration and a snapshot of the user database, swaps in the new
application tree, and restarts. On startup the new version verifies itself
and records the outcome in Aurora's update history; failures can be rolled
back from the backup.

Hard safety rules:
- the data directory, the SQLite store, and user data are NEVER overwritten
  (an online sqlite snapshot goes into the backup, but rollback restores the
  application tree and config only — never an older database);
- nothing downloaded is ever executed; an update installs files only;
- no blind `git pull` against a possibly modified working tree — this is a
  release-zip updater, not a VCS operation;
- an interrupted or corrupt download aborts before anything is replaced;
- venv/ and .git/ inside the application tree are carried across the swap;
- installation happens only after an explicit user confirmation.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path

UPDATE_DIR_NAME = "update"          # under the data dir: downloads + manifests
BACKUP_KEEP = 3                     # rolling keep-count of pre-update backups
EXCLUDE_BACKUP = {"venv", ".git", "__pycache__", ".pytest_cache", "data",
                  "data-demo", "node_modules"}
# local state that lives inside the app tree and must survive swaps and
# rollbacks (the backup archive deliberately excludes all of these)
CARRY = ("venv", ".git", "config.yaml", "aurora.env")


def version_tuple(version: str) -> tuple:
    """Numeric ordering key for versions like 0.9.0 / 0.10.0 / 0.9.0-rc.2.
    A pre-release suffix orders just below the plain release."""
    base = version.split("-", 1)[0]
    nums = []
    for part in base.split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        nums.append(int(digits or 0))
    pre = 0 if "-" in version else 1
    return tuple(nums) + (pre,)


class UpdaterError(RuntimeError):
    pass


class Updater:
    def __init__(self, config, db, *, app_dir: Path, data_dir: Path,
                 config_path: Path | None, version: str, clock=None) -> None:
        self.config = config
        self.db = db
        self.app_dir = Path(app_dir).resolve()
        self.data_dir = Path(data_dir).resolve()
        self.config_path = Path(config_path).resolve() if config_path else None
        self.version = version
        self.clock = clock
        # resolved NOW, while the cwd is still valid: apply() renames the app
        # tree, so a relative sys.argv[0] ("run.py") would break the restart
        self.script_path = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None
        self._last_check: dict = {}

    # ------------------------------------------------------------------ state
    def work_dir(self) -> Path:
        d = self.data_dir / UPDATE_DIR_NAME
        d.mkdir(parents=True, exist_ok=True)
        return d

    def current_commit(self) -> str:
        env = os.environ.get("AURORA_COMMIT", "")
        if env:
            return env
        head = self.app_dir / ".git" / "HEAD"
        try:
            text = head.read_text(encoding="utf-8").strip()  # ref: refs/heads/main
            ref = text.split(":", 1)[1].strip() if ":" in text else ""
            if ref:
                return (self.app_dir / ".git" / ref).read_text(encoding="utf-8").strip()[:12]
        except Exception:
            pass
        return ""

    def history(self) -> list[dict]:
        return list(self.db.get_meta_json("update.history", []))

    def public_state(self) -> dict:
        pending = self.db.get_meta_json("update.pending", None)
        last = self.history()[-1] if self.history() else None
        return {
            "version": self.version,
            "commit": self.current_commit(),
            "repo": self.config.get("repo", ""),
            "update_available": bool(self._last_check.get("available")),
            "latest_version": self._last_check.get("latest_version"),
            "last_check": self._last_check or None,
            "pending": pending,
            "history": self.history()[-5:],
            "last": last,
        }

    # ------------------------------------------------------------------ checking
    def check(self) -> dict:
        """Ask GitHub for the latest stable release. Read-only; never installs.
        Returns a dict with current/latest version, notes and availability."""
        repo = str(self.config.get("repo", "") or "").strip().strip("/")
        if not repo:
            return {"error": "no update repo configured (update.repo is empty)",
                    "current_version": self.version,
                    "current_commit": self.current_commit(), "available": False}
        api_base = str(self.config.get("api_base", "https://api.github.com")).rstrip("/")
        url = f"{api_base}/repos/{repo}/releases/latest"
        timeout = float(self.config.get("timeout_seconds", 20.0))
        try:
            req = urllib.request.Request(url, headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "aurora-companion-updater",
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                release = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            self._last_check = {"error": str(exc)[:200], "available": False,
                                "current_version": self.version}
            return self._last_check
        latest = str(release.get("tag_name") or "").lstrip("v")
        asset_name = str(self.config.get("asset", "Aurora-Companion.zip"))
        asset = next((a for a in release.get("assets", [])
                      if a.get("name") == asset_name), None)
        self._last_check = {
            "current_version": self.version,
            "current_commit": self.current_commit(),
            "latest_version": latest,
            "prerelease": bool(release.get("prerelease")),
            "notes": (release.get("body") or "")[:4000],
            "html_url": release.get("html_url"),
            "asset_name": asset_name,
            "asset_size": asset.get("size") if asset else None,
            "asset_url": asset.get("browser_download_url") if asset else None,
            "published_at": release.get("published_at"),
            "available": bool(latest and asset
                              and version_tuple(latest) > version_tuple(self.version)),
        }
        return self._last_check

    # ------------------------------------------------------------------ installing
    def _app_tree_writable(self) -> bool:
        """Real write probes, not os.access (mount-level read-only, as systemd
        ProtectSystem=strict enforces, is invisible to the access bits)."""
        for target in (self.app_dir, self.app_dir.parent):
            probe = target / ".aurora-write-probe"
            try:
                probe.write_text("x", encoding="utf-8")
                probe.unlink()
            except OSError:
                return False
        return True

    def _trigger_helper(self, manifest_path: Path) -> dict:
        """Hand the manifest to the root helper unit (polkit-gated: the aurora
        user may start ONLY aurora-update.service). Falls back to an honest
        manual command when systemd/polkit is not available."""
        unit = os.environ.get("AURORA_UPDATE_UNIT", "aurora-update.service")
        if shutil.which("systemctl"):
            import subprocess
            rc = subprocess.run(["systemctl", "start", unit],
                                capture_output=True).returncode
            if rc == 0:
                return {"status": "installing", "via": unit}
        helper = Path(__file__).resolve().parents[1] / "deploy" / "aurora-update-helper"
        return {"status": "staged_manual",
                "manual_command": f"sudo {helper} {manifest_path}"}

    def apply(self) -> dict:
        """Download, verify, back up, and install the checked release. The app
        is NOT restarted here — the caller restarts (updater.restart_soon or
        `sudo systemctl restart aurora-companion`)."""
        state = self._last_check or self.check()
        if state.get("error"):
            raise UpdaterError(f"cannot update: {state['error']}")
        if not state.get("asset_url"):
            raise UpdaterError("release has no usable zip asset")
        if not state.get("available"):
            raise UpdaterError(f"no newer release (installed {self.version}, "
                               f"latest {state.get('latest_version')})")
        work = self.work_dir()
        stamp = time.strftime("%Y%m%d-%H%M%S")

        # 1. download to a .part file; an interrupted download never installs
        part, final = work / "release.zip.part", work / "release.zip"
        try:
            req = urllib.request.Request(state["asset_url"], headers={
                "User-Agent": "aurora-companion-updater"})
            timeout = float(self.config.get("timeout_seconds", 20.0))
            with urllib.request.urlopen(req, timeout=timeout) as resp, \
                    open(part, "wb") as fh:
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    fh.write(chunk)
            part.replace(final)  # atomic only when the download completed
        except Exception as exc:
            part.unlink(missing_ok=True)
            raise UpdaterError(f"download interrupted, nothing changed: {exc}") from exc

        # 2. verify the archive before touching anything
        try:
            with zipfile.ZipFile(final) as zf:
                bad = zf.testzip()
                if bad is not None:
                    raise UpdaterError(f"corrupt member in release zip: {bad}")
                roots = {m.filename.split("/", 1)[0] for m in zf.infolist()
                         if m.filename.strip("/")}
                if len(roots) != 1:
                    raise UpdaterError(f"unexpected zip layout: {sorted(roots)}")
                root = roots.pop()
        except zipfile.BadZipFile as exc:
            final.unlink(missing_ok=True)
            raise UpdaterError(f"downloaded file is not a valid zip: {exc}") from exc

        # 3. back up app tree + config + an online snapshot of the database
        backup = self._backup(stamp)

        # 4. install. Direct mode (writable app tree — dev or non-hardened
        # deployments) swaps the tree in-process. Assisted mode (Portable:
        # ProtectSystem=strict + NoNewPrivileges make /opt read-only to the
        # service) stages the release inside the writable data dir and hands
        # a strictly validated manifest to the narrowly authorized root
        # helper (aurora-update.service); nothing in the app tree is touched
        # by this process.
        if not self._app_tree_writable():
            return self._stage_assisted(state, backup, final, root)

        staging = self.app_dir.parent / (self.app_dir.name + ".update-tmp")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        try:
            with zipfile.ZipFile(final) as zf:
                zf.extractall(staging)
            new_tree = staging / root
            if not (new_tree / "run.py").is_file():
                raise UpdaterError("release zip does not contain an Aurora tree")
            self._swap_trees(new_tree)
        except Exception:
            # put the previous tree back — the install failed, not the user's data
            self._restore_from_backup(backup)
            shutil.rmtree(staging, ignore_errors=True)
            raise
        finally:
            final.unlink(missing_ok=True)
            shutil.rmtree(staging, ignore_errors=True)

        manifest = {"previous_version": self.version,
                    "new_version": state["latest_version"], "ts": time.time(),
                    "backup": str(backup), "notes": state.get("notes", "")[:2000]}
        (work / "last_install.json").write_text(json.dumps(manifest, indent=2),
                                                encoding="utf-8")
        self.db.set_meta_json("update.pending",
                              {"expected_version": state["latest_version"],
                               "previous_version": self.version,
                               "ts": time.time(), "backup": str(backup)})
        self._prune_backups()
        return {"status": "installed", "restart_required": True,
                "new_version": state["latest_version"], "backup": str(backup)}

    def _stage_assisted(self, state: dict, backup: Path, final: Path, root: str) -> dict:
        """Assisted (Portable) install: everything happens inside the data
        dir; the swap itself is done by the root helper and systemd."""
        work = self.work_dir()
        staging_root = work / "staging"
        shutil.rmtree(staging_root, ignore_errors=True)
        staging_root.mkdir(parents=True)
        try:
            with zipfile.ZipFile(final) as zf:
                zf.extractall(staging_root)
            staged_tree = staging_root / root
            if not (staged_tree / "run.py").is_file():
                raise UpdaterError("release zip does not contain an Aurora tree")
        except Exception:
            shutil.rmtree(staging_root, ignore_errors=True)
            raise
        manifest = {"action": "install",
                    "app_dir": str(self.app_dir), "data_dir": str(self.data_dir),
                    "staging": str(staged_tree), "backup": str(backup),
                    "keep": list(CARRY),
                    "unit": os.environ.get("AURORA_UNIT", "aurora-companion.service"),
                    "previous_version": self.version,
                    "new_version": state["latest_version"], "ts": time.time()}
        manifest_path = work / "install-manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        (work / "last_install.json").write_text(json.dumps(
            {"previous_version": self.version, "new_version": state["latest_version"],
             "ts": time.time(), "backup": str(backup),
             "notes": state.get("notes", "")[:2000]}, indent=2), encoding="utf-8")
        # the running app will be replaced — let the next startup verify it
        self.db.set_meta_json("update.pending",
                              {"expected_version": state["latest_version"],
                               "previous_version": self.version,
                               "ts": time.time(), "backup": str(backup),
                               "assisted": True})
        final.unlink(missing_ok=True)
        trigger = self._trigger_helper(manifest_path)
        self._prune_backups()
        result = {"status": trigger["status"],
                  "new_version": state["latest_version"],
                  "manifest": str(manifest_path), "backup": str(backup)}
        if trigger.get("via"):
            result["via"] = trigger["via"]
        if trigger.get("manual_command"):
            result["manual_command"] = trigger["manual_command"]
        return result

    def _backup(self, stamp: str) -> Path:
        work = self.work_dir()
        backup_dir = work / f"backup-{stamp}"
        backup_dir.mkdir(parents=True)
        archive = backup_dir / "app-tree.tar.gz"

        def _normalized(ti):
            """Store normalized modes: on Portable the tree may be captured
            while its write bits are stripped (ProtectSystem=strict) — a
            backup that restores unwritable directories could not be
            extracted or run."""
            if ti.isdir():
                ti.mode = 0o750
            else:
                ti.mode = 0o644
            return ti

        with tarfile.open(archive, "w:gz") as tf:
            for path in sorted(self.app_dir.rglob("*")):
                rel = path.relative_to(self.app_dir)
                if any(part in EXCLUDE_BACKUP for part in rel.parts):
                    continue
                tf.add(path, arcname=str(self.app_dir.name / rel),
                       filter=_normalized)
        if self.config_path and self.config_path.is_file():
            shutil.copy2(self.config_path, backup_dir / "config.yaml")
        db_snapshot = backup_dir / "aurora.db"
        try:
            self.db.backup_to(db_snapshot)
        except Exception:
            db_snapshot.unlink(missing_ok=True)  # snapshot is best-effort
        return backup_dir

    def _swap_trees(self, new_tree: Path) -> None:
        parent = self.app_dir.parent
        previous = parent / (self.app_dir.name + ".previous")
        shutil.rmtree(previous, ignore_errors=True)
        # local state that lives inside the app tree and must survive the
        # swap: virtualenv, git history, and the dev-mode config/env files
        # (Portable deployments keep config in /etc/aurora — nothing to carry)
        carry = CARRY
        for keep in carry:
            src = self.app_dir / keep
            if src.exists():
                os.replace(src, parent / f".{keep}-carry")
        os.replace(self.app_dir, previous)
        try:
            os.replace(new_tree, self.app_dir)
        except Exception:
            os.replace(previous, self.app_dir)  # put it back
            raise
        finally:
            for keep in carry:
                carried = parent / f".{keep}-carry"
                if carried.exists():
                    os.replace(carried, self.app_dir / keep)
        shutil.rmtree(previous, ignore_errors=True)

    def _restore_from_backup(self, backup_dir: Path) -> None:
        """Restore the previous tree from a backup archive. Local state that
        the archive deliberately excludes (venv, .git, config.yaml,
        aurora.env) is carried aside FIRST and moved back after — a rollback
        must never destroy the virtualenv Aurora needs to restart."""
        archive = backup_dir / "app-tree.tar.gz"
        if not archive.is_file():
            return
        staging = self.app_dir.parent / (self.app_dir.name + ".rollback-tmp")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        with tarfile.open(archive) as tf:
            tf.extractall(staging)
        restored = staging / self.app_dir.name
        if restored.is_dir():
            carry = CARRY
            for keep in carry:
                src = self.app_dir / keep
                if src.exists():
                    os.replace(src, staging / f".{keep}-carry")
            shutil.rmtree(self.app_dir, ignore_errors=True)
            os.replace(restored, self.app_dir)
            for keep in carry:
                carried = staging / f".{keep}-carry"
                if carried.exists():
                    os.replace(carried, self.app_dir / keep)
        shutil.rmtree(staging, ignore_errors=True)

    def _prune_backups(self) -> None:
        backups = sorted(self.work_dir().glob("backup-*"))
        for old in backups[:-BACKUP_KEEP]:
            shutil.rmtree(old, ignore_errors=True)

    # ------------------------------------------------------------------ restart + verify
    def restart_soon(self, delay: float = 1.0) -> dict:
        """Restart after the HTTP response has been flushed. In-place for
        plain runs (os.execv); on systemd deployments the unit is restarted
        when passwordless sudo allows it, otherwise a manual restart is
        reported honestly."""
        unit = os.environ.get("AURORA_UNIT", "")
        if unit and shutil.which("systemctl"):
            import subprocess
            rc = subprocess.run(["sudo", "-n", "systemctl", "restart", unit],
                                capture_output=True).returncode \
                if shutil.which("sudo") else 1
            if rc == 0:
                return {"status": "restarting", "via": "systemd"}
            return {"status": "manual_restart_required",
                    "command": f"sudo systemctl restart {unit}"}
        # delayed in-place restart on a daemon thread: independent of any
        # event loop (a running uvicorn loop, tests, or no loop at all) and
        # still after the HTTP response has been flushed. os.execv replaces
        # the whole process image from any thread.
        import threading

        def _exec():
            time.sleep(delay)
            script = self.script_path or Path(sys.argv[0]).resolve()
            os.chdir(script.parent)  # apply() swapped the tree — the old cwd is gone
            os.execv(sys.executable, [sys.executable, str(script), *sys.argv[1:]])

        threading.Thread(target=_exec, daemon=True).start()
        return {"status": "restarting", "via": "exec"}

    def verify_after_restart(self) -> dict | None:
        """Called once at app startup. If an update was pending, verify the
        running version against the expectation, record the outcome in the
        update history, and return a natural announcement for the
        conversation (or a failure note suggesting rollback)."""
        pending = self.db.get_meta_json("update.pending", None)
        if not pending:
            return None
        self.db.set_meta_json("update.pending", None)
        expected = pending.get("expected_version", "")
        entry = {"from": pending.get("previous_version", "?"), "to": expected,
                 "ts": time.time(), "ran": self.version}
        if expected and self.version == expected:
            entry["result"] = "ok"
            self.db.set_meta_json("update.history",
                                  self.history()[-19:] + [entry])
            return ("I'm back! The update worked, and my memories and settings "
                    f"are still here. I'm now on version {self.version}. "
                    "Want to hear what's new?")
        entry["result"] = "failed"
        self.db.set_meta_json("update.history", self.history()[-19:] + [entry])
        return (f"Heads up: my last update did not complete cleanly — I'm "
                f"running version {self.version}, the update expected "
                f"{expected or '?'}. Nothing was lost, and I can roll back to "
                "the previous version if you'd like.")

    def rollback(self) -> dict:
        """Restore the application tree (and config) from the most recent
        pre-update backup. The database is intentionally NOT reverted — newer
        memories and reminders stay. Restarts to bring the old tree live."""
        manifest_path = self.work_dir() / "last_install.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise UpdaterError(f"no update manifest to roll back: {exc}") from exc
        backup = Path(manifest.get("backup", ""))
        if not (backup / "app-tree.tar.gz").is_file():
            raise UpdaterError(f"backup archive missing: {backup}")
        entry = {"from": manifest.get("new_version", "?"),
                 "to": manifest.get("previous_version", "?"),
                 "ts": time.time(), "ran": self.version, "result": "rolled_back"}
        if not self._app_tree_writable():
            # Portable: the swap belongs to the root helper; stage the
            # rollback manifest and hand it over (same polkit-gated unit)
            rmanifest = {"action": "rollback",
                         "app_dir": str(self.app_dir), "data_dir": str(self.data_dir),
                         "backup": str(backup), "keep": list(CARRY),
                         "unit": os.environ.get("AURORA_UNIT", "aurora-companion.service"),
                         "config_path": str(self.config_path or ""),
                         "previous_version": manifest.get("previous_version", ""),
                         "new_version": manifest.get("new_version", ""), "ts": time.time()}
            rpath = self.work_dir() / "rollback-manifest.json"
            rpath.write_text(json.dumps(rmanifest, indent=2), encoding="utf-8")
            self.db.set_meta_json("update.history", self.history()[-19:] + [entry])
            self.db.set_meta_json("update.pending", None)
            trigger = self._trigger_helper(rpath)
            result = {"status": trigger["status"],
                      "version": manifest.get("previous_version"),
                      "manifest": str(rpath)}
            if trigger.get("via"):
                result["via"] = trigger["via"]
            if trigger.get("manual_command"):
                result["manual_command"] = trigger["manual_command"]
            return result
        self._restore_from_backup(backup)
        if (backup / "config.yaml").is_file() and self.config_path:
            shutil.copy2(backup / "config.yaml", self.config_path)
        self.db.set_meta_json("update.history", self.history()[-19:] + [entry])
        self.db.set_meta_json("update.pending", None)
        restart = self.restart_soon()
        return {"status": "rolled_back", "version": manifest.get("previous_version"),
                "restart": restart}
