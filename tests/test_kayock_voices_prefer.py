"""Kayock adapter on Portable lists voices at GET /v1/voices."""
from __future__ import annotations

import inspect

from aurora.voice.kayock import KayockTTS


def test_list_voices_source_prefers_voices_endpoint():
    src = inspect.getsource(KayockTTS.list_voices)
    assert '"/voices"' in src
    assert '"/models"' in src
    assert src.index('"/voices"') < src.index('"/models"')
