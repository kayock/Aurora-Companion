"""Mood, affinity and persona evolution."""

import pytest

from aurora.personality.mood import MoodEngine
from aurora.personality.persona import Persona


def _mood(make_app):
    app, _ = make_app()
    return app, app.mood


async def test_positive_exchange_warms(make_app):
    app, mood = _mood(make_app)
    before = mood.warmth
    await mood.update_after_turn("thank you so much, that was wonderful!", "any time!")
    assert mood.warmth > before
    assert mood.affinity >= 1.0


async def test_hostile_exchange_cools_and_grudges(make_app):
    app, mood = _mood(make_app)
    mood.warmth = 0.3
    await mood.update_after_turn("you are useless, shut up, I hate this", "I'm sorry it feels that way")
    assert mood.warmth < 0.3
    assert mood.state()["grudge"] is True


async def test_apology_ends_grudge(make_app):
    app, mood = _mood(make_app)
    mood.warmth = 0.1
    await mood.update_after_turn("you are stupid and worthless", "...")
    assert mood.state()["grudge"]
    await mood.update_after_turn("I'm sorry, that was wrong of me. I apologize.", "understood")
    assert not mood.state()["grudge"]


async def test_praise_diminishing_returns(make_app):
    app, mood = _mood(make_app)
    deltas = []
    for _ in range(4):
        before = mood.warmth
        await mood.update_after_turn("thanks, great job!", "happy to help")
        deltas.append(mood.warmth - before)
    # Each repeated 'thanks' within 10 minutes should count less than the first.
    assert deltas[0] > deltas[-1]


async def test_restart_fade(make_app):
    app, mood = _mood(make_app)
    mood.warmth = 0.9
    mood._persist()
    app.clock.advance(5 * 3600)  # 5h away -> fade toward baseline
    mood._fade_restart(app.clock.now() - 5 * 3600, app.clock.now())
    assert mood.warmth < 0.9


async def test_affinity_tier_labels(make_app):
    app, mood = _mood(make_app)
    mood.affinity = 650
    assert mood.tier_label() == "close friend"
    mood.affinity = 1250
    assert mood.tier_label() == "eternal bond"


async def test_prompt_block_mentions_duties(make_app):
    app, mood = _mood(make_app)
    block = mood.prompt_block()
    assert "duties" in block or "always done" in block


# ---- persona evolution ----

async def test_evolution_trigger_cadence(make_app):
    app, _ = make_app(overrides={"personality.evolution.every_n_user_turns": 3})
    evo = app.evolution
    assert not evo.maybe_evolve(2)
    assert evo.maybe_evolve(3)


async def test_evolution_full_run_and_history(make_app):
    app, _ = make_app(overrides={"personality.evolution.every_n_user_turns": 1})
    app.db.set_meta_json("stats", {"user_turns": 10})
    result = await app.evolution.evolve("facts", "conversation", force=True)
    assert result.fired
    assert app.persona.state.version == 1
    assert "cooking" in app.persona.state.evolved_prompt
    assert app.persona.state.core_anchor  # anchor was extracted first
    history = app.persona.history()
    assert history and history[0]["version"] == 1


async def test_evolution_requires_anchor_preserved(make_app):
    rules = [
        (r"Extract 3-6 immutable core traits",
         '{"core_anchor": "a; b; c", "rationale": "x"}'),
        (r"BASELINE REVIEW|REGULAR EVOLUTION",
         '{"base_prompt": "new prompt", "style": "s", "change_summary": "x", '
         '"reason": "y", "anchor_preserved": false}'),
    ]
    app, _ = make_app(rules=rules)
    result = await app.evolution.evolve("facts", "conversation", force=True)
    assert not result.fired
    assert result.skipped_reason == "model reported anchor broken"
    assert app.persona.state.version == 0


async def test_evolution_headings_must_survive(make_app):
    rules = [
        (r"Extract 3-6 immutable core traits",
         '{"core_anchor": "a; b; c", "rationale": "x"}'),
        (r"BASELINE REVIEW|REGULAR EVOLUTION",
         '{"base_prompt": "Completely different text without any headings", "style": "", '
         '"change_summary": "rewrote everything", "reason": "why not", "anchor_preserved": true}'),
    ]
    app, _ = make_app(rules=rules)
    result = await app.evolution.evolve("facts", "conversation", force=True)
    assert not result.fired
    assert "headings" in result.skipped_reason


async def test_rollback_restores_version(make_app):
    app, _ = make_app()
    result = await app.evolution.evolve("facts", "conversation", force=True)
    assert result.fired
    ok = app.persona.rollback(1)
    assert ok
    assert app.persona.state.evolved_prompt == app.persona.history()[0]["base_prompt"]


async def test_evolution_without_model_skips_honestly(tmp_path):
    from aurora.app import CompanionApp
    from aurora.clock import Clock
    from tests.conftest import make_test_config as test_config, SEED

    cfg = test_config(tmp_path)
    cfg.set("provider.presets", {})  # genuinely no provider available
    cfg.set("provider.chat", "none-configured")
    app = CompanionApp(cfg, clock=Clock(), seed_path=SEED)
    try:
        result = await app.evolution.evolve("facts", "conversation", force=True)
        assert not result.fired
        assert result.skipped_reason == "no background model"
        assert app.conversation.chat_provider is None
    finally:
        app.db.close()
