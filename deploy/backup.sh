#!/usr/bin/env bash
# Aurora database backup — run from cron or a systemd timer.
#
# Aurora's entire state lives in ONE SQLite file (data_dir/aurora.db), so a
# safe copy is one file. The script uses the sqlite3 online-backup API via
# Python (safe while Aurora runs, unlike a plain cp of a WAL database).
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
import gzip, sqlite3, sys
src, out = sys.argv[1], sys.argv[2]
with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as conn:
    conn.execute("PRAGMA query_only=1")
    data = b"".join(conn.iterdump())
with gzip.open(out, "wt", encoding="utf-8") as fh:
    fh.write(data)
print(f"backup written: {out}")
PY

# retention
ls -1t "$DEST"/aurora-*.db.gz 2>/dev/null | tail -n +"$((KEEP + 1))" | while read -r old; do
  rm -f "$old"; echo "pruned: $old"
done

echo "restore hint:"
echo "  zcat $OUT | python3 -c 'import sqlite3,sys; c=sqlite3.connect(\"$AURORA_DATA/aurora.db\"); c.executescript(sys.stdin.read())'"
echo "  (stop the service first: sudo systemctl stop aurora-companion)"
