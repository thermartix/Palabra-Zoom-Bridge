#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import asyncio
import base64
import ctypes
import ctypes.wintypes
import dataclasses
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
import contextlib
import wave
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Optional
from urllib.parse import quote
import tomllib

import httpx
import numpy as np
import sounddevice as sd
import websockets
from dotenv import load_dotenv
from scipy.signal import resample_poly


SESSION_URL = "https://api.palabra.ai/session-storage/session"
SESSIONS_URL = "https://api.palabra.ai/session-storage/sessions"
APP_NAME = "Palabra Zoom Bridge"
__version__ = "0.3.14"
APP_VERSION = __version__
CONFIG_PATH = Path("config.toml")
LOG_DIR = Path("logs")
CABLE_ROUTE_LOG_PATH = LOG_DIR / "cable_route.log"
LAST_ERROR_LOG_PATH = LOG_DIR / "last_error.txt"
DEFAULT_ZOOM_SPEAKER_DEVICE = "CABLE-B Input"
DEFAULT_ZOOM_MIC_DEVICE = "CABLE-A Output"
DEFAULT_END_WHEN_ZOOM_MEETING_ENDS = True
DEFAULT_ZOOM_MEETING_TITLE_PATTERNS = (
    "Zoom Meeting",
    "Zoom Webinar",
)
DEFAULT_ZOOM_MEETING_CHECK_SECONDS = 2.0
DEFAULT_ZOOM_MEETING_END_GRACE_SECONDS = 15.0
DEFAULT_DEVICE_RATE = 48000
DEFAULT_API_RATE = 24000
DEFAULT_CHANNELS = 1
PALABRA_OUTPUT_RATE = 24000
PALABRA_OUTPUT_CHANNELS = 1
DEFAULT_DEVICE_CHANNELS = 2
DEFAULT_CHUNK_MS = 320
DEFAULT_INPUT_BLOCK_MS = 50
DEFAULT_PLAYBACK_BUFFER_MS = 500
DEFAULT_PHRASE_START_BUFFER_MS = 1800
DEFAULT_PLAYBACK_MAX_BUFFER_MS = 5000
DEFAULT_STARTUP_DELAY = 0.0
DEFAULT_TASK_READY_TIMEOUT_SECONDS = 30.0
DEFAULT_TASK_POLL_SECONDS = 2.0
DEFAULT_END_TASK_EOS_TIMEOUT_SECONDS = 2.0
DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS = 6.0
DEFAULT_PLAYBACK_DRAIN_TIMEOUT_SECONDS = 30.0
DEFAULT_RAW_PALABRA_PLAYBACK = False
DEFAULT_OUTPUT_GAIN = 0.55
DEFAULT_SEGMENT_CONFIRMATION_SILENCE_THRESHOLD = 0.3
DEFAULT_ONLY_CONFIRM_BY_SILENCE = False
DEFAULT_SENTENCE_SPLITTER_ENABLED = True
DEFAULT_PALABRA_TRANSLATE_PARTIALS = True
DEFAULT_PALABRA_DESIRED_QUEUE_LEVEL_MS = 2000
DEFAULT_PALABRA_MAX_QUEUE_LEVEL_MS = 5000
DEFAULT_PALABRA_AUTO_TEMPO = True
DEFAULT_PALABRA_MIN_TEMPO = 1.0
DEFAULT_PALABRA_MAX_TEMPO = 1.1
DEFAULT_TEST_SECONDS = 3.0
DEFAULT_TEST_VOLUME = 0.5
DEFAULT_RECORD_DEBUG_MP3 = False
DEFAULT_DEVICE_PROBE_SECONDS = 0.15
PLAYBACK_BUFFER_STEP_MS = 500
PLAYBACK_ACTIVE_UNDERRUN_STEP_MS = 200
PLAYBACK_BUFFER_RELAX_SECONDS = 30.0
PLAYBACK_SILENCE_RMS = 120.0
PLAYBACK_START_LOOKAHEAD_MS = 2500
PLAYBACK_CATCHUP_TARGET_MS = 1200
PLAYBACK_CATCHUP_FULL_BACKLOG_MS = 4000
DEFAULT_PLAYBACK_TEMPO = 1.0
DEFAULT_PLAYBACK_MAX_LOCAL_TEMPO = 1.06
DEFAULT_PLAYBACK_TEMPO_ALGORITHM = "resample"
PLAYBACK_TEMPO_ALGORITHMS = {"resample", "rubberband"}
DEFAULT_PLAYBACK_FADE_MS = 5
DEFAULT_IDLE_NOISE_AMPLITUDE = 96
BLOCKED_HOSTAPIS = {"Windows WASAPI"}
DEFAULT_HOSTAPI_PREFERENCE = (
    "Windows DirectSound",
    "MME",
    "Windows WDM-KS",
)
DEFAULT_SOURCE_LANGUAGE = "es"
DEFAULT_TARGET_LANGUAGE = "de"
ES_CONTINUOUS = 0x80000000
ES_DISPLAY_REQUIRED = 0x00000002
ES_SYSTEM_REQUIRED = 0x00000001
LANGUAGE_NAMES = {
    "ar": "Arabic",
    "cs": "Czech",
    "de": "German",
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "it": "Italian",
    "ja": "Japanese",
    "ko": "Korean",
    "nl": "Dutch",
    "pl": "Polish",
    "pt": "Portuguese",
    "ru": "Russian",
    "tr": "Turkish",
    "uk": "Ukrainian",
    "zh": "Chinese",
}


class PalabraRuntimeError(RuntimeError):
    pass


def language_label(language_code: str) -> str:
    return LANGUAGE_NAMES.get(language_code.lower(), language_code)


def log_timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def write_log_file(path: Path, text: str, append: bool = True) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with path.open(mode, encoding="utf-8") as file:
        file.write(text)
        if not text.endswith("\n"):
            file.write("\n")


def append_cable_route_log(lines: list[str]) -> None:
    entry = [f"[{log_timestamp()}]", *lines, ""]
    write_log_file(CABLE_ROUTE_LOG_PATH, "\n".join(entry), append=True)


def portaudio_error_lines(error: BaseException) -> list[str]:
    lines = [
        f"PortAudio error type: {type(error).__name__}",
        f"PortAudio error str: {error}",
        f"PortAudio error repr: {error!r}",
    ]
    args = getattr(error, "args", None)
    if args:
        lines.append(f"PortAudio error args: {args!r}")
    hosterror_info = getattr(error, "hosterror_info", None)
    if hosterror_info:
        lines.append(f"PortAudio hosterror_info: {hosterror_info!r}")
    return lines


def write_last_error_log(error: BaseException) -> None:
    if isinstance(error, SystemExit):
        details = f"SystemExit: {error.code}"
    else:
        details = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    text = "\n".join(
        [
            f"[{log_timestamp()}]",
            f"Command: {' '.join(sys.argv)}",
            details.rstrip(),
            "",
        ]
    )
    write_log_file(LAST_ERROR_LOG_PATH, text, append=False)


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


@dataclasses.dataclass
class PlaybackChunk:
    audio: np.ndarray
    segment_end: bool = False


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


def keep_windows_awake() -> bool:
    if os.name != "nt":
        return False

    state = ES_CONTINUOUS | ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED
    result = ctypes.windll.kernel32.SetThreadExecutionState(state)
    if result == 0:
        print("Warning: Windows did not accept the keep-awake request.", flush=True)
        return False

    print("Windows sleep/display timeout is paused while the bridge is running.")
    return True


def restore_windows_power_state(enabled: bool) -> None:
    if not enabled or os.name != "nt":
        return

    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    print("Windows sleep/display timeout restored.", flush=True)


def list_devices() -> None:
    devices = sd.query_devices()
    hostapis = sd.query_hostapis()
    print("\nAudio devices:\n")
    for idx, device in enumerate(devices):
        inputs = int(device.get("max_input_channels", 0))
        outputs = int(device.get("max_output_channels", 0))
        default_rate = int(device.get("default_samplerate", 0))
        hostapi = hostapi_name(device, hostapis)
        print(
            f"{idx:>3}  api={hostapi:<19} in={inputs:<2} out={outputs:<2} "
            f"rate={default_rate:<5}  {device['name']}"
        )
    print()


def hostapi_name(device: dict, hostapis: Optional[list[dict]] = None) -> str:
    if hostapis is None:
        hostapis = sd.query_hostapis()
    hostapi_index = device.get("hostapi")
    if hostapi_index is None:
        return "unknown"
    return str(hostapis[int(hostapi_index)]["name"])


def format_device_summary(device_id: int, devices=None, hostapis: Optional[list[dict]] = None) -> str:
    if devices is None:
        devices = sd.query_devices()
    device = devices[device_id]
    inputs = int(device.get("max_input_channels", 0))
    outputs = int(device.get("max_output_channels", 0))
    default_rate = int(device.get("default_samplerate", 0))
    return (
        f"{device_id} [{hostapi_name(device, hostapis)}, {default_rate} Hz, "
        f"in={inputs}, out={outputs}] - {device['name']}"
    )


def meter_input(selector: str, device_rate: int, channels: int) -> None:
    device = resolve_device(selector, "input")
    print(f"Metering input {device} - {sd.query_devices(device)['name']}")
    print("Press Ctrl+C to stop.")

    q: queue.Queue[np.ndarray] = queue.Queue(maxsize=30)

    def callback(indata, frames, time_info, status):
        if status:
            print(f"[meter] {status}", flush=True)
        try:
            q.put_nowait(np.frombuffer(indata, dtype=np.int16).copy())
        except queue.Full:
            pass

    with sd.RawInputStream(
        device=device,
        samplerate=device_rate,
        channels=channels,
        dtype="int16",
        blocksize=int(device_rate * 0.05),
        callback=callback,
    ):
        while True:
            audio = q.get()
            rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2))) if len(audio) else 0.0
            level = min(60, int(rms / 500))
            print(f"\rlevel [{'#' * level}{'.' * (60 - level)}] rms={rms:7.1f}", end="", flush=True)


def test_output(
    selector: str,
    device_rate: int,
    channels: int,
    seconds: float,
    volume: float,
) -> None:
    device = resolve_device(selector, "output")
    print(f"Playing test tone to output {device} - {sd.query_devices(device)['name']}")
    samples = int(device_rate * seconds)
    t = np.arange(samples, dtype=np.float32) / device_rate
    tone = volume * np.sin(2 * np.pi * 440 * t)
    audio = np.clip(tone * 32767, -32768, 32767).astype(np.int16)
    if channels > 1:
        audio = np.repeat(audio[:, None], channels, axis=1)
    try:
        sd.play(audio, samplerate=device_rate, device=device, blocking=True)
        sd.stop()
    except sd.PortAudioError as exc:
        print_sample_rate_hint(exc, device, device_rate)


def resolve_device(selector: str, kind: str) -> int:
    if selector.strip().isdigit():
        return int(selector)

    matches = find_device_matches(selector, kind)
    if not matches:
        raise SystemExit(f"No {kind} audio device matched {selector!r}. Run with --list-devices.")
    if len(matches) > 1:
        return prompt_for_device(selector, kind, matches)
    return matches[0][0]


def find_device_matches(selector: str, kind: str) -> list[tuple[int, str]]:
    selector_lower = selector.lower()
    matches = []
    for idx, device in enumerate(sd.query_devices()):
        channels = device["max_input_channels"] if kind == "input" else device["max_output_channels"]
        if channels > 0 and selector_lower in device["name"].lower():
            matches.append((idx, device["name"]))
    return matches


def prompt_for_device(selector: str, kind: str, matches: list[tuple[int, str]]) -> int:
    devices = sd.query_devices()
    hostapis = sd.query_hostapis()
    match_ids = {idx for idx, _ in matches}
    print(f"Multiple {kind} devices matched {selector!r}:")
    for idx, name in matches:
        device = devices[idx]
        default_rate = int(device.get("default_samplerate", 0))
        print(f"  {idx}: [{hostapi_name(device, hostapis)}, {default_rate} Hz] {name}")

    while True:
        try:
            choice = input(f"Type the {kind} device number to use: ").strip()
        except EOFError as exc:
            raise SystemExit("Could not read a device selection. Use a numeric device id in config.toml.") from exc

        if not choice.isdigit():
            print("Please type one of the listed numeric device ids.")
            continue

        device_id = int(choice)
        if device_id in match_ids:
            return device_id

        print("That device id is not in the matched list. Please choose one of the listed ids.")


def opposite_cable_name(device_name: str) -> str:
    if re.search(r"\bInput\b", device_name, flags=re.IGNORECASE):
        return re.sub(r"\bInput\b", "Output", device_name, count=1, flags=re.IGNORECASE)
    if re.search(r"\bOutput\b", device_name, flags=re.IGNORECASE):
        return re.sub(r"\bOutput\b", "Input", device_name, count=1, flags=re.IGNORECASE)
    raise SystemExit(
        f"Could not infer the opposite side for {device_name!r}. "
        "Use input_device/output_device directly in config.toml."
    )


