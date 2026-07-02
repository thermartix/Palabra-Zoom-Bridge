from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import queue
import re
import shutil
import subprocess
import threading
import time
from fractions import Fraction
from typing import Optional

import numpy as np
import sounddevice as sd
from scipy.signal import resample_poly

from audio_utils import PlaybackChunk, convert_channels_int16, resample_int16, stream_blocksize
from constants import *
from debug_recording import AsyncWavDebugRecorder, DebugTextLogger, WavDebugRecorder
from logging_utils import append_cable_route_log, portaudio_error_lines
from palabra_client import (
    PalabraRuntimeError,
    audio_group_key,
    compact_palabra_ids,
    compact_payload_shape,
    first_text_value,
    format_palabra_message_details,
    language_label,
    parse_palabra_message,
)

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
        output_peak_limit: float,
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
        self.output_peak_limit = max(0.0, min(1.0, float(output_peak_limit)))
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
        self.tempo_preprocess_min_samples = int(device_rate * TEMPO_PREPROCESS_MIN_MS / 1000) * device_channels
        self.tempo_pending_chunks: list[np.ndarray] = []
        self.tempo_pending_samples = 0
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

    def _append_playback_buffer_chunk(
        self,
        chunks: list[np.ndarray],
        buffered_samples: int,
        chunk_audio: np.ndarray,
    ) -> int:
        if len(chunk_audio) == 0:
            return buffered_samples

        previous_audio = chunks[-1] if chunks else np.array([], dtype=np.int16)
        at_segment_boundary = any(offset == buffered_samples for offset in self.playback_segment_end_offsets)
        fade_frames = min(
            self.playback_fade_frames,
            len(previous_audio) // self.device_channels,
            len(chunk_audio) // self.device_channels,
        )
        if self.raw_palabra_playback or at_segment_boundary or fade_frames <= 1:
            chunks.append(chunk_audio)
            return buffered_samples + len(chunk_audio)

        fade_samples = fade_frames * self.device_channels
        previous_copy = previous_audio.copy()
        chunk_copy = chunk_audio.copy()
        previous_frames = previous_copy.reshape(-1, self.device_channels)
        chunk_frames = chunk_copy.reshape(-1, self.device_channels)
        ramp_in = np.linspace(0.0, 1.0, fade_frames, dtype=np.float32)[:, None]
        ramp_out = 1.0 - ramp_in
        blended = (
            previous_frames[-fade_frames:].astype(np.float32) * ramp_out
            + chunk_frames[:fade_frames].astype(np.float32) * ramp_in
        )
        previous_frames[-fade_frames:] = np.rint(blended).astype(np.int16)
        chunks[-1] = previous_copy
        chunks.append(chunk_copy[fade_samples:])
        return buffered_samples + len(chunk_copy) - fade_samples

    def _queue_playback_chunk(self, audio: np.ndarray, segment_end: bool, force: bool = False) -> None:
        if len(audio) == 0 and not segment_end:
            return
        if not self.raw_palabra_playback:
            prepared = self._prepare_tempo_playback_chunk(audio, segment_end=segment_end, force=force)
            if prepared is None:
                return
            audio = prepared
        self.playback_queue.put_nowait(PlaybackChunk(audio, segment_end=segment_end))

    def _reset_pending_playback_phrase(self) -> None:
        self.pending_playback_phrase_key = None
        self.pending_playback_phrase_chunks = []
        self.pending_playback_phrase_samples = 0
        self.pending_playback_phrase_released = False

    def _release_pending_playback_phrase(self, segment_end: bool, force: bool = False) -> None:
        if not self.pending_playback_phrase_chunks:
            if segment_end and self.tempo_pending_chunks:
                self._queue_playback_chunk(np.array([], dtype=np.int16), segment_end=True, force=True)
            if segment_end:
                self._reset_pending_playback_phrase()
            return

        if len(self.pending_playback_phrase_chunks) == 1:
            audio = self.pending_playback_phrase_chunks[0]
        else:
            audio = np.concatenate(self.pending_playback_phrase_chunks)
        self._queue_playback_chunk(audio, segment_end=segment_end, force=segment_end or force)
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
            self._release_pending_playback_phrase(segment_end=False, force=True)
            self.pending_playback_phrase_key = phrase_key

        if self.pending_playback_phrase_released:
            self._queue_playback_chunk(audio, segment_end=segment_end, force=segment_end)
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

    def _limit_callback_output(self, outdata) -> None:
        if self.output_peak_limit <= 0.0:
            return
        peak = int(round(32767 * self.output_peak_limit))
        if peak <= 0:
            outdata.fill(0)
            return
        np.clip(outdata, -peak, peak, out=outdata)

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
        backlog_samples = max(0, self.playback_ready_samples() - target_samples)
        if backlog_samples <= 0:
            return self.playback_tempo
        ramp_samples = max(self.device_channels, self.playback_catchup_full_backlog_samples)
        catchup = min(1.0, backlog_samples / float(ramp_samples))
        return self.playback_tempo + ((self.playback_max_tempo - self.playback_tempo) * catchup)

    def _prepare_tempo_playback_chunk(
        self,
        audio: np.ndarray,
        segment_end: bool,
        force: bool = False,
    ) -> Optional[np.ndarray]:
        if self.playback_tempo_algorithm == "rubberband":
            if len(audio) > 0:
                self.tempo_pending_chunks.append(audio)
                self.tempo_pending_samples += len(audio)
            if (
                self.tempo_pending_samples < self.tempo_preprocess_min_samples
                and not segment_end
                and not force
            ):
                return None
            if self.tempo_pending_chunks:
                audio = (
                    self.tempo_pending_chunks[0]
                    if len(self.tempo_pending_chunks) == 1
                    else np.concatenate(self.tempo_pending_chunks)
                )
                self.tempo_pending_chunks = []
                self.tempo_pending_samples = 0
            elif len(audio) == 0:
                return audio

        return self._tempo_adjust_playback_chunk(audio)

    def _tempo_adjust_playback_chunk(self, audio: np.ndarray) -> np.ndarray:
        input_frames = len(audio) // self.device_channels
        if input_frames <= 1:
            return audio
        tempo = self._playback_catchup_tempo()
        if tempo <= 1.0001:
            return audio
        output_frames = max(1, int(round(input_frames / tempo)))
        output_samples = output_frames * self.device_channels
        return self._speed_adjust_playback_slice(audio, output_samples)

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
        return (
            self.playback_ready_samples()
            + sum(len(chunk) for chunk in self.pending_playback_phrase_chunks)
            + self.tempo_pending_samples
        )

    def playback_ready_samples(self) -> int:
        queued_samples = 0
        with self.playback_queue.mutex:
            queued_samples = sum(len(chunk.audio) for chunk in self.playback_queue.queue)
        return len(self.playback_buffer) + queued_samples

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
                buffered_samples = self._append_playback_buffer_chunk(chunks, buffered_samples, chunk_audio)
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
                    buffered_samples = self._append_playback_buffer_chunk(chunks, buffered_samples, chunk_audio)
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

                self._limit_callback_output(outdata)
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
                output_audio = self.playback_buffer[:needed].copy()
                if not was_playing:
                    self._fade_in(output_audio)
                outdata[:] = output_audio.reshape(frames, self.device_channels)
                self._consume_playback_buffer(needed)
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

            self._limit_callback_output(outdata)
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
        output_peak_limit=args.output_peak_limit,
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
