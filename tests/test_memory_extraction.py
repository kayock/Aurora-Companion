"""Extraction pipeline: evidence validation, anti-fabrication, supersede."""

import json

from aurora.memory.extraction import MemoryExtractor, _extract_json_array
from aurora.providers.mock import MockProvider


def _extractor(make_app, response: str):
    app, _ = make_app(rules=[(r"Conversation:", response)])
    return app, MemoryExtractor(app.db, app.store, app.conversation.chat_provider, app.clock)


def _messages():
    return [
        {"id": 1, "role": "user", "content": "I started a vegetable garden last weekend", "ts": 100.0},
        {"id": 2, "role": "assistant", "content": "That sounds lovely!", "ts": 101.0},
        {"id": 3, "role": "user", "content": "I also want to learn pottery", "ts": 102.0},
    ]


def test_json_array_extraction_tolerant():
    assert _extract_json_array("```json\n[1,2]\n```") == [1, 2]
    assert _extract_json_array('blah ["x"] blah') == ["x"]
    assert _extract_json_array("no json here") is None


async def test_valid_add_with_evidence(make_app):
    response = json.dumps([{
        "op": "add", "content": "The user is starting a vegetable garden",
        "category": "project", "weight": 1.2,
        "evidence": [{"message_id": 1, "quote": "vegetable garden"}],
    }])
    app, extractor = _extractor(make_app, response)
    result = extractor.apply_ops(json.loads(response), _messages())
    assert len(result.added) == 1
    fact = app.store.get_fact(result.added[0])
    assert fact.evidence[0]["message_id"] == 1


async def test_fabricated_evidence_rejected(make_app):
    response = json.dumps([{
        "op": "add", "content": "The user hates gardens",
        "evidence": [{"message_id": 1, "quote": "I hate gardens"}],  # quote not in message 1
    }])
    app, extractor = _extractor(make_app, response)
    result = extractor.apply_ops(json.loads(response), _messages())
    assert result.added == []
    assert result.rejected and result.rejected[0]["reason"] == "quote not found in message"
    assert app.store.counts()["active"] == 0


async def test_evidence_outside_batch_rejected(make_app):
    response = json.dumps([{
        "op": "add", "content": "Something", "evidence": [{"message_id": 999, "quote": ""}],
    }])
    app, extractor = _extractor(make_app, response)
    result = extractor.apply_ops(json.loads(response), _messages())
    assert result.added == []


async def test_supersede_updates_old_fact(make_app):
    old = app_old = None
    response = json.dumps([{
        "op": "supersede", "content": "The user now lives in Leeds",
        "evidence": [{"message_id": 1, "quote": "garden"}],  # evidence quote check only needs to be present
        "supersedes": None,  # patched below
    }])
    app, extractor = _extractor(make_app, response)
    old = app.store.add_fact("The user lives in Bristol", category="fact", source="auto")
    ops = json.loads(response)
    ops[0]["supersedes"] = old.id
    result = extractor.apply_ops(ops, _messages())
    assert result.superseded and result.superseded[0][0] == old.id
    assert not app.store.get_fact(old.id).active


async def test_supersede_unknown_target_rejected(make_app):
    response = json.dumps([{
        "op": "supersede", "content": "New content",
        "evidence": [{"message_id": 1, "quote": "garden"}], "supersedes": "f_nope",
    }])
    app, extractor = _extractor(make_app, response)
    result = extractor.apply_ops(json.loads(response), _messages())
    assert result.superseded == []


async def test_op_cap_twelve(make_app):
    ops = [{"op": "add", "content": f"Fact number {i}", "evidence": [{"message_id": 1, "quote": "garden"}]}
           for i in range(30)]
    app, extractor = _extractor(make_app, "[]")
    result = extractor.apply_ops(ops, _messages())
    assert result.raw_ops == 30
    assert len(result.added) == 12


async def test_extract_since_cursor_consumes_batch(make_app):
    app, _ = make_app()
    for text in ["msg one", "msg two", "msg three"]:
        app.db.execute("INSERT INTO messages (role, content, ts, person, source) VALUES ('user',?,?, 'owner','user')",
                       (text, 100.0))
    # provider returns no ops
    result = await app.conversation.extractor.extract_since_cursor()
    assert result is not None and result.added == []
    # cursor advanced to last message
    assert app.conversation.extractor._cursor() == 3
    # nothing new -> returns None without a model call
    again = await app.conversation.extractor.extract_since_cursor()
    assert again is None
