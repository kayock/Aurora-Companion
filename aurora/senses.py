"""Screen awareness (opt-in).

The client (browser/desktop shell) may share a screenshot when the
`screen_vision` permission is on. The frame is sent to the chat model as
an OpenAI-style image_url content block, so any vision-capable server
(LM Studio VLM models, llama.cpp multimodal, cloud endpoints) can
describe it. Frames are kept only in memory for the current turn —
Aurora never writes screenshots to disk.

If the configured model cannot see images, the request fails loudly and
the user is told; Aurora does not pretend to have seen anything.
"""

from __future__ import annotations

import base64


class ScreenSense:
    def __init__(self, permissions) -> None:
        self.permissions = permissions
        self.last_description: str = ""

    def allowed(self) -> bool:
        return self.permissions.get("screen_vision")

    async def describe(self, provider, png_bytes: bytes, question: str) -> str:
        if not self.allowed():
            return "error: screen vision permission is off"
        b64 = base64.b64encode(png_bytes).decode()
        content = [
            {"type": "text", "text": question or "Describe what is on my screen right now, briefly."},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]
        try:
            result = await provider.chat([{"role": "user", "content": content}])
        except Exception as exc:
            return f"error: model could not process the image ({exc})"
        self.last_description = result.text
        return result.text
