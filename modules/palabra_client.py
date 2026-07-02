from __future__ import annotations

import asyncio
import json
import time
from typing import Optional
from urllib.parse import quote

import httpx
import websockets

from modules.constants import (
    LANGUAGE_NAMES,
    PALABRA_OUTPUT_CHANNELS,
    PALABRA_OUTPUT_RATE,
    SESSION_URL,
    SESSIONS_URL,
)

class PalabraRuntimeError(RuntimeError):
    pass


def language_label(language_code: str) -> str:
    return LANGUAGE_NAMES.get(language_code.lower(), language_code)

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
