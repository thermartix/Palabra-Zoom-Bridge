from __future__ import annotations

import sys
import traceback
from datetime import datetime
from pathlib import Path

from modules.constants import CABLE_ROUTE_LOG_PATH, LAST_ERROR_LOG_PATH, LOG_DIR

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
