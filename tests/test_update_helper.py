#!/usr/bin/env python3
"""Security and activation-scenario tests for the root update helper.

These run deploy/aurora-update-helper as a subprocess (the way the systemd
unit invokes it) against temporary directories, with the helper's authorized
values provided through the test-only env override. On a real Portable
deployment the same rules are enforced from /etc/aurora-update-authorized.json
(root-owned; the manifest cannot influence them).
"""

import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "deploy" / "aurora-update-helper"


def run_helper(args, auth_file: Path, **kw):
    env = dict(os.environ, AURORA_UPDATE_AUTHORIZED=str(auth_file))
    return subprocess.run([sys.executable, str(HELPER), *args],
                          capture_output=True, text=True, env=env, **kw)


def authorized_file(tmp_path: Path, **overrides) -> Path:
    auth = {"app_dir": str(tmp_path / "opt" / "aurora-companion"),
            "data_dir": str(tmp_path / "var" / "lib" / "aurora"),
            "unit": "aurora-companion.service",
            "config_path": str(tmp_path / "etc" / "aurora" / "config.yaml")}
    auth.update(overrides)
    path = tmp_path / "authorized.json"
    path.write_text(json.dumps(auth), encoding="utf-8")
    os.chmod(path, 0o600)  # owned by the invoking user, not group/world-writable
    return path


def make_app_tree(app_dir: Path) -> None:
    (app_dir / "aurora").mkdir(parents=True)
    (app_dir / "run.py").write_text("print('hi')\n")
    (app_dir / "aurora" / "__init__.py").write_text('__version__ = "0.2.0"\n')
    (app_dir / "venv" / "bin").mkdir(parents=True)
    (app_dir / "venv" / "bin" / "python").write_text("#!/bin/sh\n")


def stage_release(tmp_path: Path, data_dir: Path, version: str) -> Path:
    staging = data_dir / "update" / "staging" / "aurora-companion"
    (staging / "aurora").mkdir(parents=True)
    (staging / "run.py").write_text(f"# release {version}\n")
    (staging / "aurora" / "__init__.py").write_text(f'__version__ = "{version}"\n')
    return staging


def make_backup(tmp_path: Path, data_dir: Path, app_dir: Path) -> Path:
    """A backup archive as aurora/updater.py would produce (normalized modes)."""
    backup = data_dir / "update" / "backup-test"
    backup.mkdir(parents=True)
    with tarfile.open(backup / "app-tree.tar.gz", "w:gz") as tf:
        for p in sorted(app_dir.rglob("*")):
            ti = tf.gettarinfo(str(p), arcname=str(
                Path(app_dir.name) / p.relative_to(app_dir)))
            ti.mode = 0o750 if ti.isdir() else 0o644
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = "root"
            if p.is_file():
                with open(p, "rb") as fh:
                    tf.addfile(ti, fh)
            else:
                tf.addfile(ti)
    return backup


def write_manifest(tmp_path: Path, name: str, **fields) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(fields), encoding="utf-8")
    return path


# ---------------------------------------------------------------- negative security tests

def test_refuses_foreign_app_dir(tmp_path):
    """A writable manifest cannot point the root helper at another tree."""
    auth = authorized_file(tmp_path)
    data_dir = Path(json.loads(auth.read_text())["data_dir"])
    (data_dir / "update" / "staging" / "aurora-companion").mkdir(parents=True)
    m = write_manifest(tmp_path, "m.json", action="install",
                       app_dir="/etc", data_dir=str(data_dir),
                       staging=str(data_dir / "update" / "staging" / "aurora-companion"),
                       backup=str(data_dir / "update" / "backup-test"),
                       keep=["config.yaml"], unit="aurora-companion.service")
    r = run_helper(["validate", str(m)], auth)
    assert r.returncode != 0 and "not the authorized installation" in r.stderr + r.stdout


