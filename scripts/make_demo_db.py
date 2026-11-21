#!/usr/bin/env python3
"""Create a demo database for Aurora's dashboard — fictional data only.

Uses Aurora's real APIs (conversation, memory, reminders) with the built-in
mock provider, so you can explore the interface without any model or voice
service. Nothing here is a real user record.

    python scripts/make_demo_db.py                 # -> ./data-demo/
    python run.py --config data-demo/config.yaml   # open http://127.0.0.1:8795

The generated directory is disposable; delete it whenever you like.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from aurora.app import CompanionApp  # noqa: E402
from aurora.clock import Clock  # noqa: E402
from aurora.config import Config  # noqa: E402
from aurora.providers import MockProvider  # noqa: E402

# Fictional scripted replies for the demo conversation (mock provider rules).
DEMO_RULES = [
    (r"stargazing|trip|saturday",
     "Saturday looks clear — a new moon, so the aurora viewing should be great. "
     "I kept a short checklist: warm layers, the thermos, and the wide-field "
     "binoculars. Want me to remind you Friday evening to charge the camera batteries?"),
    (r"tea|coffee|drink",
     "Noted — you lean towards the smoked black tea you found at the winter market. "
     "I'll keep that in mind when you ask me to plan cozy evenings."),
    (r"paper|write|draft",
     "The draft outline has three sections so far: motivation, the field setup, and "
     "early observations. When you're ready I can pull the memory notes about the "
     "P2000 timings into the methods section."),
    (r"remind|battery",
     "Done — I set that reminder. It will nudge Friday evening, and repeat a gentle "
     "follow-up if it goes unacknowledged."),
]


def demo_config(out: Path) -> Path:
    cfg = {
        "data_dir": str(out / "data"),
        "host": "127.0.0.1",
        "port": 8795,
        "user_name": "Eric",
        "provider": {"chat": "mock", "background": "same"},
        "voice": {"tts": "none", "stt": "none"},
        "resources": {"background_enabled": False},  # screenshots stay deterministic
    }
    path = out / "config.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return path


async def seed(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    cfg_path = demo_config(out)
    config = Config.load(cfg_path)
    provider = MockProvider(DEMO_RULES)
    app = CompanionApp(config, clock=Clock(time.time()), seed_path=ROOT / "personas" / "default.json",
                       chat_provider=provider, background_provider=provider)

    # --- fictional conversation (real pipeline: mood, summaries, extraction cursor) ---
    demo_turns = [
        "Aurora, the stargazing trip is on for Saturday — clear skies forecast!",
        "Could you plan something cozy for afterwards? Something warm to drink.",
        "The tea from the winter market, the smoked black one, is still my favourite.",
        "How is the paper draft looking? I want the methods section solid this week.",
        "Set a reminder to charge the camera batteries on Friday evening.",
    ]
    for turn in demo_turns:
        await app.conversation.submit_user_message(turn)

    # --- fictional memory facts with evidence pointers into the conversation ---
    store = app.store
    turns = app.conversation.history(limit=10)
    ev = lambda i: [{"turn_id": str(turns[i]["id"]) if i < len(turns) else "",
                     "quote": turns[i]["content"][:80] if i < len(turns) else ""}] if turns else []
    store.add_fact("Eric's favourite tea is the smoked black tea from the winter market.",
                   category="preference", source="auto", evidence=ev(2), weight=1.2)
    store.add_fact("Stargazing trip planned for Saturday night (new moon, clear forecast).",
                   category="event", kind="plan", source="auto", evidence=ev(0))
    store.add_fact("Eric is writing a paper; methods section due this week.",
                   category="project", kind="paper", source="auto", evidence=ev(3), pinned=True)
    store.add_fact("Camera batteries must be charged Friday evening before the trip.",
                   category="task", source="auto", evidence=ev(4))
    store.add_fact("The wide-field binoculars and the thermos belong in the stargazing kit.",
                   category="preference", source="auto")

    # --- reminders (general-purpose: one-time, recurring, nag-until-acknowledged) ---
    now = time.time()
    app.reminders.add("Charge the camera batteries", due_at=now + 86400 * 2,
                      follow_up=True, project="paper", source="user")
    app.reminders.add("Water the tomatoes", due_at=now + 3600 * 5,
                      repeat={"every_seconds": 86400, "kind": "daily"}, source="user")
    app.reminders.add("Return library books", due_at=now + 86400 * 6, source="user")
    app.reminders.add("Evening check-in with the aurora forecast", due_at=now + 3600 * 8,
                      repeat={"every_seconds": 86400, "kind": "daily"}, project="paper")

    await app.stop()  # flush WAL
    print(f"demo database ready: {out / 'data' / 'aurora.db'}")
    print(f"start it with: python run.py --config {cfg_path}")


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data-demo"
    asyncio.run(seed(out))


if __name__ == "__main__":
    main()
