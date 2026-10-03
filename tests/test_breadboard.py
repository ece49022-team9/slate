import asyncio
import struct
import tempfile
import unittest
from array import array
from pathlib import Path
from unittest.mock import Mock, patch

from slate.breadboard import Breadboard


def stereo(*frames: tuple[int, int]) -> bytes:
    return array("h", [sample for frame in frames for sample in frame]).tobytes()


def audio(message: bytes) -> list[int]:
    samples = []
    while message:
        kind, size = struct.unpack("<cH", message[:3])
        if kind == b"A":
            samples += array("h", message[3 : 3 + size])
        message = message[3 + size :]
    return samples


class MixerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        serial = patch("slate.breadboard.SERIAL", Path(directory.name) / "serial.log")
        serial.start()
        self.addCleanup(serial.stop)
        self.bench = Breadboard(Mock(), slot=0, rate=16_000, realtime=False)
        self.addCleanup(self.bench.link.close)

    async def test_echo_is_added_to_the_voice_at_the_same_moment(self):
        self.bench.feed(stereo((1000, 0), (-1000, 0), (30000, 0)))
        self.bench.feed_echo(stereo((500, 500), (500, 500), (5000, 5000)))
        self.assertEqual(audio(self.bench.reply()), [1500, 500, -500, 500, 32767, 5000])

    async def test_a_played_clip_finishes_while_echo_keeps_going(self):
        self.bench.feed_echo(stereo(*[(100, 100)] * 4000))
        played = asyncio.ensure_future(self.bench.play(stereo((1000, 0))))
        await asyncio.sleep(0)
        self.bench.reply()
        await asyncio.wait_for(played, 1)
        self.assertTrue(self.bench.echo)

    async def test_a_trickling_source_never_builds_a_burst_bigger_than_the_lead(self):
        self.bench.feed(stereo((1, 1)))
        for _ in range(5000):
            self.bench.now += 1_000_000
            self.bench.feed_echo(stereo((2, 2)))
            self.bench.reply()
        self.bench.now += 1_000_000
        self.bench.feed(stereo(*[(3, 3)] * 16_000))
        burst = len(audio(self.bench.reply())) * 2
        self.assertLessEqual(burst, self.bench.lead)

    async def test_extra_asks_within_a_tick_do_not_send_audio_early(self):
        self.bench.feed_echo(stereo(*[(4, 4)] * 16_000))
        sent = sum(len(audio(self.bench.reply())) * 2 for _ in range(100))
        self.assertEqual(sent, self.bench.lead)
        self.bench.now += 10_000_000
        self.assertEqual(len(audio(self.bench.reply())) * 2, 16_000 * 4 // 100)
