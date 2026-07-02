#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

import sounddevice as sd
from dotenv import load_dotenv

from modules.config import (
    config_bool,
    config_bool_with_aliases,
    config_device_selector,
    config_float,
    config_int,
    config_optional_device_selector,
    config_optional_string,
    config_section,
    config_string,
    config_string_list,
    load_config,
    remove_blocked_hostapis,
    resolve_translation_settings,
    validate_runtime_args,
)
from modules.constants import *
from modules.debug_recording import (
    AsyncWavDebugRecorder,
    DebugTextLogger,
    WavDebugRecorder,
    convert_debug_wavs_to_mp3,
    require_ffmpeg_for_debug_mp3,
)
from modules.logging_utils import LAST_ERROR_LOG_PATH, write_last_error_log
from modules.palabra_agent import PalabraAgent, PalabraAgentSettings
from modules.palabra_client import (
    connect_websocket,
    create_session,
    delete_session,
    language_label,
    wait_for_current_task,
)
from modules.system_power import keep_windows_awake, restore_windows_power_state
from modules.transports.cable import (
    AudioBridge,
    check_devices,
    drain_playback_before_close,
    list_devices,
    meter_input,
    start_audio_with_fallback,
    test_output,
)
from modules.transports.zoom_sdk import ZoomSdkProbeSettings, ZoomSdkTransport
from modules.zoom_desktop import monitor_zoom_meeting_window

