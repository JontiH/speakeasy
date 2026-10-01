import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from speakeasy import streaming
from speakeasy.streaming import SAMPLE_RATE, Streamer, quietest_cut, wav_data_offset

CFG = {"engine": "parakeet", "parakeet": {"max_chunk_seconds": 20, "stream": True}}
# ffmpeg's header: 78 bytes, with a LIST chunk before "data".
FFMPEG_HEADER = bytes.fromhex(
    "52494646ffffffff57415645666d74201000000001000100803e0000007d0000"
    "020010004c4953541a000000494e464f495346540e0000004c61766636302e31"
    "362e313030006461746100000000"
)


def speech(seconds: float, rng) -> np.ndarray:
    return (rng.standard_normal(int(seconds * SAMPLE_RATE)) * 8000).astype(np.int16)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * SAMPLE_RATE), dtype=np.int16)


class Helpers(unittest.TestCase):
    def test_finds_data_after_list_chunk(self):
        with tempfile.NamedTemporaryFile(suffix=".wav") as fh:
            fh.write(FFMPEG_HEADER)
            fh.flush()
            self.assertEqual(wav_data_offset(Path(fh.name)), 78)

    def test_cut_lands_in_the_pause(self):
        rng = np.random.default_rng(0)
        audio = np.concatenate([speech(12, rng), silence(0.5), speech(8, rng)])
        cut = quietest_cut(audio, 9 * SAMPLE_RATE, 18 * SAMPLE_RATE)
        self.assertTrue(12 * SAMPLE_RATE <= cut <= 12.5 * SAMPLE_RATE, cut / SAMPLE_RATE)


class StreamerTest(unittest.TestCase):
    """Feeds a growing WAV one second at a time, the way ffmpeg writes it."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.wav = Path(self.dir.name) / "record.wav"
        self.pid = 4242
        self.chunks = []
        patcher = mock.patch.object(streaming, "mark")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.dir.cleanup)

    def recognize(self, cfg, samples):
        self.chunks.append(len(samples))
        return f"<{len(self.chunks)}>"

    def record(self, audio: np.ndarray) -> Streamer:
        s = Streamer(self.wav, lambda: self.pid, lambda: CFG, self.recognize)
        self.wav.write_bytes(FFMPEG_HEADER)
        data = audio.astype("<i2").tobytes()
        for pos in range(0, len(data), 2 * SAMPLE_RATE):
            with self.wav.open("ab") as fh:
                fh.write(data[pos:pos + 2 * SAMPLE_RATE])
            s.poll()
        self.pid = None  # stop pressed
        return s

    def test_long_recording_is_cut_while_recording(self):
        rng = np.random.default_rng(1)
        audio = np.concatenate([speech(14, rng), silence(0.4), speech(14, rng), silence(0.4), speech(20, rng)])
        s = self.record(audio)
        during = len(self.chunks)
        self.assertGreaterEqual(during, 2)
        text = s.finish(self.wav)
        self.assertEqual(text, " ".join(f"<{i}>" for i in range(1, len(self.chunks) + 1)))
        self.assertEqual(sum(self.chunks), len(audio))  # every sample transcribed exactly once
        self.assertTrue(all(n <= 18 * SAMPLE_RATE for n in self.chunks))
        self.assertLess(self.chunks[-1], 18 * SAMPLE_RATE)

    def test_short_recording_is_one_tail(self):
        s = self.record(speech(5, np.random.default_rng(2)))
        self.assertEqual(self.chunks, [])
        self.assertEqual(s.finish(self.wav), "<1>")
        self.assertEqual(self.chunks, [5 * SAMPLE_RATE])

    def test_nothing_written_before_stop_still_streams(self):
        s = Streamer(self.wav, lambda: self.pid, lambda: CFG, self.recognize)
        s.poll()  # recording started, file not there yet
        self.wav.write_bytes(FFMPEG_HEADER + speech(3, np.random.default_rng(3)).tobytes())
        self.assertEqual(s.finish(self.wav), "<1>")

    def test_replaced_file_falls_back(self):
        s = self.record(speech(5, np.random.default_rng(4)))
        self.wav.unlink()
        self.wav.write_bytes(FFMPEG_HEADER + silence(1).tobytes())
        self.assertIsNone(s.finish(self.wav))

    def test_whisper_engine_is_not_streamed(self):
        s = Streamer(self.wav, lambda: self.pid, lambda: {**CFG, "engine": "whisper"}, self.recognize)
        self.wav.write_bytes(FFMPEG_HEADER + speech(25, np.random.default_rng(5)).tobytes())
        s.poll()
        self.assertEqual(self.chunks, [])
        self.assertIsNone(s.finish(self.wav))


if __name__ == "__main__":
    unittest.main()
