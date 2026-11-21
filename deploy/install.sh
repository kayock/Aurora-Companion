#!/usr/bin/env bash
# Aurora Companion — Ubuntu Portable installer.
#
# Installs Aurora into configurable directories, registers the systemd
# service, and refuses to collide with ports that are already in use.
# It never touches the existing Kayock / Father Fox / model services.
#
# Usage (from the project root):
#   sudo ./deploy/install.sh                    # defaults below
#   sudo ./deploy/install.sh --port 8800 --home /srv/aurora
#   sudo ./deploy/install.sh --tls              # + self-signed HTTPS (LAN push-to-talk)
#   ./deploy/install.sh --no-service            # files only, no systemd unit
#
# Overridable locations:
#   AURORA_HOME   app dir          (default /opt/aurora-companion)
#   AURORA_DATA   data dir         (default /var/lib/aurora)
#   AURORA_ETC    config dir       (default /etc/aurora)
#   AURORA_USER   service user     (default aurora)

set -euo pipefail

AURORA_HOME="${AURORA_HOME:-/opt/aurora-companion}"
AURORA_DATA="${AURORA_DATA:-/var/lib/aurora}"
AURORA_ETC="${AURORA_ETC:-/etc/aurora}"
AURORA_USER="${AURORA_USER:-aurora}"
AURORA_PORT="${AURORA_PORT:-8795}"
AURORA_HOST="${AURORA_HOST:-0.0.0.0}"
INSTALL_SERVICE=1
WITH_TLS=0

# Ports that already belong to the existing voice/AI ecosystem on Portable.
# Aurora must not bind them; the script reports them for the checklist only.
EXISTING_PORTS="8765 8766 8768 8769 8791"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) AURORA_PORT="$2"; shift 2 ;;
    --host) AURORA_HOST="$2"; shift 2 ;;
    --home) AURORA_HOME="$2"; shift 2 ;;
    --tls) WITH_TLS=1; shift ;;
    --no-service) INSTALL_SERVICE=0; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $EUID -ne 0 && $INSTALL_SERVICE -eq 1 ]]; then
  echo "ERROR: run with sudo (systemd registration needs root), or use --no-service." >&2
  exit 1
fi

echo "== Aurora install =="
echo "   app   : $AURORA_HOME"
echo "   data  : $AURORA_DATA"
echo "   config: $AURORA_ETC"
echo "   user  : $AURORA_USER"
echo "   bind  : $AURORA_HOST:$AURORA_PORT"
echo

# ---------------------------------------------------------------- checks
if ! command -v python3 >/dev/null; then
  echo "ERROR: python3 not found (Ubuntu: sudo apt install python3 python3-venv)" >&2; exit 1
fi
PYVER=$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
echo "python: $PYVER"