async def run_sdk_probe(args) -> None:
    settings = ZoomSdkProbeSettings(
        meeting_number=args.zoom_sdk_meeting_number,
        password=args.zoom_sdk_password,
        display_name=args.zoom_sdk_display_name,
        probe_seconds=args.zoom_sdk_probe_seconds,
        output_wav=Path(args.zoom_sdk_output_wav),
        sample_rate=args.zoom_sdk_sample_rate,
        channels=args.zoom_sdk_channels,
        adapter_module=args.zoom_sdk_adapter_module,
        dry_run=args.zoom_sdk_dry_run,
    )
    print("Running Zoom SDK audio probe.")
    if settings.dry_run:
        print("SDK dry run is enabled; no Zoom meeting will be joined.")
    elif not settings.meeting_number:
        raise SystemExit("zoom_sdk.meeting_number is required for SDK probe mode.")
    transport = ZoomSdkTransport(settings)
    await transport.run_probe()


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

            agent_settings = PalabraAgentSettings(
                source_language=source_language,
                target_language=target_language,
                voice_id=voice_id,
                api_rate=args.api_rate,
                channels=args.channels,
                segment_confirmation_silence_threshold=args.segment_confirmation_silence_threshold,
                only_confirm_by_silence=args.only_confirm_by_silence,
                sentence_splitter_enabled=args.sentence_splitter_enabled,
                translate_partial_transcriptions=args.palabra_translate_partials,
                desired_queue_level_ms=args.palabra_desired_queue_level_ms,
                max_queue_level_ms=args.palabra_max_queue_level_ms,
                auto_tempo=args.palabra_auto_tempo,
                min_tempo=args.palabra_min_tempo,
                max_tempo=args.palabra_max_tempo,
                end_task_eos_timeout_seconds=args.end_task_eos_timeout,
                graceful_shutdown_timeout_seconds=args.graceful_shutdown_timeout,
                dump_messages=args.dump_palabra_messages,
            )
            agent = PalabraAgent(agent_settings, bridge)

            async with await connect_websocket(ws_url, token) as websocket:
                await agent.configure(websocket)
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
                agent_tasks = agent.start(websocket)
                send_task = agent.send_task
                receive_task = agent.receive_task
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
                tasks = set(agent_tasks)
                tasks.add(stop_task)
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
                            await agent.stop_gracefully(websocket)
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
    app = config_section(config, "app")
    translation = config_section(config, "translation")
    audio = config_section(config, "audio")
    zoom = config_section(config, "zoom")
    zoom_sdk = config_section(config, "zoom_sdk")
    bridge = config_section(config, "bridge")
    palabra = config_section(config, "palabra")
    diagnostics = config_section(config, "diagnostics")

    parser = argparse.ArgumentParser(
        description="Bridge Zoom audio through Palabra and play interpreted audio into Zoom."
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VERSION}")
    parser.add_argument(
        "--mode",
        choices=sorted(APP_MODES),
        default=config_string(app, "mode", DEFAULT_APP_MODE, "app.mode"),
        help="Runtime mode. Use cable for the current bridge or sdk-probe for Zoom SDK audio capture testing.",
    )
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
        "--zoom-sdk-meeting-number",
        default=config_string(zoom_sdk, "meeting_number", "", "zoom_sdk.meeting_number"),
        help="Zoom meeting number for SDK probe mode.",
    )
    parser.add_argument(
        "--zoom-sdk-password",
        default=config_string(zoom_sdk, "password", "", "zoom_sdk.password"),
        help="Zoom meeting password for SDK probe mode.",
    )
    parser.add_argument(
        "--zoom-sdk-display-name",
        default=config_string(
            zoom_sdk,
            "display_name",
            DEFAULT_ZOOM_SDK_DISPLAY_NAME,
            "zoom_sdk.display_name",
        ),
        help="Display name used by the SDK probe participant.",
    )
    parser.add_argument(
        "--zoom-sdk-probe-seconds",
        type=float,
        default=config_float(
            zoom_sdk,
            "probe_seconds",
            DEFAULT_ZOOM_SDK_PROBE_SECONDS,
            "zoom_sdk.probe_seconds",
        ),
        help="Seconds of SDK meeting audio to capture in sdk-probe mode.",
    )
    parser.add_argument(
        "--zoom-sdk-output-wav",
        default=config_string(
            zoom_sdk,
            "output_wav",
            DEFAULT_ZOOM_SDK_OUTPUT_WAV,
            "zoom_sdk.output_wav",
        ),
        help="WAV file written by sdk-probe mode.",
    )
    parser.add_argument(
        "--zoom-sdk-sample-rate",
        type=int,
        default=config_int(
            zoom_sdk,
            "sample_rate",
            DEFAULT_ZOOM_SDK_SAMPLE_RATE,
            "zoom_sdk.sample_rate",
        ),
        help="Expected SDK probe PCM sample rate.",
    )
    parser.add_argument(
        "--zoom-sdk-channels",
        type=int,
        choices=(1, 2),
        default=config_int(
            zoom_sdk,
            "channels",
            DEFAULT_ZOOM_SDK_CHANNELS,
            "zoom_sdk.channels",
            choices=(1, 2),
        ),
        help="Expected SDK probe PCM channel count.",
    )
    parser.add_argument(
        "--zoom-sdk-adapter-module",
        default=config_string(
            zoom_sdk,
            "adapter_module",
            DEFAULT_ZOOM_SDK_ADAPTER_MODULE,
            "zoom_sdk.adapter_module",
        ),
        help="Python module that wraps Zoom Meeting SDK raw audio callbacks.",
    )
    parser.add_argument(
        "--zoom-sdk-dry-run",
        action=argparse.BooleanOptionalAction,
        default=config_bool(
            zoom_sdk,
            "dry_run",
            DEFAULT_ZOOM_SDK_DRY_RUN,
            "zoom_sdk.dry_run",
        ),
        help="Generate a test WAV without joining Zoom; useful for verifying sdk-probe plumbing.",
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
        help=(
            "Local tempo algorithm. Use resample for stable live output; "
            "rubberband is experimental and should be checked with callback debug recordings."
        ),
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
        "--output-peak-limit",
        type=float,
        default=config_float(
            bridge,
            "output_peak_limit",
            DEFAULT_OUTPUT_PEAK_LIMIT,
            "bridge.output_peak_limit",
        ),
        help="Final callback peak limiter from 0.0 to 1.0 before Zoom's microphone cable.",
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
    if args.mode not in APP_MODES:
        allowed_modes = ", ".join(sorted(APP_MODES))
        raise SystemExit(f"app.mode must be one of: {allowed_modes}.")
    if args.zoom_sdk_probe_seconds <= 0:
        raise SystemExit("zoom_sdk.probe_seconds must be greater than 0.")
    if args.zoom_sdk_sample_rate <= 0:
        raise SystemExit("zoom_sdk.sample_rate must be greater than 0.")
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
    elif cli_args.mode == "sdk-probe":
        asyncio.run(run_sdk_probe(cli_args))
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
