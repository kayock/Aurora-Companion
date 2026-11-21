# Installing and running Aurora

> **Ubuntu Portable deployment** (systemd service, Kayock voice services, LAN access,
> backups): see [PORTABLE_DEPLOY.md](PORTABLE_DEPLOY.md) and `deploy/install.sh`. The
> quick-start below stays valid for any machine.

## Requirements

- Python 3.10+ (3.12 tested), pip
- Any OpenAI-compatible model server for conversation: **LM Studio** (default port 1234),
  **llama.cpp `llama-server`** (port 8080), **Ollama** (`http://127.0.0.1:11434/v1`), vLLM,
  or a cloud OpenAI-style endpoint. Any model size works; a tool-capable model enables the
  tool loop, everything else degrades gracefully.
- Optional voice: an existing **OpenAI-compatible speech adapter** (`voice.tts: kayock`,
  e.g. the Kayock one on port 8768), a local Kokoro server (`voice.tts: kokoro`), or
  faster-whisper / an existing Faster-Whisper transcription service for STT
  (`voice.stt: whisper` / `kayock`).
- Optional: an OpenAI-compatible embeddings endpoint (semantic recall).

## Install

```bash
git clone <this repository> aurora && cd aurora
pip install -r requirements.txt
cp config.example.yaml config.yaml
# edit config.yaml → provider.presets.local.base_url + model
python run.py --check     # verifies the model endpoint is reachable
python run.py             # → http://127.0.0.1:8795  (the dashboard)
```

`python run.py --config other.yaml --port 9000` work as expected. All state lives in
`data/aurora.db` (override with `data_dir` in the config); back up that one file to back
up the entire companion — conversation, memory, reminders, personality, mood.

## First minutes

1. Open the dashboard → **Settings**: confirm the model preset, press *Test*.
2. Chat. Say "remember that …" or use `/remember`, set reminders with `/remind in 20 minutes to X`
   or the Reminders panel (the nag-until-done checkbox enables follow-ups).
3. **Privacy** panel: microphone / camera / screen-vision / voice output — all default off.
4. **Personality** panel: watch mood/affinity evolve; trigger an evolution manually or wait
   for the cadence (default every 40 user turns); roll back any version.
5. **Memory** panel: search, edit, pin, archive, restore, delete, export JSON.

## Configuration

Every key is documented inline in `config.example.yaml`; defaults live in
`aurora/config.py`. Key groups: `provider` (chat/background presets — use a small local
model as the background model for extraction/consolidation/evolution if you like),
`memory` (extraction cadence, consolidation idle time), `personality` (evolution cadence),
`reminders`, `proactive` (quiet hours, caps, focus), `voice` (kayock/kokoro/edge TTS,
whisper/kayock STT, live voice discovery), `resources` (traffic-light probe, background
deferral), `server` (LAN auth token), `senses`, `people`.
