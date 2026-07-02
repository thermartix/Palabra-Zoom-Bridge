from __future__ import annotations

from pathlib import Path
from typing import Optional
import tomllib

from modules.constants import *

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
    if args.output_peak_limit < 0.0 or args.output_peak_limit > 1.0:
        raise SystemExit("bridge.output_peak_limit must be between 0.0 and 1.0.")
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