def resolve_opposite_cable_device(
    zoom_selector: str,
    zoom_kind: str,
    bridge_kind: str,
    label: str,
) -> int:
    zoom_device = resolve_device(zoom_selector, zoom_kind)
    devices = sd.query_devices()
    zoom_info = devices[zoom_device]
    zoom_name = zoom_info["name"]
    counterpart_name = opposite_cable_name(zoom_name)
    bridge_device = resolve_matching_counterpart(
        counterpart_name,
        bridge_kind,
        zoom_info.get("hostapi"),
    )
    print(f"{label}: Zoom uses {zoom_device} - {zoom_name}")
    print(f"{label}: bridge uses matching {bridge_device} - {sd.query_devices(bridge_device)['name']}")
    return bridge_device


def resolve_matching_counterpart(
    counterpart_name: str,
    bridge_kind: str,
    zoom_hostapi: Optional[int],
) -> int:
    devices = sd.query_devices()
    candidates = []
    for idx, device in enumerate(devices):
        channels = device["max_input_channels"] if bridge_kind == "input" else device["max_output_channels"]
        if channels <= 0:
            continue
        if device["name"].lower() == counterpart_name.lower():
            candidates.append((idx, device))

    if not candidates:
        return resolve_device(counterpart_name, bridge_kind)

    same_hostapi = [
        (idx, device)
        for idx, device in candidates
        if zoom_hostapi is not None and device.get("hostapi") == zoom_hostapi
    ]
    if len(same_hostapi) == 1:
        return same_hostapi[0][0]
    if len(candidates) == 1:
        return candidates[0][0]

    print(
        f"Found multiple matching {bridge_kind} devices for {counterpart_name!r}, "
        "but none could be uniquely paired by host API."
    )
    return prompt_for_device(counterpart_name, bridge_kind, [(idx, device["name"]) for idx, device in candidates])


def device_channels(device: dict, kind: str) -> int:
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    return int(device.get(key, 0))


def device_default_rate(device: dict) -> int:
    return int(device.get("default_samplerate", 0))


def stream_blocksize(device_rate: int, block_ms: int) -> int:
    if block_ms <= 0:
        return 0
    return max(1, int(device_rate * block_ms / 1000))


def candidate_counterparts(zoom_device: int, bridge_kind: str) -> list[tuple[int, int]]:
    devices = sd.query_devices()
    zoom_info = devices[zoom_device]
    counterpart_name = opposite_cable_name(zoom_info["name"])
    pairs = []
    for idx, device in enumerate(devices):
        if device_channels(device, bridge_kind) <= 0:
            continue
        if device["name"].lower() == counterpart_name.lower():
            pairs.append((zoom_device, idx))
    return pairs


def candidate_pairs(selector: str, zoom_kind: str, bridge_kind: str) -> list[tuple[int, int]]:
    pairs = []
    for zoom_device, _ in find_device_matches(selector, zoom_kind):
        pairs.extend(candidate_counterparts(zoom_device, bridge_kind))
    return pairs


def can_open_bridge_pair(
    input_device: int,
    output_device: int,
    device_rate: int,
    channels: int,
    input_block_ms: int,
) -> tuple[bool, Optional[sd.PortAudioError]]:
    blocksize = stream_blocksize(device_rate, input_block_ms)

    def input_callback(indata, frames, time_info, status):
        pass

    def output_callback(outdata, frames, time_info, status):
        outdata.fill(0)

    try:
        with sd.RawInputStream(
            device=input_device,
            samplerate=device_rate,
            channels=channels,
            dtype="int16",
            blocksize=blocksize,
            callback=input_callback,
        ):
            with sd.OutputStream(
                device=output_device,
                samplerate=device_rate,
                channels=channels,
                dtype="int16",
                blocksize=blocksize,
                callback=output_callback,
            ):
                time.sleep(DEFAULT_DEVICE_PROBE_SECONDS)
    except sd.PortAudioError as exc:
        return False, exc
    return True, None


def score_pair_choice(
    speaker_pair: tuple[int, int],
    mic_pair: tuple[int, int],
    hostapi_preference: dict[str, int],
) -> tuple[int, int, int, int, int, int]:
    devices = sd.query_devices()
    hostapis = sd.query_hostapis()
    speaker_zoom, speaker_bridge = speaker_pair
    mic_zoom, mic_bridge = mic_pair
    hostapi_index = int(devices[speaker_zoom].get("hostapi", -1))
    hostapi = hostapi_name(devices[speaker_zoom], hostapis)
    hostapi_rank = hostapi_preference.get(hostapi, len(hostapi_preference))
    rates = [
        device_default_rate(devices[idx])
        for idx in (speaker_zoom, speaker_bridge, mic_zoom, mic_bridge)
    ]
    rate_penalty = len(set(rates))
    channel_penalty = (
        device_channels(devices[speaker_bridge], "input")
        + device_channels(devices[mic_bridge], "output")
    )
    return (
        hostapi_rank,
        rate_penalty,
        -channel_penalty,
        hostapi_index,
        speaker_bridge,
        mic_bridge,
    )


def candidate_rates(speaker_pair: tuple[int, int], mic_pair: tuple[int, int]) -> list[int]:
    devices = sd.query_devices()
    rates = [
        device_default_rate(devices[idx])
        for idx in (*speaker_pair, *mic_pair)
        if device_default_rate(devices[idx]) > 0
    ]
    ordered_rates = []
    for rate in rates:
        if rate not in ordered_rates:
            ordered_rates.append(rate)
    return ordered_rates


def auto_select_bridge_devices(args) -> tuple[int, int, int]:
    devices = sd.query_devices()
    hostapis = sd.query_hostapis()
    hostapi_preference = {
        hostapi: rank for rank, hostapi in enumerate(args.hostapi_preference)
    }
    speaker_pairs = candidate_pairs(args.zoom_speaker_device, "output", "input")
    mic_pairs = candidate_pairs(args.zoom_mic_device, "input", "output")

    choices = []
    for speaker_pair in speaker_pairs:
        speaker_zoom, speaker_bridge = speaker_pair
        speaker_hostapi = devices[speaker_zoom].get("hostapi")
        for mic_pair in mic_pairs:
            mic_zoom, mic_bridge = mic_pair
            if devices[mic_zoom].get("hostapi") != speaker_hostapi:
                continue
            if devices[speaker_bridge].get("hostapi") != speaker_hostapi:
                continue
            if devices[mic_bridge].get("hostapi") != speaker_hostapi:
                continue
            choices.append((speaker_pair, mic_pair))

    if not choices:
        raise SystemExit(
            "Could not find a complete VB-Cable A/B pair on one backend. "
            "Run with --list-devices and check zoom.speaker_device / zoom.mic_device in config.toml."
        )

    choices.sort(key=lambda choice: score_pair_choice(choice[0], choice[1], hostapi_preference))
    for speaker_pair, mic_pair in choices:
        speaker_zoom, input_device = speaker_pair
        mic_zoom, output_device = mic_pair
        for device_rate in candidate_rates(speaker_pair, mic_pair):
            can_open, open_error = can_open_bridge_pair(
                input_device,
                output_device,
                device_rate,
                args.device_channels,
                args.input_block_ms,
            )
            if not can_open:
                hostapi = hostapi_name(devices[speaker_zoom], hostapis)
                error_lines = (
                    portaudio_error_lines(open_error)
                    if open_error
                    else ["No PortAudio error detail available."]
                )
                append_cable_route_log(
                    [
                        (
                            f"Rejected {hostapi} cable pair at {device_rate} Hz "
                            f"with {args.device_channels} channel(s): input {input_device}, output {output_device}"
                        ),
                        *error_lines,
                    ]
                )
                continue
            hostapi = hostapi_name(devices[speaker_zoom], hostapis)
            route_lines = [
                f"Auto-selected {hostapi} cable pair at {device_rate} Hz: input {input_device}, output {output_device}",
                "Selected cable route:",
                f"  Zoom speaker side:    {format_device_summary(speaker_zoom, devices, hostapis)}",
                f"  Bridge input side:    {format_device_summary(input_device, devices, hostapis)}",
                f"  Zoom microphone side: {format_device_summary(mic_zoom, devices, hostapis)}",
                f"  Bridge output side:   {format_device_summary(output_device, devices, hostapis)}",
            ]
            for line in route_lines:
                print(line)
            append_cable_route_log(route_lines)
            return input_device, output_device, device_rate

    raise SystemExit(
        "Found matching VB-Cable pairs, but none opened at their advertised default sample rates. "
        "Run with --list-devices and check whether another app is using the selected cable exclusively."
    )


def refresh_bridge_devices(args) -> None:
    input_device, output_device, device_rate = auto_select_bridge_devices(args)
    args.input_device = str(input_device)
    args.output_device = str(output_device)
    args.device_rate = device_rate


def print_sample_rate_hint(error: Exception, device: int, requested_rate: int) -> None:
    device_info = sd.query_devices(device)
    default_rate = int(device_info.get("default_samplerate", 0))
    hostapi = hostapi_name(device_info)
    print()
    print(f"Could not open device {device} at {requested_rate} Hz.")
    print(f"Device name: {device_info['name']}")
    print(f"Device host API: {hostapi}")
    print(f"Device default sample rate: {default_rate} Hz")
    for line in portaudio_error_lines(error):
        print(line)
    if default_rate and default_rate != requested_rate:
        print("Try again with the device default rate, for example:")
        print(f"  --device-rate {default_rate}")
    error_text = str(error)
    if "WDM-KS" in hostapi or "WdmSyncIoctl" in error_text:
        print("Windows reported a low-level WDM/KS driver error for this endpoint.")
        print("For VB-Audio cables on this bot PC, use Windows DirectSound; Windows WASAPI is blocked.")
    print("Also check that Zoom is using the Zoom-side cable devices and no app has the bridge-side cable open exclusively.")
    print()
    raise error


def resample_int16(audio: np.ndarray, source_rate: int, target_rate: int, gain: float = 1.0) -> np.ndarray:
    gain = max(0.0, float(gain))
    if source_rate == target_rate or len(audio) == 0:
        if gain == 1.0:
            return audio.astype(np.int16, copy=False)
        scaled = np.rint(audio.astype(np.float32) * gain)
        return np.clip(scaled, -32768, 32767).astype(np.int16)

    ratio = Fraction(target_rate, source_rate).limit_denominator()
    resampled = resample_poly(audio.astype(np.float32) * gain, ratio.numerator, ratio.denominator)
    return np.clip(np.rint(resampled), -32768, 32767).astype(np.int16)


def convert_channels_int16(audio: np.ndarray, source_channels: int, target_channels: int) -> np.ndarray:
    if source_channels == target_channels or len(audio) == 0:
        return audio.astype(np.int16, copy=False)

    frames = audio.reshape(-1, source_channels)
    if target_channels == 1:
        mono = np.mean(frames.astype(np.float32), axis=1)
        return np.clip(np.rint(mono), -32768, 32767).astype(np.int16)
    if source_channels == 1 and target_channels == 2:
        return np.repeat(frames, 2, axis=1).reshape(-1).astype(np.int16, copy=False)

    raise ValueError(f"Unsupported channel conversion: {source_channels} -> {target_channels}")


async def create_session(client_id: str, client_secret: str) -> dict:
    headers = {"ClientId": client_id, "ClientSecret": client_secret}
    payload = {"data": {"subscriber_count": 0, "publisher_can_subscribe": True}}
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(SESSION_URL, json=payload, headers=headers)
        response.raise_for_status()
        return response.json()


async def delete_session(client_id: str, client_secret: str, session_id: str) -> None:
    headers = {"ClientId": client_id, "ClientSecret": client_secret}
    url = f"{SESSIONS_URL}/{quote(session_id, safe='')}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.delete(url, headers=headers)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"Warning: could not delete Palabra session {session_id}: {exc}", flush=True)
        return

    print("Palabra session closed.", flush=True)


async def connect_websocket(ws_url: str, publisher_token: str):
    return await websockets.connect(
        f"{ws_url}?token={publisher_token}",
        ping_interval=10,
        ping_timeout=30,
        max_size=None,
    )


async def configure_translation(
    websocket,
    source_language: str,
    target_language: str,
    voice_id: Optional[str],
    api_rate: int,
    channels: int,
    segment_confirmation_silence_threshold: float,
    only_confirm_by_silence: bool,
    sentence_splitter_enabled: bool,
    translate_partial_transcriptions: bool,
    desired_queue_level_ms: int,
    max_queue_level_ms: int,
    auto_tempo: bool,
    min_tempo: float,
    max_tempo: float,
) -> None:
    speech_generation = {}
    if voice_id:
        speech_generation["voice_id"] = voice_id

    settings = {
        "message_type": "set_task",
        "data": {
            "input_stream": {
                "content_type": "audio",
                "source": {
                    "type": "ws",
                    "format": "pcm_s16le",
                    "sample_rate": api_rate,
                    "channels": channels,
                },
            },
            "output_stream": {
                "content_type": "audio",
                "target": {
                    "type": "ws",
                    "format": "pcm_s16le",
                    "sample_rate": PALABRA_OUTPUT_RATE,
                    "channels": PALABRA_OUTPUT_CHANNELS,
                },
            },
            "pipeline": {
                "preprocessing": {},
                # These segmentation settings are intentionally conservative.
                # Loosening them can make Palabra split or replace speech
                # segments unpredictably and may reintroduce missing audio.
                "transcription": {
                    "source_language": source_language,
                    "segment_confirmation_silence_threshold": segment_confirmation_silence_threshold,
                    "only_confirm_by_silence": only_confirm_by_silence,
                    "sentence_splitter": {
                        "enabled": sentence_splitter_enabled,
                    },
                },
                "translations": [
                    {
                        "target_language": target_language,
                        "translate_partial_transcriptions": translate_partial_transcriptions,
                        "speech_generation": speech_generation,
                    }
                ],
                "translation_queue_configs": {
                    "global": {
                        "desired_queue_level_ms": desired_queue_level_ms,
                        "max_queue_level_ms": max_queue_level_ms,
                        "auto_tempo": auto_tempo,
                        "min_tempo": min_tempo,
                        "max_tempo": max_tempo,
                    }
                },
                "allowed_message_types": [
                    "partial_transcription",
                    "validated_transcription",
                    "partial_translated_transcription",
                    "translated_transcription",
                ],
            },
        },
    }
    await websocket.send(json.dumps(settings))



