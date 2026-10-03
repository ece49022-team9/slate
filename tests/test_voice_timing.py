import asyncio
import copy
import json
import unittest
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, Mock, patch

import websockets
from slate.voice.audio import SAMPLE_RATE
from slate.voice.device import converse
from slate.voice.session import Hello, VoiceSession
from slate.voice.timing import Timeline, milliseconds
from slate.voice.turn import Turn

from scripts.profile_voice import summarize


def profile_fixture() -> dict:
    def clock(host: str, **marks: int) -> dict:
        return {
            "host": host,
            "marks_ns": {key: value * 1_000_000 for key, value in marks.items()},
        }

    return {
        "device": clock(
            "mac",
            connect_requested=0,
            connected=100,
            turn_requested=100,
            turn_ready=150,
            end_requested=1000,
            reply_first_frame=1615,
            reply_first_audible=1640,
        ),
        "server": clock(
            "modal",
            end_requested=1002,
            input_finished=1302,
            stt_requested=500,
            stt_first_text=600,
            stt_completed=1400,
            agent_requested=1410,
            agent_completed=1800,
            tts_requested=1510,
            tts_first_audio=1600,
            reply_first_enqueue=1605,
            tts_completed=2100,
        ),
        "agent": {
            key: value * 1_000_000
            for key, value in {
                "requested": 1410,
                "admitted": 1420,
                "first_text": 1430,
                "last_text": 1490,
                "completed": 1800,
            }.items()
        },
        "stt": {
            "remote": {"tail_decode_ms": 10.0, "tail_decode_after_first_text_ms": 5.0}
        },
        "tts": {
            "segments": [
                {
                    "client": {
                        key: value * 1_000_000
                        for key, value in {
                            "requested": 1510,
                            "queues_ready": 1520,
                            "spawned": 1530,
                            "first_pcm": 1600,
                            "completed": 1750,
                        }.items()
                    },
                    "remote": {
                        "generate_ms": 200.0,
                        "first_token_ms": 20.0,
                        "first_pcm_ms": 40.0,
                        "remote_total_ms": 230.0,
                        "model_age_ms": 10000.0,
                    },
                },
                {
                    "remote": {
                        "generate_ms": 250.0,
                        "first_token_ms": 30.0,
                        "first_pcm_ms": 50.0,
                        "remote_total_ms": 280.0,
                        "model_age_ms": 10777.0,
                    }
                },
            ]
        },
    }


