import asyncio
import json
import os
import unittest
from contextlib import contextmanager
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi.testclient import TestClient
from slate.agent.agent import BACKGROUND_PROMPT
from slate.app import app
from slate.device import OrbRequest
from slate.voice.audio import MAX_AUDIO_SECONDS, SAMPLE_BYTES, SAMPLE_RATE
from slate.voice.session import Hello, VoiceSession
from slate.voice.turn import Turn
from starlette.websockets import WebSocketDisconnect

TOKEN = "device-token-for-tests"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class TurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_keeps_audio_in_order_and_ignores_late_frames(self):
        turn = Turn()
        turn.push(b"\x01\x00")
        turn.push(b"\x02\x00")
        turn.finish()
        turn.push(b"\x03\x00")
        turn.finish()
        async with asyncio.timeout(1):
            audio = b"".join([chunk async for chunk in turn.chunks()])
        self.assertEqual(audio, b"\x01\x00\x02\x00")

    async def test_limits_audio_in_memory(self):
        turn = Turn()
        limit = MAX_AUDIO_SECONDS * SAMPLE_RATE * SAMPLE_BYTES
        turn.push(bytes(limit))
        with self.assertRaisesRegex(ValueError, "limited"):
            turn.push(b"\x00\x00")
        async with asyncio.timeout(1):
            size = sum([len(chunk) async for chunk in turn.chunks()])
        self.assertEqual(size, limit)

    async def test_rejects_partial_samples(self):
        turn = Turn()
        with self.assertRaisesRegex(ValueError, "complete"):
            turn.push(b"\x00")


class FakeAgent:
    """Stands in for Hermes at its HTTP boundary: it streams a reply and, when
    asked, controls the device through the public scoped device route."""

    def __init__(self, reply="Twelve.", control=False) -> None:
        self.reply = reply
        self.control = control
        self.pending = set()
        self.timings = {}
        self.last_run = {}
        self.messages = []
        self.receipts = []

    async def run(self, message, progress, *, device_context):
        self.messages.append(message)
        if self.control:
            scope = device_context.split("opaque scope is ", 1)[1].split(".", 1)[0]
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://slate"
            ) as client:
                response = await client.post(
                    "/api/device/set_orb",
                    json={"color": "#112233", "radius": 30},
                    headers={"Authorization": f"Bearer {scope}"},
                )
                self.receipts.append((response.status_code, response.json()))
        await progress({"type": "reply.delta", "delta": self.reply})
        await progress({"type": "answer.complete", "text": self.reply})
        return self.reply

    async def close(self):
        pass