def parse_palabra_message(raw_message) -> tuple[Optional[str], dict]:
    if isinstance(raw_message, bytes):
        raw_message = raw_message.decode("utf-8")
    message = json.loads(raw_message)
    data = message.get("data", {})
    if isinstance(data, str):
        data = json.loads(data)
    if not isinstance(data, dict):
        data = {}
    return message.get("message_type"), data


def format_palabra_message_details(data: dict) -> str:
    if not isinstance(data, dict):
        return str(data)
    code = data.get("code") or data.get("error_code") or data.get("type")
    desc = data.get("desc") or data.get("description") or data.get("message") or data.get("detail")
    parts = []
    if code:
        parts.append(str(code))
    if desc:
        parts.append(str(desc))
    return ": ".join(parts) if parts else json.dumps(data, ensure_ascii=False)


def current_task_is_ready(data: dict) -> bool:
    if not data:
        return False
    current_task = data.get("current_task", data)
    if not isinstance(current_task, dict):
        return True
    status = current_task.get("task_status") or current_task.get("status") or current_task.get("state")
    if isinstance(status, str) and status.lower() in {"error", "failed", "ended", "stopped"}:
        raise PalabraRuntimeError(format_palabra_message_details(current_task))
    return bool(current_task)

def is_palabra_task_not_ready_error(data: dict) -> bool:
    if not isinstance(data, dict):
        return False
    code = data.get("code") or data.get("error_code") or data.get("type")
    return isinstance(code, str) and code.upper() == "NOT_FOUND"


async def wait_for_current_task(websocket, poll_seconds: float, timeout_seconds: float) -> None:
    poll_seconds = min(2.0, max(0.25, float(poll_seconds)))
    deadline = time.monotonic() + max(poll_seconds, float(timeout_seconds))
    next_poll = 0.0

    while time.monotonic() < deadline:
        now = time.monotonic()
        if now >= next_poll:
            await websocket.send(
                json.dumps(
                    {
                        "message_type": "get_task",
                        "data": {"exclude_hidden": True},
                    }
                )
            )
            next_poll = now + poll_seconds

        try:
            raw_message = await asyncio.wait_for(websocket.recv(), timeout=poll_seconds)
        except asyncio.TimeoutError:
            continue

        msg_type, data = parse_palabra_message(raw_message)
        if msg_type == "current_task":
            if current_task_is_ready(data):
                print("Palabra task is ready.", flush=True)
                return
        elif msg_type == "warning":
            print(f"[palabra warning] {format_palabra_message_details(data)}", flush=True)
        elif msg_type == "error":
            if is_palabra_task_not_ready_error(data):
                continue
            raise PalabraRuntimeError(format_palabra_message_details(data))

    raise TimeoutError("Timed out waiting for Palabra current_task after set_task.")


async def end_palabra_task(websocket, eos_timeout_seconds: float) -> None:
    await websocket.send(
        json.dumps(
            {
                "message_type": "end_task",
                "data": {"eos_timeout": max(0.0, float(eos_timeout_seconds))},
            }
        )
    )


async def graceful_stop_palabra_task(
    websocket,
    receive_task: asyncio.Task,
    eos_timeout_seconds: float,
    shutdown_timeout_seconds: float,
) -> None:
    if receive_task.done():
        return
    try:
        await end_palabra_task(websocket, eos_timeout_seconds)
    except Exception as exc:
        print(f"Warning: could not send Palabra end_task: {exc}", flush=True)
        return

    try:
        await asyncio.wait_for(receive_task, timeout=max(0.5, float(shutdown_timeout_seconds)))
    except asyncio.TimeoutError:
        print("Palabra did not finish before graceful shutdown timeout; closing websocket.", flush=True)
    except PalabraRuntimeError:
        raise
    except Exception as exc:
        print(f"Warning: Palabra receive loop ended during shutdown: {exc}", flush=True)


def validate_runtime_args(args) -> None:
    if args.api_rate < 16000 or args.api_rate > 48000:
        raise SystemExit("audio.api_rate must be between 16000 and 48000 for Palabra websocket input.")
    if args.chunk_ms <= 0:
        raise SystemExit("bridge.chunk_ms must be greater than 0.")
    raw_chunk_bytes = int(args.api_rate * args.chunk_ms / 1000) * args.channels * 2
    if raw_chunk_bytes < 1024:
        raise SystemExit(
            "bridge.chunk_ms is too small for Palabra websocket audio payloads; "
            f"current raw chunk is {raw_chunk_bytes} bytes, minimum is 1024."
        )
    if raw_chunk_bytes > 512 * 1024:
        raise SystemExit(
            "bridge.chunk_ms is too large for Palabra websocket audio payloads; "
            f"current raw chunk is {raw_chunk_bytes} bytes, maximum is 512 KiB."
        )
    if args.playback_buffer_ms < 0:
        raise SystemExit("bridge.playback_buffer_ms must be zero or greater.")
    if args.phrase_start_buffer_ms < 0:
        raise SystemExit("bridge.phrase_start_buffer_ms must be zero or greater.")
    if args.playback_tempo < 1.0:
        raise SystemExit("bridge.playback_tempo must be 1.0 or greater.")
    if args.playback_max_tempo < 1.0:
        raise SystemExit("bridge.playback_max_tempo must be 1.0 or greater.")
    if args.playback_max_tempo < args.playback_tempo:
        raise SystemExit("bridge.playback_max_tempo must be greater than or equal to bridge.playback_tempo.")
    if args.playback_tempo_algorithm not in PLAYBACK_TEMPO_ALGORITHMS:
        allowed = ", ".join(sorted(PLAYBACK_TEMPO_ALGORITHMS))
        raise SystemExit(f"bridge.playback_tempo_algorithm must be one of: {allowed}.")
    if args.task_ready_timeout <= 0:
        raise SystemExit("bridge.task_ready_timeout_seconds must be greater than 0.")
    if args.task_poll_seconds <= 0:
        raise SystemExit("bridge.task_poll_seconds must be greater than 0.")
    if args.playback_drain_timeout < 0:
        raise SystemExit("bridge.playback_drain_timeout_seconds must be zero or greater.")


def load_config(config_path: Path = CONFIG_PATH) -> dict:
    if not config_path.exists():
        return {}

    with config_path.open("rb") as fh:
        data = tomllib.load(fh)

    if not isinstance(data, dict):
        raise SystemExit(f"{config_path} must contain a TOML table.")

    return data


def config_section(config: dict, section_name: str) -> dict:
    section = config.get(section_name, {})
    if section is not None and not isinstance(section, dict):
        raise SystemExit(f"The [{section_name}] section in config.toml must be a table.")
    return section or {}


def config_string(section: dict, key: str, default: str, dotted_name: str) -> str:
    value = section.get(key, default)
    if not isinstance(value, str):
        raise SystemExit(f"{dotted_name} in config.toml must be a string.")
    return value


def config_device_selector(
    section: dict,
    key: str,
    default: str,
    dotted_name: str,
    *,
    aliases: tuple[str, ...] = (),
) -> str:
    value = section.get(key)
    if value is None:
        for alias in aliases:
            value = section.get(alias)
            if value is not None:
                break
    if value is None:
        value = default
    if not isinstance(value, (int, str)):
        raise SystemExit(f"{dotted_name} in config.toml must be a string or integer.")
    return str(value)


def config_optional_device_selector(section: dict, key: str, dotted_name: str) -> Optional[str]:
    value = section.get(key)
    if value is not None and not isinstance(value, (int, str)):
        raise SystemExit(f"{dotted_name} in config.toml must be a string or integer.")
    return str(value) if value is not None else None


def config_optional_string(section: dict, key: str, dotted_name: str) -> Optional[str]:
    value = section.get(key)
    if value is not None and not isinstance(value, str):
        raise SystemExit(f"{dotted_name} in config.toml must be a string.")
    return value


def config_string_list(
    section: dict,
    key: str,
    default: tuple[str, ...],
    dotted_name: str,
) -> list[str]:
    value = section.get(key, list(default))
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SystemExit(f"{dotted_name} in config.toml must be a list of strings.")
    if not value:
        raise SystemExit(f"{dotted_name} in config.toml must contain at least one backend name.")
    return value


def remove_blocked_hostapis(hostapis: list[str]) -> list[str]:
    allowed = [hostapi for hostapi in hostapis if hostapi not in BLOCKED_HOSTAPIS]
    blocked = [hostapi for hostapi in hostapis if hostapi in BLOCKED_HOSTAPIS]
    if blocked:
        blocked_names = ", ".join(dict.fromkeys(blocked))
        print(f"Ignoring blocked audio backend(s): {blocked_names}", flush=True)
    if not allowed:
        blocked_names = ", ".join(sorted(BLOCKED_HOSTAPIS))
        raise SystemExit(f"No usable audio backends remain after blocking: {blocked_names}.")
    return allowed


def config_int(
    section: dict,
    key: str,
    default: int,
    dotted_name: str,
    *,
    choices: Optional[tuple[int, ...]] = None,
) -> int:
    value = section.get(key, default)
    if not isinstance(value, int):
        raise SystemExit(f"{dotted_name} in config.toml must be an integer.")
    if choices and value not in choices:
        valid = ", ".join(str(choice) for choice in choices)
        raise SystemExit(f"{dotted_name} in config.toml must be one of: {valid}.")
    return value


def config_float(section: dict, key: str, default: float, dotted_name: str) -> float:
    value = section.get(key, default)
    if not isinstance(value, (int, float)):
        raise SystemExit(f"{dotted_name} in config.toml must be a number.")
    return float(value)


def config_bool(section: dict, key: str, default: bool, dotted_name: str) -> bool:
    value = section.get(key, default)
    if not isinstance(value, bool):
        raise SystemExit(f"{dotted_name} in config.toml must be true or false.")
    return value


def config_bool_with_aliases(
    section: dict,
    key: str,
    default: bool,
    dotted_name: str,
    *,
    aliases: tuple[str, ...] = (),
) -> bool:
    value = section.get(key)
    if value is None:
        for alias in aliases:
            value = section.get(alias)
            if value is not None:
                break
    if value is None:
        value = default
    if not isinstance(value, bool):
        raise SystemExit(f"{dotted_name} in config.toml must be true or false.")
    return value


def resolve_translation_settings(args) -> tuple[str, str, Optional[str]]:
    source_language = args.source_language
    target_language = args.target_language
    voice_id = args.voice_id

    return source_language, target_language, voice_id


def first_text_value(data: dict, *container_keys: str) -> str:
    candidates = [data]
    for key in container_keys:
        value = data.get(key)
        if isinstance(value, dict):
            candidates.append(value)
        elif isinstance(value, list):
            candidates.extend(item for item in value if isinstance(item, dict))

    for candidate in candidates:
        for key in ("text", "transcription", "translation"):
            value = candidate.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def compact_palabra_ids(data: dict) -> dict:
    return {
        key: data.get(key)
        for key in (
            "transcription_id",
            "translation_part_id",
            "language",
            "last_chunk",
        )
        if key in data
    }


def compact_payload_shape(data: dict) -> dict:
    shape = {}
    for key, value in data.items():
        if isinstance(value, dict):
            shape[key] = {"type": "dict", "keys": sorted(value.keys())}
        elif isinstance(value, list):
            shape[key] = {"type": "list", "length": len(value)}
        elif isinstance(value, str):
            shape[key] = {"type": "str", "length": len(value)}
        else:
            shape[key] = {"type": type(value).__name__, "value": value}
    return shape


def audio_group_key(data: dict) -> Optional[tuple[str, str, str]]:
    transcription_id = data.get("transcription_id")
    translation_part_id = data.get("translation_part_id")
    language = data.get("language")
    if transcription_id is None or translation_part_id is None or language is None:
        return None
    return str(transcription_id), str(translation_part_id), str(language)