def test_refuses_traversal_app_dir(tmp_path):
    auth = authorized_file(tmp_path)
    data_dir = Path(json.loads(auth.read_text())["data_dir"])
    (data_dir / "update" / "staging" / "aurora-companion").mkdir(parents=True)
    escaped = str(Path(data_dir) / ".." / ".." / "etc")
    m = write_manifest(tmp_path, "m.json", action="install",
                       app_dir=escaped, data_dir=str(data_dir),
                       staging=str(data_dir / "update" / "staging" / "aurora-companion"),
                       backup=str(data_dir / "update" / "backup-test"),
                       keep=["config.yaml"], unit="aurora-companion.service")
    r = run_helper(["validate", str(m)], auth)
    assert r.returncode != 0 and "not the authorized installation" in r.stderr + r.stdout


def test_refuses_symlink_escape(tmp_path):
    """Paths are canonicalized before comparison: a manifest may point through
    a symlink ONLY if it resolves to the authorized target; one resolving
    elsewhere is refused. Symlinked staging outside the data dir too."""
    app_dir = tmp_path / "opt" / "aurora-companion"
    make_app_tree(app_dir)  # authorized value records this REAL directory
    auth = authorized_file(tmp_path)
    a = json.loads(auth.read_text())
    data_dir = Path(a["data_dir"])
    (data_dir / "update" / "staging" / "aurora-companion").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "evil").write_text("x")
    decoy = tmp_path / "decoy-link"
    decoy.symlink_to(elsewhere)  # resolves outside the authorized tree
    m = write_manifest(tmp_path, "m.json", action="install",
                       app_dir=str(decoy), data_dir=a["data_dir"],
                       staging=str(data_dir / "update" / "staging" / "aurora-companion"),
                       backup=str(data_dir / "update" / "backup-test"),
                       keep=["config.yaml"], unit=a["unit"])
    r = run_helper(["validate", str(m)], auth)
    assert r.returncode != 0 and "not the authorized installation" in r.stderr + r.stdout
    # symlink INSIDE the data dir but resolving OUTSIDE: staging escape
    esc_link = data_dir / "update" / "staging-link"
    esc_link.symlink_to(elsewhere)
    m2 = write_manifest(tmp_path, "m2.json", action="install",
                        app_dir=a["app_dir"], data_dir=a["data_dir"],
                        staging=str(esc_link),
                        backup=str(data_dir / "update" / "backup-test"),
                        keep=["config.yaml"], unit=a["unit"])
    r2 = run_helper(["validate", str(m2)], auth)
    assert r2.returncode != 0 and "outside the data dir" in r2.stderr + r2.stdout


def test_refuses_foreign_unit_name(tmp_path):
    """The service identity is root-controlled: restarting another unit via a
    crafted manifest must fail."""
    auth = authorized_file(tmp_path)
    data_dir = Path(json.loads(auth.read_text())["data_dir"])
    (data_dir / "update" / "staging" / "aurora-companion").mkdir(parents=True)
    m = write_manifest(tmp_path, "m.json", action="install",
                       app_dir=json.loads(auth.read_text())["app_dir"],
                       data_dir=str(data_dir),
                       staging=str(data_dir / "update" / "staging" / "aurora-companion"),
                       backup=str(data_dir / "update" / "backup-test"),
                       keep=["config.yaml"], unit="sshd.service")
    r = run_helper(["validate", str(m)], auth)
    assert r.returncode != 0 and "not the authorized service" in r.stderr + r.stdout


def test_refuses_foreign_config_path_in_rollback(tmp_path):
    """Rollback must not copy backup content over an arbitrary root file."""
    app_dir = tmp_path / "opt" / "aurora-companion"
    make_app_tree(app_dir)
    auth = authorized_file(tmp_path)
    a = json.loads(auth.read_text())
    data_dir = Path(a["data_dir"])
    (data_dir / "update").mkdir(parents=True)
    backup = make_backup(tmp_path, data_dir, app_dir)
    (backup / "config.yaml").write_text("attacker content\n")
    m = write_manifest(tmp_path, "m.json", action="rollback",
                       app_dir=a["app_dir"], data_dir=a["data_dir"],
                       backup=str(backup), keep=["venv"],
                       unit=a["unit"], config_path="/etc/shadow")
    r = run_helper(["validate", str(m)], auth)
    assert r.returncode != 0 and "not the authorized config" in r.stderr + r.stdout