@contextmanager
def cloud(agent, heard=None, said=None):
    async def transcribe(audio, *, timings):
        pcm = b"".join([chunk async for chunk in audio])
        if heard is not None:
            heard.append(pcm)
        yield "What is seven plus five?"

    async def speak(text, *, timings):
        if said is not None:
            said.append(text)
        yield b"\x40\x10" * (SAMPLE_RATE // 50)

    with (
        patch.dict(os.environ, {"SLATE_DEVICE_TOKEN": TOKEN}),
        patch("slate.voice.session.create_agent", return_value=agent),
        patch("slate.voice.session.transcribe_stream", transcribe),
        patch("slate.voice.session.speak_stream", speak),
        TestClient(app) as client,
    ):
        yield client


def receive_until_final(socket, receipt=None):
    events, audio = [], b""
    while True:
        message = socket.receive()
        if message.get("bytes") is not None:
            audio += message["bytes"]
            continue
        event = json.loads(message["text"])
        events.append(event)
        if event["type"] == "command" and receipt is not None:
            socket.send_text(json.dumps(receipt(event)))
        if event["type"] == "reply" and event["final"]:
            return events, audio


class DeviceSocketTests(unittest.TestCase):
    def test_rejects_missing_or_wrong_device_token(self):
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            with self.subTest(headers=headers), cloud(FakeAgent()) as client:
                with self.assertRaises(WebSocketDisconnect) as caught:
                    with client.websocket_connect(
                        "/api/device/socket", headers=headers
                    ):
                        pass
                self.assertEqual(caught.exception.code, 4401)

    def test_requires_hello_before_anything_else(self):
        with cloud(FakeAgent()) as client:
            with client.websocket_connect("/api/device/socket", headers=AUTH) as socket:
                socket.send_text(json.dumps({"type": "start"}))
                with self.assertRaises(WebSocketDisconnect) as caught:
                    socket.receive_text()
        self.assertEqual(caught.exception.code, 4400)

    def test_browser_can_authenticate_with_a_subprotocol(self):
        with cloud(FakeAgent()) as client:
            with client.websocket_connect(
                "/api/device/socket", subprotocols=["slate", TOKEN]
            ) as socket:
                self.assertEqual(socket.accepted_subprotocol, "slate")

    def test_spoken_turn_resamples_device_audio_and_streams_speech_back(self):
        agent, heard, said = FakeAgent(), [], []
        with cloud(agent, heard, said) as client:
            with client.websocket_connect("/api/device/socket", headers=AUTH) as socket:
                socket.send_text(json.dumps({"type": "hello", "rate": 16000}))
                socket.send_text(json.dumps({"type": "start"}))
                turn = json.loads(socket.receive_text())
                self.assertEqual(turn["type"], "turn")
                for _ in range(10):
                    socket.send_bytes(b"\x10\x00" * 320)
                socket.send_text(json.dumps({"type": "end"}))
                events, audio = receive_until_final(socket)
        self.assertEqual(agent.messages, ["What is seven plus five?"])
        self.assertEqual(said, ["Twelve."])
        self.assertEqual(audio, b"\x40\x10" * (SAMPLE_RATE // 50))
        self.assertAlmostEqual(len(heard[0]) / (10 * 640), 1.5, delta=0.1)
        self.assertTrue(all(event["turn_id"] == turn["turn_id"] for event in events))
        transcript = [e for e in events if e["type"] == "transcript" and e["final"]]
        self.assertEqual(transcript[0]["text"], "What is seven plus five?")
        self.assertEqual(events[-1], {**events[-1], "text": "Twelve.", "final": True})

    def test_agent_controls_the_device_through_the_scoped_route(self):
        agent = FakeAgent(control=True)

        def receipt(command):
            self.assertEqual(command["operation"], "set_orb")
            self.assertEqual(command["arguments"], {"color": "#112233", "radius": 30.0})
            return {
                "type": "receipt",
                "request_id": command["request_id"],
                "operation": "set_orb",
                "revision": 7,
                "state": 4,
                "color": "#112233",
                "radius": 30.0,
                "text": "",
                "custom": True,
            }

        with cloud(agent) as client:
            with client.websocket_connect("/api/device/socket", headers=AUTH) as socket:
                socket.send_text(json.dumps({"type": "hello", "rate": 24000}))
                socket.send_text(json.dumps({"type": "start"}))
                socket.receive_text()
                socket.send_bytes(b"\x10\x00" * 480)
                socket.send_text(json.dumps({"type": "end"}))
                receive_until_final(socket, receipt)
            stale = client.get(
                "/api/device/status", headers={"Authorization": "Bearer stale-scope"}
            )
        self.assertEqual(agent.receipts[0][0], 200)
        self.assertEqual(agent.receipts[0][1]["revision"], 7)
        self.assertEqual(stale.status_code, 409)

    def test_new_connection_replaces_the_old_device(self):
        with cloud(FakeAgent()) as client:
            with client.websocket_connect("/api/device/socket", headers=AUTH) as first:
                first.send_text(json.dumps({"type": "hello"}))
                with client.websocket_connect(
                    "/api/device/socket", headers=AUTH
                ) as second:
                    second.send_text(json.dumps({"type": "hello"}))
                    with self.assertRaises(WebSocketDisconnect) as caught:
                        first.receive_text()
                    self.assertEqual(caught.exception.code, 4000)
                    second.send_text(json.dumps({"type": "start"}))
                    self.assertEqual(json.loads(second.receive_text())["type"], "turn")


class VoiceStreamingTests(unittest.IsolatedAsyncioTestCase):
    def session(self, profile=True):
        socket = Mock(send_text=AsyncMock(), send_bytes=AsyncMock(), close=AsyncMock())
        with patch(
            "slate.voice.session.create_agent", return_value=Mock(pending=set())
        ):
            session = VoiceSession(
                socket, Hello(type="hello", rate=SAMPLE_RATE, profile=profile)
            )
        session.turn = Turn()
        return session

    def events(self, session):
        return [
            json.loads(call.args[0])
            for call in session.socket.send_text.await_args_list
        ]

    async def test_streams_speech_before_generation_completes_without_duplicates(
        self,
    ):
        session = self.session()
        spoken = []
        synthesis_started = asyncio.Event()
        first = " \nThis is the first spoken sentence. "
        second = "This is the second spoken sentence.\n "

        async def generate(message, progress, *, device_context):
            self.assertIn(session.turn.scope, device_context)
            await progress({"type": "reply.delta", "delta": first})
            await asyncio.wait_for(synthesis_started.wait(), 1)
            await progress({"type": "reply.delta", "delta": second})
            await progress(
                {"type": "answer.complete", "text": (first + second).strip()}
            )
            return (first + second).strip()

        async def speak(text, *, timings):
            spoken.append(text)
            synthesis_started.set()
            yield b"\x01\x00" * (SAMPLE_RATE // 50)

        session.agent.run = generate
        with patch("slate.voice.session.speak_stream", speak):
            answer = await session.respond(session.turn, "request", [])
        self.assertEqual(answer, (first + second).strip())
        self.assertEqual(spoken, [first.strip(), second.strip()])
        self.assertEqual(session.socket.send_bytes.await_count, 2)
        self.assertEqual(
            [event["text"] for event in self.events(session)], [first, first + second]
        )

    async def reply_with_details(self, streamed: bool) -> tuple[list, list, str]:
        session = self.session()
        spoken = []
        summary = "Paris is the capital of France."
        full = f"{summary}\n---\nIt has about two million residents."

        async def generate(message, progress, *, device_context):
            if streamed:
                for delta in (summary + "\n-", "--\n", full.split("\n", 2)[2]):
                    await progress({"type": "reply.delta", "delta": delta})
                await progress({"type": "answer.complete", "text": full})
            return full

        async def speak(text, *, timings):
            spoken.append(text)
            yield b"\x01\x00"

        session.agent.run = generate
        with patch("slate.voice.session.speak_stream", speak):
            self.assertEqual(await session.respond(session.turn, "request", []), full)
        return spoken, self.events(session), full

    async def test_speaks_the_summary_and_shows_the_details(self):
        for streamed in (True, False):
            with self.subTest(streamed=streamed):
                spoken, events, full = await self.reply_with_details(streamed)
                self.assertEqual(spoken, ["Paris is the capital of France."])
                self.assertEqual(events[-1]["text"], full)

    async def test_background_result_is_announced_once_the_user_turn_ends(self):
        session = self.session()
        user_turn = session.turn
        finished = asyncio.Event()
        session.agent.pending = {"deleg_a"}

        async def background_finished():
            session.agent.pending.clear()
            finished.set()
            return {"deleg_a"}

        session.agent.background_finished = background_finished
        session.agent.run = AsyncMock(
            return_value="The 30th Fibonacci number is 832040."
        )
        spoken = []

        async def speak(text, *, timings):
            spoken.append(text)
            yield b"\x01\x00"

        with patch("slate.voice.session.speak_stream", speak):
            session.watch_background()
            await asyncio.wait_for(finished.wait(), 1)
            await asyncio.sleep(0.3)
            self.assertIs(session.turn, user_turn)
            session.agent.run.assert_not_awaited()
            session.turn = None
            await asyncio.wait_for(session.watcher, 2)
        self.assertEqual(session.agent.run.await_args.args[0], BACKGROUND_PROMPT)
        self.assertEqual(spoken, ["The 30th Fibonacci number is 832040."])
        events = self.events(session)
        self.assertTrue(all(event["announcement"] for event in events))
        self.assertTrue(events[-1]["final"])
        self.assertIsNone(session.turn)

    async def test_completion_only_provider_synthesizes_once(self):
        session = self.session()
        answer = "This provider only delivers a complete answer."
        session.agent.run = AsyncMock(return_value=answer)
        spoken = []

        async def speak(text, *, timings):
            spoken.append(text)
            yield b"\x01\x00"

        with patch("slate.voice.session.speak_stream", speak):
            self.assertEqual(await session.respond(session.turn, "request", []), answer)
        self.assertEqual(spoken, [answer])

    async def test_synthesis_failure_cancels_the_agent_producer(self):
        session = self.session()
        producer_cancelled = asyncio.Event()

        async def generate(message, progress, *, device_context):
            try:
                await progress(
                    {
                        "type": "reply.delta",
                        "delta": "A sentence long enough to speak. ",
                    }
                )
                await asyncio.Event().wait()
            finally:
                producer_cancelled.set()

        async def speak(text, *, timings):
            raise ConnectionError("TTS stream disconnected")
            yield b""

        session.agent.run = generate
        with patch("slate.voice.session.speak_stream", speak):
            with self.assertRaises(ExceptionGroup) as caught:
                await session.respond(session.turn, "request", [])
        self.assertIsInstance(caught.exception.exceptions[0], ConnectionError)
        self.assertTrue(producer_cancelled.is_set())

    async def test_abort_closes_tts_stream_and_stops_sending(self):
        session = self.session()
        turn = session.turn
        synthesis_started = asyncio.Event()
        producer_cancelled = asyncio.Event()
        stream_closed = asyncio.Event()

        async def generate(message, progress, *, device_context):
            try:
                await progress(
                    {
                        "type": "reply.delta",
                        "delta": "A sentence long enough to speak. ",
                    }
                )
                await asyncio.Event().wait()
            finally:
                producer_cancelled.set()

        async def speak(text, *, timings):
            try:
                yield b"\x01\x00"
                synthesis_started.set()
                await asyncio.Event().wait()
            finally:
                stream_closed.set()

        session.agent.run = generate
        with patch("slate.voice.session.speak_stream", speak):
            session.turn_task = asyncio.create_task(
                session.respond(turn, "request", [])
            )
            await asyncio.wait_for(synthesis_started.wait(), 1)
            await session.abort()
        self.assertIsNone(session.turn)
        self.assertTrue(producer_cancelled.is_set())
        self.assertTrue(stream_closed.is_set())
        sent = len(self.events(session))
        await session.publish(turn, "reply", text="late answer")
        await session.play(b"\x01\x00", lambda: session.turn is turn)
        self.assertEqual(len(self.events(session)), sent)
        self.assertEqual(session.socket.send_bytes.await_count, 1)
        self.assertEqual(
            self.events(session)[-1], {"type": "cancelled", "turn_id": turn.id}
        )

    async def test_start_cancels_the_old_turn_before_the_new_one_begins(self):
        session = self.session()
        old = session.turn
        cleanup_started = asyncio.Event()
        cleanup_done = asyncio.Event()

        async def producer():
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await cleanup_done.wait()
                await session.publish(old, "reply", text="stale producer")

        session.turn_task = asyncio.create_task(producer())
        await asyncio.sleep(0)
        session.transcribe = AsyncMock()
        start = asyncio.create_task(session.start_turn())
        await asyncio.wait_for(cleanup_started.wait(), 1)
        self.assertIsNone(session.turn)
        self.assertFalse(start.done())
        cleanup_done.set()
        await start
        events = self.events(session)
        self.assertEqual(events[0], {"type": "cancelled", "turn_id": old.id})
        self.assertEqual(events[1], {"type": "turn", "turn_id": session.turn.id})
        await session.turn_task

    async def test_device_command_waits_for_the_matching_receipt(self):
        session = self.session()
        device = session.device_sdk(session.turn)

        async def reply_to_command():
            while not session.receipts:
                await asyncio.sleep(0)
            command = self.events(session)[-1]
            await session.control(
                {"type": "receipt", "request_id": "f" * 32, "error": "late"}
            )
            await session.control(
                {
                    "type": "receipt",
                    "request_id": command["request_id"],
                    "operation": "set_orb",
                    "revision": 3,
                    "state": 4,
                    "color": "#00ff00",
                    "radius": 20.0,
                    "text": "",
                    "custom": True,
                }
            )

        responder = asyncio.create_task(reply_to_command())
        with self.assertLogs("slate.voice.session", level="WARNING"):
            receipt = await device.set_orb(OrbRequest(color="#00ff00", radius=20))
        await responder
        self.assertEqual(receipt.revision, 3)
        self.assertEqual(session.receipts, {})

    async def test_rejected_device_command_raises(self):
        session = self.session()
        device = session.device_sdk(session.turn)

        async def reject():
            while not session.receipts:
                await asyncio.sleep(0)
            (request_id,) = session.receipts
            await session.control(
                {"type": "receipt", "request_id": request_id, "error": "invalid"}
            )

        responder = asyncio.create_task(reject())
        with self.assertRaisesRegex(RuntimeError, "rejected"):
            await device.get_status()
        await responder

    async def test_final_answer_mismatch_closes_active_tts_consumer(self):
        session = self.session()
        synthesis_started = asyncio.Event()
        stream_closed = asyncio.Event()

        async def generate(message, progress, *, device_context):
            await progress(
                {
                    "type": "reply.delta",
                    "delta": "This sentence will already be spoken. ",
                }
            )
            await synthesis_started.wait()
            await progress({"type": "answer.complete", "text": "A different answer."})
            return "A different answer."

        async def speak(text, *, timings):
            try:
                synthesis_started.set()
                await asyncio.Event().wait()
                yield b""
            finally:
                stream_closed.set()

        session.agent.run = generate
        with patch("slate.voice.session.speak_stream", speak):
            with self.assertRaises(ExceptionGroup) as caught:
                await session.respond(session.turn, "request", [])
        self.assertRegex(str(caught.exception.exceptions[0]), "differs from spoken")
        self.assertTrue(stream_closed.is_set())


if __name__ == "__main__":
    unittest.main()
