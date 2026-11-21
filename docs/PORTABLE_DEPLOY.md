# Aurora on Ubuntu Portable (kayock-Ubuntu-Portable)

Aurora's permanent host is **Portable** — `kayock-Ubuntu-Portable`, `10.0.0.45`, Ubuntu,
Intel i7-8850H, ~45 GiB RAM, NVIDIA Quadro P2000 (4 GB). The machine already runs the
Kayock Voice ecosystem and other AI workloads. Aurora joins it as a **lightweight
companion service**:

- no language model is reserved — Aurora makes short request bursts against whatever
  model server you already run, and yields compute whenever it is needed elsewhere;
- the existing voice services are **used, never modified**;
- everything Aurora adds lives inside Aurora.

## 0. What Aurora touches — and what it never touches

| Existing service | Address | Aurora's use |
|---|---|---|
| kayock-openai-speech-adapter | `http://10.0.0.45:8768/v1` (`POST /v1/audio/speech`, `GET /v1/models`) | TTS output; voice ids discovered live |
| kayock-craft-stt (Faster-Whisper `base.en`, lazy, auto-unload) | `http://127.0.0.1:8769/v1` (`POST /v1/audio/transcriptions`) | voice input (webm/opus, wav, ...) |
| GPU Traffic Light | `http://…:8791` (protocol to be verified on-site) | read-only GET probe for busy/free |
| Kayock Voice (`:8766`), Father Fox Voice Hub (`:8765`), ALS, NOMAD, model servers | — | **never touched** |

Aurora never sends a mutating request (no POST/DELETE/RESTART) to any service it does not
own. Its own state is one SQLite file in `/var/lib/aurora`.

## 1. Install

```bash
# from the extracted Aurora-Companion.zip / repo root
sudo ./deploy/install.sh
```

Defaults (overridable via env vars or flags — see the script header):
`/opt/aurora-companion` (app), `/var/lib/aurora` (data), `/etc/aurora` (config), service
user `aurora`, port `8795`. The installer

- **inspects existing ports** (`ss -ltn`) and refuses to start if Aurora's chosen port is
  occupied; it reports 8765/8766/8768/8769/8791 for the checklist without binding them;
- creates the venv from `requirements.txt`, writes `/etc/aurora/config.yaml` from
  `deploy/config.portable.yaml` (generating an auth token), registers
  `aurora-companion.service` (from `deploy/aurora-companion.service`), enables + starts it;
- (`--tls`) also generates the local HTTPS certificate — see §4b.

### 1a. Filesystem permission model (non-root service account)

The service runs as the unprivileged `aurora` account, and the installer sets up exactly
the access it needs — verified **as that account** before the service is ever started
(`runuser` read/write checks + a startup and configuration-persistence smoke test; any
failure aborts the install):

| Path | Owner / mode | Why |
|---|---|---|
| `/opt/aurora-companion` | `aurora` 750, **read-only to the service** (systemd `ProtectSystem=strict`) | application files never change at runtime |
| `/var/lib/aurora` | `aurora` 750, read-write | all persistent state (`aurora.db`) |
| `/etc/aurora` | `aurora` 750 | traversable for the service, invisible to others |
| `/etc/aurora/config.yaml` | `aurora` **600** | the service **reads** it and **persists user preferences** (voice selection) into it; the auth token inside is hidden from every other account |
| `/etc/aurora/aurora.env` | `root:aurora` 640 | root-managed environment file, service-readable only |

Nothing is world-writable, nothing runs as root, and the installer refuses to start the
service if any check fails. Re-verify an existing deployment any time with
`sudo ./deploy/verify-permissions.sh --smoke` (it skips the parts that need root with an
explicit SKIP note when run unprivileged, e.g. in CI).

Then edit `/etc/aurora/config.yaml` → `provider.presets.local.base_url`/`model` to point
at the model server you already run on Portable, and `sudo systemctl restart
aurora-companion`.

**Manual alternative** (no sudo): `pip install -r requirements.txt`, copy
`deploy/config.portable.yaml` to `config.yaml`, `python run.py`.

