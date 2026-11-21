#!/usr/bin/env bash
# Aurora ↔ existing voice services — post-install connection checklist.
#
# Read-only: this script only GETs the services' status endpoints and sends
# one small test synthesis/transcription, exactly what Aurora itself does.
# It never configures, restarts or unloads anything. The AURORA_TOKEN value
# is never printed.
#
#   sudo ./deploy/verify-voice.sh
#   TTS_URL=http://10.0.0.45:8768 STT_URL=http://127.0.0.1:8769 ./deploy/verify-voice.sh
#   AURORA_URL=https://10.0.0.45:8795 AURORA_TOKEN=... ./deploy/verify-voice.sh

set -uo pipefail

TTS_URL="${TTS_URL:-http://10.0.0.45:8768}"       # kayock-openai-speech-adapter
STT_URL="${STT_URL:-http://127.0.0.1:8769}"       # kayock-craft-stt (faster-whisper)
TL_URL="${TL_URL:-http://127.0.0.1:8791}"         # GPU Traffic Light (optional probe)
AURORA_URL="${AURORA_URL:-http://127.0.0.1:8795}"
AURORA_TOKEN="${AURORA_TOKEN:-}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
FAILS=0

pass() { echo "  PASS  $1"; }
fail() { echo "  FAIL  $1"; FAILS=$((FAILS+1)); }
warn() { echo "  WARN  $1"; }
hdr()  { echo; echo "== $1 =="; }

# Auth header as a proper Bash array (no word-splitting surprises). The token
# is only ever passed as a curl argument, never echoed.
AUTH_ARGS=()
AUTH_ENABLED=0
if [[ -n "$AURORA_TOKEN" ]]; then
  AUTH_ARGS=(-H "Authorization: Bearer $AURORA_TOKEN")
  AUTH_ENABLED=1
fi

# Local self-signed TLS is accepted with -k for EVERY request to Aurora
# (HTTPS on 8795 uses the deploy/make-cert.sh local certificate — trusting it
# in the browser is documented separately and NOT required here). TLS_ARGS
# applies to both the authenticated and the unauthenticated probes so the 401
# check behaves identically over HTTPS. It is never applied to the other
# (http) services.
TLS_ARGS=()
if [[ "$AURORA_URL" == https:* ]]; then
  TLS_ARGS=(-k)
fi

aurora_curl() {
  curl -sS -m 10 "${TLS_ARGS[@]}" "${AUTH_ARGS[@]}" "$@"
}

hdr "1. Speech adapter ($TTS_URL — OpenAI-compatible TTS)"

MODELS_JSON="$(curl -sS -m 10 "$TTS_URL/v1/models" 2>"$TMP/tts.err" || true)"
if [[ -n "$MODELS_JSON" ]]; then
  pass "GET /v1/models answered"
  VOICES="$(echo "$MODELS_JSON" | python3 -c '
import json, sys
raw = sys.stdin.read()
try:
    d = json.loads(raw)
except Exception:
    print("")
    raise SystemExit
items = d.get("data", []) if isinstance(d, dict) else d
ids = []
for it in items or []:
    if isinstance(it, str):
        ids.append(it)
    elif isinstance(it, dict):
        for k in ("id", "name", "voice_id", "voice"):
            if it.get(k):
                ids.append(str(it[k]))
                break
print(" ".join(sorted(set(ids))))' 2>/dev/null || true)"
  if [[ -n "$VOICES" ]]; then
    pass "voice ids discovered: $VOICES"
    echo "        (configure one in the dashboard: Settings → Voice)"
  else
    warn "no voice ids parseable from /v1/models — send this JSON to confirm the shape:"
    echo "        $MODELS_JSON" | head -c 400; echo
  fi
else
  fail "GET /v1/models unreachable: $(head -c 120 "$TMP/tts.err")"
fi

hdr "2. Speech synthesis test (POST /v1/audio/speech)"
BODY_OK=0
if curl -sS -m 60 -o "$TMP/speech.mp3" -w "%{http_code}" \
     -H "Content-Type: application/json" \
     -d '{"model":"kokoro","input":"Aurora connection check.","voice":"am_adam","response_format":"mp3"}' \
     "$TTS_URL/v1/audio/speech" > "$TMP/speech.code" 2>/dev/null; then
  CODE="$(cat "$TMP/speech.code")"
  SIZE="$(stat -c%s "$TMP/speech.mp3" 2>/dev/null || echo 0)"
  if [[ "$CODE" == "200" && "$SIZE" -gt 1000 ]]; then
    pass "POST /v1/audio/speech → $SIZE bytes of audio (format mp3)"
    BODY_OK=1
  else
    fail "synthesis HTTP $CODE, $SIZE bytes — if 400/422, adjust voice.kayock_tts_model / voice_id"
  fi
else
  fail "POST /v1/audio/speech unreachable"
fi

