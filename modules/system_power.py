from __future__ import annotations

import ctypes
import os

from modules.constants import ES_CONTINUOUS, ES_DISPLAY_REQUIRED, ES_SYSTEM_REQUIRED

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
