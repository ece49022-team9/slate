import struct
import tempfile
import unittest
import wave
from pathlib import Path

from slate.voice.firmware import RATE, stereo_pcm


class FirmwareAudioTests(unittest.TestCase):
    def wav(self, directory, channels, samples, rate=RATE, width=2):
        path = Path(directory) / "input.wav"
        with wave.open(str(path), "wb") as audio:
            audio.setparams((channels, width, rate, 0, "NONE", "not compressed"))
            audio.writeframes(samples)
        return path

    def test_mono_populates_left_slot_only(self):
        with tempfile.TemporaryDirectory() as directory:
            data = struct.pack("<hhh", 1000, -2000, 3000)
            result = stereo_pcm(self.wav(directory, 1, data))
        padding = RATE * 4 // 5
        self.assertEqual(
            result[padding:-padding], struct.pack("<hhhhhh", 1000, 0, -2000, 0, 3000, 0)
        )

    def test_mono_can_populate_the_right_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            data = struct.pack("<hh", 1000, -2000)
            result = stereo_pcm(self.wav(directory, 1, data), slot=1)
        padding = RATE * 4 // 5
        self.assertEqual(
            result[padding:-padding], struct.pack("<hhhh", 0, 1000, 0, -2000)
        )

    def test_stereo_preserves_channel_order(self):
        data = struct.pack("<hhhh", 1000, -2000, 3000, -4000)
        with tempfile.TemporaryDirectory() as directory:
            result = stereo_pcm(self.wav(directory, 2, data))
        padding = RATE * 4 // 5
        self.assertEqual(result[padding:-padding], data)

    def test_rejects_unsupported_or_truncated_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            for channels, rate, width in [(3, RATE, 2), (1, 8000, 2), (1, RATE, 1)]:
                with self.subTest(channels=channels, rate=rate, width=width):
                    path = self.wav(directory, channels, bytes(96), rate, width)
                    with self.assertRaisesRegex(ValueError, "16-bit WAV"):
                        stereo_pcm(path)
            path = self.wav(directory, 2, bytes(16))
            path.write_bytes(path.read_bytes()[:-2])
            with self.assertRaisesRegex(ValueError, "truncated"):
                stereo_pcm(path)
