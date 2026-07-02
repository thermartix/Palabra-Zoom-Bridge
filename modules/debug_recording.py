from __future__ import annotations

import contextlib
import json
import queue
import shutil
import subprocess
import threading
import time
import wave
from pathlib import Path
from typing import Optional

import numpy as np

from modules.logging_utils import log_timestamp

class WavDebugRecorder:
    def __init__(self, path: Path, sample_rate: int, channels: int) -> None:
        self.path = path
        self.sample_rate = sample_rate
        self.channels = channels
        self.file: Optional[wave.Wave_write] = None
        self.lock = threading.Lock()

    def __enter__(self) -> "WavDebugRecorder":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = wave.open(str(self.path), "wb")
        self.file.setnchannels(self.channels)
        self.file.setsampwidth(2)
        self.file.setframerate(self.sample_rate)
        return self

    def write(self, audio: np.ndarray) -> None:
        if self.file is None or len(audio) == 0:
            return
        with self.lock:
            self.file.writeframes(audio.astype(np.int16, copy=False).tobytes())

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.file is not None:
            self.file.close()
            self.file = None


class AsyncWavDebugRecorder(WavDebugRecorder):
    def __init__(self, path: Path, sample_rate: int, channels: int) -> None:
        super().__init__(path, sample_rate, channels)
        self.queue: queue.Queue[Optional[np.ndarray]] = queue.Queue(maxsize=300)
        self.thread: Optional[threading.Thread] = None
        self.dropped_blocks = 0

    def __enter__(self) -> "AsyncWavDebugRecorder":
        super().__enter__()

        def run() -> None:
            while True:
                audio = self.queue.get()
                if audio is None:
                    break
                super(AsyncWavDebugRecorder, self).write(audio)

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        return self

    def write(self, audio: np.ndarray) -> None:
        if self.file is None or len(audio) == 0:
            return
        block = audio.astype(np.int16, copy=True)
        try:
            self.queue.put_nowait(block)
        except queue.Full:
            self.dropped_blocks += 1
            with contextlib.suppress(queue.Empty):
                self.queue.get_nowait()
            with contextlib.suppress(queue.Full):
                self.queue.put_nowait(block)

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.thread is not None:
            while True:
                try:
                    self.queue.put(None, timeout=0.1)
                    break
                except queue.Full:
                    self.dropped_blocks += 1
                    with contextlib.suppress(queue.Empty):
                        self.queue.get_nowait()
            self.thread.join(timeout=2)
            self.thread = None
        if self.dropped_blocks:
            print(f"Warning: dropped {self.dropped_blocks} debug WAV block(s) for {self.path}", flush=True)
        super().__exit__(exc_type, exc, tb)


def require_ffmpeg_for_debug_mp3() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg is required for debug MP3 conversion. Install ffmpeg or disable recording.")
    return ffmpeg


def convert_debug_wavs_to_mp3(wav_paths: list[Path], ffmpeg: str) -> None:
    for wav_path in wav_paths:
        if not wav_path.exists():
            continue
        mp3_path = wav_path.with_suffix(".mp3")
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(wav_path),
                "-codec:a",
                "libmp3lame",
                "-b:a",
                "128k",
                str(mp3_path),
            ],
            check=True,
        )
        wav_path.unlink()
        print(f"Converted debug MP3: {mp3_path}", flush=True)
class DebugTextLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.file = None
        self.lock = threading.Lock()
        self.started_at = time.monotonic()

    def __enter__(self) -> "DebugTextLogger":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("w", encoding="utf-8")
        self.write("debug_log_start", timestamp=log_timestamp())
        return self

    def write(self, event: str, **fields) -> None:
        if self.file is None:
            return
        elapsed = time.monotonic() - self.started_at
        field_text = " ".join(f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in fields.items())
        line = f"{elapsed:10.3f}s {event}"
        if field_text:
            line = f"{line} {field_text}"
        with self.lock:
            self.file.write(line + "\n")
            self.file.flush()

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.file is not None:
            self.write("debug_log_stop", timestamp=log_timestamp())
            self.file.close()
            self.file = None