class AudioBridge:
    def __init__(
        self,
        input_device: int,
        output_device: int,
        device_rate: int,
        api_rate: int,
        chunk_ms: int,
        api_channels: int,
        device_channels: int,
        input_block_ms: int,
        playback_buffer_ms: int,
        phrase_start_buffer_ms: int,
        playback_tempo: float,
        playback_max_tempo: float,
        playback_tempo_algorithm: str,
        playback_fade_ms: int,
        idle_noise_amplitude: int,
        output_gain: float,
        raw_palabra_playback: bool,
    ) -> None:
        self.input_device = input_device
        self.output_device = output_device
        self.device_rate = device_rate
        self.api_rate = api_rate
        self.output_api_rate = PALABRA_OUTPUT_RATE
        self.chunk_samples = int(api_rate * chunk_ms / 1000)
        self.api_channels = api_channels
        self.output_api_channels = PALABRA_OUTPUT_CHANNELS
        self.device_channels = device_channels
        self.output_gain = max(0.0, float(output_gain))
        self.raw_palabra_playback = bool(raw_palabra_playback)
        self.input_blocksize = stream_blocksize(device_rate, input_block_ms)
        self.playback_base_preroll_samples = int(device_rate * playback_buffer_ms / 1000) * device_channels
        self.playback_max_preroll_samples = (
            int(device_rate * max(playback_buffer_ms, DEFAULT_PLAYBACK_MAX_BUFFER_MS) / 1000)
            * device_channels
        )
        self.playback_preroll_samples = self.playback_base_preroll_samples
        self.phrase_start_preroll_samples = (
            int(device_rate * max(playback_buffer_ms, phrase_start_buffer_ms) / 1000) * device_channels
        )
        self.playback_preroll_step_samples = int(device_rate * PLAYBACK_BUFFER_STEP_MS / 1000) * device_channels
        self.playback_active_underrun_step_samples = (
            int(device_rate * PLAYBACK_ACTIVE_UNDERRUN_STEP_MS / 1000) * device_channels
        )
        self.playback_start_lookahead_samples = (
            int(device_rate * PLAYBACK_START_LOOKAHEAD_MS / 1000) * device_channels
        )
        self.playback_catchup_target_samples = (
            int(device_rate * PLAYBACK_CATCHUP_TARGET_MS / 1000) * device_channels
        )
        self.playback_catchup_full_backlog_samples = (
            int(device_rate * PLAYBACK_CATCHUP_FULL_BACKLOG_MS / 1000) * device_channels
        )
        self.playback_tempo = max(1.0, float(playback_tempo))
        self.playback_max_tempo = max(1.0, float(playback_max_tempo))
        self.playback_tempo_algorithm = playback_tempo_algorithm
        self.playback_fade_frames = max(1, int(device_rate * playback_fade_ms / 1000))
        noise_frames = max(device_rate, self.playback_fade_frames)
        noise_rng = np.random.default_rng(1)
        idle_noise_amplitude = max(0, int(idle_noise_amplitude))
        self.idle_noise = noise_rng.integers(
            -idle_noise_amplitude,
            idle_noise_amplitude + 1,
            size=noise_frames * device_channels,
            dtype=np.int16,
        )
        self.idle_noise_offset = 0
        self.capture_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=300)
        self.playback_queue: queue.Queue[PlaybackChunk] = queue.Queue()
        self.playback_buffer = np.array([], dtype=np.int16)
        self.playback_segment_end_offsets: list[int] = []
        self.pending_playback_phrase_key: Optional[tuple[str, str, str]] = None
        self.pending_playback_phrase_chunks: list[np.ndarray] = []
        self.pending_playback_phrase_samples = 0
        self.pending_playback_phrase_released = False
        self.playback_started = False
        self.last_playback_underrun = time.monotonic()
        self.stop_event = threading.Event()
        self.capture_dropped = 0
        self.playback_dropped = 0
        self.playback_status_count = 0
        self.output_audio_chunks = 0
        self.output_audio_groups: dict[tuple[str, str, str], dict[str, object]] = {}
        self.last_output_audio_group_key: Optional[tuple[str, str, str]] = None
        self.dumped_palabra_message_types: set[str] = set()
        self.api_input_recorder: Optional[WavDebugRecorder] = None
        self.api_output_recorder: Optional[WavDebugRecorder] = None
        self.device_output_recorder: Optional[WavDebugRecorder] = None
        self.callback_output_recorder: Optional[AsyncWavDebugRecorder] = None
        self.debug_text_logger: Optional[DebugTextLogger] = None
        self.first_source_transcription_time: Optional[float] = None
        self.last_no_output_warning = 0.0
        self.last_capture_drop_notice = 0.0
        self.last_no_capture_notice = 0.0
        self.last_playback_drop_notice = 0.0
        self.last_playback_hold_notice = 0.0
        self.first_input_audio_sent = False
        self.playback_partial_holds = 0
        self.stop_reason = ""

    def request_stop(self, reason: str) -> None:
        if not self.stop_reason:
            self.stop_reason = reason
        self.stop_event.set()

    def _playback_block_is_quiet(self, audio: np.ndarray) -> bool:
        if len(audio) == 0:
            return True
        frames = audio.reshape(-1, self.device_channels).astype(np.float32)
        mono = np.mean(frames, axis=1)
        rms = float(np.sqrt(np.mean(mono * mono))) if len(mono) else 0.0
        return rms <= PLAYBACK_SILENCE_RMS

    def _increase_playback_preroll(self, step_samples: Optional[int] = None) -> None:
        if step_samples is None:
            step_samples = self.playback_preroll_step_samples
        self.playback_preroll_samples = min(
            self.playback_max_preroll_samples,
            self.playback_preroll_samples + step_samples,
        )
        self.last_playback_underrun = time.monotonic()

    def _relax_playback_preroll(self) -> None:
        if self.playback_preroll_samples <= self.playback_base_preroll_samples:
            return
        if time.monotonic() - self.last_playback_underrun < PLAYBACK_BUFFER_RELAX_SECONDS:
            return
        self.playback_preroll_samples = max(
            self.playback_base_preroll_samples,
            self.playback_preroll_samples - self.playback_preroll_step_samples,
        )

    def _consume_playback_buffer(self, sample_count: int) -> None:
        self.playback_buffer = self.playback_buffer[sample_count:]
        self.playback_segment_end_offsets = [
            offset - sample_count for offset in self.playback_segment_end_offsets if offset > sample_count
        ]

    def _next_segment_end_within(self, sample_count: int) -> Optional[int]:
        for offset in self.playback_segment_end_offsets:
            if 0 < offset <= sample_count:
                return offset
        return None

    def _first_segment_end_offset(self) -> Optional[int]:
        for offset in self.playback_segment_end_offsets:
            if offset > 0:
                return offset
        return None

    def _queue_playback_chunk(self, audio: np.ndarray, segment_end: bool) -> None:
        if len(audio) == 0 and not segment_end:
            return
        self.playback_queue.put_nowait(PlaybackChunk(audio, segment_end=segment_end))

    def _reset_pending_playback_phrase(self) -> None:
        self.pending_playback_phrase_key = None
        self.pending_playback_phrase_chunks = []
        self.pending_playback_phrase_samples = 0
        self.pending_playback_phrase_released = False

    def _release_pending_playback_phrase(self, segment_end: bool) -> None:
        if not self.pending_playback_phrase_chunks:
            if segment_end:
                self._reset_pending_playback_phrase()
            return

        if len(self.pending_playback_phrase_chunks) == 1:
            audio = self.pending_playback_phrase_chunks[0]
        else:
            audio = np.concatenate(self.pending_playback_phrase_chunks)
        self._queue_playback_chunk(audio, segment_end=segment_end)
        self.pending_playback_phrase_chunks = []
        self.pending_playback_phrase_samples = 0

        if segment_end:
            self._reset_pending_playback_phrase()
        else:
            self.pending_playback_phrase_released = True

    def _enqueue_phrase_playback(
        self,
        audio: np.ndarray,
        segment_end: bool,
        phrase_key: Optional[tuple[str, str, str]],
    ) -> None:
        if phrase_key is None:
            self._queue_playback_chunk(audio, segment_end=segment_end)
            return

        if self.pending_playback_phrase_key != phrase_key:
            self._release_pending_playback_phrase(segment_end=False)
            self.pending_playback_phrase_key = phrase_key

        if self.pending_playback_phrase_released:
            self._queue_playback_chunk(audio, segment_end=segment_end)
            if segment_end:
                self._reset_pending_playback_phrase()
            return

        self.pending_playback_phrase_chunks.append(audio)
        self.pending_playback_phrase_samples += len(audio)
        if segment_end or self.pending_playback_phrase_samples >= self.phrase_start_preroll_samples:
            self._release_pending_playback_phrase(segment_end=segment_end)

    def _idle_audio(self, sample_count: int) -> np.ndarray:
        if sample_count <= 0:
            return np.array([], dtype=np.int16)
        if sample_count > len(self.idle_noise):
            repeats = int(np.ceil(sample_count / len(self.idle_noise)))
            return np.tile(self.idle_noise, repeats)[:sample_count].copy()
        start = self.idle_noise_offset
        end = start + sample_count
        if end <= len(self.idle_noise):
            audio = self.idle_noise[start:end].copy()
        else:
            audio = np.concatenate((self.idle_noise[start:], self.idle_noise[: end % len(self.idle_noise)]))
        self.idle_noise_offset = end % len(self.idle_noise)
        return audio

    def _fill_idle_audio(self, outdata, frames: int) -> None:
        outdata[:] = self._idle_audio(frames * self.device_channels).reshape(frames, self.device_channels)

    def _fade_in(self, audio: np.ndarray) -> None:
        frames = min(self.playback_fade_frames, len(audio) // self.device_channels)
        if frames <= 1:
            return
        shaped = audio.reshape(-1, self.device_channels)
        ramp = np.linspace(0.0, 1.0, frames, dtype=np.float32)[:, None]
        shaped[:frames] = np.rint(shaped[:frames].astype(np.float32) * ramp).astype(np.int16)

    def _fade_out(self, audio: np.ndarray, active_samples: int) -> None:
        active_frames = min(active_samples // self.device_channels, len(audio) // self.device_channels)
        frames = min(self.playback_fade_frames, active_frames)
        if frames <= 1:
            return
        shaped = audio.reshape(-1, self.device_channels)
        start = active_frames - frames
        ramp = np.linspace(1.0, 0.0, frames, dtype=np.float32)[:, None]
        shaped[start:active_frames] = np.rint(
            shaped[start:active_frames].astype(np.float32) * ramp
        ).astype(np.int16)

    def _playback_catchup_tempo(self) -> float:
        target_samples = max(self.playback_preroll_samples, self.playback_catchup_target_samples)
        backlog_samples = max(0, len(self.playback_buffer) - target_samples)
        if backlog_samples <= 0:
            return self.playback_tempo
        ramp_samples = max(self.device_channels, self.playback_catchup_full_backlog_samples)
        catchup = min(1.0, backlog_samples / float(ramp_samples))
        return self.playback_tempo + ((self.playback_max_tempo - self.playback_tempo) * catchup)

    def _speed_adjust_playback_slice(self, audio: np.ndarray, output_samples: int) -> np.ndarray:
        if len(audio) == output_samples:
            return audio.copy()
        input_frames = len(audio) // self.device_channels
        output_frames = output_samples // self.device_channels
        if input_frames <= 0 or output_frames <= 0:
            return np.zeros(output_samples, dtype=np.int16)
        if self.playback_tempo_algorithm == "rubberband" and input_frames > output_frames:
            return self._speed_adjust_playback_slice_rubberband(audio, output_samples)
        return self._speed_adjust_playback_slice_resample(audio, output_samples)

    def _speed_adjust_playback_slice_rubberband(self, audio: np.ndarray, output_samples: int) -> np.ndarray:
        input_frames = len(audio) // self.device_channels
        output_frames = output_samples // self.device_channels
        if input_frames <= output_frames:
            return self._speed_adjust_playback_slice_resample(audio, output_samples)
        tempo = input_frames / float(output_frames)
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            return self._speed_adjust_playback_slice_resample(audio, output_samples)

        filter_spec = (
            f"rubberband=tempo={tempo:.6f}:pitch=1:"
            "transients=smooth:detector=soft:phase=laminar:"
            "window=short:smoothing=on:formant=preserved:"
            "pitchq=quality:channels=together"
        )
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "s16le",
            "-ar",
            str(self.device_rate),
            "-ac",
            str(self.device_channels),
            "-i",
            "pipe:0",
            "-af",
            filter_spec,
            "-f",
            "s16le",
            "pipe:1",
        ]
        try:
            completed = subprocess.run(
                command,
                input=audio.tobytes(),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=2.0,
                check=True,
            )
            adjusted = np.frombuffer(completed.stdout, dtype=np.int16)
        except Exception:
            return self._speed_adjust_playback_slice_resample(audio, output_samples)

        if len(adjusted) > output_samples:
            adjusted = adjusted[:output_samples]
        elif len(adjusted) < output_samples:
            adjusted = np.pad(adjusted, (0, output_samples - len(adjusted)))
        input_peak = int(np.max(np.abs(audio.astype(np.int32)))) if len(audio) else 0
        output_peak = int(np.max(np.abs(adjusted.astype(np.int32)))) if len(adjusted) else 0
        if input_peak > 0 and output_peak > input_peak:
            adjusted = np.rint(adjusted.astype(np.float32) * (input_peak / output_peak)).astype(np.int16)
        return adjusted.astype(np.int16, copy=False)

    def _speed_adjust_playback_slice_resample(self, audio: np.ndarray, output_samples: int) -> np.ndarray:
        input_frames = len(audio) // self.device_channels
        output_frames = output_samples // self.device_channels
        if input_frames <= 0 or output_frames <= 0:
            return np.zeros(output_samples, dtype=np.int16)
        shaped = audio.reshape(input_frames, self.device_channels).astype(np.float32)
        ratio = Fraction(output_frames, input_frames).limit_denominator(1000)
        adjusted = resample_poly(shaped, ratio.numerator, ratio.denominator, axis=0)
        if len(adjusted) > output_frames:
            adjusted = adjusted[:output_frames]
        elif len(adjusted) < output_frames:
            pad = np.zeros((output_frames - len(adjusted), self.device_channels), dtype=np.float32)
            adjusted = np.vstack((adjusted, pad))
        return np.clip(np.rint(adjusted), -32768, 32767).astype(np.int16).reshape(-1)

    def playback_pending_samples(self) -> int:
        queued_samples = 0
        with self.playback_queue.mutex:
            queued_samples = sum(len(chunk.audio) for chunk in self.playback_queue.queue)
        pending_samples = sum(len(chunk) for chunk in self.pending_playback_phrase_chunks)
        return len(self.playback_buffer) + queued_samples + pending_samples

    def playback_is_drained(self) -> bool:
        return self.playback_pending_samples() == 0

    def drain_capture_queue(self) -> int:
        drained = 0
        while True:
            try:
                self.capture_queue.get_nowait()
            except queue.Empty:
                return drained
            drained += 1

    def validate_devices(self) -> None:
        print("Checking audio devices...")

        def input_callback(indata, frames, time_info, status):
            pass

        def output_callback(outdata, frames, time_info, status):
            outdata.fill(0)

        try:
            with sd.RawInputStream(
                device=self.input_device,
                samplerate=self.device_rate,
                channels=self.device_channels,
                dtype="int16",
                blocksize=self.input_blocksize,
                callback=input_callback,
            ):
                pass
        except sd.PortAudioError as exc:
            print_sample_rate_hint(exc, self.input_device, self.device_rate)

        try:
            with sd.OutputStream(
                device=self.output_device,
                samplerate=self.device_rate,
                channels=self.device_channels,
                dtype="int16",
                blocksize=self.input_blocksize,
                callback=output_callback,
            ):
                pass
        except sd.PortAudioError as exc:
            print_sample_rate_hint(exc, self.output_device, self.device_rate)

        print("Audio devices opened successfully.")

    def start_capture(self, source_language: str) -> threading.Thread:
        startup_results = queue.Queue(maxsize=1)

        def callback(indata, frames, time_info, status):
            if status:
                print(f"[capture] {status}", flush=True)
            samples = np.frombuffer(indata, dtype=np.int16).copy()
            samples = convert_channels_int16(samples, self.device_channels, self.api_channels)
            try:
                self.capture_queue.put_nowait(samples)
            except queue.Full:
                self.capture_dropped += 1
                with contextlib.suppress(queue.Empty):
                    self.capture_queue.get_nowait()
                with contextlib.suppress(queue.Full):
                    self.capture_queue.put_nowait(samples)
                now = time.monotonic()
                if now - self.last_capture_drop_notice >= 5:
                    print(
                        f"[capture] input is behind; dropped {self.capture_dropped} stale audio blocks",
                        flush=True,
                    )
                    self.last_capture_drop_notice = now

        def run():
            try:
                with sd.RawInputStream(
                    device=self.input_device,
                    samplerate=self.device_rate,
                    channels=self.device_channels,
                    dtype="int16",
                    blocksize=self.input_blocksize,
                    callback=callback,
                ):
                    startup_results.put(None)
                    print(f"Capturing Zoom {language_label(source_language)} audio.")
                    while not self.stop_event.is_set():
                        time.sleep(0.05)
            except sd.PortAudioError as exc:
                with contextlib.suppress(queue.Full):
                    startup_results.put(exc)
                self.stop_event.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            startup_error = startup_results.get(timeout=5)
        except queue.Empty as exc:
            self.stop_event.set()
            raise TimeoutError("Timed out while opening the input audio stream.") from exc
        if startup_error is not None:
            thread.join(timeout=2)
            print_sample_rate_hint(startup_error, self.input_device, self.device_rate)
        return thread

    def start_playback(self, target_language: str) -> sd.OutputStream:
        def callback(outdata, frames, time_info, status):
            if status:
                self.playback_status_count += 1

            needed = frames * self.device_channels
            target_buffer_samples = max(needed, self.playback_preroll_samples)
            was_playing = self.playback_started

            chunks = [self.playback_buffer]
            buffered_samples = len(self.playback_buffer)
            while buffered_samples < target_buffer_samples:
                try:
                    next_chunk = self.playback_queue.get_nowait()
                except queue.Empty:
                    break
                chunk_audio = next_chunk.audio
                chunks.append(chunk_audio)
                buffered_samples += len(chunk_audio)
                if next_chunk.segment_end:
                    self.playback_segment_end_offsets.append(buffered_samples)

            if not self.playback_started and self._first_segment_end_offset() is None:
                start_lookahead_samples = max(target_buffer_samples, self.playback_start_lookahead_samples)
                while buffered_samples < start_lookahead_samples:
                    try:
                        next_chunk = self.playback_queue.get_nowait()
                    except queue.Empty:
                        break
                    chunk_audio = next_chunk.audio
                    chunks.append(chunk_audio)
                    buffered_samples += len(chunk_audio)
                    if next_chunk.segment_end:
                        self.playback_segment_end_offsets.append(buffered_samples)
                        break

            if buffered_samples != len(self.playback_buffer):
                self.playback_buffer = np.concatenate(chunks)

            if self.raw_palabra_playback:
                if len(self.playback_buffer) >= needed:
                    output_audio = self.playback_buffer[:needed].copy()
                    outdata[:] = output_audio.reshape(frames, self.device_channels)
                    self._consume_playback_buffer(needed)
                    self.playback_started = True
                elif len(self.playback_buffer) > 0:
                    output_audio = self._idle_audio(needed)
                    available_samples = len(self.playback_buffer)
                    output_audio[:available_samples] = self.playback_buffer
                    outdata[:] = output_audio.reshape(frames, self.device_channels)
                    self._consume_playback_buffer(available_samples)
                    self.playback_started = False
                else:
                    self.playback_started = False
                    self._fill_idle_audio(outdata, frames)

                if self.callback_output_recorder is not None:
                    self.callback_output_recorder.write(outdata.reshape(-1))
                return

            first_segment_end = self._first_segment_end_offset()
            if (
                not self.playback_started
                and len(self.playback_buffer) < self.playback_preroll_samples
                and first_segment_end is None
            ):
                self._fill_idle_audio(outdata, frames)
            elif len(self.playback_buffer) >= needed:
                self.playback_started = True
                tempo = self._playback_catchup_tempo()
                consume_samples = needed
                if tempo > 1.0:
                    consume_frames = min(
                        len(self.playback_buffer) // self.device_channels,
                        max(frames + 1, int(round(frames * tempo))),
                    )
                    consume_samples = consume_frames * self.device_channels
                output_audio = self._speed_adjust_playback_slice(
                    self.playback_buffer[:consume_samples],
                    needed,
                )
                if not was_playing:
                    self._fade_in(output_audio)
                outdata[:] = output_audio.reshape(frames, self.device_channels)
                self._consume_playback_buffer(consume_samples)
                self._relax_playback_preroll()
            elif first_segment_end is not None and first_segment_end <= len(self.playback_buffer):
                output_audio = self._idle_audio(needed)
                output_audio[:first_segment_end] = self.playback_buffer[:first_segment_end]
                if not was_playing:
                    self._fade_in(output_audio[:first_segment_end])
                self._fade_out(output_audio, first_segment_end)
                outdata[:] = output_audio.reshape(frames, self.device_channels)
                self._consume_playback_buffer(first_segment_end)
                self.playback_started = False
            elif was_playing and len(self.playback_buffer) > 0:
                self.playback_partial_holds += 1
                self.playback_started = True
                self.last_playback_underrun = time.monotonic()
                self._fill_idle_audio(outdata, frames)
            else:
                if was_playing:
                    self.playback_started = True
                    self.last_playback_underrun = time.monotonic()
                else:
                    self._increase_playback_preroll()
                    self.playback_started = False
                self._fill_idle_audio(outdata, frames)

            if self.callback_output_recorder is not None:
                self.callback_output_recorder.write(outdata.reshape(-1))

        stream = sd.OutputStream(
            device=self.output_device,
            samplerate=self.device_rate,
            channels=self.device_channels,
            dtype="int16",
            blocksize=self.input_blocksize,
            latency="high",
            callback=callback,
        )
        try:
            stream.start()
        except sd.PortAudioError as exc:
            with contextlib.suppress(Exception):
                stream.close()
            print_sample_rate_hint(exc, self.output_device, self.device_rate)
        print(f"Playing {language_label(target_language)} audio into Zoom microphone cable.")
        return stream

    async def send_audio(self, websocket) -> None:
        api_buffer = np.array([], dtype=np.int16)
        send_interval_sec = self.chunk_samples / float(self.api_rate)
        next_send_time = time.monotonic()
        while not self.stop_event.is_set():
            try:
                device_audio = self.capture_queue.get(timeout=0.1)
            except queue.Empty:
                now = time.monotonic()
                if now - self.last_no_capture_notice >= 10:
                    print(
                        "[capture] waiting for Zoom audio from the selected speaker cable...",
                        flush=True,
                    )
                    self.last_no_capture_notice = now
                await asyncio.sleep(0.001)
                continue

            api_audio = resample_int16(device_audio, self.device_rate, self.api_rate)
            api_buffer = np.concatenate([api_buffer, api_audio])

            while len(api_buffer) >= self.chunk_samples * self.api_channels:
                chunk = api_buffer[: self.chunk_samples * self.api_channels]
                api_buffer = api_buffer[self.chunk_samples * self.api_channels :]
                now = time.monotonic()
                if next_send_time > now:
                    await asyncio.sleep(next_send_time - now)
                if self.api_input_recorder is not None:
                    self.api_input_recorder.write(chunk)
                await websocket.send(
                    json.dumps(
                        {
                            "message_type": "input_audio_data",
                            "data": {
                                "data": base64.b64encode(chunk.tobytes()).decode("utf-8")
                            },
                        }
                    )
                )
                if not self.first_input_audio_sent:
                    print("[capture] sending Zoom audio to Palabra.", flush=True)
                    self.first_input_audio_sent = True
                next_send_time = max(next_send_time + send_interval_sec, time.monotonic())

    def _dump_palabra_message_shape(self, msg_type: str, data: dict) -> None:
        if msg_type in self.dumped_palabra_message_types:
            return
        self.dumped_palabra_message_types.add(msg_type)
        safe_data = dict(data)
        if "data" in safe_data and isinstance(safe_data["data"], str):
            safe_data["data"] = f"<base64 audio, {len(safe_data['data'])} chars>"
        transcription = safe_data.get("transcription")
        if isinstance(transcription, dict) and isinstance(transcription.get("data"), str):
            transcription = dict(transcription)
            transcription["data"] = f"<base64 audio, {len(transcription['data'])} chars>"
            safe_data["transcription"] = transcription
        print(
            f"[palabra message] {msg_type}: keys={sorted(data.keys())} sample={safe_data}",
            flush=True,
        )

    def _log_skipped_output_audio(self, reason: str, data: dict, transcription: object) -> None:
        if self.debug_text_logger is not None:
            fields = {
                "reason": reason,
                "payload_shape": compact_payload_shape(data),
            }
            if isinstance(transcription, dict):
                fields.update(compact_palabra_ids(transcription))
                fields["transcription_shape"] = compact_payload_shape(transcription)
            else:
                fields["transcription_type"] = type(transcription).__name__
            self.debug_text_logger.write("skipped_output_audio_data", **fields)
        print(f"[diagnostics] skipped output_audio_data: {reason}", flush=True)

    def _update_output_audio_group(
        self,
        group_key: Optional[tuple[str, str, str]],
        transcription: dict,
        duration_ms: float,
    ) -> None:
        if group_key is None:
            return
        group = self.output_audio_groups.setdefault(
            group_key,
            {
                "chunks": 0,
                "duration_ms": 0.0,
                "complete": False,
                "warned_incomplete": False,
            },
        )
        if (
            self.last_output_audio_group_key is not None
            and self.last_output_audio_group_key != group_key
        ):
            self._log_incomplete_output_audio_group(
                self.last_output_audio_group_key,
                self.output_audio_groups[self.last_output_audio_group_key],
                "next_output_group",
            )
        self.last_output_audio_group_key = group_key
        group["chunks"] = int(group["chunks"]) + 1
        group["duration_ms"] = float(group["duration_ms"]) + duration_ms
        group["complete"] = transcription.get("last_chunk") is True

    def _log_incomplete_output_audio_group(
        self,
        key: tuple[str, str, str],
        group: dict[str, object],
        trigger: str,
    ) -> None:
        if group.get("complete") is True or group.get("warned_incomplete") is True:
            return
        group["warned_incomplete"] = True
        fields = {
            "trigger": trigger,
            "transcription_id": key[0],
            "translation_part_id": key[1],
            "language": key[2],
            "chunks": group.get("chunks"),
            "duration_ms": round(float(group.get("duration_ms", 0.0)), 1),
        }
        if self.debug_text_logger is not None:
            self.debug_text_logger.write("incomplete_output_audio_group", **fields)
        print(
            "[diagnostics] output audio group ended without last_chunk=true: "
            f"{key[0]} / part {key[1]} / {key[2]}",
            flush=True,
        )

    def _log_incomplete_output_audio_groups(self, trigger: str) -> None:
        for key, group in self.output_audio_groups.items():
            self._log_incomplete_output_audio_group(key, group, trigger)

    async def receive_audio(
        self,
        websocket,
        source_language: str,
        target_language: str,
        dump_messages: bool = False,
    ) -> None:
        async for raw_message in websocket:
            msg_type, data = parse_palabra_message(raw_message)
            if dump_messages and isinstance(msg_type, str):
                self._dump_palabra_message_shape(msg_type, data)

            if msg_type == "output_audio_data":
                transcription_payload = data.get("transcription")
                if isinstance(transcription_payload, dict):
                    transcription = transcription_payload
                elif "data" in data:
                    transcription = data
                    if transcription_payload is not None and self.debug_text_logger is not None:
                        self.debug_text_logger.write(
                            "output_audio_top_level_fallback",
                            transcription_type=type(transcription_payload).__name__,
                            payload_shape=compact_payload_shape(data),
                        )
                else:
                    self._log_skipped_output_audio(
                        "no transcription object or top-level audio data",
                        data,
                        transcription_payload,
                    )
                    continue
                encoded_audio = transcription.get("data")
                if not isinstance(encoded_audio, str):
                    self._log_skipped_output_audio("missing string audio data", data, transcription)
                    continue
                segment_end = transcription.get("last_chunk") is True
                phrase_key = audio_group_key(transcription)
                try:
                    api_audio = np.frombuffer(base64.b64decode(encoded_audio), dtype=np.int16)
                except Exception:
                    self._log_skipped_output_audio("invalid base64 audio data", data, transcription)
                    continue
                duration_ms = len(api_audio) / max(1, self.output_api_rate * self.output_api_channels) * 1000
                self._update_output_audio_group(phrase_key, transcription, duration_ms)
                if self.api_output_recorder is not None:
                    self.api_output_recorder.write(api_audio)
                if self.debug_text_logger is not None:
                    self.debug_text_logger.write(
                        "output_audio_chunk",
                        duration_ms=round(duration_ms, 1),
                        samples=len(api_audio),
                        text=first_text_value(transcription, "translation", "translations", "transcription"),
                        **compact_palabra_ids(transcription),
                    )
                device_audio = resample_int16(api_audio, self.output_api_rate, self.device_rate, gain=self.output_gain)
                device_audio = convert_channels_int16(
                    device_audio,
                    self.output_api_channels,
                    self.device_channels,
                )
                if self.device_output_recorder is not None:
                    self.device_output_recorder.write(device_audio)
                self.output_audio_chunks += 1
                if self.output_audio_chunks == 1:
                    print("[diagnostics] Received translated audio from Palabra.", flush=True)
                if self.raw_palabra_playback:
                    self._queue_playback_chunk(device_audio, segment_end=False)
                else:
                    self._enqueue_phrase_playback(device_audio, segment_end=segment_end, phrase_key=phrase_key)
            elif msg_type == "validated_transcription":
                text = first_text_value(data, "transcription")
                if text:
                    print(f"[{language_label(source_language)}] {text}")
                    if self.debug_text_logger is not None:
                        self.debug_text_logger.write(
                            "validated_transcription",
                            configured_language=source_language,
                            text=text,
                            **compact_palabra_ids(data),
                        )
                    now = time.monotonic()
                    if self.first_source_transcription_time is None:
                        self.first_source_transcription_time = now
                    elif (
                        self.output_audio_chunks == 0
                        and now - self.first_source_transcription_time >= 10
                        and now - self.last_no_output_warning >= 10
                    ):
                        print(
                            "[diagnostics] Source transcription is arriving, but no translated audio "
                            "has been received yet. Check target voice/language and Palabra task settings.",
                            flush=True,
                        )
                        self.last_no_output_warning = now
            elif msg_type == "partial_transcription":
                text = first_text_value(data, "transcription")
                if text and self.debug_text_logger is not None:
                    self.debug_text_logger.write(
                        "partial_transcription",
                        configured_language=source_language,
                        text=text,
                        **compact_palabra_ids(data),
                    )
            elif msg_type == "translated_transcription":
                text = first_text_value(data, "translation", "translations")
                if text:
                    print(f"[{language_label(target_language)}] {text}")
                    if self.debug_text_logger is not None:
                        self.debug_text_logger.write(
                            "translated_transcription",
                            configured_language=target_language,
                            text=text,
                            **compact_palabra_ids(data),
                        )
            elif msg_type == "partial_translated_transcription":
                text = first_text_value(data, "translation", "translations")
                if text and self.debug_text_logger is not None:
                    self.debug_text_logger.write(
                        "partial_translated_transcription",
                        configured_language=target_language,
                        text=text,
                        **compact_palabra_ids(data),
                    )
            elif msg_type == "warning":
                print(f"[palabra warning] {format_palabra_message_details(data)}", flush=True)
                if self.debug_text_logger is not None:
                    self.debug_text_logger.write("warning", details=format_palabra_message_details(data))
            elif msg_type == "current_task":
                continue
            elif msg_type == "eos":
                self._log_incomplete_output_audio_groups("eos")
                self._release_pending_playback_phrase(segment_end=True)
                print("[palabra] End of stream received.", flush=True)
                if self.debug_text_logger is not None:
                    self.debug_text_logger.write("eos")
                return
            elif msg_type == "error":
                raise PalabraRuntimeError(format_palabra_message_details(data))


def build_audio_bridge(args) -> AudioBridge:
    if not args.no_refresh_devices:
        refresh_bridge_devices(args)

    if args.input_device:
        input_device = resolve_device(args.input_device, "input")
    else:
        input_device = resolve_opposite_cable_device(
            args.zoom_speaker_device,
            "output",
            "input",
            "Zoom speaker",
        )

    if args.output_device:
        output_device = resolve_device(args.output_device, "output")
    else:
        output_device = resolve_opposite_cable_device(
            args.zoom_mic_device,
            "input",
            "output",
            "Zoom microphone",
        )

    selected_device_lines = [
        f"Input device:  {format_device_summary(input_device)}",
        f"Output device: {format_device_summary(output_device)}",
        f"Audio channels: API={args.channels}, Windows cable={args.device_channels}",
    ]
    for line in selected_device_lines:
        print(line)
    append_cable_route_log(["Final bridge devices:", *selected_device_lines])

    return AudioBridge(
        input_device=input_device,
        output_device=output_device,
        device_rate=args.device_rate,
        api_rate=args.api_rate,
        chunk_ms=args.chunk_ms,
        api_channels=args.channels,
        device_channels=args.device_channels,
        input_block_ms=args.input_block_ms,
        playback_buffer_ms=args.playback_buffer_ms,
        phrase_start_buffer_ms=args.phrase_start_buffer_ms,
        playback_tempo=args.playback_tempo,
        playback_max_tempo=args.playback_max_tempo,
        playback_tempo_algorithm=args.playback_tempo_algorithm,
        playback_fade_ms=args.playback_fade_ms,
        idle_noise_amplitude=args.idle_noise_amplitude,
        output_gain=args.output_gain,
        raw_palabra_playback=args.raw_palabra_playback,
    )


def check_devices(args) -> None:
    bridge = build_audio_bridge(args)
    bridge.validate_devices()
    print("Device check completed successfully.")


def start_audio_with_fallback(
    args,
    source_language: str,
    target_language: str,
) -> tuple[AudioBridge, threading.Thread, sd.OutputStream]:
    while True:
        bridge = build_audio_bridge(args)
        capture_thread: Optional[threading.Thread] = None
        playback_stream: Optional[sd.OutputStream] = None
        try:
            capture_thread = bridge.start_capture(source_language)
            playback_stream = bridge.start_playback(target_language)
            return bridge, capture_thread, playback_stream
        except sd.PortAudioError as exc:
            bridge.stop_event.set()
            if playback_stream is not None:
                with contextlib.suppress(Exception):
                    playback_stream.stop()
                with contextlib.suppress(Exception):
                    playback_stream.close()
            if capture_thread is not None:
                capture_thread.join(timeout=2)

            failed_hostapi = hostapi_name(sd.query_devices(bridge.input_device))
            remaining_hostapis = [
                hostapi for hostapi in args.hostapi_preference if hostapi != failed_hostapi
            ]
            if args.no_refresh_devices or not remaining_hostapis:
                raise

            retry_lines = [
                f"Skipping {failed_hostapi} after audio startup failure and retrying device selection.",
                f"Remaining audio.hostapi_preference: {remaining_hostapis}",
                *portaudio_error_lines(exc),
            ]
            for line in retry_lines:
                print(line, flush=True)
            append_cable_route_log(retry_lines)

            args.hostapi_preference = remaining_hostapis
            args.input_device = None
            args.output_device = None



PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _query_process_image_name(pid: int) -> str:
    if sys.platform != "win32" or pid <= 0:
        return ""

    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = (
        ctypes.wintypes.DWORD,
        ctypes.wintypes.BOOL,
        ctypes.wintypes.DWORD,
    )
    kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = (
        ctypes.wintypes.HANDLE,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.LPWSTR,
        ctypes.POINTER(ctypes.wintypes.DWORD),
    )
    kernel32.QueryFullProcessImageNameW.restype = ctypes.wintypes.BOOL
    kernel32.CloseHandle.argtypes = (ctypes.wintypes.HANDLE,)
    kernel32.CloseHandle.restype = ctypes.wintypes.BOOL

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = ctypes.wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
    finally:
        kernel32.CloseHandle(handle)
    return ""


def _visible_zoom_window_titles() -> list[str]:
    if sys.platform != "win32":
        return []

    user32 = ctypes.windll.user32
    titles: list[str] = []

    enum_windows_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @enum_windows_proc
    def collect_window(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True

        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True

        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        process_image = _query_process_image_name(int(pid.value)).casefold()
        if not process_image.endswith("zoom.exe"):
            return True

        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value.strip()
        if title:
            titles.append(title)
        return True

    user32.EnumWindows(collect_window, 0)
    return titles


def zoom_meeting_window_is_visible(title_patterns: list[str]) -> tuple[bool, str]:
    patterns = [pattern.casefold() for pattern in title_patterns if pattern.strip()]
    if not patterns:
        return False, ""

    for title in _visible_zoom_window_titles():
        folded_title = title.casefold()
        if any(pattern in folded_title for pattern in patterns):
            return True, title
    return False, ""


async def drain_playback_before_close(bridge: AudioBridge, timeout_seconds: float) -> None:
    timeout_seconds = max(0.0, float(timeout_seconds))
    if timeout_seconds <= 0:
        return
    deadline = time.monotonic() + timeout_seconds
    announced = False
    while not bridge.playback_is_drained() and time.monotonic() < deadline:
        if not announced:
            pending_ms = bridge.playback_pending_samples() / max(1, bridge.device_rate * bridge.device_channels) * 1000
            print(f"Draining translated playback ({pending_ms:.0f} ms queued)...", flush=True)
            announced = True
        await asyncio.sleep(0.05)
    if announced:
        pending_ms = bridge.playback_pending_samples() / max(1, bridge.device_rate * bridge.device_channels) * 1000
        if pending_ms > 0:
            print(f"Playback drain timeout; {pending_ms:.0f} ms may remain queued.", flush=True)
        else:
            print("Translated playback drained.", flush=True)


async def monitor_zoom_meeting_window(
    bridge: AudioBridge,
    title_patterns: list[str],
    check_seconds: float,
    end_grace_seconds: float,
) -> None:
    if sys.platform != "win32":
        print("Zoom meeting-end monitor is only available on Windows.", flush=True)
        await asyncio.to_thread(bridge.stop_event.wait)
        return

    check_seconds = max(0.5, float(check_seconds))
    end_grace_seconds = max(check_seconds, float(end_grace_seconds))
    seen_meeting = False
    missing_since: Optional[float] = None
    print(
        "Watching for Zoom meeting window to close "
        f"(grace {end_grace_seconds:.0f}s).",
        flush=True,
    )

    while not bridge.stop_event.is_set():
        visible, title = zoom_meeting_window_is_visible(title_patterns)
        now = time.monotonic()

        if visible:
            if not seen_meeting:
                print(
                    f"Detected Zoom meeting window: {title}. Bridge is live; waiting for meeting audio.",
                    flush=True,
                )
            seen_meeting = True
            missing_since = None
        elif seen_meeting:
            if missing_since is None:
                missing_since = now
            elif now - missing_since >= end_grace_seconds:
                print("Zoom meeting window closed; stopping bridge.", flush=True)
                bridge.request_stop("meeting-ended")
                return

        await asyncio.sleep(check_seconds)


async def run(args) -> None:
    load_dotenv()
    client_id = os.getenv("PALABRA_CLIENT_ID")
    client_secret = os.getenv("PALABRA_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise SystemExit("Set PALABRA_CLIENT_ID and PALABRA_CLIENT_SECRET in .env or the environment.")

    source_language, target_language, voice_id = resolve_translation_settings(args)
    print(f"Translation:   {language_label(source_language)} -> {language_label(target_language)}")
    if voice_id:
        print(f"Voice ID:      {voice_id}")

    keep_awake_enabled = keep_windows_awake()
    session_id: Optional[str] = None
    bridge: Optional[AudioBridge] = None
    capture_thread: Optional[threading.Thread] = None
    playback_stream: Optional[sd.OutputStream] = None
    api_input_recorder: Optional[WavDebugRecorder] = None
    api_output_recorder: Optional[WavDebugRecorder] = None
    device_output_recorder: Optional[WavDebugRecorder] = None
    callback_output_recorder: Optional[AsyncWavDebugRecorder] = None
    debug_text_logger: Optional[DebugTextLogger] = None
    debug_wav_paths: list[Path] = []
    debug_ffmpeg: Optional[str] = None
    try:
        try:
            bridge, capture_thread, playback_stream = start_audio_with_fallback(
                args,
                source_language,
                target_language,
            )
            if args.record_debug_mp3:
                debug_ffmpeg = require_ffmpeg_for_debug_mp3()
                timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                debug_wav_paths = [
                    Path("debug") / f"palabra_input_api_{timestamp}.wav",
                    Path("debug") / f"palabra_output_api_{timestamp}.wav",
                    Path("debug") / f"zoom_mic_output_{timestamp}.wav",
                    Path("debug") / f"zoom_mic_callback_{timestamp}.wav",
                ]
                api_input_recorder = WavDebugRecorder(
                    debug_wav_paths[0],
                    args.api_rate,
                    args.channels,
                ).__enter__()
                api_output_recorder = WavDebugRecorder(
                    debug_wav_paths[1],
                    PALABRA_OUTPUT_RATE,
                    PALABRA_OUTPUT_CHANNELS,
                ).__enter__()
                device_output_recorder = WavDebugRecorder(
                    debug_wav_paths[2],
                    args.device_rate,
                    args.device_channels,
                ).__enter__()
                callback_output_recorder = AsyncWavDebugRecorder(
                    debug_wav_paths[3],
                    args.device_rate,
                    args.device_channels,
                ).__enter__()
                debug_text_logger = DebugTextLogger(
                    Path("debug") / f"palabra_text_events_{timestamp}.txt",
                ).__enter__()
                bridge.api_input_recorder = api_input_recorder
                bridge.api_output_recorder = api_output_recorder
                bridge.device_output_recorder = device_output_recorder
                bridge.callback_output_recorder = callback_output_recorder
                bridge.debug_text_logger = debug_text_logger
                print("Recording debug WAVs during the live run; converting to MP3 after shutdown.")
                print(f"Recording Palabra input WAV: {api_input_recorder.path}")
                print(f"Recording Palabra output WAV: {api_output_recorder.path}")
                print(f"Recording Zoom mic output WAV: {device_output_recorder.path}")
                print(f"Recording exact callback output WAV: {callback_output_recorder.path}")
                print(f"Recording Palabra text events: {debug_text_logger.path}")

            session = await create_session(client_id, client_secret)
            session_data = session["data"]
            session_id = session_data.get("id")
            ws_url = session_data["ws_url"]
            token = session_data["publisher"]

            async with await connect_websocket(ws_url, token) as websocket:
                await configure_translation(
                    websocket,
                    source_language=source_language,
                    target_language=target_language,
                    voice_id=voice_id,
                    api_rate=args.api_rate,
                    channels=args.channels,
                    segment_confirmation_silence_threshold=(
                        args.segment_confirmation_silence_threshold
                    ),
                    only_confirm_by_silence=args.only_confirm_by_silence,
                    sentence_splitter_enabled=args.sentence_splitter_enabled,
                    translate_partial_transcriptions=args.palabra_translate_partials,
                    desired_queue_level_ms=args.palabra_desired_queue_level_ms,
                    max_queue_level_ms=args.palabra_max_queue_level_ms,
                    auto_tempo=args.palabra_auto_tempo,
                    min_tempo=args.palabra_min_tempo,
                    max_tempo=args.palabra_max_tempo,
                )
                print("Waiting for Palabra task readiness...")
                await wait_for_current_task(
                    websocket,
                    poll_seconds=args.task_poll_seconds,
                    timeout_seconds=args.task_ready_timeout,
                )
                if args.startup_delay > 0:
                    print(f"Waiting {args.startup_delay:.1f}s after Palabra task readiness...")
                    await asyncio.sleep(args.startup_delay)
                drained_blocks = bridge.drain_capture_queue()
                if drained_blocks:
                    print(
                        f"[capture] discarded {drained_blocks} startup audio blocks before going live.",
                        flush=True,
                    )

                def stop_now(*_):
                    print("Stopping bridge...", flush=True)
                    bridge.request_stop("manual")

                signal.signal(signal.SIGINT, stop_now)
                signal.signal(signal.SIGTERM, stop_now)

                print("Bridge is live. Press Ctrl+C to stop.")
                send_task = asyncio.create_task(bridge.send_audio(websocket))
                receive_task = asyncio.create_task(
                    bridge.receive_audio(
                        websocket,
                        source_language,
                        target_language,
                        dump_messages=args.dump_palabra_messages,
                    )
                )
                stop_task = asyncio.create_task(asyncio.to_thread(bridge.stop_event.wait))
                monitor_task: Optional[asyncio.Task] = None
                if args.end_when_zoom_meeting_ends:
                    monitor_task = asyncio.create_task(
                        monitor_zoom_meeting_window(
                            bridge,
                            args.zoom_meeting_title_patterns,
                            args.zoom_meeting_check_seconds,
                            args.zoom_meeting_end_grace_seconds,
                        )
                    )
                tasks = {send_task, receive_task, stop_task}
                if monitor_task is not None:
                    tasks.add(monitor_task)
                try:
                    done, pending = await asyncio.wait(
                        tasks,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if stop_task in done or (monitor_task in done and bridge.stop_event.is_set()):
                        if bridge.stop_reason == "meeting-ended":
                            await websocket.close()
                        else:
                            if not send_task.done():
                                send_done, _ = await asyncio.wait(
                                    {send_task},
                                    timeout=min(2.0, args.graceful_shutdown_timeout),
                                )
                                if send_task not in send_done:
                                    send_task.cancel()
                                    await asyncio.gather(send_task, return_exceptions=True)
                            await graceful_stop_palabra_task(
                                websocket,
                                receive_task,
                                eos_timeout_seconds=args.end_task_eos_timeout,
                                shutdown_timeout_seconds=args.graceful_shutdown_timeout,
                            )
                            await websocket.close()
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    for task in done:
                        if task is stop_task:
                            continue
                        if task is monitor_task:
                            task.result()
                        elif not bridge.stop_event.is_set():
                            task.result()
                finally:
                    bridge.stop_event.set()
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    print("Bridge stopped.", flush=True)
        finally:
            if session_id:
                await delete_session(client_id, client_secret, session_id)
    finally:
        if bridge is not None:
            bridge.stop_event.set()
            if bridge.stop_reason != "meeting-ended":
                await drain_playback_before_close(bridge, args.playback_drain_timeout)
        if playback_stream is not None:
            with contextlib.suppress(Exception):
                playback_stream.stop()
            with contextlib.suppress(Exception):
                playback_stream.close()
        if bridge is not None:
            bridge.api_input_recorder = None
            bridge.api_output_recorder = None
            bridge.device_output_recorder = None
            bridge.callback_output_recorder = None
            bridge.debug_text_logger = None
        if debug_text_logger is not None:
            debug_text_logger.__exit__(None, None, None)
        if api_input_recorder is not None:
            api_input_recorder.__exit__(None, None, None)
        if api_output_recorder is not None:
            api_output_recorder.__exit__(None, None, None)
        if device_output_recorder is not None:
            device_output_recorder.__exit__(None, None, None)
        if callback_output_recorder is not None:
            callback_output_recorder.__exit__(None, None, None)
        if capture_thread is not None:
            capture_thread.join(timeout=2)
        restore_windows_power_state(keep_awake_enabled)
        if debug_ffmpeg is not None and debug_wav_paths:
            try:
                convert_debug_wavs_to_mp3(debug_wav_paths, debug_ffmpeg)
            except Exception as exc:
                print(
                    f"Warning: could not convert debug WAV files to MP3: {exc}",
                    flush=True,
                )


def parse_args():
    config = load_config()
    translation = config_section(config, "translation")
    audio = config_section(config, "audio")
    zoom = config_section(config, "zoom")
    bridge = config_section(config, "bridge")
    palabra = config_section(config, "palabra")
    diagnostics = config_section(config, "diagnostics")

    parser = argparse.ArgumentParser(
        description="Bridge Zoom audio through Palabra and play interpreted audio into Zoom."
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VERSION}")
    parser.add_argument("--list-devices", action="store_true", help="Print audio devices and exit.")
    parser.add_argument(
        "--check-devices",
        action="store_true",
        help="Auto-select and validate bridge audio devices without starting a Palabra session.",
    )
    parser.add_argument("--meter-input", help="Meter an input device and exit.")
    parser.add_argument("--test-output", help="Play a short test tone to an output device and exit.")
    parser.add_argument(
        "--no-refresh-devices",
        action="store_true",
        help="Use configured/manual audio devices as-is; skip automatic VB-Cable pair detection for this run.",
    )
    parser.add_argument(
        "--test-seconds",
        type=float,
        default=config_float(diagnostics, "test_seconds", DEFAULT_TEST_SECONDS, "diagnostics.test_seconds"),
        help="Duration for --test-output.",
    )
    parser.add_argument(
        "--test-volume",
        type=float,
        default=config_float(diagnostics, "test_volume", DEFAULT_TEST_VOLUME, "diagnostics.test_volume"),
        help="Tone volume from 0.0 to 1.0.",
    )
    parser.add_argument(
        "--record-debug-mp3",
        "--record-output-mp3",
        "--record-output-wav",
        dest="record_debug_mp3",
        action="store_true",
        default=config_bool_with_aliases(
            diagnostics,
            "record_debug_mp3",
            DEFAULT_RECORD_DEBUG_MP3,
            "diagnostics.record_debug_mp3",
            aliases=("record_output_mp3", "record_output_wav"),
        ),
        help="Record bridge input/output MP3 files into debug/ for audio-quality analysis.",
    )
    parser.add_argument(
        "--dump-palabra-messages",
        action="store_true",
        help="Print the first payload shape for each Palabra message type without dumping audio.",
    )
    parser.add_argument(
        "--input-device",
        default=config_optional_device_selector(audio, "input_device", "audio.input_device"),
        help="Manual bridge input device id/name substring. Usually auto-selected from Zoom devices.",
    )
    parser.add_argument(
        "--output-device",
        default=config_optional_device_selector(audio, "output_device", "audio.output_device"),
        help="Manual bridge output device id/name substring. Usually auto-selected from Zoom devices.",
    )
    parser.add_argument(
        "--zoom-speaker-device",
        default=config_device_selector(
            zoom,
            "speaker_device",
            DEFAULT_ZOOM_SPEAKER_DEVICE,
            "zoom.speaker_device",
        ),
        help="Zoom speaker device id/name substring. Used when input_device is not set.",
    )
    parser.add_argument(
        "--zoom-mic-device",
        "--zoom-microphone-device",
        dest="zoom_mic_device",
        default=config_device_selector(
            zoom,
            "mic_device",
            DEFAULT_ZOOM_MIC_DEVICE,
            "zoom.mic_device",
            aliases=("microphone_device",),
        ),
        help="Zoom microphone device id/name substring. Used when output_device is not set.",
    )
    parser.add_argument(
        "--end-when-zoom-meeting-ends",
        action=argparse.BooleanOptionalAction,
        default=config_bool(
            zoom,
            "end_bridge_when_meeting_ends",
            DEFAULT_END_WHEN_ZOOM_MEETING_ENDS,
            "zoom.end_bridge_when_meeting_ends",
        ),
        help="Stop the bridge after the Zoom meeting/webinar window closes.",
    )
    parser.add_argument(
        "--zoom-meeting-title-patterns",
        nargs="+",
        default=config_string_list(
            zoom,
            "meeting_title_patterns",
            DEFAULT_ZOOM_MEETING_TITLE_PATTERNS,
            "zoom.meeting_title_patterns",
        ),
        help="Window title substrings that identify an active Zoom meeting/webinar.",
    )
    parser.add_argument(
        "--zoom-meeting-check-seconds",
        type=float,
        default=config_float(
            zoom,
            "meeting_check_seconds",
            DEFAULT_ZOOM_MEETING_CHECK_SECONDS,
            "zoom.meeting_check_seconds",
        ),
        help="Seconds between Zoom meeting window checks.",
    )
    parser.add_argument(
        "--zoom-meeting-end-grace-seconds",
        type=float,
        default=config_float(
            zoom,
            "meeting_end_grace_seconds",
            DEFAULT_ZOOM_MEETING_END_GRACE_SECONDS,
            "zoom.meeting_end_grace_seconds",
        ),
        help="Seconds to wait after the Zoom meeting window disappears before stopping.",
    )
    parser.add_argument(
        "--source-language",
        default=config_string(
            translation,
            "source_language",
            DEFAULT_SOURCE_LANGUAGE,
            "translation.source_language",
        ),
        help="Palabra source language. Overrides config.toml.",
    )
    parser.add_argument(
        "--target-language",
        default=config_string(
            translation,
            "target_language",
            DEFAULT_TARGET_LANGUAGE,
            "translation.target_language",
        ),
        help="Palabra target language. Overrides config.toml.",
    )
    parser.add_argument(
        "--voice-id",
        default=config_optional_string(translation, "voice_id", "translation.voice_id"),
        help="Palabra voice id. Overrides config.toml.",
    )
    parser.add_argument(
        "--device-rate",
        type=int,
        default=config_int(audio, "device_rate", DEFAULT_DEVICE_RATE, "audio.device_rate"),
        help="Manual virtual cable sample rate. Usually auto-selected from the chosen backend.",
    )
    parser.add_argument(
        "--api-rate",
        type=int,
        default=config_int(audio, "api_rate", DEFAULT_API_RATE, "audio.api_rate"),
        help="Palabra sample rate, usually 16000 or 24000. Overrides config.toml.",
    )
    parser.add_argument(
        "--channels",
        type=int,
        default=config_int(audio, "channels", DEFAULT_CHANNELS, "audio.channels", choices=(1, 2)),
        choices=(1, 2),
        help="Palabra API channels. Overrides config.toml.",
    )
    parser.add_argument(
        "--device-channels",
        type=int,
        default=config_int(
            audio,
            "device_channels",
            DEFAULT_DEVICE_CHANNELS,
            "audio.device_channels",
            choices=(1, 2),
        ),
        choices=(1, 2),
        help="Windows virtual cable channels. Overrides config.toml.",
    )
    parser.add_argument(
        "--hostapi-preference",
        nargs="+",
        default=config_string_list(
            audio,
            "hostapi_preference",
            DEFAULT_HOSTAPI_PREFERENCE,
            "audio.hostapi_preference",
        ),
        help="Preferred Windows audio backends in order. Overrides config.toml.",
    )
    parser.add_argument(
        "--chunk-ms",
        type=int,
        default=config_int(bridge, "chunk_ms", DEFAULT_CHUNK_MS, "bridge.chunk_ms"),
        help="Palabra input chunk size. Overrides config.toml.",
    )
    parser.add_argument(
        "--input-block-ms",
        type=int,
        default=config_int(
            bridge,
            "input_block_ms",
            DEFAULT_INPUT_BLOCK_MS,
            "bridge.input_block_ms",
        ),
        help="PortAudio callback block size. Overrides config.toml.",
    )
    parser.add_argument(
        "--raw",
        action=argparse.BooleanOptionalAction,
        dest="raw_palabra_playback",
        default=config_bool(
            bridge,
            "raw_palabra_playback",
            DEFAULT_RAW_PALABRA_PLAYBACK,
            "bridge.raw_palabra_playback",
        ),
        help="Diagnostic mode: play converted Palabra audio chunks directly, bypassing phrase buffering and catch-up.",
    )
    parser.add_argument(
        "--playback-buffer-ms",
        type=int,
        default=config_int(
            bridge,
            "playback_buffer_ms",
            DEFAULT_PLAYBACK_BUFFER_MS,
            "bridge.playback_buffer_ms",
        ),
        help="Translated audio buffer before playback starts. Overrides config.toml.",
    )
    parser.add_argument(
        "--phrase-start-buffer-ms",
        type=int,
        default=config_int(
            bridge,
            "phrase_start_buffer_ms",
            DEFAULT_PHRASE_START_BUFFER_MS,
            "bridge.phrase_start_buffer_ms",
        ),
        help="Audio to collect for a phrase before releasing partial Palabra output. Overrides config.toml.",
    )
    parser.add_argument(
        "--playback-tempo",
        type=float,
        default=config_float(
            bridge,
            "playback_tempo",
            DEFAULT_PLAYBACK_TEMPO,
            "bridge.playback_tempo",
        ),
        help="Normal local playback speed for translated audio. Overrides config.toml.",
    )
    parser.add_argument(
        "--playback-max-tempo",
        type=float,
        default=config_float(
            bridge,
            "playback_max_tempo",
            DEFAULT_PLAYBACK_MAX_LOCAL_TEMPO,
            "bridge.playback_max_tempo",
        ),
        help="Maximum local playback speed-up when translated audio backlog builds. Overrides config.toml.",
    )
    parser.add_argument(
        "--playback-tempo-algorithm",
        choices=sorted(PLAYBACK_TEMPO_ALGORITHMS),
        default=config_string(
            bridge,
            "playback_tempo_algorithm",
            DEFAULT_PLAYBACK_TEMPO_ALGORITHM,
            "bridge.playback_tempo_algorithm",
        ),
        help="Local tempo algorithm: resample changes pitch, rubberband preserves pitch better. Overrides config.toml.",
    )
    parser.add_argument(
        "--playback-fade-ms",
        type=int,
        default=config_int(
            bridge,
            "playback_fade_ms",
            DEFAULT_PLAYBACK_FADE_MS,
            "bridge.playback_fade_ms",
        ),
        help="Fade length for callback start/stop transitions. Overrides config.toml.",
    )
    parser.add_argument(
        "--idle-noise-amplitude",
        type=int,
        default=config_int(
            bridge,
            "idle_noise_amplitude",
            DEFAULT_IDLE_NOISE_AMPLITUDE,
            "bridge.idle_noise_amplitude",
        ),
        help="Low-level comfort-noise amplitude during idle callback periods. Use 0 to disable.",
    )
    parser.add_argument(
        "--output-gain",
        type=float,
        default=config_float(
            bridge,
            "output_gain",
            DEFAULT_OUTPUT_GAIN,
            "bridge.output_gain",
        ),
        help="Gain applied to translated audio before Zoom's microphone cable.",
    )
    parser.add_argument(
        "--segment-confirmation-silence-threshold",
        type=float,
        default=config_float(
            palabra,
            "segment_confirmation_silence_threshold",
            DEFAULT_SEGMENT_CONFIRMATION_SILENCE_THRESHOLD,
            "palabra.segment_confirmation_silence_threshold",
        ),
        help=(
            "Silence Palabra uses before confirming a phrase boundary. "
            "Changing this may cause unpredictable segment splits and missing audio."
        ),
    )
    parser.add_argument(
        "--sentence-splitter-enabled",
        action=argparse.BooleanOptionalAction,
        default=config_bool(
            palabra,
            "sentence_splitter_enabled",
            DEFAULT_SENTENCE_SPLITTER_ENABLED,
            "palabra.sentence_splitter_enabled",
        ),
        help=(
            "Allow Palabra to split long sentences into phrase-sized segments. "
            "Changing this may cause unpredictable segment replacement and missing audio."
        ),
    )
    parser.add_argument(
        "--only-confirm-by-silence",
        action=argparse.BooleanOptionalAction,
        default=config_bool(
            palabra,
            "only_confirm_by_silence",
            DEFAULT_ONLY_CONFIRM_BY_SILENCE,
            "palabra.only_confirm_by_silence",
        ),
        help=(
            "Force Palabra to confirm phrase boundaries only after detected silence. "
            "Changing this may cause unpredictable segment splits and missing audio."
        ),
    )
    parser.add_argument(
        "--palabra-translate-partials",
        action=argparse.BooleanOptionalAction,
        default=config_bool(
            palabra,
            "translate_partial_transcriptions",
            DEFAULT_PALABRA_TRANSLATE_PARTIALS,
            "palabra.translate_partial_transcriptions",
        ),
        help=(
            "Let Palabra translate partial transcriptions so long phrases start speaking earlier. "
            "Changing this may cause unstable partial speech or missing audio."
        ),
    )
    parser.add_argument(
        "--palabra-desired-queue-level-ms",
        type=int,
        default=config_int(
            palabra,
            "desired_queue_level_ms",
            DEFAULT_PALABRA_DESIRED_QUEUE_LEVEL_MS,
            "palabra.desired_queue_level_ms",
        ),
        help="Desired translated speech queue level inside Palabra.",
    )
    parser.add_argument(
        "--palabra-max-queue-level-ms",
        type=int,
        default=config_int(
            palabra,
            "max_queue_level_ms",
            DEFAULT_PALABRA_MAX_QUEUE_LEVEL_MS,
            "palabra.max_queue_level_ms",
        ),
        help="Maximum translated speech queue level inside Palabra.",
    )
    parser.add_argument(
        "--palabra-auto-tempo",
        action=argparse.BooleanOptionalAction,
        default=config_bool(
            palabra,
            "auto_tempo",
            DEFAULT_PALABRA_AUTO_TEMPO,
            "palabra.auto_tempo",
        ),
        help="Let Palabra slightly adjust speech tempo to manage queue delay.",
    )
    parser.add_argument(
        "--palabra-min-tempo",
        type=float,
        default=config_float(
            palabra,
            "min_tempo",
            DEFAULT_PALABRA_MIN_TEMPO,
            "palabra.min_tempo",
        ),
        help="Minimum Palabra speech tempo multiplier.",
    )
    parser.add_argument(
        "--palabra-max-tempo",
        type=float,
        default=config_float(
            palabra,
            "max_tempo",
            DEFAULT_PALABRA_MAX_TEMPO,
            "palabra.max_tempo",
        ),
        help="Maximum Palabra speech tempo multiplier.",
    )
    parser.add_argument(
        "--startup-delay",
        type=float,
        default=config_float(bridge, "startup_delay", DEFAULT_STARTUP_DELAY, "bridge.startup_delay"),
        help="Extra seconds to wait after Palabra reports task readiness. Overrides config.toml.",
    )
    parser.add_argument(
        "--task-ready-timeout",
        type=float,
        default=config_float(
            bridge,
            "task_ready_timeout_seconds",
            DEFAULT_TASK_READY_TIMEOUT_SECONDS,
            "bridge.task_ready_timeout_seconds",
        ),
        help="Seconds to wait for Palabra current_task after set_task.",
    )
    parser.add_argument(
        "--task-poll-seconds",
        type=float,
        default=config_float(
            bridge,
            "task_poll_seconds",
            DEFAULT_TASK_POLL_SECONDS,
            "bridge.task_poll_seconds",
        ),
        help="Seconds between Palabra get_task readiness polls, capped at 2 seconds.",
    )
    parser.add_argument(
        "--end-task-eos-timeout",
        type=float,
        default=config_float(
            bridge,
            "end_task_eos_timeout_seconds",
            DEFAULT_END_TASK_EOS_TIMEOUT_SECONDS,
            "bridge.end_task_eos_timeout_seconds",
        ),
        help="Palabra eos_timeout sent with end_task during graceful manual stops.",
    )
    parser.add_argument(
        "--graceful-shutdown-timeout",
        type=float,
        default=config_float(
            bridge,
            "graceful_shutdown_timeout_seconds",
            DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS,
            "bridge.graceful_shutdown_timeout_seconds",
        ),
        help="Maximum seconds to wait for Palabra EOS after a graceful manual stop.",
    )
    parser.add_argument(
        "--playback-drain-timeout",
        type=float,
        default=config_float(
            bridge,
            "playback_drain_timeout_seconds",
            DEFAULT_PLAYBACK_DRAIN_TIMEOUT_SECONDS,
            "bridge.playback_drain_timeout_seconds",
        ),
        help="Maximum seconds to keep the output stream open while translated audio drains.",
    )
    args = parser.parse_args()
    args.hostapi_preference = remove_blocked_hostapis(args.hostapi_preference)
    validate_runtime_args(args)
    return args


def main() -> None:
    cli_args = parse_args()
    if cli_args.list_devices:
        list_devices()
    elif cli_args.check_devices:
        check_devices(cli_args)
    elif cli_args.meter_input:
        meter_input(cli_args.meter_input, cli_args.device_rate, cli_args.device_channels)
    elif cli_args.test_output:
        test_output(
            cli_args.test_output,
            cli_args.device_rate,
            cli_args.device_channels,
            cli_args.test_seconds,
            cli_args.test_volume,
        )
    else:
        asyncio.run(run(cli_args))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise
    except SystemExit as exc:
        if exc.code not in (None, 0):
            write_last_error_log(exc)
            print(f"Last error written to {LAST_ERROR_LOG_PATH}", flush=True)
        raise
    except Exception as exc:
        write_last_error_log(exc)
        print(f"Last error written to {LAST_ERROR_LOG_PATH}", flush=True)
        raise