hdr "3. Faster-Whisper STT ($STT_URL — POST /v1/audio/transcriptions)"
python3 - "$TMP/check.wav" <<'PY' 2>/dev/null || echo "WAVFAIL" > "$TMP/check.wav"
import math, struct, sys, wave
with wave.open(sys.argv[1], "w") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
    frames = b"".join(struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / 16000)))
                      for i in range(16000))
    w.writeframes(frames)
PY
if curl -sS -m 120 -o "$TMP/stt.json" -w "%{http_code}" \
     -F "file=@$TMP/check.wav;type=audio/wav" -F "model=base.en" \
     "$STT_URL/v1/audio/transcriptions" > "$TMP/stt.code" 2>/dev/null; then
  CODE="$(cat "$TMP/stt.code")"
  if [[ "$CODE" == "200" ]]; then
    pass "POST /v1/audio/transcriptions → HTTP 200"
    echo "        response: $(head -c 200 "$TMP/stt.json")"
    echo "        (a 440 Hz tone may transcribe to empty/gibberish — HTTP 200 is the success signal;"
    echo "         lazy loading means the first call can take tens of seconds)"
  else
    fail "transcription HTTP $CODE: $(head -c 200 "$TMP/stt.json")"
  fi
else
  fail "STT service unreachable at $STT_URL"
fi

hdr "4. GPU Traffic Light probe ($TL_URL — protocol inspection, optional)"
TL_REPLIED=0
for path in /health /status /; do
  BODY="$(curl -sS -m 5 "$TL_URL$path" 2>/dev/null || true)"
  if [[ -n "$BODY" ]]; then
    TL_REPLIED=1
    pass "GET $path answered:"
    echo "        $(echo "$BODY" | head -c 300)"
  fi
done
if [[ $TL_REPLIED -eq 0 ]]; then
  warn "no probe answered — Aurora runs fine without it (resources.traffic_light_url stays empty)"
else
  echo "  NEXT: if the JSON above shows a busy/idle signal, set resources.traffic_light_url"
  echo "        in config.yaml and adjust resources.busy_markers to match its field names."
fi

hdr "5. Aurora itself ($AURORA_URL)"
if [[ "$AURORA_URL" == https:* ]]; then
  echo "        (using --insecure for the LOCAL self-signed certificate only)"
fi
STATUS="$(aurora_curl "$AURORA_URL/api/status" 2>"$TMP/aur.err" || true)"
if echo "$STATUS" | grep -q '"version"'; then
  pass "GET /api/status OK (authenticated)"
  VOICE_STATE="$(echo "$STATUS" | python3 -c '
import json, sys
d = json.load(sys.stdin)
v = d.get("voice", {})
print("tts={} voice={} stt={} stt_available={}".format(
    v.get("tts"), v.get("voice"), v.get("stt"), v.get("stt_available")))' 2>/dev/null || true)"
  echo "        voice: $VOICE_STATE"
  RESOURCES_STATE="$(echo "$STATUS" | python3 -c '
import json, sys
d = json.load(sys.stdin)
r = d.get("resources", {}) or {}
c = (r.get("coordinator") or {}) if isinstance(r, dict) else {}
print("traffic_light={} last_deferral={}".format(
    c.get("state"), bool(r.get("last_deferral")) if isinstance(r, dict) else False))' 2>/dev/null || true)"
  [[ -n "$RESOURCES_STATE" ]] && echo "        resources: $RESOURCES_STATE"
else
  fail "Aurora not answering (with auth args): $(head -c 120 "$TMP/aur.err")"
fi

if [[ $AUTH_ENABLED -eq 1 ]]; then
  # Probe WITHOUT the auth args (but with the local TLS handling). A received
  # HTTP code wins even if the transport closes rudely afterwards; only an
  # empty/absent code means a connection-level failure (000).
  UNAUTH_CODE="$(curl -sS -m 10 "${TLS_ARGS[@]}" -o /dev/null -w "%{http_code}" \
                 "$AURORA_URL/api/status" 2>/dev/null || true)"
  [[ "$UNAUTH_CODE" =~ ^[0-9]{3}$ ]] || UNAUTH_CODE="000"
  if [[ "$UNAUTH_CODE" == "401" ]]; then
    pass "requests WITHOUT the token are rejected (HTTP 401)"
  elif [[ "$UNAUTH_CODE" == "200" ]]; then
    warn "API answered WITHOUT the token (HTTP 200) — server.auth_token not active?"
  else
    warn "unauthenticated probe got HTTP $UNAUTH_CODE (000 = connection failure)"
  fi
else
  echo "  NOTE: no AURORA_TOKEN given; if server.auth_token is set, pass it: AURORA_TOKEN=... $0"
fi

hdr "summary"
if [[ $FAILS -eq 0 ]]; then
  echo "  all mandatory checks passed; Aurora can use the existing voice services."
else
  echo "  $FAILS check(s) failed — see docs/PORTABLE_DEPLOY.md § troubleshooting."
fi
exit "$FAILS"
