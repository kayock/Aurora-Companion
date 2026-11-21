"""Central configuration.

One YAML file drives the whole application. Defaults live here; a user
config (config.yaml) deep-merges over them. `Config.get("dotted.path")`
reads live values so settings changes do not require a restart.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "data_dir": "data",
    "host": "127.0.0.1",
    "port": 8795,
    "user_name": "Alex",
    "provider": {
        # Which preset serves each role. "background" covers memory
        # extraction, consolidation, mood appraisal and persona evolution;
        # setting it to "same" reuses the chat model.
        "chat": "local",
        "background": "same",
        "presets": {
            "local": {
                "type": "openai_compat",
                "base_url": "http://127.0.0.1:1234/v1",
                "model": "local-model",
                "api_key": "",
                "temperature": 0.8,
                "max_tokens": 1024,
            },
        },
    },
    "embeddings": {
        "enabled": False,
        "base_url": "http://127.0.0.1:1234/v1/embeddings",
        "model": "text-embedding",
    },
    "conversation": {
        "max_history_turns": 24,
        "summary_trigger_messages": 60,
        "max_tool_rounds": 3,
    },
    "memory": {
        "enabled": True,
        "extraction_every_n_user_turns": 4,
        "max_facts_in_prompt": 12,
        "recent_days": 3,
        "consolidation_idle_minutes": 15,
        "consolidation_check_seconds": 300,
        "tidy_duplicate_threshold": 0.75,
        "forget_words": True,
    },
    "personality": {
        "mood": {"enabled": True, "intensity": 1.0, "warmth_home": 0.45},
        "affinity": {"enabled": True},
        "evolution": {
            "enabled": True,
            "every_n_user_turns": 40,
            "baseline_review_every": 3,
        },
    },
    "reminders": {
        "follow_up_default_seconds": 600,
        "follow_up_max": 3,
        "missed_replay": True,
        "min_repeat_seconds": 60,
    },
    "proactive": {
        "enabled": True,
        "tick_seconds": 20,
        "quiet_start": "23:00",
        "quiet_end": "07:30",
        "sleep_window_learning": True,
        "min_silence_seconds": 900,
        "max_per_day": 6,
        "check_in": {
            "enabled": True,
            "after_silence_minutes": 90,
            "min_gap_minutes": 180,
        },
        "topics": {"enabled": True, "min_gap_minutes": 240},
        "morning_greeting": {"enabled": True, "window": "07:30-10:00"},
        "work_break": {
            "enabled": True,
            "after_minutes": 90,
            "max_per_day": 2,
            "min_gap_minutes": 120,
        },
        "memory_resurfacing": {"enabled": True, "older_than_days": 14},
    },
    "voice": {
        "stt": "none",  # none | whisper | kayock (host Faster-Whisper service)
        "tts": "none",  # none | kokoro | edge | kayock (OpenAI speech adapter)
        "kokoro_url": "http://127.0.0.1:8880/v1/audio/speech",
        "kokoro_voice": "af_heart",
        "edge_voice": "en-US-AriaNeural",
        "auto_speak": False,
        # Kayock Voice integration (existing services on the host; Aurora only
        # calls them, never configures them). Voice ids are discovered from
        # GET /v1/models at runtime — voice_id is just the initial preference.
        "kayock_tts_url": "http://127.0.0.1:8768/v1",
        "kayock_tts_api_key": "",
        "kayock_tts_model": "kokoro",       # value of the "model" field; empty omits it
        "kayock_stt_url": "http://127.0.0.1:8769/v1",
        "kayock_stt_api_key": "",
        "kayock_stt_model": "base.en",      # model field for /v1/audio/transcriptions
        "voice_id": "am_adam",
        "format": "mp3",                    # mp3 | wav (speech adapter supports both)
        "tts_timeout_seconds": 45.0,
        "stt_timeout_seconds": 60.0,
        "stt_language": "",
        # Server-side auto-synthesis hook (dashboard playback is client-driven
        # and does not need this; keep off to avoid double synthesis).
        "server_speak": False,
    },
    "resources": {
        # Read-only coordination with the existing GPU Traffic Light. Leave
        # empty until the service's real protocol has been verified on the
        # host (deploy/verify-voice.sh prints it); Aurora never writes to it.
        "traffic_light_url": "",
        "probe_seconds": 30,
        "busy_markers": ["busy", "occupied", "loading", "inferencing"],
        # Gate for nonessential background AI (extraction, consolidation,
        # evolution, mood appraisal, rolling summary, proactive initiation).
        "background_enabled": True,
        "model_health_ttl_seconds": 60,
        "foreground_grace_seconds": 120,
    },
    "server": {
        # When set, every /api/* call needs 'Authorization: Bearer <token>',
        # an 'X-Aurora-Token' header, or (SSE only) '?token=...'. The static
        # dashboard page itself stays public so the browser can ask for it.
        "auth_token": "",
        # Optional HTTPS on Aurora's own port (secure context → browsers allow
        # LAN push-to-talk; see deploy/make-cert.sh). Empty = plain HTTP.
        "tls_cert": "",
        "tls_key": "",
    },
    "senses": {
        "screen_vision": False,  # opt-in: client uploads screenshots on request
        "camera": False,
    },
    "people": {
        "multi_person": False,  # explicit opt-in for multi-person sessions
        "guest_mode": True,
    },
    "logging": {"level": "info"},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class Config:
    def __init__(self, data: dict[str, Any], path: Path | str | None = None) -> None:
        self._data = _deep_merge(DEFAULTS, data)
        self.path = Path(path) if path is not None else None

    @classmethod
    def load(cls, path: Path | str | None) -> "Config":
        if path is None:
            return cls({})
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return cls(data, path)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node = self._data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(self._data, fh, sort_keys=False, allow_unicode=True)

    def data_dir(self) -> Path:
        raw = Path(str(self.get("data_dir", "data")))
        if not raw.is_absolute():
            # Relative to the config file's directory (or cwd when no file).
            base = self.path.parent if self.path else Path.cwd()
            return base / raw
        return raw

    def snapshot(self) -> dict:
        return copy.deepcopy(self._data)
