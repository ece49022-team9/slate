import asyncio
import copy
import json
import unittest
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from livekit import rtc
from slate.voice.audio import SAMPLE_RATE, write_wav
from slate.voice.device import transcribe_audio
from slate.voice.session import VoiceSession
from slate.voice.settings import REPLY_TOPIC, TRANSCRIPT_TOPIC
from slate.voice.timing import Timeline, milliseconds
from slate.voice.turn import Turn

from scripts.profile_voice import summarize


def profile_fixture() -> dict:
    def clock(**marks: int) -> dict:
        return {
            "host": "local-host",
            "marks_ns": {key: value * 1_000_000 for key, value in marks.items()},
        }

    return {
        "device": clock(
            end_requested=1000, end_acknowledged=1001, reply_first_audible=2000
        ),
        "server": clock(
            end_requested=1002,
            input_finished=1302,
            stt_requested=500,
            stt_first_text=600,
            stt_completed=1400,
            agent_requested=1410,
            agent_completed=1500,
            tts_requested=1510,
            tts_completed=1900,
        ),
        "agent": {
            key: value * 1_000_000
            for key, value in {
                "requested": 1410,
                "admitted": 1420,
                "first_text": 1430,
                "last_text": 1490,
                "completed": 1500,
            }.items()
        },
        "simulator": clock(build_requested=0, build_completed=20, board_ready=25),
        "stt": {
            "remote": {"tail_decode_ms": 10.0, "tail_decode_after_first_text_ms": 5.0}
        },
        "tts": {"remote": {"generate_ms": 100.0, "first_token_ms": 20.0}},
    }


class ProfileReportTests(unittest.TestCase):
    def test_cross_host_timestamps_are_rejected_before_subtraction(self):
        timing = profile_fixture()
        timing["server"]["host"] = "different-host"
        with self.assertRaisesRegex(ValueError, "one host"):
            summarize(timing)

    def test_unknown_ttft_stays_unavailable_and_prevents_retained_estimate(self):
        for value in ("missing", None):
            with self.subTest(value=value):
                timing = profile_fixture()
                if value == "missing":
                    timing["agent"].pop("first_text")
                else:
                    timing["agent"]["first_text"] = None
                report = summarize(timing)
                self.assertIsNone(report["agent_ttft_ms"])
                self.assertIsNone(report["agent_text_delivery_ms"])
                self.assertIsNone(report["retained_ttft_ms"])
                self.assertEqual(report["end_to_audible_ms"], 1000.0)

    def test_missing_critical_path_endpoints_leave_only_affected_metrics_unavailable(
        self,
    ):
        for stage, name, metric in (
            ("device", "end_requested", "end_to_audible_ms"),
            ("server", "stt_completed", "end_to_stt_ms"),
            ("server", "tts_completed", "tts_to_audible_ms"),
            ("server", "tts_requested", "tts_total_ms"),
        ):
            for missing in (True, False):
                with self.subTest(stage=stage, name=name, missing=missing):
                    timing = profile_fixture()
                    if missing:
                        timing[stage]["marks_ns"].pop(name)
                    else:
                        timing[stage]["marks_ns"][name] = None
                    report = summarize(timing)
                    self.assertIsNone(report[metric])
                    if metric == "tts_total_ms":
                        self.assertIsNone(report["tts_outside_generate_ms"])

    def test_measured_critical_path_sums_without_counting_overlapping_stt_twice(self):
        timing = profile_fixture()
        original = copy.deepcopy(timing)
        report = summarize(timing)
        intervals = (
            "end_to_stt_ms",
            "stt_to_agent_ms",
            "agent_total_ms",
            "agent_to_tts_ms",
            "tts_total_ms",
            "tts_to_audible_ms",
        )
        self.assertEqual(
            sum(report[name] for name in intervals), report["end_to_audible_ms"]
        )
        self.assertEqual(report["stt_first_text_ms"], 100.0)
        self.assertEqual(report["stt_flush_ms"], 98.0)
        self.assertEqual(report["retained_ttft_ms"], 855.0)
        self.assertEqual(timing, original)

    def test_impossible_generation_duration_rejects_negative_residual(self):
        timing = profile_fixture()
        timing["tts"]["remote"]["first_token_ms"] = 101.0
        with self.assertRaisesRegex(ValueError, "negative"):
            summarize(timing)

    def test_remote_elapsed_time_is_separated_from_local_transport_residual(self):
        timing = profile_fixture()
        timing["tts"]["remote"]["remote_total_ms"] = 360.0
        report = summarize(timing)
        self.assertEqual(report["tts_total_ms"], 390.0)
        self.assertEqual(report["tts_transport_startup_ms"], 30.0)
        timing["tts"]["remote"]["remote_total_ms"] = None
        self.assertIsNone(summarize(timing)["tts_transport_startup_ms"])

    def test_remote_duration_exceeding_local_interval_cannot_be_labeled_transport(self):
        timing = profile_fixture()
        timing["tts"]["remote"]["remote_total_ms"] = 391.0
        with self.assertRaisesRegex(ValueError, "negative"):
            summarize(timing)


