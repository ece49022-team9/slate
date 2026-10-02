import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
from livekit import api, rtc
from slate.app import app
from slate.voice.audio import MAX_AUDIO_SECONDS, SAMPLE_BYTES, SAMPLE_RATE
from slate.voice.session import VoiceSession
from slate.voice.settings import VoiceSettings
from slate.voice.turn import Turn


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


class VoiceAccessTests(unittest.TestCase):
    def test_device_token_is_scoped_to_one_room_and_microphone(self):
        settings = VoiceSettings("ws://127.0.0.1:7880", "key", "s" * 32)
        token = settings.token("room-a", "device-a")
        claims = api.TokenVerifier("key", "s" * 32).verify(token)
        self.assertEqual(claims.identity, "device-a")
        self.assertEqual(claims.video.room, "room-a")
        self.assertEqual(claims.video.can_publish_sources, ["microphone"])
        self.assertFalse(claims.video.room_admin)

    def test_requires_complete_livekit_configuration(self):
        with patch.dict("os.environ", {"LIVEKIT_URL": "wss://example.com"}, clear=True):
            with self.assertRaisesRegex(ValueError, "together"):
                VoiceSettings.from_env()

    def test_rejects_foreign_browser_origin(self):
        with TestClient(app, client=("127.0.0.1", 1234)) as client:
            response = client.post(
                "/api/voice/sessions",
                json={},
                headers={"Origin": "https://example.com"},
            )
        self.assertEqual(response.status_code, 403)

    def test_rejects_remote_session_creation(self):
        with TestClient(app, client=("192.0.2.1", 1234)) as client:
            response = client.post("/api/voice/sessions", json={})
        self.assertEqual(response.status_code, 403)

    def test_rejects_invalid_session_request_before_connecting(self):
        with TestClient(app, client=("127.0.0.1", 1234)) as client:
            response = client.post("/api/voice/sessions", json={"room": "other-room"})
        self.assertEqual(response.status_code, 422)

    def test_profile_option_is_forwarded_and_defaults_to_disabled(self):
        for body, profile in (
            ({}, False),
            ({"profile": False}, False),
            ({"profile": True}, True),
        ):
            with self.subTest(body=body):
                with TestClient(app, client=("127.0.0.1", 1234)) as client:
                    voice = Mock(
                        create=AsyncMock(
                            return_value={
                                "session_id": "session-a",
                                "server_url": "ws://127.0.0.1:7880",
                                "participant_token": "test-token",
                                "worker_identity": "worker-a",
                            }
                        ),
                        close=AsyncMock(),
                    )
                    app.state.voice = voice
                    response = client.post("/api/voice/sessions", json=body)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                voice.create.assert_awaited_once_with(profile=profile)

    def test_profile_requires_a_boolean_and_never_admits_extra_fields(self):
        for body in (
            {"profile": "true"},
            {"profile": 1},
            {"profile": None},
            {"profile": True, "room": "other-room"},
        ):
            with self.subTest(body=body):
                with TestClient(app, client=("127.0.0.1", 1234)) as client:
                    voice = Mock(
                        create=AsyncMock(
                            side_effect=ValueError("Connection attempted")
                        ),
                        close=AsyncMock(),
                    )
                    app.state.voice = voice
                    response = client.post("/api/voice/sessions", json=body)
                self.assertEqual(response.status_code, 422)
                voice.create.assert_not_awaited()