def test_refuses_foreign_data_dir(tmp_path):
    """Backup/staging containment is checked against the ROOT-AUTHORIZED data
    dir, not the data dir the manifest claims."""
    app_dir = tmp_path / "opt" / "aurora-companion"
    make_app_tree(app_dir)
    auth = authorized_file(tmp_path)
    a = json.loads(auth.read_text())
    evil_backup = tmp_path / "elsewhere" / "backup"
    evil_backup.mkdir(parents=True)
    m = write_manifest(tmp_path, "m.json", action="rollback",
                       app_dir=a["app_dir"], data_dir=a["data_dir"],
                       backup=str(evil_backup), keep=["venv"],
                       unit=a["unit"], config_path="")
    r = run_helper(["validate", str(m)], auth)
    assert r.returncode != 0 and "outside the data dir" in r.stderr + r.stdout


def test_refuses_unwritable_authorization_file(tmp_path):
    """A group/world-writable authorization file is refused (root-owned check
    is additionally enforced on the real system path)."""
    auth = authorized_file(tmp_path)
    os.chmod(auth, 0o666)
    data_dir = Path(json.loads(auth.read_text())["data_dir"])
    (data_dir / "update" / "staging" / "aurora-companion").mkdir(parents=True)
    m = write_manifest(tmp_path, "m.json", action="install",
                       app_dir=json.loads(auth.read_text())["app_dir"],
                       data_dir=str(data_dir),
                       staging=str(data_dir / "update" / "staging" / "aurora-companion"),
                       backup=str(data_dir / "update" / "backup-test"),
                       keep=["config.yaml"], unit="aurora-companion.service")
    r = run_helper(["validate", str(m)], auth)
    assert r.returncode != 0 and "authorization file" in r.stderr + r.stdout


# ---------------------------------------------------------------- activation scenarios

def _prepare_install(tmp_path):
    app_dir = tmp_path / "opt" / "aurora-companion"
    make_app_tree(app_dir)
    auth = authorized_file(tmp_path)
    a = json.loads(auth.read_text())
    data_dir = Path(a["data_dir"])
    (data_dir / "update").mkdir(parents=True)
    staging = stage_release(tmp_path, data_dir, "0.3.0")
    manifest = write_manifest(
        tmp_path, "install-manifest.json", action="install",
        app_dir=a["app_dir"], data_dir=a["data_dir"], staging=str(staging),
        backup=str(data_dir / "update" / "backup-test"), keep=["venv", "config.yaml"],
        unit=a["unit"], previous_version="0.2.0", new_version="0.3.0")
    # the systemd unit reads manifests from the data dir, so place it there
    live = data_dir / "update" / manifest.name
    manifest.replace(live)
    return auth, a, app_dir, live


def test_apply_none_is_inert(tmp_path):
    auth, a, app_dir, _ = _prepare_install(tmp_path)
    (Path(a["data_dir"]) / "update" / "install-manifest.json").unlink()
    r = run_helper(["apply", str(Path(a["data_dir"]) / "update")], auth)
    assert r.returncode == 0 and "nothing to do" in r.stdout
    assert (app_dir / "run.py").read_text() == "print('hi')\n"  # untouched


def test_apply_install_only(tmp_path):
    auth, a, app_dir, manifest = _prepare_install(tmp_path)
    r = run_helper(["apply", str(Path(a["data_dir"]) / "update")], auth)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (app_dir / "aurora" / "__init__.py").read_text() == '__version__ = "0.3.0"\n'
    assert (app_dir / "venv" / "bin" / "python").exists()      # carried across
    assert not manifest.exists()                               # consumed
    assert "not the authorized" not in r.stdout                # no authority warning