class TimingTests(unittest.TestCase):
    def test_unmeasured_endpoints_remain_missing_instead_of_zero(self):
        for marks in (
            {},
            {"start": 10},
            {"end": 20},
            {"start": None, "end": 20},
            {"start": 10, "end": None},
            {"start": None, "end": None},
        ):
            with self.subTest(marks=marks):
                self.assertIsNone(milliseconds(marks, "start", "end"))

    def test_intervals_use_nanoseconds_and_preserve_zero_duration(self):
        self.assertEqual(
            milliseconds({"start": 100, "end": 2_000_100}, "start", "end"), 2.0
        )
        self.assertEqual(milliseconds({"start": 100, "end": 100}, "start", "end"), 0.0)

    def test_reversed_endpoints_raise_instead_of_reporting_negative_latency(self):
        with self.assertRaisesRegex(ValueError, "start follows end"):
            milliseconds({"start": 200, "end": 100}, "start", "end")

    def test_snapshot_keeps_original_clock_marks_when_timeline_changes(self):
        with (
            patch("slate.voice.timing.socket.gethostname", return_value="local-host"),
            patch("slate.voice.timing.time.monotonic_ns", side_effect=[10, 40]),
        ):
            timing = Timeline()
            timing.mark("started")
            original = timing.snapshot()
            timing.mark("started")
            timing.mark("started", replace=True)
            self.assertEqual(
                original, {"host": "local-host", "marks_ns": {"started": 10}}
            )
            self.assertEqual(timing.snapshot()["marks_ns"]["started"], 40)

    def test_turn_measures_first_and_last_accepted_frames_and_finishes_once(self):
        with patch(
            "slate.voice.timing.time.monotonic_ns", side_effect=[10, 20, 30, 40, 50]
        ):
            turn = Turn()
            turn.push(b"\x01\x00")
            turn.push(b"\x02\x00")
            turn.finish()
            finished = turn.timing.snapshot()
            turn.push(b"\x03\x00")
            turn.finish()
        self.assertEqual(
            turn.timing.marks,
            {
                "turn_started": 10,
                "mic_first_frame": 20,
                "mic_last_frame": 40,
                "input_finished": 50,
            },
        )
        self.assertEqual(turn.timing.snapshot(), finished)