port_in_use() {
  if command -v ss >/dev/null; then
    ss -ltn "( sport = :$1 )" 2>/dev/null | grep -q LISTEN
  else
    (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && { exec 3>&-; return 0; } || return 1
  fi
}

# Inspect existing ports — never assume they are free, never bind them.
echo "-- existing voice/AI services (expected occupied, untouched) --"
for p in $EXISTING_PORTS; do
  if port_in_use "$p"; then echo "   $p  in use (existing service — left alone)"; else echo "   $p  free (service not running right now)"; fi
done

if port_in_use "$AURORA_PORT"; then
  echo "ERROR: Aurora's port $AURORA_PORT is already in use. Pick another:" >&2
  echo "       sudo ./deploy/install.sh --port 8796" >&2
  exit 1
fi
echo "-- Aurora port $AURORA_PORT is free --"

# ---------------------------------------------------------------- layout
if ! id "$AURORA_USER" >/dev/null 2>&1; then
  useradd --system --home "$AURORA_HOME" --shell /usr/sbin/nologin "$AURORA_USER"
  echo "created system user: $AURORA_USER"
fi

mkdir -p "$AURORA_HOME" "$AURORA_DATA" "$AURORA_ETC"
echo "-- copying application tree --"
rsync -a --delete \
  --exclude ".git" --exclude "__pycache__" --exclude ".pytest_cache" \
  --exclude "*.pyc" --exclude "data" --exclude "config.yaml" \
  --exclude "venv" --exclude ".venv" \
  "$SRC/" "$AURORA_HOME/"

echo "-- creating venv + dependencies --"
python3 -m venv "$AURORA_HOME/venv"
"$AURORA_HOME/venv/bin/pip" install --upgrade pip -q
"$AURORA_HOME/venv/bin/pip" install -r "$AURORA_HOME/requirements.txt" -q

echo "-- writing config --"
if [[ -f "$AURORA_ETC/config.yaml" ]]; then
  echo "   keeping existing $AURORA_ETC/config.yaml (merge any new keys from deploy/config.portable.yaml)"
else
  sed -e "s|^port: 8795|port: $AURORA_PORT|" -e "s|^host: 0.0.0.0|host: $AURORA_HOST|" \
      -e "s|data_dir: /var/lib/aurora|data_dir: $AURORA_DATA|" \
      -e "s|auth_token: \"CHANGE_ME_openssl_rand_hex_32\"|auth_token: \"\"|"  # empty = no LAN token (home network) \
      "$SRC/deploy/config.portable.yaml" > "$AURORA_ETC/config.yaml"
  echo "   wrote $AURORA_ETC/config.yaml (edit model base_url + model id!)"
fi

# Root-managed environment file. Copied BEFORE the permission block below so
# ownership and mode are applied in one place; an existing file is preserved
# (it is root-managed configuration, like config.yaml).
if [[ ! -f "$AURORA_ETC/aurora.env" ]]; then
  cp "$SRC/deploy/aurora.env.example" "$AURORA_ETC/aurora.env"
fi

chown -R "$AURORA_USER":"$AURORA_USER" "$AURORA_HOME" "$AURORA_DATA"

# Configuration permissions — the model that makes the non-root service work:
#   /etc/aurora   750 aurora:aurora   traversable for the service, invisible to others
#   config.yaml   600 aurora:aurora   the service READS it and PERSISTS user
#                                     preferences (voice selection); the auth token
#                                     inside is hidden from every other account
#   aurora.env    640 root:aurora     root-managed environment, service-readable
# Aurora never runs as root and nothing is ever world-writable.
chown -R "$AURORA_USER":"$AURORA_USER" "$AURORA_ETC"
chmod 750 "$AURORA_HOME" "$AURORA_DATA" "$AURORA_ETC"
chmod 600 "$AURORA_ETC/config.yaml"
if [[ -f "$AURORA_ETC/aurora.env" ]]; then
  chown root:"$AURORA_USER" "$AURORA_ETC/aurora.env"
  chmod 0640 "$AURORA_ETC/aurora.env"
fi

# ------------------------------------------------------------- verify (pre-start)
# Verify all relevant paths and ownership AS THE SERVICE ACCOUNT before the
# service is started. Any failure aborts the install with a clear cause.
echo "-- verifying layout as '$AURORA_USER' --"
VERIFY_OK=1
as_service() { runuser -u "$AURORA_USER" -- "$@"; }
check() {  # check <description> <command...>
  local description="$1"
  shift
  if as_service "$@"; then echo "   ok   $description"; else echo "   FAIL $description"; VERIFY_OK=0; fi
}
check "app tree readable ($AURORA_HOME)" test -r "$AURORA_HOME/run.py"
check "seed persona readable" test -r "$AURORA_HOME/personas/default.json"
check "venv python executable" test -x "$AURORA_HOME/venv/bin/python"
check "config.yaml readable (service account)" test -r "$AURORA_ETC/config.yaml"
check "config.yaml writable (preference persistence)" test -w "$AURORA_ETC/config.yaml"
check "data dir writable ($AURORA_DATA)" test -w "$AURORA_DATA"
if [[ -f "$AURORA_ETC/aurora.env" ]]; then
  check "aurora.env readable" test -r "$AURORA_ETC/aurora.env"
fi
if find "$AURORA_ETC" -maxdepth 2 -perm /o+rwx -print -quit 2>/dev/null | grep -q .; then
  echo "   FAIL world-accessible entries in $AURORA_ETC:"
  find "$AURORA_ETC" -maxdepth 2 -perm /o+rwx 2>/dev/null | sed 's/^/        /'
  VERIFY_OK=0
else
  echo "   ok   no world-accessible entries in $AURORA_ETC"
fi
if [[ $VERIFY_OK -ne 1 ]]; then
  echo "ERROR: permission verification failed — refusing to start the service." >&2
  exit 1
fi

# Startup + configuration-persistence smoke test under the service account.
echo "-- service-account smoke test --"
runuser -u "$AURORA_USER" -- env PYTHONPATH="$AURORA_HOME" "$AURORA_HOME/venv/bin/python" -c "
from aurora.config import Config
import sys
c = Config.load(sys.argv[1])
c.set('voice.voice_id', c.get('voice.voice_id', 'am_adam'))
c.save()
print('   ok   configuration read + persistence works as $AURORA_USER')
" "$AURORA_ETC/config.yaml" || { echo "ERROR: config write failed for $AURORA_USER" >&2; exit 1; }
# run.py --check exits 0 (all good) / 1 (no chat provider configured) / 2
# (provider unreachable) — all three prove the app imports, reads the config
# and probes services; anything else means startup is broken.
runuser -u "$AURORA_USER" -- "$AURORA_HOME/venv/bin/python" "$AURORA_HOME/run.py" \
  --config "$AURORA_ETC/config.yaml" --check >/dev/null 2>&1 && RC=0 || RC=$?
if [[ $RC -le 2 ]]; then
  echo "   ok   application starts under $AURORA_USER (run.py --check rc=$RC)"
else
  echo "   WARN run.py --check rc=$RC — service will still start; inspect: journalctl -u aurora-companion"
fi

# ---------------------------------------------------------------- service
if [[ $INSTALL_SERVICE -eq 1 ]]; then
  echo "-- installing systemd unit --"
  sed -e "s|User=aurora|User=$AURORA_USER|" \
      -e "s|Group=aurora|Group=$AURORA_USER|" \
      -e "s|WorkingDirectory=/opt/aurora-companion|WorkingDirectory=$AURORA_HOME|" \
      -e "s|ExecStart=/opt/aurora-companion/venv/bin/python /opt/aurora-companion/run.py|ExecStart=$AURORA_HOME/venv/bin/python $AURORA_HOME/run.py|" \
      -e "s|ReadWritePaths=/var/lib/aurora /etc/aurora|ReadWritePaths=$AURORA_DATA $AURORA_ETC|" \
      "$SRC/deploy/aurora-companion.service" > /etc/systemd/system/aurora-companion.service

  # optional TLS for secure-context push-to-talk from other LAN machines
  if [[ $WITH_TLS -eq 1 ]]; then
    "$SRC/deploy/make-cert.sh" --out "$AURORA_ETC/tls" || echo "WARN: certificate generation failed — continuing without TLS"
    if [[ -f "$AURORA_ETC/tls/aurora.crt" ]]; then
      sed -i -e "s|^  # tls_cert: |  tls_cert: |" -e "s|^  # tls_key: |  tls_key: |" \
             -e "s|/etc/aurora/tls/aurora.crt|$AURORA_ETC/tls/aurora.crt|" \
             -e "s|/etc/aurora/tls/aurora.key|$AURORA_ETC/tls/aurora.key|" \
             "$AURORA_ETC/config.yaml"
      chmod 640 "$AURORA_ETC/tls/aurora.key"
      echo "   TLS enabled: dashboard will be served at https://...:$AURORA_PORT"
    fi
  fi

  systemctl daemon-reload
  systemctl enable aurora-companion.service
  systemctl restart aurora-companion.service
  sleep 2
  systemctl --no-pager status aurora-companion.service | head -8 || true

  # one-button updater support: the service itself runs read-only
  # (ProtectSystem=strict) and cannot elevate (NoNewPrivileges), so updates
  # are applied by a fixed, root-only helper unit triggered via polkit. The
  # polkit rule authorizes the $AURORA_USER user to START exactly
  # aurora-update.service — nothing else.
  echo "-- installing update helper (aurora-update.service) --"
  # GNU install takes ownership via -o/-g (a "root:root" positional would be
  # parsed as a SOURCE path and abort the whole installer under set -e)
  install -m 0755 -o root -g root \
      "$SRC/deploy/aurora-update-helper" /usr/local/bin/aurora-update-helper
  # the helper's authority is pinned to root-controlled values, never to
  # whatever the manifest claims: non-default installs are recorded here
  # (root-owned 0644; the helper refuses a file it does not own)
  cat > /etc/aurora-update-authorized.json <<EOF
{"app_dir": "$AURORA_HOME", "data_dir": "$AURORA_DATA", "unit": "aurora-companion.service", "config_path": "$AURORA_ETC/config.yaml"}
EOF
  chown root:root /etc/aurora-update-authorized.json
  chmod 0644 /etc/aurora-update-authorized.json
  sed -e "s|/var/lib/aurora/update|$AURORA_DATA/update|g" \
      -e "s|/opt/aurora-companion|$AURORA_HOME|g" \
      "$SRC/deploy/aurora-update.service" > /etc/systemd/system/aurora-update.service
  if [[ -d /etc/polkit-1/rules.d ]]; then
    sed "s|subject.user == \"aurora\"|subject.user == \"$AURORA_USER\"|" \
        "$SRC/deploy/aurora-update.polkit" > /etc/polkit-1/rules.d/60-aurora-update.rules
    echo "   polkit rule installed (/etc/polkit-1/rules.d/60-aurora-update.rules)"
  else
    echo "   WARN polkit rules.d not found — dashboard updates will report the manual command"
  fi
  systemctl daemon-reload

  cat <<EOF

== done ==
  dashboard : $( [[ $WITH_TLS -eq 1 && -f "$AURORA_ETC/tls/aurora.crt" ]] && echo https || echo http)://$AURORA_HOST:$AURORA_PORT  (from the LAN: $( [[ $WITH_TLS -eq 1 && -f "$AURORA_ETC/tls/aurora.crt" ]] && echo https || echo http)://10.0.0.45:$AURORA_PORT)
  auth token: disabled by default (server.auth_token empty); set only if you need LAN gating
  logs      : journalctl -u aurora-companion -f
  status    : $AURORA_HOME/venv/bin/python $AURORA_HOME/run.py --config $AURORA_ETC/config.yaml --check
  voice check: sudo $AURORA_HOME/deploy/verify-voice.sh
  perm check : sudo $AURORA_HOME/deploy/verify-permissions.sh

NEXT: edit $AURORA_ETC/config.yaml → provider.presets.local.base_url/model
      to point at the model server you already run on Portable, then:
      sudo systemctl restart aurora-companion
$( [[ $WITH_TLS -eq 1 ]] && echo "      Push-to-talk from LAN machines now works over HTTPS — trust the
      self-signed certificate once (docs/PORTABLE_DEPLOY.md §4b)." || echo "      Push-to-talk from LAN machines needs HTTPS: sudo ./deploy/install.sh --tls
      (or see docs/PORTABLE_DEPLOY.md §4b).")
EOF
else
  echo "-- files installed (--no-service); start manually:"
  echo "   $AURORA_HOME/venv/bin/python $AURORA_HOME/run.py --config $AURORA_ETC/config.yaml"
fi
