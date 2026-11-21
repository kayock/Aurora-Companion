"""CompanionApp — builds and owns every subsystem, with a clean lifecycle.

Wiring (one of each — the "one X" requirements live here):
  * one Database, one Config
  * one chat provider + one background provider (both from the registry)
  * one FactStore / MemoryRetriever / MemoryExtractor / Consolidator
  * one Persona / MoodEngine / PersonaEvolution
  * one ReminderStore / ReminderScheduler
  * one ProactiveScheduler
  * one ConversationManager (all turns)
  * one VoiceService, one PermissionRegistry, one PeopleRegistry

Restart persistence is free: every subsystem reads its state from the
shared store at construction.
"""

from __future__ import annotations

from pathlib import Path

from . import __version__
from .clock import Clock
from .config import Config
from .conversation.manager import ConversationManager
from .conversation.tools import default_tool_registry
from .memory.changelog import MemoryChangelog
from .memory.consolidation import Consolidator
from .memory.extraction import MemoryExtractor
from .memory.retrieval import MemoryRetriever
from .memory.store import FactStore
from .people import PeopleRegistry
from .permissions import PermissionRegistry
from .personality.evolution import PersonaEvolution
from .personality.mood import MoodEngine
from .personality.persona import Persona
from .proactive.scheduler import ProactiveScheduler
from .providers import ChatProvider, EmbeddingClient, create_chat_provider
from .reminders.scheduler import ReminderScheduler
from .reminders.store import ReminderStore
from .resources import ComputeGuard, ResourceCoordinator
from .senses import ScreenSense
from .storage import Database
from .voice.kayock import KayockSTT, KayockTTS
from .voice.service import VoiceService
from .voice.stt import WhisperSTT
from .voice.tts import KokoroTTS, NoneTTS


