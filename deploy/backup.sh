#!/usr/bin/env bash
# Aurora database backup — run from cron or a systemd timer.
#
# Aurora's entire state lives in ONE SQLite file (data_dir/aurora.db).
# This script takes a consistent snapshot with SQLite's online backup API
# (safe while Aurora is running and while a WAL is active), then gzips it.
# A failed run leaves no file at the final backup path.
#
#   sudo ./deploy/backup.sh [--keep N] [--dest DIR]
#
# systemd timer suggestion (see docs/PORTABLE_DEPLOY.md):
#   OnCalendar=*-*-* 03:30:00
#   Persistent=true

set -euo pipefail

AURORA_DATA="${AURORA_DATA:-/var/lib/aurora}"
DEST="${DEST:-/var/backups/aurora}"
KEEP="${KEEP:-14}"
DB="$AURORA_DATA/aurora.db"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep) KEEP="$2"; shift 2 ;;
    --dest) DEST="$2"; shift 2 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done

[[ -f "$DB" ]] || { echo "ERROR: $DB not found (adjust AURORA_DATA)" >&2; exit 1; }
mkdir -p "$DEST"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$DEST/aurora-$STAMP.db.gz"

python3 - "$DB" "$OUT" <<'PY'
import gzip, os, sqlite3, sys
from pathlib import Path

src, out = sys.argv[1], sys.argv[2]
final = Path(out)
partial_db = final.with_name(final.name + ".partial.db")
partial_gz = final.with_name(final.name + ".partial")

def _cleanup() -> None:
    for path in (partial_db, partial_gz):
        try:
            path.unlink()
        except FileNotFoundError:
            pass

try:
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        src_conn.execute("PRAGMA query_only=1")
        dst_conn = sqlite3.connect(partial_db)
        try:
            src_conn.backup(dst_conn)
            check = dst_conn.execute("PRAGMA integrity_check").fetchone()
            if not check or check[0] != "ok":
                raise SystemExit(f"ERROR: backup integrity_check failed: {check}")
        finally:
            dst_conn.close()
    finally:
        src_conn.close()
    with partial_db.open("rb") as raw, gzip.open(partial_gz, "wb") as gz:
        while True:
            chunk = raw.read(1024 * 1024)
            if not chunk:
                break
            gz.write(chunk)
    os.chmod(partial_gz, 0o600)
    os.replace(partial_gz, final)
except Exception:
    _cleanup()
    raise
finally:
    # The gzip temp is gone after a successful replace. Always drop the
    # uncompressed snapshot so a crash cannot leave a second live database.
    try:
        partial_db.unlink()
    except FileNotFoundError:
        pass

print(f"backup written: {final}")
PY

# retention — process the list in this shell so a failure still exits nonzero
mapfile -t old_backups < <(ls -1t "$DEST"/aurora-*.db.gz 2>/dev/null || true)
if ((${#old_backups[@]} > KEEP)); then
  for old in "${old_backups[@]:KEEP}"; do
    rm -f "$old"
    echo "pruned: $old"
  done
fi

echo "restore hint:"
echo "  sudo systemctl stop aurora-companion"
echo "  gunzip -c $OUT > $AURORA_DATA/aurora.db.restore"
echo "  # confirm it opens, then replace aurora.db (and remove -wal/-shm) with that file"
echo "  sudo systemctl start aurora-companion"
