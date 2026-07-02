from __future__ import annotations

import dataclasses
from fractions import Fraction

import numpy as np
from scipy.signal import resample_poly

class PlaybackChunk:
    audio: np.ndarray
    segment_end: bool = False

def stream_blocksize(device_rate: int, block_ms: int) -> int:
    if block_ms <= 0:
        return 0
    return max(1, int(device_rate * block_ms / 1000))

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
