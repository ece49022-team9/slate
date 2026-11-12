import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from slate.api.events import EventBus
from slate.voice.audio import SAMPLE_RATE
from slate.voice.session import Hello, VoiceSession


async def no_text(chunks, timings=None):
    async for _ in chunks:
        pass
    if False:
        yield ""


async def says_hello(chunks, timings=None):
    async for _ in chunks:
        pass
    yield "hello there"


class DashboardStatusTests(unittest.IsolatedAsyncioTestCase):
    def session(self, events):
        socket = Mock(send_text=AsyncMock(), send_bytes=AsyncMock(), close=AsyncMock())
        with patch(
            "slate.voice.session.create_agent",
            return_value=Mock(pending=set(), close=AsyncMock()),
        ):
            return VoiceSession(socket, Hello(type="hello", rate=SAMPLE_RATE), events)

    def states(self, events):
        return [
            (e["data"]["state"], e["data"]["online"])
            for e in events.since(None)
            if e["type"] == "device.status"
        ]

    async def test_turn_with_no_speech_goes_listen_transcribe_idle(self):
        events = EventBus()
        session = self.session(events)
        with patch("slate.voice.session.transcribe_stream", no_text):
            await session.start_turn()
            task = session.turn_task
            await session.end_turn()
            await asyncio.wait_for(task, 2)
        await session.close()
        self.assertEqual(
            self.states(events),
            [
                ("listen", True),
                ("transcribe", True),
                ("idle", True),
                ("idle", False),
            ],
        )

    async def test_spoken_request_is_shown_then_respond(self):
        events = EventBus()
        session = self.session(events)
        session.respond = AsyncMock(return_value="hi")
        session.watch_background = Mock()
        with patch("slate.voice.session.transcribe_stream", says_hello):
            await session.start_turn()
            task = session.turn_task
            await session.end_turn()
            await asyncio.wait_for(task, 2)
        steps = [e for e in events.since(None) if e["type"] == "agent.step"]
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["data"]["title"], "hello there")
        self.assertEqual(steps[0]["device_id"], session.device_identity)
        self.assertEqual(
            [s for s, _ in self.states(events)],
            ["listen", "transcribe", "respond", "idle"],
        )
        await session.close()

    async def test_failure_reports_error(self):
        events = EventBus()
        session = self.session(events)

        async def broken(chunks, timings=None):
            raise RuntimeError("stt down")
            yield ""

        with patch("slate.voice.session.transcribe_stream", broken):
            await session.start_turn()
            task = session.turn_task
            await session.end_turn()
            await asyncio.wait_for(task, 2)
        self.assertIn(("error", True), self.states(events))
        await session.close()

    async def test_without_event_bus_nothing_breaks(self):
        session = self.session(None)
        with patch("slate.voice.session.transcribe_stream", no_text):
            await session.start_turn()
            task = session.turn_task
            await session.end_turn()
            await asyncio.wait_for(task, 2)
        await session.close()


if __name__ == "__main__":
    unittest.main()