## 2. Service management, restarts, logging

```bash
systemctl status aurora-companion
journalctl -u aurora-companion -f        # live logs
sudo systemctl restart aurora-companion  # config changes
```

The unit uses `Restart=on-failure`, `RestartSec=10s` (`StartLimitBurst=10` per 5 min) —
see `deploy/aurora-companion.service`; adjust via `systemctl edit aurora-companion`.
Restarts are safe by design: reminders that came due while Aurora was down are detected at
startup and replayed; personality, memory, reminders and the voice selection all live in
SQLite and survive. Aurora's health self-check: `python run.py --config
/etc/aurora/config.yaml --check` (reports chat model, TTS adapter + voice count, STT
service, traffic-light state).

## 3. Data, backups

Everything persistent is `/var/lib/aurora/aurora.db` (one file). Back it up with
`deploy/backup.sh` — it uses the SQLite online-backup API (safe while Aurora runs), gzips
to `/var/backups/aurora/aurora-YYYYMMDD-HHMMSS.db.gz`, keeps 14 by default
(`--keep N --dest DIR`). Schedule it, e.g. a systemd timer:

```ini
# /etc/systemd/system/aurora-backup.timer  (+ a .service running deploy/backup.sh)
[Timer]
OnCalendar=*-*-* 03:30:00
Persistent=true
```

Restore: stop the service, run the restore hint printed by `backup.sh` (executes the SQL
dump into a fresh `aurora.db`), start the service.

## 4. LAN access and security

- `host: 0.0.0.0` in the config makes the dashboard reachable at `http://10.0.0.45:8795`
  from authorized LAN browsers.
- **Auth**: `server.auth_token` (the installer generates one). Every `/api/*` call then
  requires `Authorization: Bearer <token>` or `X-Aurora-Token`; the dashboard page itself
  loads so the browser can prompt (Settings → Access token stores it in localStorage; the
  SSE stream accepts `?token=` only). Do **not** run Aurora without the token when bound
  to the LAN, and never port-forward it to the internet — nothing here is hardened for
  public exposure.
- Microphone/camera remain **explicit browser permissions** (`getUserMedia` only fires
  from the 🎤 button) *and* Aurora's own permission registry (Privacy panel, default off).
  Nothing records without both.
- Same-origin design: no CORS is opened; access Aurora through its own origin (or your
  reverse proxy on the LAN).

### 4b. Secure LAN push-to-talk (HTTPS)

Browsers only expose the microphone (`getUserMedia`) in a **secure context** — HTTPS or
`localhost`. Over plain HTTP, push-to-talk works only on Portable itself; from other LAN
machines the 🎤 button is disabled with an explanatory tooltip (no silent attempts, no
auto-activation). The design stays push-to-talk; there is no continuous listening or
wake-word behavior.

Aurora's built-in option serves TLS on its own port — no new service, no conflict with the
existing HTTPS services (Kayock Voice 8766, Father Fox 8765 keep their own certificates):

```bash
sudo ./deploy/install.sh --tls          # generates cert + enables TLS in one step
# or on an existing install:
sudo ./deploy/make-cert.sh              # writes /etc/aurora/tls/aurora.{crt,key}
# then set in /etc/aurora/config.yaml:  server.tls_cert / server.tls_key
# and: sudo systemctl restart aurora-companion
```

- `make-cert.sh` writes a self-signed certificate with SANs for
  `kayock-Ubuntu-Portable`, `10.0.0.45`, `localhost`, `127.0.0.1`; the key is
  root-owned 0640 (service-readable, others denied). TLS keys never ship in the repo.
- **Trusted origin (per browser, once):** either accept the self-signed-certificate
  warning once at `https://10.0.0.45:8795`, or trust the cert properly —
  `sudo cp /etc/aurora/tls/aurora.crt /usr/local/share/ca-certificates/aurora-local.crt
  && sudo update-ca-certificates` (Chromium/Chrome uses the system store on Linux; for
  Firefox: Settings → Privacy → Certificates → Import). After that the mic prompt works
  normally.
