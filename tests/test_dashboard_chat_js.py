"""Regression guards for dashboard chat/SSE/hands-free defects found on Portable.

These assert structural properties of aurora/server/static/index.html — the
bugs were frontend DOM/SSE handling, so the HTML is the unit under test.
"""
from __future__ import annotations

from pathlib import Path

HTML = Path(__file__).resolve().parents[1] / "aurora" / "server" / "static" / "index.html"


def _html() -> str:
    return HTML.read_text(encoding="utf-8")


def test_message_body_separated_from_source_label():
    h = _html()
    assert "msg-body" in h
    assert "div.textContent = m.content; div.insertAdjacentHTML('beforeend', src)" not in h


def test_sse_deltas_do_not_rewrite_whole_message_textcontent():
    h = _html()
    # The original bug: last.textContent += d.text (flattens "chat" into body)
    assert "last.textContent += d.text" not in h
    assert "ensureStreamBubble" in h
    assert "body.textContent += (d.text || '')" in h or "body.textContent +=" in h


def test_final_messages_upserted_by_id():
    h = _html()
    assert "function upsertMsg" in h
    # chat POST must not blindly addMsg (duplicate with SSE)
    assert "upsertMsg(msg)" in h


def test_handsfree_does_not_route_mic_to_speakers():
    h = _html()
    assert "collector.connect(wake.ctx.destination)" not in h
    assert "wake.node.connect(wake.ctx.destination)" not in h
    assert "wake.sink" in h
    assert "gain.value = 0" in h


def test_handsfree_mutes_during_tts_playback():
    h = _html()
    assert "setPlaybackMute" in h
    assert "wake.muted" in h
    assert "wake.busy" in h


def test_handsfree_documents_vad_not_true_wakeword():
    h = _html()
    assert "NOT a true wake-word" in h or "energy-VAD" in h or "energy VAD" in h.lower() or "energy-VAD" in h
