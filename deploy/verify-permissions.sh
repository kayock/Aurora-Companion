#!/usr/bin/env bash
# Aurora filesystem-permission verification.
#
# Validates the deployed permission model before/at runtime:
#   app files  $AURORA_HOME   aurora-owned, readable (app code never needs write)
#   data       $AURORA_DATA   aurora-owned, writable
#   config     $AURORA_ETC    aurora-owned 750; config.yaml 600 aurora-owned
#                             (readable AND writable for voice-selection
#                             persistence; auth token hidden from others)
#   aurora.env 640 root:aurora root-managed, service-readable
#   no world-accessible entries under $AURORA_ETC
#
# When run as root it ALSO executes the startup + configuration-persistence
# smoke test under the actual service account (runuser) — this is the check
# that cannot run in a non-root sandbox and belongs in the Portable pass:
#   sudo ./deploy/verify-permissions.sh
#   sudo ./deploy/verify-permissions.sh --smoke    # + run.py --check + config write
#
# Non-root (e.g. CI/sandbox): mode and content checks still run; the
# service-account smoke test is reported as SKIP, honestly.

set -uo pipefail

AURORA_HOME="${AURORA_HOME:-/opt/aurora-companion}"
AURORA_DATA="${AURORA_DATA:-/var/lib/aurora}"
AURORA_ETC="${AURORA_ETC:-/etc/aurora}"
AURORA_USER="${AURORA_USER:-aurora}"
SMOKE=0
FAILS=0
SKIPS=0

[[ "${1:-}" == "--smoke" ]] && SMOKE=1

pass() { echo "  PASS  $1"; }
fail() { echo "  FAIL  $1"; FAILS=$((FAILS+1)); }
skip() { echo "  SKIP  $1"; SKIPS=$((SKIPS+1)); }
hdr()  { echo; echo "== $1 =="; }

IS_ROOT=0; [[ $EUID -eq 0 ]] && IS_ROOT=1
as_service() { runuser -u "$AURORA_USER" -- "$@" 2>/dev/null; }

hdr "layout"
if [[ -d "$AURORA_HOME" && -d "$AURORA_DATA" && -d "$AURORA_ETC" ]]; then
  pass "directories exist ($AURORA_HOME, $AURORA_DATA, $AURORA_ETC)"
else
  fail "a directory is missing — has install.sh been run? (missing: $(for d in "$AURORA_HOME" "$AURORA_DATA" "$AURORA_ETC"; do [[ -d $d ]] || echo -n "$d "; done))"
fi

hdr "ownership + modes"
if [[ -f "$AURORA_ETC/config.yaml" ]]; then
  MODE="$(stat -c %a "$AURORA_ETC/config.yaml")"
  OWNER="$(stat -c %U "$AURORA_ETC/config.yaml")"
  [[ "$MODE" == "600" ]] && pass "config.yaml mode 600 (token private, service can persist)" \
                        || fail "config.yaml mode is $MODE (expected 600)"
  [[ "$OWNER" == "$AURORA_USER" ]] && pass "config.yaml owned by $AURORA_USER (read + persistence under the service account)" \
                            || fail "config.yaml owned by $OWNER (expected $AURORA_USER — the service cannot persist settings)"
else
  fail "config.yaml not found in $AURORA_ETC"
fi