class ProfileReportTests(unittest.TestCase):
    def test_device_and_server_clocks_are_never_subtracted_from_each_other(self):
        timing = profile_fixture()
        timing["server"]["marks_ns"] = {
            key: value + 10**15 for key, value in timing["server"]["marks_ns"].items()
        }
        timing["agent"] = {
            key: value + 10**15 for key, value in timing["agent"].items()
        }
        report = summarize(timing)
        self.assertEqual(report["end_to_audible_ms"], 640.0)
        self.assertEqual(report["end_to_first_pcm_ms"], 598.0)
        self.assertEqual(report["connect_ms"], 100.0)
        self.assertEqual(report["turn_setup_ms"], 50.0)

    def test_unknown_ttft_stays_unavailable_without_inference_removed_estimate(self):
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
                self.assertEqual(report["end_to_audible_ms"], 640.0)

    def test_missing_critical_path_endpoints_leave_only_affected_metrics_unavailable(
        self,
    ):
        for stage, name, metric in (
            ("device", "end_requested", "end_to_audible_ms"),
            ("device", "reply_first_audible", "end_to_audible_ms"),
            ("server", "end_requested", "end_to_stt_ms"),
            ("server", "stt_completed", "end_to_stt_ms"),
            ("server", "tts_completed", "tts_total_ms"),
            ("server", "tts_requested", "tts_total_ms"),
            ("server", "tts_first_audio", "end_to_first_pcm_ms"),
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
                    if name == "tts_completed":
                        self.assertEqual(report["end_to_first_pcm_ms"], 598.0)

    def test_first_audio_path_sums_on_the_server_clock(self):
        timing = profile_fixture()
        original = copy.deepcopy(timing)
        report = summarize(timing)
        self.assertEqual(
            report["end_to_first_pcm_ms"] + report["tts_first_pcm_to_enqueue_ms"],
            report["end_to_enqueue_ms"],
        )
        self.assertEqual(report["stt_first_text_ms"], 100.0)
        self.assertEqual(report["stt_flush_ms"], 98.0)
        self.assertEqual(report["agent_total_ms"], 390.0)
        self.assertEqual(report["tts_total_ms"], 590.0)
        self.assertEqual(timing, original)

    def test_remote_generation_offsets_must_follow_token_pcm_completion_order(self):
        for key, value in (
            ("first_token_ms", 201.0),
            ("first_pcm_ms", 201.0),
            ("first_token_ms", 41.0),
        ):
            with self.subTest(key=key, value=value):
                timing = profile_fixture()
                timing["tts"]["segments"][0]["remote"][key] = value
                with self.assertRaisesRegex(ValueError, "reversed"):
                    summarize(timing)

    def test_remote_offsets_and_client_spans_describe_first_segment_only(self):
        timing = profile_fixture()
        report = summarize(timing)
        self.assertEqual(report["tts_first_segment_remote_total_ms"], 230.0)
        self.assertEqual(report["tts_first_segment_client_total_ms"], 240.0)
        self.assertEqual(report["tts_first_segment_first_pcm_ms"], 40.0)
        self.assertEqual(report["tts_first_segment_client_first_pcm_ms"], 90.0)
        self.assertEqual(report["tts_model_age_ms"], 10000.0)
        timing["tts"]["segments"][0]["remote"]["first_pcm_ms"] = None
        self.assertIsNone(summarize(timing)["tts_first_segment_first_pcm_ms"])

    def test_audible_reply_before_end_rejects_reversed_device_marks(self):
        timing = profile_fixture()
        timing["device"]["marks_ns"]["reply_first_audible"] = 999 * 1_000_000
        with self.assertRaisesRegex(ValueError, "follows"):
            summarize(timing)

    def test_unmeasured_segments_do_not_invent_remote_timing(self):
        timing = profile_fixture()
        timing["tts"]["segments"] = []
        report = summarize(timing)
        self.assertIsNone(report["tts_first_segment_first_pcm_ms"])
        self.assertIsNone(report["tts_first_segment_client_total_ms"])
        self.assertIsNone(report["tts_model_age_ms"])
        self.assertEqual(report["end_to_audible_ms"], 640.0)


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
        agent = Mock(
            run=AsyncMock(return_value="The heading is Slate."),
            timings={"duration_ms": 12.5},
            last_run={"runtime": {"provider": "test", "model": "test-model"}},
            pending=set(),
        )
        socket = Mock(send_text=AsyncMock(), send_bytes=AsyncMock())
        with patch("slate.voice.session.create_agent", return_value=agent):
            session = VoiceSession(
                socket, Hello(type="hello", rate=SAMPLE_RATE, profile=profile)
            )
        session.turn = Turn()
        session.turn.push(b"\x01\x00")
        session.turn.finish()
        session.publish = AsyncMock()
        return session

    async def test_profile_measures_stages_without_changing_spoken_output(self):
        for profile in (False, True):
            with self.subTest(profile=profile):
                session = self.session(profile=profile)
                turn = session.turn
                stt_calls: list[dict | None] = []
                tts_calls: list[dict | None] = []
                pcm = b"\x80\x01" * (SAMPLE_RATE // 50)

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
                    text: str, *, timings: dict | None, calls=tts_calls, audio=pcm
                ) -> AsyncIterator[bytes]:
                    self.assertEqual(text, "The heading is Slate.")
                    calls.append(timings)
                    if timings is not None:
                        timings.update({"model_ms": 8.0})
                    yield audio

                with (
                    patch("slate.voice.session.transcribe_stream", transcribe),
                    patch("slate.voice.session.speak_stream", speak),
                ):
                    await session.transcribe(turn)
                session.agent.run.assert_awaited_once()
                self.assertEqual(
                    session.agent.run.await_args.args[0], "Read the heading."
                )
                session.socket.send_bytes.assert_awaited_once()
                self.assertEqual(session.publish.await_args.args[1], "reply")
                final = session.publish.await_args.kwargs
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
                self.assertEqual(report["tts"], {"segments": [{"model_ms": 8.0}]})
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
                    "tts_first_audio",
                    "reply_first_enqueue",
                    "tts_completed",
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
        self.assertEqual(session.publish.await_args.args[1], "error")
        final = session.publish.await_args.kwargs
        self.assertEqual(final["stage"], "transcription")
        self.assertNotIn("timing", final)
        session.agent.run.assert_not_awaited()
        self.assertIsNone(session.turn)


class DeviceAudioTimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_silent_reply_frame_does_not_count_as_first_audible_reply(self):
        received = []

        async def slate(socket):
            received.append(socket.request.headers["Authorization"])
            received.append(json.loads(await socket.recv()))
            assert json.loads(await socket.recv()) == {"type": "start"}
            await socket.send(json.dumps({"type": "turn", "turn_id": "turn-a"}))
            while isinstance(message := await socket.recv(), bytes):
                received.append(len(message))
            assert json.loads(message) == {"type": "end"}
            await socket.send(
                json.dumps(
                    {
                        "type": "transcript",
                        "turn_id": "turn-a",
                        "text": "Hello.",
                        "final": True,
                    }
                )
            )
            reply = {"type": "reply", "turn_id": "turn-a", "text": "Hello back."}
            await socket.send(json.dumps({**reply, "final": False}))
            await socket.send(b"\x60\x00" * 480)
            await socket.send(b"\x61\x00" * 480)
            await socket.send(json.dumps({**reply, "final": True, "timing": {}}))
            await socket.wait_closed()

        async def input_audio():
            yield b"\x01\x00" * 320

        async with websockets.serve(slate, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            result = await converse(
                input_audio(), f"http://127.0.0.1:{port}", "token-a", profile=True
            )
        self.assertEqual(
            received[:2],
            ["Bearer token-a", {"type": "hello", "rate": 16000, "profile": True}],
        )
        self.assertEqual(received[2:], [640])
        self.assertEqual(result.transcript, "Hello.")
        self.assertEqual(result.reply, "Hello back.")
        self.assertTrue(result.audio)
        marks = result.timings["device"]["marks_ns"]
        self.assertLess(marks["reply_first_frame"], marks["reply_first_audible"])
        self.assertLess(marks["reply_first_audible"], marks["reply_complete_received"])


if __name__ == "__main__":
    unittest.main()
