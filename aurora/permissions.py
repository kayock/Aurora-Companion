"""Privacy permissions — user-controlled, default-deny.

Nothing in Aurora records or monitors without explicit permission:
microphone, camera, screen vision and voice output are toggles stored in
config and surfaced on the dashboard's Permissions panel. Cameras and
screens are captured *by the user's client* (browser) only when the
corresponding permission is on and the user shares a frame; the server
never captures on its own and no audio or images are persisted by
default. This mirrors the privacy posture of both upstream projects.
"""

from __future__ import annotations

PERMISSIONS = ("microphone", "camera", "screen_vision", "voice_output")


class PermissionRegistry:
    def __init__(self, config) -> None:
        self.config = config

    def get(self, name: str) -> bool:
        if name == "microphone":
            return self.config.get("voice.stt", "none") != "none"
        if name == "camera":
            return bool(self.config.get("senses.camera", False))
        if name == "screen_vision":
            return bool(self.config.get("senses.screen_vision", False))
        if name == "voice_output":
            return self.config.get("voice.tts", "none") != "none"
        raise KeyError(name)

    def set(self, name: str, value: bool) -> bool:
        if name == "microphone":
            # Remember which STT provider was active so re-enabling restores
            # it instead of guessing (kayock when that service is configured).
            if value:
                current = self.config.get("voice.stt_last", "") or \
                    ("kayock" if self.config.get("voice.kayock_stt_url") else "whisper")
                if current in ("", "none"):
                    current = "kayock" if self.config.get("voice.kayock_stt_url") else "whisper"
                self.config.set("voice.stt", current)
            else:
                if self.config.get("voice.stt", "none") != "none":
                    self.config.set("voice.stt_last", self.config.get("voice.stt"))
                self.config.set("voice.stt", "none")
        elif name == "camera":
            self.config.set("senses.camera", bool(value))
        elif name == "screen_vision":
            self.config.set("senses.screen_vision", bool(value))
        elif name == "voice_output":
            if value:
                current = self.config.get("voice.tts_last", "") or \
                    ("kayock" if self.config.get("voice.kayock_tts_url") else "kokoro")
                if current in ("", "none"):
                    current = "kayock" if self.config.get("voice.kayock_tts_url") else "kokoro"
                self.config.set("voice.tts", current)
            else:
                if self.config.get("voice.tts", "none") != "none":
                    self.config.set("voice.tts_last", self.config.get("voice.tts"))
                self.config.set("voice.tts", "none")
        else:
            raise KeyError(name)
        self.config.save()
        return self.get(name)

    def snapshot(self) -> dict:
        return {name: self.get(name) for name in PERMISSIONS}
