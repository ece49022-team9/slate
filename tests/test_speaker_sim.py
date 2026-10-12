import importlib.util
import math
import struct
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "check_speaker", Path(__file__).resolve().parents[1] / "scripts/check_speaker.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SpeakerTests(unittest.TestCase):
    def tone(self, frequency=440, amplitude=20000, right=True):
        return b"".join(
            struct.pack("<hh", sample, sample if right else 0)
            for i in range(16000)
            for sample in [
                int(amplitude * math.sin(2 * math.pi * frequency * i / 16000))
            ]
        )

    def test_valid_tone_with_dma_startup_silence(self):
        frames, rms = module.check_tone(bytes(2048) + self.tone())
        self.assertEqual(frames, 15999)
        self.assertAlmostEqual(rms, 20000 / math.sqrt(2), delta=1)

    def test_rejects_missing_silent_truncated_or_incorrect_output(self):
        for pcm in (
            b"",
            bytes(64000),
            b"\x01",
            self.tone()[:4000],
            self.tone(frequency=880),
            self.tone(amplitude=10000),
            self.tone(right=False),
        ):
            with self.subTest(size=len(pcm)), self.assertRaises(ValueError):
                module.check_tone(pcm)

    def test_rejects_dropped_dma_frames(self):
        pcm = self.tone()
        with self.assertRaises(ValueError):
            module.check_tone(pcm[:4096] + pcm[4100:])


class SpeakerBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_dma_packets_reach_capture(self):
        import asyncio
        import tempfile
        from unittest.mock import patch

        from slate.breadboard import Breadboard, Ssd1351

        class Writer:
            def write(self, data):
                pass

            async def drain(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            with patch("slate.breadboard.SERIAL", Path(directory) / "serial.log"):
                bench = Breadboard(Ssd1351({}), 0, 16000, False)
            reader = asyncio.StreamReader()
            first = struct.pack("<hhhh", 1000, 1000, -2000, -2000)
            second = struct.pack("<hh", 3000, 3000)
            for ns, payload in [(100, first), (200, second)]:
                reader.feed_data(struct.pack("<cQH", b"P", ns, len(payload)) + payload)
            reader.feed_eof()
            try:
                with self.assertRaises(asyncio.IncompleteReadError):
                    await bench.wires(reader, Writer())
                self.assertEqual(bench.speaker_pcm, first + second)
                self.assertEqual(bench.speaker_blocks, [(100, 8), (200, 4)])
                self.assertEqual(bench.audio, b"")
            finally:
                bench.link.log.close()