- TLS is optional: everything else works over HTTP; the dashboard states exactly why the
  mic is unavailable when the context is insecure.
- Still token-gated and LAN-only — never port-forward, and do not reuse these certs for
  anything public.

## 5. Voice integration (Kayock)

- **Discovery over hardcoding**: Aurora reads `GET /v1/models` on the speech adapter and
  offers the ids it actually returns (defaults are just the initial `voice_id`). The
  dashboard's Settings → Voice shows the discovered list, a *Refresh voices* button and a
  *Test voice* button; the selection is saved to Aurora's config — the existing
  infrastructure is never reconfigured.
- **TTS**: replies are synthesized per utterance through `POST /v1/audio/speech`
  (`voice.voice_id`, `voice.format` mp3/wav). The dashboard plays audio client-side
  (the "speak replies" toggle / 🔊 Speak last); server-side synthesis hooks exist behind
  `voice.server_speak` for API-only setups and are off by default to avoid double
  synthesis.
- **STT**: browser recordings (MediaRecorder webm/opus) are forwarded byte-for-byte with
  their content type to `POST /v1/audio/transcriptions`. The service's lazy loading and
  idle unload are respected — Aurora just waits a bit longer on the first call
  (`voice.stt_timeout_seconds`).
- **Degradation**: adapter/STT down → chat, reminders, memory, dashboard all keep working;
  the dashboard shows the error (`tts_error`/`stt_error` in `/api/voice/status`) instead
  of pretending. Microphone/voice-output permissions keep gating everything.

## 6. Resource-aware continuous operation

Aurora's dashboard, memory, reminders, and scheduling loops are model-free. AI work
(mood appraisal, extraction, evolution, consolidation, rolling summary, proactive
initiations) runs through a **ComputeGuard** which allows it only when ALL hold:

1. `resources.background_enabled` (kill-switch);
2. the model endpoint is healthy **and** the configured model is actually loaded
   (cached probe, `resources.model_health_ttl_seconds`);
3. no foreground inference is in flight or in the grace window
   (`resources.foreground_grace_seconds`) — user chat always wins;
4. the GPU Traffic Light, when configured, does not report busy.

Deferred work is **deferred, not dropped**: the post-turn hooks simply run on a later turn
(the last deferral reason is visible in `/api/status` → `resources.last_deferral`);
proactive decisions retry next tick without burning daily caps or cooldowns; consolidation
retries on its 60 s loop. When the model is down mid-chat, the user gets an honest
degraded reply (marked `degraded: model_unreachable`), and reminders deliver as plain
text.

**Traffic Light**: its real protocol must be verified on Portable before enabling —
Aurora ships a generic read-only prober, disabled by default (`resources.traffic_light_url:
""`). Run `deploy/verify-voice.sh` (section 4) to dump `/health`, `/status` and `/`; if
the JSON exposes a busy/idle signal, set `resources.traffic_light_url` and match
`resources.busy_markers` to its field names. Until then, and whenever the probe cannot be
understood, Aurora treats coordination as `unknown` and **does not block itself** — a
broken traffic light must never take the companion down. Aurora only ever sends GETs.

## 7. Post-install verification checklist (voice-service connections)

Run `sudo deploy/verify-voice.sh` (add `AURORA_TOKEN=…` if auth is on — verification
works with authentication enabled; it proves the token is accepted, that requests
**without** it get HTTP 401, and never prints the token). It walks the exact
list below; do it manually as well if you prefer:

1. `curl http://10.0.0.45:8768/v1/models` — HTTP 200 and a parseable voice-id list
   (`am_adam`, `picard`, `data`, …).
2. `curl -d '{"model":"kokoro","input":"check","voice":"am_adam","response_format":"mp3"}'
   -H 'Content-Type: application/json' http://10.0.0.45:8768/v1/audio/speech` — audio
   bytes > 1 KB. (400/422 → adjust `voice.kayock_tts_model`/`voice_id`.)
