#!/usr/bin/env bash
# Generate a self-signed TLS certificate for Aurora's LAN HTTPS.
#
# Why: browsers only grant microphone access (getUserMedia) in a secure
# context — HTTPS or localhost. Serving TLS on Aurora's own port (8795)
# enables push-to-talk from other LAN machines WITHOUT adding a service or
# touching any existing HTTPS service (Kayock Voice, Father Fox, ...).
#
#   sudo ./deploy/make-cert.sh                      # defaults below
#   sudo ./deploy/make-cert.sh --hostname kayock-Ubuntu-Portable --ip 10.0.0.45
#   sudo ./deploy/install.sh --tls                  # installer variant
#
# Output: $AURORA_ETC/tls/aurora.crt + aurora.key (dir 0750, both files 0640
# root:$SERVICE_USER — service-readable, no world access; the certificate is
# not secret but gets the same blanket policy as the config dir). Trusting the
# certificate in browsers is described
# in docs/PORTABLE_DEPLOY.md §4b — until then browsers show the usual
# self-signed-certificate warning you accept once.

set -euo pipefail

HOSTNAME_CERT="${AURORA_HOSTNAME:-kayock-Ubuntu-Portable}"
IP="${AURORA_IP:-10.0.0.45}"
OUT="${AURORA_ETC:-/etc/aurora}/tls"
DAYS="${AURORA_CERT_DAYS:-3650}"
SERVICE_USER="${AURORA_USER:-aurora}"
MODE="0750"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --hostname) HOSTNAME_CERT="$2"; shift 2 ;;
    --ip) IP="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --user) SERVICE_USER="$2"; shift 2 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

command -v openssl >/dev/null || { echo "ERROR: openssl not found (sudo apt install openssl)" >&2; exit 1; }
mkdir -p "$OUT"
CRT="$OUT/aurora.crt"
KEY="$OUT/aurora.key"

if [[ -s "$CRT" && -s "$KEY" ]]; then
  echo "certificate already present: $CRT (delete to regenerate)"
else
  SUBJ="/C=XX/O=Aurora Companion/CN=$HOSTNAME_CERT"
  if openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days "$DAYS" \
       -keyout "$KEY" -out "$CRT" -subj "$SUBJ" \
       -addext "subjectAltName=DNS:$HOSTNAME_CERT,DNS:localhost,IP:$IP,IP:127.0.0.1" \
       2>/dev/null; then
    echo "certificate written:"
    echo "   $CRT"
    echo "   $KEY"
    openssl x509 -in "$CRT" -noout -subject -ext subjectAltName 2>/dev/null | sed 's/^/   /'
  else
    echo "ERROR: openssl certificate generation failed" >&2
    exit 1
  fi
fi

# TLS permission policy — consistent with the installer and
# deploy/verify-permissions.sh: EVERYTHING under the config dir is invisible
# to "other" users. The certificate is public material, but nothing needs to
# read it except root and the service account, so it gets the same treatment:
#   tls dir   0750 root:$SERVICE_USER   traversable for the service only
#   aurora.crt 0640 root:$SERVICE_USER  service-readable (uvicorn loads it)
#   aurora.key 0640 root:$SERVICE_USER  private, service-readable
if [[ $EUID -eq 0 ]]; then
  chown root:"$SERVICE_USER" "$OUT"
  chmod 0750 "$OUT"
  chown root:"$SERVICE_USER" "$CRT" "$KEY"
  chmod 0640 "$CRT" "$KEY"
  echo "ownership: $OUT, $CRT, $KEY -> root:$SERVICE_USER 0750/0640/0640"
else
  chmod 0640 "$CRT" "$KEY"
  echo "NOTE: not root — set ownership yourself: chown -R root:$SERVICE_USER '$OUT'"
fi

cat <<EOF

NEXT (Aurora config, e.g. /etc/aurora/config.yaml):
    server:
      auth_token: "…"
      tls_cert: $CRT
      tls_key: $KEY
  then: sudo systemctl restart aurora-companion
  trust once per LAN browser: docs/PORTABLE_DEPLOY.md §4b
EOF
