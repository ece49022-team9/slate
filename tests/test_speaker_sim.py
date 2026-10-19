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

    def test_requires_silence_after_playback(self):
        module.check_silence(self.tone() + bytes(16000))
        for tail in (b"", bytes(100), self.tone(), bytes(16000) + self.tone()):
            with self.subTest(size=len(tail)), self.assertRaises(ValueError):
                module.check_silence(self.tone() + tail)

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


class LiveSpeakerTests(unittest.TestCase):
    def test_live_capture_is_bounded_and_listeners_receive_only_new_pcm(self):
        import asyncio
        import tempfile
        from unittest.mock import patch

        from slate.breadboard import Breadboard, Ssd1351

        with tempfile.TemporaryDirectory() as directory:
            with patch("slate.breadboard.SERIAL", Path(directory) / "serial.log"):
                bench = Breadboard(Ssd1351({}), 0, 16000, True)
            try:
                bench.capture_speaker(0, bytes(4))
                slow = asyncio.Queue(maxsize=2)
                fast = asyncio.Queue(maxsize=2)
                bench.speaker_listeners.update((slow, fast))
                for ns in range(10):
                    payload = struct.pack("<hh", ns, ns)
                    bench.capture_speaker(ns, payload)
                    self.assertEqual(fast.get_nowait(), payload)
                self.assertLessEqual(slow.qsize(), 2)
                self.assertEqual(slow.get_nowait(), struct.pack("<hh", 8, 8))
                self.assertEqual(slow.get_nowait(), struct.pack("<hh", 9, 9))
                self.assertEqual(bench.speaker_pcm, b"")
                self.assertEqual(bench.speaker_blocks, [])
                bench.speaker_listeners.clear()
                bench.capture_speaker(11, bytes(4))
                self.assertTrue(fast.empty())
            finally:
                bench.link.log.close()


class SpeakerSocketTests(unittest.TestCase):
    def test_socket_streams_pcm_and_unsubscribes_on_disconnect(self):
        from contextlib import asynccontextmanager
        from types import SimpleNamespace
        from unittest.mock import patch

        from fastapi.testclient import TestClient
        from slate.display import app

        pcm = struct.pack("<hhhh", 1000, -1000, 2000, -2000)

        class Listeners(set):
            def add(self, queue):
                super().add(queue)
                queue.put_nowait(pcm)

        listeners = Listeners()

        @asynccontextmanager
        async def lifespan(app):
            app.state.bench = SimpleNamespace(speaker_listeners=listeners)
            yield

        with patch.object(app.router, "lifespan_context", lifespan):
            with TestClient(app) as client:
                for _ in range(2):
                    with client.websocket_connect("/speaker") as socket:
                        self.assertEqual(socket.receive_bytes(), pcm)
                        self.assertEqual(len(listeners), 1)
                    self.assertEqual(len(listeners), 0)
                response = client.get("/speaker-worklet.js")
                self.assertEqual(response.status_code, 200)
                self.assertIn("registerProcessor", response.text)


class BufferedSpeakerTestTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_waits_for_complete_tone_and_cleans_up(self):
        import asyncio
        from types import SimpleNamespace

        from slate.display import speaker_test

        tone = SpeakerTests().tone()
        listeners = set()
        sent = []
        tasks = []

        async def emit():
            # Ignore leading silence and collect the entire tone despite gaps.
            for packet in (bytes(512), tone[:512], tone[512:], bytes(16000)):
                await asyncio.sleep(0.01)
                for queue in listeners:
                    queue.put_nowait(packet)

        def send(text):
            sent.append(text)
            tasks.append(asyncio.create_task(emit()))

        bench = SimpleNamespace(
            speaker_listeners=listeners, link=SimpleNamespace(type=send)
        )
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(bench=bench))
        )
        response = await speaker_test(request)
        self.assertEqual(response.body, tone + bytes(16000))
        self.assertEqual(sent, ["t"])
        self.assertEqual(listeners, set())
        self.assertFalse(bench.speaker_testing)
        await asyncio.gather(*tasks)

    async def test_concurrent_test_rejected_and_cancel_removes_listener(self):
        import asyncio
        from types import SimpleNamespace

        from fastapi import HTTPException
        from slate.display import speaker_test

        bench = SimpleNamespace(
            speaker_listeners=set(), link=SimpleNamespace(type=lambda text: None)
        )
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(bench=bench))
        )
        pending = asyncio.create_task(speaker_test(request))
        await asyncio.sleep(0)
        with self.assertRaises(HTTPException) as caught:
            await speaker_test(request)
        self.assertEqual(caught.exception.status_code, 409)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(bench.speaker_listeners, set())
        self.assertFalse(bench.speaker_testing)
