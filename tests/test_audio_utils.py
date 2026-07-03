from __future__ import annotations

import unittest

import numpy as np

from modules.audio_utils import PlaybackChunk


class PlaybackChunkTests(unittest.TestCase):
    def test_constructs_with_audio_and_segment_flag(self) -> None:
        audio = np.array([1, -1, 2, -2], dtype=np.int16)

        chunk = PlaybackChunk(audio, segment_end=True)

        self.assertIs(chunk.audio, audio)
        self.assertTrue(chunk.segment_end)


if __name__ == "__main__":
    unittest.main()