if [[ -d "$AURORA_ETC" ]]; then
  MODE="$(stat -c %a "$AURORA_ETC")"
  [[ "$MODE" == "750" || "$MODE" == "700" ]] && pass "$AURORA_ETC mode $MODE (no world access)" \
                                          || fail "$AURORA_ETC mode $MODE (expected 750/700)"
  WORLD="$(find "$AURORA_ETC" -maxdepth 2 -perm /o+rwx -print -quit 2>/dev/null)"
  [[ -z "$WORLD" ]] && pass "no world-accessible entries under $AURORA_ETC" \
                    || fail "world-accessible entries: $WORLD"
  if [[ -f "$AURORA_ETC/aurora.env" ]]; then
    EMODE="$(stat -c %a "$AURORA_ETC/aurora.env")"
    EOWN="$(stat -c %U:%G "$AURORA_ETC/aurora.env")"
    if [[ $IS_ROOT -eq 1 ]]; then
      [[ "$EMODE" == "640" && "$EOWN" == "root:$AURORA_USER" ]] \
        && pass "aurora.env $EMODE $EOWN (root-managed, service-readable)" \
        || fail "aurora.env is $EMODE $EOWN (expected 640 root:$AURORA_USER)"
    else
      [[ "$EMODE" == "640" || "$EMODE" == "600" ]] \
        && pass "aurora.env mode $EMODE (ownership root:$AURORA_USER can only be verified as root)" \
        || fail "aurora.env mode $EMODE (expected 640)"
    fi
  fi
  if [[ -d "$AURORA_ETC/tls" ]]; then
    TMODE="$(stat -c %a "$AURORA_ETC/tls")"
    if [[ $IS_ROOT -eq 1 ]]; then
      TOWN="$(stat -c %U:%G "$AURORA_ETC/tls")"
      [[ "$TMODE" == "750" && "$TOWN" == "root:$AURORA_USER" ]] \
        && pass "tls dir $TMODE $TOWN (service-traversable, no world access)" \
        || fail "tls dir is $TMODE $TOWN (expected 750 root:$AURORA_USER)"
    else
      [[ "$TMODE" == "750" || "$TMODE" == "700" ]] \
        && pass "tls dir mode $TMODE" || fail "tls dir mode $TMODE (expected 750)"
    fi
    for tlsfile in aurora.crt aurora.key; do
      if [[ -f "$AURORA_ETC/tls/$tlsfile" ]]; then
        FMODE="$(stat -c %a "$AURORA_ETC/tls/$tlsfile")"
        [[ "$FMODE" == "640" || "$FMODE" == "600" ]] \
          && pass "tls/$tlsfile mode $FMODE (service-readable, no world access)" \
          || fail "tls/$tlsfile mode $FMODE (expected 640 — never world-readable)"
      fi
    done
  fi
fi

for d in "$AURORA_HOME" "$AURORA_DATA"; do
  if [[ -d "$d" ]]; then
    OWNER="$(stat -c %U "$d")"
    [[ "$OWNER" == "$AURORA_USER" ]] && pass "$d owned by $AURORA_USER" \
                                     || fail "$d owned by $OWNER (expected $AURORA_USER)"
  fi
done
if [[ -f "$AURORA_HOME/run.py" ]]; then
  [[ -r "$AURORA_HOME/run.py" ]] && pass "app tree readable" || fail "app tree unreadable"
fi
# separation of concerns: the service must never NEED to write the app tree
if [[ -d "$AURORA_HOME" ]]; then
  WW="$(find "$AURORA_HOME" -maxdepth 1 -perm /o+w -print -quit 2>/dev/null)"
  [[ -z "$WW" ]] && pass "app tree has no world-writable entries" \
                 || fail "world-writable app entries: $WW"
fi

hdr "service-account smoke test (startup + configuration persistence)"
if [[ $IS_ROOT -eq 1 ]] && command -v runuser >/dev/null && id "$AURORA_USER" >/dev/null 2>&1; then
  PYBIN="$AURORA_HOME/venv/bin/python"
  [[ -x "$PYBIN" ]] || PYBIN="python3"
  if as_service test -r "$AURORA_ETC/config.yaml"; then
    pass "service account can read config.yaml"
  else
    fail "service account CANNOT read config.yaml"
  fi
  if as_service test -w "$AURORA_ETC/config.yaml"; then
    pass "service account can write config.yaml (voice-selection persistence)"
  else
    fail "service account CANNOT write config.yaml"
  fi
  if [[ $SMOKE -eq 1 ]]; then
    if runuser -u "$AURORA_USER" -- env PYTHONPATH="$AURORA_HOME" "$PYBIN" -c "
from aurora.config import Config
import sys
c = Config.load(sys.argv[1])
c.set('voice.voice_id', c.get('voice.voice_id', 'am_adam'))
c.save()
print('        configuration read + persisted OK')" "$AURORA_ETC/config.yaml" 2>/dev/null; then
      pass "Config.load → set → save round-trip as $AURORA_USER"
    else
      fail "configuration persistence failed under $AURORA_USER"
    fi
    runuser -u "$AURORA_USER" -- "$PYBIN" "$AURORA_HOME/run.py" \
      --config "$AURORA_ETC/config.yaml" --check >/dev/null 2>&1 && RC=0 || RC=$?
    if [[ $RC -le 2 ]]; then
      pass "run.py --check started under $AURORA_USER (rc=$RC; 0/1/2 all mean the app boots)"
    else
      fail "run.py --check rc=$RC under $AURORA_USER"
    fi
  else
    skip "startup smoke not requested — re-run with --smoke"
  fi
else
  skip "requires root + the service account (run on Portable with sudo) — mode checks above are authoritative for layout"
fi

hdr "summary"
echo "  $FAILS failed, $SKIPS skipped (skips require the real machine)"
exit "$FAILS"