class VoiceStreamingTests(unittest.IsolatedAsyncioTestCase):
    def session(self):
        session = object.__new__(VoiceSession)
        session.profile = True
        session.closed = False
        session.device_identity = "device"
        session.turn_lock = asyncio.Lock()
        session.audio_ready = asyncio.Event()
        session.audio_ready.set()
        session.tasks = set()
        session.turn = Turn()
        session.turn_task = None
        session.room = Mock(local_participant=Mock(publish_data=AsyncMock()))
        session.speaker = Mock(
            capture_frame=AsyncMock(), wait_for_playout=AsyncMock(), clear_queue=Mock()
        )
        session.agent = Mock()
        return session

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
        self.assertEqual(session.speaker.capture_frame.await_count, 2)
        session.speaker.wait_for_playout.assert_awaited_once()
        packets = [
            json.loads(call.args[0])
            for call in session.room.local_participant.publish_data.await_args_list
        ]
        self.assertEqual(
            [packet["text"] for packet in packets], [first, first + second]
        )

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
        session.speaker.wait_for_playout.assert_not_awaited()

    async def test_abort_closes_tts_stream_cancels_producer_and_clears_audio(self):
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
        self.assertEqual(session.speaker.clear_queue.call_count, 2)
        count = session.room.local_participant.publish_data.await_count
        await session.publish(turn, text="late answer")
        await session.agent_progress(
            turn, {"type": "approval.request", "run_id": "late", "request_id": "late"}
        )
        await session.play(b"\x01\x00", turn)
        self.assertEqual(session.room.local_participant.publish_data.await_count, count)
        self.assertEqual(session.speaker.capture_frame.await_count, 1)
        cancelled = json.loads(
            session.room.local_participant.publish_data.await_args.args[0]
        )
        self.assertEqual(cancelled, {"turn_id": turn.id, "cancelled": True})

    async def test_start_invalidates_old_turn_before_waiting_for_producer_cleanup(self):
        session = self.session()
        old = session.turn
        started = asyncio.Event()
        cleanup_started = asyncio.Event()
        cleanup_done = asyncio.Event()

        async def producer():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await cleanup_done.wait()
                await session.publish(old, text="stale producer")

        session.turn_task = asyncio.create_task(producer())
        await started.wait()
        session.transcribe = AsyncMock()
        start = asyncio.create_task(
            session.start_turn(SimpleNamespace(caller_identity="device", payload=""))
        )
        await asyncio.wait_for(cleanup_started.wait(), 1)
        self.assertIsNone(session.turn)
        session.speaker.clear_queue.assert_called_once()
        self.assertFalse(start.done())
        cleanup_done.set()
        turn_id = await start
        self.assertEqual(session.turn.id, turn_id)
        packets = [
            json.loads(call.args[0])
            for call in session.room.local_participant.publish_data.await_args_list
        ]
        self.assertEqual(packets, [{"turn_id": old.id, "cancelled": True}])
        await session.turn_task

    async def test_new_turn_waits_for_native_capture_then_clears_committed_audio(self):
        session = self.session()
        old = session.turn
        submitted = asyncio.Event()
        release_native = asyncio.Event()
        first_clear = asyncio.Event()
        native_tasks = []
        queued = []
        order = []
        old_pcm = b"\x01\x00" * (SAMPLE_RATE // 50)
        new_pcm = b"\x02\x00" * (SAMPLE_RATE // 50)

        async def capture_frame(frame):
            pcm = frame.data.tobytes()

            async def native_capture():
                submitted.set()
                await release_native.wait()
                queued.append(pcm)
                order.append("commit")

            native = asyncio.create_task(native_capture())
            native_tasks.append(native)
            await asyncio.shield(native)

        def clear_queue():
            queued.clear()
            order.append("clear")
            first_clear.set()

        async def transcribe(turn):
            order.append("new")

        session.speaker.capture_frame = capture_frame
        session.speaker.clear_queue = clear_queue
        session.transcribe = transcribe
        old_task = session.turn_task = asyncio.create_task(
            session.play(old_pcm * 2, old)
        )
        start = None
        try:
            await asyncio.wait_for(submitted.wait(), 1)
            start = asyncio.create_task(
                session.start_turn(
                    SimpleNamespace(caller_identity="device", payload="")
                )
            )
            await asyncio.wait_for(first_clear.wait(), 1)
            self.assertIsNone(session.turn)
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(start), 0.01)
            old_task.cancel()
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(start), 0.01)
            self.assertEqual(order, ["clear"])
            self.assertFalse(native_tasks[0].done())

            release_native.set()
            turn_id = await asyncio.wait_for(start, 1)
            await session.turn_task
            self.assertEqual(session.turn.id, turn_id)
            self.assertTrue(old_task.cancelled())
            self.assertEqual(order, ["clear", "commit", "clear", "new"])
            self.assertEqual(queued, [])
            self.assertEqual(len(native_tasks), 1)
            await session.play(new_pcm, session.turn)
            self.assertEqual(queued, [new_pcm])
        finally:
            release_native.set()
            await asyncio.gather(old_task, return_exceptions=True)
            if start is not None:
                await asyncio.gather(start, return_exceptions=True)
            if session.turn_task is not None:
                await asyncio.gather(session.turn_task, return_exceptions=True)
            await asyncio.gather(*native_tasks, return_exceptions=True)

    async def test_stalled_capture_retires_session_and_unlocks_new_admission(self):
        session = self.session()
        session.close_done = asyncio.Event()
        session.agent.close = AsyncMock()
        session.room.disconnect = AsyncMock()
        session.speaker.aclose = AsyncMock()
        session.transcribe = AsyncMock()
        submitted = asyncio.Event()
        unsubscribed = asyncio.Event()

        async def capture(frame):
            submitted.set()
            try:
                await asyncio.Event().wait()
            finally:
                unsubscribed.set()

        session.speaker.capture_frame = AsyncMock(side_effect=capture)
        session.turn_task = session.spawn(session.play(b"\x01\x00", session.turn))
        await asyncio.wait_for(submitted.wait(), 1)
        with (
            patch("slate.voice.session.CAPTURE_DRAIN_SECONDS", 0.01),
            self.assertLogs("slate.voice.session", level="ERROR"),
        ):
            with self.assertRaises(rtc.RpcError) as caught:
                await asyncio.wait_for(
                    session.start_turn(
                        SimpleNamespace(caller_identity="device", payload="")
                    ),
                    1,
                )
            await asyncio.wait_for(session.close_done.wait(), 1)
        self.assertEqual(caught.exception.code, 1501)
        self.assertTrue(session.closed)
        self.assertIsNone(session.turn)
        self.assertFalse(session.turn_lock.locked())
        self.assertTrue(unsubscribed.is_set())
        session.speaker.aclose.assert_awaited_once()
        session.room.disconnect.assert_awaited_once()
        session.agent.close.assert_awaited_once()
        session.transcribe.assert_not_awaited()
        await session.play(b"\x02\x00", Turn())
        session.speaker.capture_frame.assert_awaited_once()

    async def test_close_retires_stalled_capture_once_without_waiting_forever(self):
        session = self.session()
        session.close_done = asyncio.Event()
        session.agent.close = AsyncMock()
        session.room.disconnect = AsyncMock()
        session.speaker.aclose = AsyncMock()
        submitted = asyncio.Event()

        async def capture(frame):
            submitted.set()
            await asyncio.Event().wait()

        session.speaker.capture_frame = AsyncMock(side_effect=capture)
        session.turn_task = session.spawn(session.play(b"\x01\x00", session.turn))
        await asyncio.wait_for(submitted.wait(), 1)
        with (
            patch("slate.voice.session.CAPTURE_DRAIN_SECONDS", 0.01),
            self.assertLogs("slate.voice.session", level="ERROR"),
        ):
            await asyncio.wait_for(session.close(), 1)
        self.assertTrue(session.closed)
        self.assertTrue(session.close_done.is_set())
        session.speaker.aclose.assert_awaited_once()
        session.room.disconnect.assert_awaited_once()
        session.agent.close.assert_awaited_once()

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
