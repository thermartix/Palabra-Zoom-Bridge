from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import threading
from typing import Awaitable, Callable


AudioCallback = Callable[[bytes, int, int], Awaitable[None] | None]


def _adapter_environment(settings) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "ZOOM_SDK_AUTH_TOKEN": settings.auth_token,
            "ZOOM_SDK_MEETING_NUMBER": settings.meeting_number,
            "ZOOM_SDK_PASSWORD": settings.password,
            "ZOOM_SDK_DISPLAY_NAME": settings.display_name,
            "ZOOM_SDK_PROBE_SECONDS": str(settings.probe_seconds),
            "ZOOM_SDK_SAMPLE_RATE": str(settings.sample_rate),
            "ZOOM_SDK_CHANNELS": str(settings.channels),
            "ZOOM_MEETING_SDK_ROOT": settings.sdk_root,
        }
    )
    return env


def _relay_stderr(process: subprocess.Popen[str]) -> None:
    assert process.stderr is not None
    for line in process.stderr:
        text = line.rstrip()
        if text:
            print(f"[zoom sdk adapter] {text}", flush=True)


async def run_probe(settings, audio_callback: AudioCallback) -> None:
    if not settings.adapter_command:
        raise SystemExit(
            "zoom_sdk.adapter_command is required when using modules.zoom_sdk_process_adapter."
        )

    command = [settings.adapter_command, *settings.adapter_args]
    print(f"Starting Zoom SDK process adapter: {settings.adapter_command}", flush=True)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        env=_adapter_environment(settings),
    )
    stderr_thread = threading.Thread(target=_relay_stderr, args=(process,), daemon=True)
    stderr_thread.start()

    assert process.stdout is not None
    try:
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"[zoom sdk adapter] {line}", flush=True)
                continue

            message_type = message.get("type")
            if message_type == "status":
                text = str(message.get("message", ""))
                if text:
                    print(f"[zoom sdk adapter] {text}", flush=True)
                continue
            if message_type == "done":
                break
            if message_type == "error":
                raise RuntimeError(str(message.get("message", "SDK adapter reported an error.")))
            if message_type != "audio":
                raise RuntimeError(f"SDK adapter emitted unknown message type: {message_type!r}")

            audio_b64 = message.get("pcm_s16le_base64")
            if not isinstance(audio_b64, str):
                raise RuntimeError("SDK adapter audio message is missing pcm_s16le_base64.")
            sample_rate = int(message.get("sample_rate", settings.sample_rate))
            channels = int(message.get("channels", settings.channels))
            result = audio_callback(base64.b64decode(audio_b64), sample_rate, channels)
            if asyncio.iscoroutine(result):
                await result

        return_code = process.wait(timeout=5)
        if return_code != 0:
            raise RuntimeError(f"SDK adapter exited with code {return_code}.")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        stderr_thread.join(timeout=1)
