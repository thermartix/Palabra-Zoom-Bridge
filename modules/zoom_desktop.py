from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes
import sys
import time
from typing import Any, Optional

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

async def monitor_zoom_meeting_window(
    bridge: Any,
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