class VoiceProfileTests(unittest.IsolatedAsyncioTestCase):
    def session(self, *, profile: bool) -> VoiceSession:
        session = object.__new__(VoiceSession)
        session.profile = profile
        session.turn = Turn()
        session.turn.push(b"\x01\x00")
        session.turn.finish()
        session.publish = AsyncMock()
        session.agent = Mock(
            run=AsyncMock(return_value="The heading is Slate."),
            timings={"duration_ms": 12.5},
            last_run={"runtime": {"provider": "test", "model": "test-model"}},
        )
        session.speaker = Mock(
            capture_frame=AsyncMock(), wait_for_playout=AsyncMock(), clear_queue=Mock()
        )
        return session

    async def test_profile_measures_stages_without_changing_spoken_output(self):
        for profile in (False, True):
            with self.subTest(profile=profile):
                session = self.session(profile=profile)
                turn = session.turn
                stt_calls: list[dict | None] = []
                tts_calls: list[dict | None] = []
                wave = write_wav(b"\x80\x01" * (SAMPLE_RATE // 50))

                async def transcribe(
                    audio: AsyncIterator[bytes],
                    *,
                    timings: dict | None,
                    calls=stt_calls,
                ) -> AsyncIterator[str]:
                    calls.append(timings)
                    self.assertEqual([chunk async for chunk in audio], [b"\x01\x00"])
                    if timings is not None:
                        timings.update({"model_ms": 4.0})
                    yield "Read the "
                    yield "heading."

                async def speak(
                    text: str, *, timings: dict | None, calls=tts_calls, audio=wave
                ) -> bytes:
                    self.assertEqual(text, "The heading is Slate.")
                    calls.append(timings)
                    if timings is not None:
                        timings.update({"model_ms": 8.0})
                    return audio

                with (
                    patch("slate.voice.session.transcribe_stream", transcribe),
                    patch("slate.voice.session.speak", speak),
                ):
                    await session.transcribe(turn)
                session.agent.run.assert_awaited_once()
                self.assertEqual(
                    session.agent.run.await_args.args[0], "Read the heading."
                )
                session.speaker.capture_frame.assert_awaited_once()
                session.speaker.wait_for_playout.assert_awaited_once()
                final = session.publish.await_args.kwargs
                self.assertEqual(final["topic"], REPLY_TOPIC)
                self.assertEqual(final["text"], "The heading is Slate.")
                self.assertTrue(final["final"])
                self.assertIsNone(session.turn)
                if not profile:
                    self.assertNotIn("timing", final)
                    self.assertEqual(stt_calls, [None])
                    self.assertEqual(tts_calls, [None])
                    continue
                report = final["timing"]
                self.assertEqual(report["stt"], {"model_ms": 4.0})
                self.assertEqual(report["tts"], {"model_ms": 8.0})
                self.assertEqual(report["agent"], {"duration_ms": 12.5})
                self.assertEqual(report["agent_runtime"]["model"], "test-model")
                marks = report["server"]["marks_ns"]
                stages = [
                    "turn_started",
                    "mic_first_frame",
                    "mic_last_frame",
                    "input_finished",
                    "stt_requested",
                    "stt_first_text",
                    "stt_completed",
                    "transcript_published",
                    "agent_requested",
                    "agent_completed",
                    "reply_text_published",
                    "tts_requested",
                    "tts_completed",
                    "reply_first_enqueue",
                    "reply_playout_done",
                ]
                self.assertEqual(
                    [marks[name] for name in stages], sorted(marks.values())
                )

    async def test_cancelled_profile_closes_transcription_without_success(self):
        session = self.session(profile=True)
        turn = session.turn
        started = asyncio.Event()
        closed = asyncio.Event()

        async def transcribe(
            audio: AsyncIterator[bytes], *, timings: dict | None
        ) -> AsyncIterator[str]:
            try:
                started.set()
                await asyncio.Future()
                yield "Unexpected text"
            finally:
                closed.set()

        with patch("slate.voice.session.transcribe_stream", transcribe):
            task = asyncio.create_task(session.transcribe(turn))
            try:
                await asyncio.wait_for(started.wait(), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 1)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(closed.is_set())
        self.assertIsNone(session.turn)
        session.agent.run.assert_not_awaited()
        session.publish.assert_not_awaited()

    async def test_failed_profile_publishes_error_without_successful_report(self):
        session = self.session(profile=True)
        turn = session.turn

        async def transcribe(
            audio: AsyncIterator[bytes], *, timings: dict | None
        ) -> AsyncIterator[str]:
            raise RuntimeError("STT test failure")
            yield "Unexpected text"

        with (
            patch("slate.voice.session.transcribe_stream", transcribe),
            self.assertLogs("slate.voice.session", level="ERROR"),
        ):
            await session.transcribe(turn)
        session.publish.assert_awaited_once()
        final = session.publish.await_args.kwargs
        self.assertEqual(final["topic"], TRANSCRIPT_TOPIC)
        self.assertIn("error", final)
        self.assertNotIn("timing", final)
        session.agent.run.assert_not_awaited()
        self.assertIsNone(session.turn)


class DeviceAudioTimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_silent_reply_frame_does_not_count_as_first_audible_reply(self):
        handlers: dict[str, Callable] = {}
        announced = asyncio.Event()
        audio_received = asyncio.Event()
        audio_closed = asyncio.Event()
        worker = SimpleNamespace(identity="worker-a")
        room = Mock()
        source = Mock(
            capture_frame=AsyncMock(), wait_for_playout=AsyncMock(), aclose=AsyncMock()
        )
        publication = Mock(wait_for_subscription=AsyncMock())
        room.disconnect = AsyncMock()
        room.local_participant.publish_track = AsyncMock(return_value=publication)

        def register(name: str):
            def attach(handler):
                handlers[name] = handler
                return handler

            return attach

        room.on.side_effect = register

        async def connect(*args) -> None:
            handlers["track_subscribed"](
                SimpleNamespace(kind=rtc.TrackKind.KIND_AUDIO), None, worker
            )

        def data(topic: str, **fields) -> None:
            handlers["data_received"](
                SimpleNamespace(
                    topic=topic,
                    participant=worker,
                    data=json.dumps({"turn_id": "turn-a", **fields}).encode(),
                )
            )

        async def rpc(*, destination_identity, method, payload) -> str:
            if method == "start_turn":
                return "turn-a"
            data(TRANSCRIPT_TOPIC, text="Hello.", final=True)
            data(REPLY_TOPIC, text="Hello back.", final=False)
            announced.set()
            await asyncio.wait_for(audio_received.wait(), 1)
            data(REPLY_TOPIC, text="Hello back.", final=True, timing={})
            return "turn-a"

        room.connect = connect
        room.local_participant.perform_rpc = rpc

        class AudioStream:
            async def __aiter__(self):
                await announced.wait()
                for chunk in (b"\x60\x00" * 480, b"\x61\x00" * 480):
                    yield SimpleNamespace(frame=SimpleNamespace(data=memoryview(chunk)))
                audio_received.set()
                await asyncio.Future()

            async def aclose(self):
                audio_closed.set()

        async def input_audio():
            yield b"\x01\x00" * 320

        requests = Mock(
            side_effect=[
                {
                    "session_id": "session-a",
                    "worker_identity": "worker-a",
                    "server_url": "ws://local.test",
                    "participant_token": "test-token",
                },
                {},
            ]
        )
        with (
            patch("slate.voice.device.session_request", requests),
            patch("slate.voice.device.rtc.Room", return_value=room),
            patch("slate.voice.device.rtc.AudioSource", return_value=source),
            patch("slate.voice.device.rtc.AudioStream", return_value=AudioStream()),
            patch("slate.voice.device.rtc.LocalAudioTrack.create_audio_track"),
        ):
            result = await transcribe_audio(
                input_audio(), "http://local.test", 16000, profile=True
            )
        self.assertEqual(result.transcript, "Hello.")
        self.assertEqual(result.reply, "Hello back.")
        self.assertTrue(result.audio)
        marks = result.timings["device"]["marks_ns"]
        self.assertLess(marks["reply_first_frame"], marks["reply_first_audible"])
        self.assertLess(marks["reply_first_audible"], marks["reply_complete_received"])
        self.assertTrue(audio_closed.is_set())
        source.aclose.assert_awaited_once()
        room.disconnect.assert_awaited_once()
        self.assertEqual(
            json.loads(requests.call_args_list[0].args[2]), {"profile": True}
        )
        self.assertEqual(requests.call_args_list[-1].args[1], "DELETE")


if __name__ == "__main__":
    unittest.main()
