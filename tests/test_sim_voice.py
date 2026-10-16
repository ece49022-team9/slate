import asyncio
import tempfile
import unittest
from array import array
from pathlib import Path
from types import SimpleNamespace

from slate.breadboard import Breadboard, mix
from slate.link import Link
from slate.voice.sim import Echo, SimCall


class EchoTests(unittest.TestCase):
    def test_mixing_clips_and_keeps_unmatched_samples(self):
        first = array("h", (30000, -30000, 7)).tobytes()
        second = array("h", (10000, -10000)).tobytes()
        self.assertEqual(array("h", mix(first, second)).tolist(), [32767, -32768, 7])

    def test_echo_resamples_scales_delays_and_preserves_chunk_boundaries(self):
        def run(chunks):
            result = bytearray()
            bench = SimpleNamespace(rate=16000, now=0, feed_echo=result.extend)
            echo = Echo(bench, 0.5)
            for pcm in chunks:
                echo.push(pcm)
            return result

        pcm = array("h", range(480)).tobytes()
        whole = run([pcm])
        self.assertEqual(run([pcm[:142], pcm[142:388], pcm[388:]]), whole)
        self.assertEqual(whole[:2560], bytes(2560))
        stereo = array("h", whole[2560:])
        self.assertEqual(len(stereo), 640)
        self.assertEqual(stereo[0::2], stereo[1::2])
        self.assertEqual(stereo[-1], 239)

    def test_pacing_does_not_refill_lead_when_browser_chunks_pause(self):
        bench = object.__new__(Breadboard)
        bench.uart = bytearray()
        bench.audio = bytearray(12800)
        bench.echo = bytearray()
        bench.rate = 16000
        bench.lead = 12800
        bench.buffered = 0
        bench.frames_drained = 0
        bench.audio_bytes = 0
        bench.now = 0
        bench.played = []
        bench.reply()
        bench.feed(bytes(12800))
        bench.now = 1_000_000
        bench.reply()
        self.assertEqual(bench.audio_bytes, 12864)
        bench.feed_echo(array("h", [7, 7]).tobytes())
        bench.now = 2_000_000
        self.assertIn(array("h", [7, 7]).tobytes(), bench.reply())


class SimCallTests(unittest.IsolatedAsyncioTestCase):
    async def test_call_uses_firmware_and_broadcasts_speaker(self):
        with tempfile.TemporaryDirectory() as directory:
            keys = bytearray()
            link = Link(keys.extend, Path(directory) / "serial.log")
            bench = SimpleNamespace(link=link, echo=bytearray())
            call = SimCall(bench, live=True, echo=0)
            queue = asyncio.Queue(maxsize=2)
            call.speakers.add(queue)
            await call.start()
            try:
                self.assertEqual(keys, b"ac")
                link.line("slate.live: started fixture")
                link.line("slate.live.heard: Hello")
                link.line("slate.live.heard:  Slate")
                link.line("slate.cloud.tool: hermes")
                status = await call.status()
                self.assertTrue(status["ready"])
                self.assertEqual(status["call_id"], "fixture")
                self.assertEqual(
                    status["words"], [{"role": "user", "text": "Hello Slate"}]
                )
                self.assertEqual(status["tools"], ["hermes"])
                link.speaker.put_nowait(b"\x00\x01")
                async with asyncio.timeout(1):
                    self.assertEqual(await queue.get(), b"\x00\x01")
                link.line("slate.live: ended 1.25s error=fixture")
                self.assertFalse((await call.status())["active"])
                self.assertEqual((await call.status())["error"], "fixture")
            finally:
                await call.stop()
                link.close()
            self.assertEqual(keys, b"ac0x")

    async def test_push_to_talk_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            keys = bytearray()
            link = Link(keys.extend, Path(directory) / "serial.log")
            call = SimCall(
                SimpleNamespace(link=link, echo=bytearray()), live=False, echo=0
            )
            await call.start()
            call.talk()
            call.release()
            await call.stop()
            link.close()
            self.assertEqual(keys, b"a130x")