def test_apply_rollback_only(tmp_path):
    app_dir = tmp_path / "opt" / "aurora-companion"
    make_app_tree(app_dir)                      # old state: 0.2.0
    auth = authorized_file(tmp_path)
    a = json.loads(auth.read_text())
    data_dir = Path(a["data_dir"])
    (data_dir / "update").mkdir(parents=True)
    backup = make_backup(tmp_path, data_dir, app_dir)   # archive the OLD tree
    (app_dir / "aurora" / "__init__.py").write_text('__version__ = "0.3.0"\n')
    write_manifest(tmp_path, "rollback-manifest.json", action="rollback",
                   app_dir=a["app_dir"], data_dir=a["data_dir"], backup=str(backup),
                   keep=["venv"], unit=a["unit"], config_path=a["config_path"],
                   previous_version="0.2.0", new_version="0.3.0")
    live = data_dir / "update" / "rollback-manifest.json"
    (tmp_path / "rollback-manifest.json").replace(live)
    r = run_helper(["apply", str(data_dir / "update")], auth)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (app_dir / "aurora" / "__init__.py").read_text() == '__version__ = "0.2.0"\n'
    assert (app_dir / "venv" / "bin" / "python").exists()
    assert not live.exists()


def test_apply_both_manifests_refused(tmp_path):
    auth, a, app_dir, install_manifest = _prepare_install(tmp_path)
    data_dir = Path(a["data_dir"])
    (data_dir / "update" / "rollback-manifest.json").write_text(
        json.dumps({"action": "rollback", "app_dir": a["app_dir"],
                    "data_dir": a["data_dir"],
                    "backup": str(data_dir / "update" / "backup-test"),
                    "keep": ["venv"], "unit": a["unit"], "config_path": ""}))
    r = run_helper(["apply", str(data_dir / "update")], auth)
    assert r.returncode != 0 and "both install and rollback" in r.stdout
    assert (app_dir / "run.py").read_text() == "print('hi')\n"  # nothing done
    assert install_manifest.exists()  # neither manifest consumed


def test_invalid_manifest_cannot_trigger_changes(tmp_path):
    auth, a, app_dir, manifest = _prepare_install(tmp_path)
    data_dir = Path(a["data_dir"])
    m = json.loads(manifest.read_text())
    m["keep"] = ["../../etc"]  # keep list must stay within the carry set
    (data_dir / "update" / "install-manifest.json").write_text(json.dumps(m))
    r = run_helper(["apply", str(data_dir / "update")], auth)
    assert r.returncode != 0 and "keep list" in r.stdout
    assert (app_dir / "run.py").read_text() == "print('hi')\n"  # untouched
    assert (data_dir / "update" / "install-manifest.json").exists()


def test_validate_matches_action_semantics(tmp_path):
    """validate works for both manifest kinds and direct install/rollback
    invocation refuses a manifest whose action does not match the verb."""
    auth, a, app_dir, install_manifest = _prepare_install(tmp_path)
    r = run_helper(["validate", str(install_manifest)], auth)
    assert r.returncode == 0 and "manifest OK" in r.stdout
    r = run_helper(["rollback", str(install_manifest)], auth)
    assert r.returncode != 0 and "manifest is for install" in r.stdout


# ---------------------------------------------------------------- safe extraction (rollback archive is service-user-writable)

def _rollback_env(tmp_path):
    """A live app tree, authorized values, and a backup dir ready for an
    archive; returns (auth, manifest fields, app_dir, data_dir)."""
    app_dir = tmp_path / "opt" / "aurora-companion"
    make_app_tree(app_dir)
    auth = authorized_file(tmp_path)
    a = json.loads(auth.read_text())
    data_dir = Path(a["data_dir"])
    (data_dir / "update").mkdir(parents=True)
    return auth, a, app_dir, data_dir


def _write_archive(backup: Path, app_name: str, members) -> None:
    """members: list of TarInfo-like tuples (name, type, payload, link)."""
    with tarfile.open(backup / "app-tree.tar.gz", "w:gz") as tf:
        for name, typ, payload, link in members:
            ti = tarfile.TarInfo(name)
            ti.type = typ
            ti.linkname = link
            if typ == tarfile.REGTYPE:
                ti.size = len(payload)
                tf.addfile(ti, io.BytesIO(payload.encode()))
            else:
                tf.addfile(ti)


def _run_rollback(auth, a, backup, tmp_path, expect_ok):
    manifest = tmp_path / "rollback-manifest.json"
    manifest.write_text(json.dumps(
        {"action": "rollback", "app_dir": a["app_dir"], "data_dir": a["data_dir"],
         "backup": str(backup), "keep": ["venv", "config.yaml"], "unit": a["unit"],
         "config_path": a["config_path"],
         "previous_version": "0.2.0", "new_version": "0.3.0"}))
    return run_helper(["rollback", str(manifest)], auth)