3. `curl -F file=@sample.webm -F model=base.en http://127.0.0.1:8769/v1/audio/transcriptions`
   — HTTP 200 with a `text` field; first call may take tens of seconds (lazy load).
4. Traffic Light probe (`/health`, `/status`, `/`) — optional; note the field names for
   `busy_markers`.
5. Permissions: `sudo deploy/verify-permissions.sh --smoke` — config.yaml 600
   aurora-owned, service account can read + persist preferences, `run.py --check` boots
   under the service account.
6. `python run.py --config /etc/aurora/config.yaml --check` — chat provider reachable,
   `tts: kayock … N voices, selected '…'`, `stt: kayock_whisper … reachable`.
7. Dashboard: Settings → Voice shows the discovered ids; select one, press *Test voice*,
   hear audio on a LAN machine. Chat 🎤 records → transcript appears in the input.
   From another LAN machine over plain HTTP the 🎤 is disabled by design → set up §4b
   HTTPS, then confirm the mic prompt appears.
8. Personalization: the dashboard greets you as **Eric** (`user_name: "Eric"`); the
   companion's own name/identity/personality are unchanged.
9. Model-unavailable drill: stop your model server briefly → chat replies honestly
   (`couldn't reach my language model`), reminder set via `/remind` still fires;
   restart the server → next turn is normal again.

## 8. Honest testing status

The sandbox cannot reach Portable's LAN or hardware and cannot switch users (non-root
environment). Everything above **except the physical layers** is covered by the automated
suite (**229 tests**): speech-adapter and STT clients against the real request/response
dialects (mock transport), voice discovery and selection, model-unavailable degradation,
background-task deferral, reminder operation without inference, service restart and
recovery, LAN token gating — plus the deployment-fix regressions: systemd unit content,
installer permission commands, verify-script behavior (auth array, 401 handling, embedded
Python compatibility), the permission model as a functional round-trip (read + persist +
mode preservation), and TLS option handling. `verify-voice.sh` was additionally run
end-to-end against a mock Kayock/Aurora ecosystem (valid token → all PASS; wrong token →
rejected; no token leakage), `verify-permissions.sh` against a simulated installer layout,
and a generated certificate was proven to serve Aurora over HTTPS.

**Not tested in the sandbox and therefore not claimed**: the actual `aurora`-account
startup under systemd (needs root + Portable — `verify-permissions.sh --smoke` covers it
on-site), real voice hardware, live Kayock binaries, LAN reachability, and the Traffic
Light's real protocol — section 7 is the on-site verification pass that closes that gap.

## 9. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `tts error` in dashboard, audio buttons fail | adapter down or voice id rejected — re-run checklist 1–2; refresh voices; check `voice.kayock_tts_model` |
| STT returns empty text + error | service unloaded and starting (wait), or permission off; first call after idle is slow by design |
| Chat says model unreachable | model server busy/unloaded — that's the shared-machine design; Aurora keeps working degraded |
| Background AI never runs (deferral note in /api/status) | `resources.background_enabled` off, model unhealthy, or traffic light busy — check the guard snapshot |
| LAN browser can't reach dashboard | check `host: 0.0.0.0`, port free, firewall (`sudo ufw allow 8795` if UFW is active) |
| 401 on every API call | token set in config but not entered in the dashboard (Settings → Access token) |
| Service can't read / persist config (permission denied) | re-run `sudo ./deploy/install.sh` (it re-applies the permission model and aborts on failure), or fix manually: `chown -R aurora:aurora /etc/aurora && chmod 750 /etc/aurora && chmod 600 /etc/aurora/config.yaml`; verify with `sudo deploy/verify-permissions.sh --smoke` |
| 🎤 disabled from a LAN machine ("secure context") | browser needs HTTPS: `sudo ./deploy/install.sh --tls` or `make-cert.sh` + config keys (§4b), then trust the certificate once |
| Certificate warning in the browser | expected for the self-signed local cert — accept once or install it into the trust store (§4b); only for the LAN, never public |
