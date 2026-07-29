from __future__ import annotations

import json
import unittest

from modules.palabra_client import configure_translation


class RecordingWebSocket:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, message: str) -> None:
        self.messages.append(message)


class ConfigureTranslationTests(unittest.IsolatedAsyncioTestCase):
    async def configure(self, *, voice_id: str | None, voice_cloning: bool) -> dict:
        websocket = RecordingWebSocket()
        await configure_translation(
            websocket,
            source_language="es",
            target_language="de",
            voice_id=voice_id,
            voice_cloning=voice_cloning,
            api_rate=24000,
            channels=1,
            segment_confirmation_silence_threshold=0.7,
            only_confirm_by_silence=True,
            sentence_splitter_enabled=False,
            translate_partial_transcriptions=False,
            desired_queue_level_ms=5000,
            max_queue_level_ms=20000,
            auto_tempo=True,
            min_tempo=1.0,
            max_tempo=1.45,
        )
        return json.loads(websocket.messages[0])

    async def test_voice_cloning_ignores_configured_voice_id(self) -> None:
        payload = await self.configure(voice_id="fixed-voice", voice_cloning=True)

        speech_generation = payload["data"]["pipeline"]["translations"][0]["speech_generation"]

        self.assertEqual(speech_generation, {"voice_cloning": True})

    async def test_voice_id_is_used_when_voice_cloning_is_disabled(self) -> None:
        payload = await self.configure(voice_id="fixed-voice", voice_cloning=False)

        speech_generation = payload["data"]["pipeline"]["translations"][0]["speech_generation"]

        self.assertEqual(speech_generation, {"voice_id": "fixed-voice"})


if __name__ == "__main__":
    unittest.main()