GOOD_TREE = [(f"aurora-companion/{d}", tarfile.REGTYPE, f"# {d}\n", "")
             for d in ("run.py", "aurora/__init__.py")]


def test_rollback_valid_archive_and_preservation(tmp_path):
    """A normal, valid backup archive rolls back cleanly — and the virtualenv,
    config, database, memories and persona files are NOT touched."""
    auth, a, app_dir, data_dir = _rollback_env(tmp_path)
    (app_dir / "config.yaml").write_text("name: Aurora\n")
    db_dir = data_dir / "db"
    db_dir.mkdir(parents=True)
    (db_dir / "aurora.db").write_text("memories+reminders+persona\n")
    backup = data_dir / "update" / "backup-ok"
    backup.mkdir(parents=True)
    _write_archive(backup, "aurora-companion", [
        *GOOD_TREE,
        ("aurora-companion/aurora/server", tarfile.REGTYPE, "# nested ok\n", ""),
    ])
    (app_dir / "aurora" / "__init__.py").write_text('__version__ = "0.3.0"\n')
    r = _run_rollback(auth, a, backup, tmp_path, expect_ok=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (app_dir / "run.py").read_text() == "# run.py\n"          # from archive
    assert (app_dir / "aurora" / "server").read_text() == "# nested ok\n"
    assert (app_dir / "aurora" / "__init__.py").read_text() == "# aurora/__init__.py\n"
    assert (app_dir / "venv" / "bin" / "python").exists()             # carried venv
    assert (app_dir / "config.yaml").read_text() == "name: Aurora\n"  # carried config
    assert (db_dir / "aurora.db").read_text() == "memories+reminders+persona\n"  # db intact


def test_rollback_refuses_traversal_member(tmp_path):
    auth, a, app_dir, data_dir = _rollback_env(tmp_path)
    backup = data_dir / "update" / "backup-t"
    backup.mkdir(parents=True)
    _write_archive(backup, "aurora-companion", [
        *GOOD_TREE,
        ("aurora-companion/../../../../ESCAPED.txt", tarfile.REGTYPE, "x\n", ""),
    ])
    r = _run_rollback(auth, a, backup, tmp_path, expect_ok=False)
    assert r.returncode != 0 and "traversal member" in r.stdout
    assert not (data_dir.parent / "ESCAPED.txt").exists()   # nothing outside
    assert (app_dir / "run.py").read_text() == "print('hi')\n"  # live app untouched


def test_rollback_refuses_absolute_member(tmp_path):
    auth, a, app_dir, data_dir = _rollback_env(tmp_path)
    backup = data_dir / "update" / "backup-a"
    backup.mkdir(parents=True)
    _write_archive(backup, "aurora-companion", [
        *GOOD_TREE,
        (str(tmp_path / "ABS.txt"), tarfile.REGTYPE, "x\n", ""),
    ])
    r = _run_rollback(auth, a, backup, tmp_path, expect_ok=False)
    assert r.returncode != 0 and "absolute member" in r.stdout
    assert not (tmp_path / "ABS.txt").exists()
    assert (app_dir / "run.py").read_text() == "print('hi')\n"


def test_rollback_refuses_symlink_member(tmp_path):
    auth, a, app_dir, data_dir = _rollback_env(tmp_path)
    backup = data_dir / "update" / "backup-s"
    backup.mkdir(parents=True)
    _write_archive(backup, "aurora-companion", [
        *GOOD_TREE,
        ("aurora-companion/evil", tarfile.SYMTYPE, "", "/etc/passwd"),
    ])
    r = _run_rollback(auth, a, backup, tmp_path, expect_ok=False)
    assert r.returncode != 0 and "symlink member" in r.stdout
    assert (app_dir / "run.py").read_text() == "print('hi')\n"


def test_rollback_refuses_hardlink_member(tmp_path):
    """A hardlink member whose target is outside staging would make root write
    through to that target."""
    auth, a, app_dir, data_dir = _rollback_env(tmp_path)
    backup = data_dir / "update" / "backup-h"
    backup.mkdir(parents=True)
    _write_archive(backup, "aurora-companion", [
        *GOOD_TREE,
        ("aurora-companion/evil", tarfile.LNKTYPE, "", str(data_dir.parent / "target.txt")),
    ])
    r = _run_rollback(auth, a, backup, tmp_path, expect_ok=False)
    assert r.returncode != 0 and "hardlink member" in r.stdout
    assert not (data_dir.parent / "target.txt").exists()
    assert (app_dir / "run.py").read_text() == "print('hi')\n"


def test_rollback_refuses_special_members(tmp_path):
    """FIFOs and device files have no place in an application backup."""
    auth, a, app_dir, data_dir = _rollback_env(tmp_path)
    for idx, (typ, label) in enumerate(
            ((tarfile.FIFOTYPE, "special"), (tarfile.CHRTYPE, "special"))):
        backup = data_dir / "update" / f"backup-x{idx}"
        backup.mkdir(parents=True)
        _write_archive(backup, "aurora-companion", [
            *GOOD_TREE,
            ("aurora-companion/fifodev", typ, "", ""),
        ])
        r = _run_rollback(auth, a, backup, tmp_path, expect_ok=False)
        assert r.returncode != 0 and "special" in r.stdout, f"type {typ}"
        assert (app_dir / "run.py").read_text() == "print('hi')\n"


def test_rollback_malformed_archive(tmp_path):
    """A truncated/corrupt archive is refused cleanly — no traceback, live app
    untouched, staging cleaned up."""
    auth, a, app_dir, data_dir = _rollback_env(tmp_path)
    backup = data_dir / "update" / "backup-m"
    backup.mkdir(parents=True)
    (backup / "app-tree.tar.gz").write_bytes(b"\x1f\x8b\x08\x00" + b"corrupt" * 40)
    r = _run_rollback(auth, a, backup, tmp_path, expect_ok=False)
    assert r.returncode != 0 and "malformed backup archive" in r.stdout
    assert "Traceback" not in (r.stdout + r.stderr)
    assert (app_dir / "run.py").read_text() == "print('hi')\n"
    assert not (backup.parent / ".rollback-staging").exists()  # cleaned up


def test_rollback_refuses_overwrite_of_live_tree(tmp_path):
    """Traversal targeting the LIVE application tree (outside staging) must
    fail safely without modifying it."""
    auth, a, app_dir, data_dir = _rollback_env(tmp_path)
    backup = data_dir / "update" / "backup-o"
    backup.mkdir(parents=True)
    live_target = app_dir / "aurora" / "overwritten.py"
    rel = os.path.relpath(live_target, data_dir / "update" / "backup-o"
                          / ".rollback-staging" / "aurora-companion")
    assert rel.startswith("..")  # a genuine staging escape
    _write_archive(backup, "aurora-companion", [
        *GOOD_TREE,
        (f"aurora-companion/{rel}", tarfile.REGTYPE, "PWNED\n", ""),
    ])
    r = _run_rollback(auth, a, backup, tmp_path, expect_ok=False)
    assert r.returncode != 0 and "traversal member" in r.stdout
    assert not live_target.exists()                               # never written
    assert (app_dir / "aurora" / "__init__.py").read_text() == '__version__ = "0.2.0"\n'


def test_rollback_extraction_never_writes_before_validation(tmp_path):
    """The validation pass runs BEFORE any extraction: an archive with a
    legitimate-looking first member and a malicious later one leaves NO
    staging content behind at all."""
    auth, a, app_dir, data_dir = _rollback_env(tmp_path)
    backup = data_dir / "update" / "backup-p"
    backup.mkdir(parents=True)
    _write_archive(backup, "aurora-companion", [
        ("aurora-companion/run.py", tarfile.REGTYPE, "print('x')\n", ""),
        ("aurora-companion/../../out.txt", tarfile.REGTYPE, "x\n", ""),
    ])
    r = _run_rollback(auth, a, backup, tmp_path, expect_ok=False)
    assert r.returncode != 0
    assert not (backup.parent / ".rollback-staging" / "aurora-companion").exists()
    assert not (data_dir / "update" / "out.txt").exists()
