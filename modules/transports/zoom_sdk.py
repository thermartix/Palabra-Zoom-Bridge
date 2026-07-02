from __future__ import annotations

import asyncio
import importlib
import math
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

import numpy as np


AudioCallback = Callable[[bytes, int, int], Awaitable[None] | None]


@dataclass(frozen=True)
class ZoomSdkProbeSettings:
    meeting_number: str
    password: str
    display_name: str
    probe_seconds: float
    output_wav: Path
    sample_rate: int
    channels: int
    adapter_module: str
    adapter_command: str
    sdk_root: str
    dry_run: bool
    adapter_args: tuple[str, ...] = ()
    auth_token: str = ""


class ZoomSdkProbeRecorder:
    def __init__(self, path: Path, sample_rate: int, channels: int) -> None:
        self.path = path
        self.sample_rate = sample_rate
        self.channels = channels
        self.bytes_written = 0
        self.peak = 0
        self._file: Optional[wave.Wave_write] = None

    def __enter__(self) -> "ZoomSdkProbeRecorder":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = wave.open(str(self.path), "wb")
        self._file.setnchannels(self.channels)
        self._file.setsampwidth(2)
        self._file.setframerate(self.sample_rate)
        return self

    async def write(self, audio: bytes, sample_rate: int, channels: int) -> None:
        if sample_rate != self.sample_rate or channels != self.channels:
            raise ValueError(
                "SDK probe audio format changed during capture: "
                f"expected {self.sample_rate} Hz/{self.channels} ch, "
                f"got {sample_rate} Hz/{channels} ch."
            )
        if len(audio) % 2:
            raise ValueError("SDK probe received an odd number of PCM bytes.")
        if self._file is None:
            raise RuntimeError("SDK probe recorder is not open.")
        self._file.writeframes(audio)
        self.bytes_written += len(audio)
        samples = np.frombuffer(audio, dtype=np.int16)
        if len(samples):
            self.peak = max(self.peak, int(np.max(np.abs(samples.astype(np.int32)))))

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None


class ZoomSdkTransport:
    """Minimal Zoom SDK audio-capture probe.

    A real SDK binding can be plugged in with ``zoom_sdk.adapter_module``. The
    module must expose either ``run_probe(settings, audio_callback)`` or
    ``create_adapter(settings, audio_callback)`` returning an object with
    ``run_probe()``.
    """

    def __init__(self, settings: ZoomSdkProbeSettings) -> None:
        self.settings = settings

    async def run_probe(self) -> Path:
        with ZoomSdkProbeRecorder(
            self.settings.output_wav,
            self.settings.sample_rate,
            self.settings.channels,
        ) as recorder:
            if self.settings.dry_run:
                await self._run_dry_probe(recorder.write)
            else:
                await self._run_adapter_probe(recorder.write)

            seconds = recorder.bytes_written / max(
                1,
                self.settings.sample_rate * self.settings.channels * 2,
            )
            print(
                "Zoom SDK probe recorded "
                f"{seconds:.2f}s, peak={recorder.peak}, file={self.settings.output_wav}",
                flush=True,
            )
        return self.settings.output_wav

    async def _run_adapter_probe(self, audio_callback: AudioCallback) -> None:
        if not self.settings.adapter_module:
            raise SystemExit(
                "zoom_sdk.adapter_module is not configured. "
                "Set it to a Python module that wraps the Zoom Meeting SDK raw audio callbacks, "
                "or run --zoom-sdk-dry-run to test the probe recorder without the SDK. "
                "The adapter receives the Meeting SDK JWT as settings.auth_token."
            )

        try:
            module = importlib.import_module(self.settings.adapter_module)
        except ImportError as exc:
            raise SystemExit(
                f"Could not import zoom_sdk.adapter_module {self.settings.adapter_module!r}: {exc}"
            ) from exc

        if hasattr(module, "run_probe"):
            result = module.run_probe(self.settings, audio_callback)
            if asyncio.iscoroutine(result):
                await result
            return

        if hasattr(module, "create_adapter"):
            adapter = module.create_adapter(self.settings, audio_callback)
            result = adapter.run_probe()
            if asyncio.iscoroutine(result):
                await result
            return

        raise SystemExit(
            f"{self.settings.adapter_module!r} must define run_probe(settings, audio_callback) "
            "or create_adapter(settings, audio_callback)."
        )

    async def _run_dry_probe(self, audio_callback: AudioCallback) -> None:
        print("Running Zoom SDK probe dry run with generated PCM.", flush=True)
        sample_rate = self.settings.sample_rate
        channels = self.settings.channels
        block_ms = 20
        frames_per_block = max(1, int(sample_rate * block_ms / 1000))
        total_blocks = max(1, int(self.settings.probe_seconds * 1000 / block_ms))
        amplitude = 0.2 * 32767

        for block_index in range(total_blocks):
            start = block_index * frames_per_block
            stop = start + frames_per_block
            t = np.arange(start, stop, dtype=np.float32) / sample_rate
            mono = np.sin(2.0 * math.pi * 440.0 * t) * amplitude
            samples = np.clip(np.rint(mono), -32768, 32767).astype(np.int16)
            if channels > 1:
                samples = np.repeat(samples[:, None], channels, axis=1).reshape(-1)
            result = audio_callback(samples.tobytes(), sample_rate, channels)
            if asyncio.iscoroutine(result):
                await result
            await asyncio.sleep(0)
