from __future__ import annotations

from typing import Protocol


class AudioTransport(Protocol):
    """Minimal shape shared by cable and future Zoom SDK transports."""

    def request_stop(self, reason: str) -> None: ...

    def playback_is_drained(self) -> bool: ...

    def playback_pending_samples(self) -> int: ...
