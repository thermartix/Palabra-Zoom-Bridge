from __future__ import annotations

import unittest

import numpy as np

from modules.transports.cable import AudioBridge


class PalabraManagedTempoTests(unittest.TestCase):
    def test_palabra_mode_skips_local_tempo_adjustment(self) -> None:
        bridge = AudioBridge.__new__(AudioBridge)
        bridge.playback_tempo_algorithm = "palabra"
        audio = np.array([100, -100, 200, -200], dtype=np.int16)

        prepared = bridge._prepare_tempo_playback_chunk(
            audio,
            segment_end=False,
        )

        self.assertIs(prepared, audio)


if __name__ == "__main__":
    unittest.main()
