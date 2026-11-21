#!/usr/bin/env python3
"""Reproduce (or refute) the TAR-extraction escape in the rollback path.

Builds a rollback backup archive containing unsafe members, runs the real
deploy/aurora-update-helper against it (test-only authorization override),
and reports exactly where files landed. Run BEFORE the fix: it demonstrates
the escape. Run AFTER the fix: the helper must refuse the archive outright.
"""

import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "deploy" / "aurora-update-helper"

APP_DIR_NAME = "aurora-companion"


def make_app_tree(base: Path) -> Path:
    app = base / APP_DIR_NAME
    (app / "aurora").mkdir(parents=True)
    (app / "run.py").write_text("print('app')\n")
    (app / "aurora" / "__init__.py").write_text('__version__ = "0.3.0"\n')
    return app


def malicious_archive(backup: Path, tmp: Path) -> None:
    """A 'backup' the service account could have written: one legitimate tree
    plus unsafe members (traversal escaping the authorized data dir, absolute
    path, symlink, fifo)."""
    with tarfile.open(backup / "app-tree.tar.gz", "w:gz") as tf:

        def add_file(name: str, data: str) -> None:
            ti = tarfile.TarInfo(name)
            ti.type = tarfile.REGTYPE
            ti.size = len(data)
            ti.mode = 0o644
            tf.addfile(ti, io.BytesIO(data.encode()))

        add_file(f"{APP_DIR_NAME}/run.py", "print('evil tree')\n")
        # 4-level traversal: staging/app/../../../../X escapes the authorized
        # data dir entirely (data = tmp/var/lib/aurora → lands in var/lib)
        add_file(f"{APP_DIR_NAME}/../../../../TRAVErsal-ESCAPE.txt", "traversal\n")
        # absolute member — extracted at that absolute path, outside staging
        add_file(str(tmp / "ABSOLUTE-ESCAPE.txt"), "absolute\n")
        # symlink pointing outside the staging dir
        ti = tarfile.TarInfo(f"{APP_DIR_NAME}/symlink-escape")
        ti.type = tarfile.SYMTYPE
        ti.linkname = str(tmp / "symlink-target.txt")
        tf.addfile(ti)
        # a fifo — not a regular file or directory
        ti = tarfile.TarInfo(f"{APP_DIR_NAME}/fifodev")
        ti.type = tarfile.FIFOTYPE
        tf.addfile(ti)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="tar-vuln-"))
    etc = tmp / "etc"; opt = tmp / "opt"
    data = tmp / "var" / "lib" / "aurora"
    app = make_app_tree(opt)
    (data / "update").mkdir(parents=True)
    backup = data / "update" / "backup-vuln"
    backup.mkdir(parents=True)
    malicious_archive(backup, tmp)

    auth = tmp / "authorized.json"
    auth.write_text(json.dumps({
        "app_dir": str(app), "data_dir": str(data),
        "unit": "aurora-companion.service", "config_path": ""}))
    os.chmod(auth, 0o600)
    manifest = tmp / "rollback-manifest.json"
    manifest.write_text(json.dumps({
        "action": "rollback", "app_dir": str(app), "data_dir": str(data),
        "backup": str(backup), "keep": ["venv"],
        "unit": "aurora-companion.service", "config_path": "",
        "previous_version": "0.2.0", "new_version": "0.3.0"}))

    env = dict(os.environ, AURORA_UPDATE_AUTHORIZED=str(auth))
    r = subprocess.run([sys.executable, str(HELPER), "rollback", str(manifest)],
                       capture_output=True, text=True, env=env)
    out = r.stdout + r.stderr
    canaries = {
        "traversal (4 levels up — outside the AUTHORIZED data dir)":
            data.parent / "TRAVErsal-ESCAPE.txt",
        "absolute path (written at its absolute location)":
            tmp / "ABSOLUTE-ESCAPE.txt",
    }
    print(f"helper exit code: {r.returncode}")
    for label, p in canaries.items():
        print(f"  {label}: {'ESCAPED — file exists' if p.exists() else 'not created'}")
    print("live app run.py:", (app / "run.py").read_text().strip())
    print("--- helper output (tail) ---")
    print("\n".join(out.strip().splitlines()[-4:]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