class CompanionApp:
    def __init__(self, config: Config, clock: Clock | None = None,
                 seed_path: Path | None = None, chat_provider: ChatProvider | None = None,
                 background_provider: ChatProvider | None = None) -> None:
        self.config = config
        self.clock = clock or Clock()
        self.data_dir = config.data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # providers (allow injection for tests)
        if chat_provider is None:
            preset_name = config.get("provider.chat", "local")
            presets = config.get("provider.presets", {}) or {}
            preset = presets.get(preset_name)
            if preset is None:
                chat_provider = None
            else:
                try:
                    chat_provider = create_chat_provider(preset)
                except Exception:
                    chat_provider = None
        self.chat_provider = chat_provider
        if background_provider is None:
            bg_name = config.get("provider.background", "same")
            if bg_name in ("same", "", None):
                background_provider = chat_provider
            else:
                preset = (config.get("provider.presets", {}) or {}).get(bg_name)
                try:
                    background_provider = create_chat_provider(preset) if preset else chat_provider
                except Exception:
                    background_provider = chat_provider
        self.background_provider = background_provider

        # storage + memory
        self.db = Database(self.data_dir / "aurora.db")
        self.store = FactStore(self.db, self.clock,
                               duplicate_threshold=float(config.get("memory.tidy_duplicate_threshold", 0.75)))
        embeddings = None
        if config.get("embeddings.enabled", False):
            embeddings = EmbeddingClient(
                config.get("embeddings.base_url"), config.get("embeddings.model"), db=self.db
            )
        self.embeddings = embeddings
        self.retriever = MemoryRetriever(
            self.db, self.store, self.clock, embeddings,
            max_facts=int(config.get("memory.max_facts_in_prompt", 12)),
            recent_days=int(config.get("memory.recent_days", 3)),
        )
        self.people = PeopleRegistry(self.db, config)
        self.permissions = PermissionRegistry(config)

        # personality
        self.persona = Persona(self.db, self.clock, seed_path=seed_path)
        self.mood = MoodEngine(self.db, self.clock, self.background_provider,
                               config=config.get("personality", {}))
        self.evolution = PersonaEvolution(
            self.db, self.persona, self.background_provider, self.clock,
            affinity_label=self.mood.tier_label(),
            config=config.get("personality.evolution", {}),
        )

        # resource coordination (read-only traffic-light probe + background gate)
        self.coordinator = ResourceCoordinator(config, self.clock)
        self.guard = ComputeGuard(config, self.clock, coordinator=self.coordinator)

        # reminders
        self.reminders = ReminderStore(self.db, self.clock, config=config.get("reminders", {}))
        self.reminder_scheduler = ReminderScheduler(
            self.reminders, self.clock, self._on_reminder_due,
            missed_replay=bool(config.get("reminders.missed_replay", True)),
        )

        # consolidation
        self.consolidator = Consolidator(
            self.db, self.store, self.background_provider, self.clock,
            config={
                "consolidation_idle_minutes": config.get("memory.consolidation_idle_minutes", 15),
                "tidy_duplicate_threshold": config.get("memory.tidy_duplicate_threshold", 0.75),
            },
            compute_guard=self.guard,
        )

        # conversation
        self.tools = default_tool_registry({
            "reminders": self.reminders, "clock": self.clock, "memory": self.store,
            "config": config,
        })
        self.conversation = ConversationManager(
            self.db, self.clock, config, self.persona, self.mood, self.retriever,
            self.store, self.reminders, self.people,
            self.chat_provider, self.background_provider, self.tools,
            evolution=self.evolution, compute_guard=self.guard,
        )
        self.reminder_scheduler.on_due = self.conversation.deliver_reminder

        # proactive
        self.proactive = ProactiveScheduler(
            self.db, self.clock, self.chat_provider, self.store, self.mood,
            config=config.get("proactive", {}), compute_guard=self.guard,
        )
        self.proactive.on_initiate = self.conversation.send_proactive
        self.conversation.set_proactive_callback(self._on_user_activity)

        # voice + senses (Kayock = the host's existing speech services)
        tts_kind = config.get("voice.tts", "none")
        if tts_kind == "kayock":
            tts = KayockTTS(
                config.get("voice.kayock_tts_url"),
                voice=config.get("voice.voice_id", "am_adam"),
                model=config.get("voice.kayock_tts_model", "kokoro"),
                response_format=config.get("voice.format", "mp3"),
                api_key=config.get("voice.kayock_tts_api_key", ""),
                timeout=float(config.get("voice.tts_timeout_seconds", 45.0)),
            )
        elif tts_kind == "kokoro":
            tts = KokoroTTS(config.get("voice.kokoro_url"), config.get("voice.kokoro_voice", "af_heart"))
        elif tts_kind == "edge":
            from .voice.tts import EdgeTTS

            tts = EdgeTTS(config.get("voice.edge_voice", "en-US-AriaNeural"))
        else:
            tts = NoneTTS()
        stt_kind = config.get("voice.stt", "none")
        if stt_kind == "kayock":
            stt = KayockSTT(
                config.get("voice.kayock_stt_url"),
                model=config.get("voice.kayock_stt_model", "base.en"),
                language=config.get("voice.stt_language") or None,
                api_key=config.get("voice.kayock_stt_api_key", ""),
                timeout=float(config.get("voice.stt_timeout_seconds", 60.0)),
            )
        else:
            stt = WhisperSTT()
        self.voice = VoiceService(config, self.permissions, tts=tts, stt=stt)
        # Client-driven playback is the default; the server-side hook exists
        # for API-only setups and must be explicitly enabled to avoid double
        # synthesis on the host.
        self.conversation._voice = self.voice \
            if config.get("voice.auto_speak", False) and config.get("voice.server_speak", False) \
            else None
        self.senses = ScreenSense(self.permissions)

        self._started = False

    # ---------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.reminder_scheduler.start()
        if self.config.get("proactive.enabled", True):
            self.proactive.start()
        if self.config.get("memory.enabled", True):
            self.consolidator.start()

    async def stop(self) -> None:
        self._started = False
        await self.proactive.stop()
        await self.consolidator.stop()
        await self.reminder_scheduler.stop()
        await self.voice.close()
        await self.guard.close()
        if self.embeddings is not None:
            await self.embeddings.close()
        if self.chat_provider is not None:
            await self.chat_provider.close()
        if self.background_provider is not None and self.background_provider is not self.chat_provider:
            await self.background_provider.close()
        self.db.close()

    def _on_user_activity(self) -> None:
        self.proactive.touch_user_message()
        self.consolidator.touch()
        self.reminder_scheduler.notify_changed()

    async def _on_reminder_due(self, reminder, note: str) -> None:
        await self.conversation.deliver_reminder(reminder, note)

    # ---------------------------------------------------------------- info
    def status(self) -> dict:
        return {
            "version": __version__,
            "model": getattr(self.chat_provider, "model", "") if self.chat_provider else "",
            "model_reachable": None,
            "background_model": getattr(self.background_provider, "name", "") if self.background_provider else "",
            "memory": self.store.counts(),
            "mood": self.mood.state(),
            "persona_version": self.persona.state.version,
            "voice": self.voice.status(),
            "proactive_enabled": self.proactive.enabled,
            "focus": self.proactive.focus_active(),
            "quiet_hours": f"{self.proactive.quiet_start}-{self.proactive.quiet_end}",
            "resources": {
                "coordinator": self.coordinator.snapshot(),
                "guard": self.guard.snapshot(),
                "last_deferral": self.db.get_meta_json("resources.last_deferral", None),
            },
        }
