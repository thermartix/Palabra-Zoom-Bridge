from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

from modules.palabra_client import configure_translation, graceful_stop_palabra_task


@dataclass(frozen=True)
class PalabraAgentSettings:
    source_language: str
    target_language: str
    voice_id: Optional[str]
    api_rate: int
    channels: int
    segment_confirmation_silence_threshold: float
    only_confirm_by_silence: bool
    sentence_splitter_enabled: bool
    translate_partial_transcriptions: bool
    desired_queue_level_ms: int
    max_queue_level_ms: int
    auto_tempo: bool
    min_tempo: float
    max_tempo: float
    end_task_eos_timeout_seconds: float
    graceful_shutdown_timeout_seconds: float
    dump_messages: bool = False


class PalabraAgent:
    """One Palabra worker for one target language.

    Phase II can create several of these against a shared source stream. The
    current cable transport still runs exactly one target-language agent.
    """

    def __init__(self, settings: PalabraAgentSettings, audio_bridge) -> None:
        self.settings = settings
        self.audio_bridge = audio_bridge
        self.send_task: Optional[asyncio.Task] = None
        self.receive_task: Optional[asyncio.Task] = None

    async def configure(self, websocket) -> None:
        await configure_translation(
            websocket,
            source_language=self.settings.source_language,
            target_language=self.settings.target_language,
            voice_id=self.settings.voice_id,
            api_rate=self.settings.api_rate,
            channels=self.settings.channels,
            segment_confirmation_silence_threshold=(
                self.settings.segment_confirmation_silence_threshold
            ),
            only_confirm_by_silence=self.settings.only_confirm_by_silence,
            sentence_splitter_enabled=self.settings.sentence_splitter_enabled,
            translate_partial_transcriptions=self.settings.translate_partial_transcriptions,
            desired_queue_level_ms=self.settings.desired_queue_level_ms,
            max_queue_level_ms=self.settings.max_queue_level_ms,
            auto_tempo=self.settings.auto_tempo,
            min_tempo=self.settings.min_tempo,
            max_tempo=self.settings.max_tempo,
        )

    def start(self, websocket) -> set[asyncio.Task]:
        self.send_task = asyncio.create_task(self.audio_bridge.send_audio(websocket))
        self.receive_task = asyncio.create_task(
            self.audio_bridge.receive_audio(
                websocket,
                self.settings.source_language,
                self.settings.target_language,
                dump_messages=self.settings.dump_messages,
            )
        )
        return {self.send_task, self.receive_task}

    async def stop_gracefully(self, websocket) -> None:
        if self.send_task is not None and not self.send_task.done():
            send_done, _ = await asyncio.wait(
                {self.send_task},
                timeout=min(2.0, self.settings.graceful_shutdown_timeout_seconds),
            )
            if self.send_task not in send_done:
                self.send_task.cancel()
                await asyncio.gather(self.send_task, return_exceptions=True)
        if self.receive_task is not None:
            await graceful_stop_palabra_task(
                websocket,
                self.receive_task,
                eos_timeout_seconds=self.settings.end_task_eos_timeout_seconds,
                shutdown_timeout_seconds=self.settings.graceful_shutdown_timeout_seconds,
            )
