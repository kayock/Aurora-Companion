"""Regression tests for config paths supplied by callers and installer smoke tests."""

from pathlib import Path

from aurora.config import Config


def test_load_from_string_can_save_and_resolve_relative_data_dir(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("data_dir: companion-data\nvoice:\n  voice_id: am_adam\n", encoding="utf-8")

    config = Config.load(str(config_file))
    assert isinstance(config.path, Path)
    assert config.data_dir() == tmp_path / "companion-data"
    config.set("voice.voice_id", "picard")
    config.save()

    reloaded = Config.load(str(config_file))
    assert reloaded.get("voice.voice_id") == "picard"
    assert reloaded.data_dir() == tmp_path / "companion-data"


def test_config_constructor_normalizes_string_path(tmp_path):
    dest = tmp_path / "nested" / "companion.yaml"
    config = Config({"voice": {"voice_id": "am_adam"}}, path=str(dest))
    config.save()
    assert dest.exists()
    assert Config.load(dest).get("voice.voice_id") == "am_adam"
