import asyncio
import base64
import json
import time
import unittest
from contextlib import asynccontextmanager
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from slate.voice.live import (
    ALLOWED,
    BACK_ON_THE_LINE,
    BACKEND_CONTEXT,
    FAILED,
    ON_THE_LINE,
    UNSAID,
    WITHDRAWN,
    LiveCall,
    Words,
    ok_question,
    outcome,
)
from test_voice_session import AUTH, FakeAgent, cloud

QUESTION = "What is seven plus five?"
RATE = 16_000
FILLER = b"\x00\x10" * (RATE // 25)


def event(kind: str, **fields) -> SimpleNamespace:
    return SimpleNamespace(type=kind, **fields)


def words(kind: str, start_ms: int, text: str) -> SimpleNamespace:
    return event(
        f"session.{kind}_transcript.delta",
        start_ms=start_ms,
        end_ms=start_ms + 200,
        delta=text,
    )


def handoff(delegation_id: str, offset_ms: int) -> SimpleNamespace:
    return event(
        "session.delegation.created",
        offset_ms=offset_ms,
        delegation=SimpleNamespace(id=delegation_id, target="client"),
    )


def closed(reason: str = "close_requested", seconds: float = 4.0) -> SimpleNamespace:
    return event(
        "session.closed", reason=reason, usage=SimpleNamespace(seconds=seconds)
    )


class FakeLive:
    def __init__(
        self, script: list | None = None, ask_after: int = 0, voiced: bool = True
    ) -> None:
        self.events: asyncio.Queue = asyncio.Queue()
        for item in script or []:
            self.events.put_nowait(item)
        self.ask_after = ask_after
        self.voiced = voiced
        self.asked = False
        self.heard = bytearray()
        self.starts: list[dict] = []
        self.created: list[dict] = []
        self.commentary: list[dict] = []
        self.thinking: list[dict] = []
        self.session = SimpleNamespace(
            start=self.start,
            close=self.close,
            input_audio=SimpleNamespace(append=self.append),
            commentary=SimpleNamespace(append=self.comment),
            thinking=SimpleNamespace(append=self.think),
        )

    def client(self) -> SimpleNamespace:
        @asynccontextmanager
        async def connect(**options):
            yield self

        async def create(*, session: dict, transport: dict):
            self.created.append({"session": session, "transport": transport})
            return SimpleNamespace(
                session=SimpleNamespace(id="sess_rtc"),
                transport=SimpleNamespace(sdp="answer-sdp", type="webrtc"),
            )

        return SimpleNamespace(
            live=SimpleNamespace(
                connect=connect,
                create=create,
                sideband=SimpleNamespace(connect=connect),
            )
        )

    async def start(self, *, session: dict) -> None:
        self.starts.append(session)
        self.events.put_nowait(
            event("session.started", session=SimpleNamespace(id="sess"))
        )

    async def append(self, *, audio: str) -> None:
        self.heard += base64.b64decode(audio)
        if self.ask_after and len(self.heard) >= self.ask_after and not self.asked:
            self.asked = True
            self.events.put_nowait(
                event("session.output_audio.delta", delta=base64.b64encode(FILLER))
            )
            self.events.put_nowait(words("input", 0, QUESTION))
            self.events.put_nowait(handoff("only", 1000))

    async def comment(self, *, content: str, delegation_id: str | None):
        self.commentary.append({"delegation_id": delegation_id, "content": content})
        self.events.put_nowait(words("output", 2000, " " + content))
        if self.voiced:
            speech = event("session.output_audio.delta", delta=base64.b64encode(FILLER))
            asyncio.get_running_loop().call_later(0.35, self.events.put_nowait, speech)

    async def think(self, *, content: str, delegation_id: str | None):
        self.thinking.append({"delegation_id": delegation_id, "content": content})

    async def close(self) -> None:
        self.events.put_nowait(closed())

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.events.get()


class LiveCallTests(unittest.IsolatedAsyncioTestCase):
    def call(self, live: FakeLive, delegate, approve=None) -> LiveCall:
        call = LiveCall(
            delegate=delegate,
            approve=approve or AsyncMock(),
            speak=Mock(),
            send=AsyncMock(),
            client=live.client(),
        )
        call.connection = live
        return call

    async def settle(self, call: LiveCall) -> None:
        async with asyncio.timeout(5):
            while call.tasks or call.held or call.owed:
                await asyncio.sleep(0.02)

    def requests(self, delegate: AsyncMock) -> list[str]:
        return [entry.args[0] for entry in delegate.await_args_list]

    async def test_each_handoff_carries_the_words_since_the_last_real_reply(self):
        delegate = AsyncMock(side_effect=[("Twelve.", {}), ("Fifteen.", {})])
        live = FakeLive(
            [
                words("input", 1000, " What is seven"),
                words("input", 1200, " plus five?"),
                handoff("first", 1600),
            ]
        )
        call = self.call(live, delegate)
        receiving = asyncio.create_task(call.receive())
        await asyncio.sleep(0.05)
        await self.settle(call)
        await asyncio.sleep(0.5)
        live.events.put_nowait(words("output", 2600, " Mm-hmm."))
        live.events.put_nowait(words("input", 3000, " And plus three?"))
        live.events.put_nowait(handoff("second", 3400))
        await asyncio.sleep(0.05)
        await self.settle(call)
        await live.close()
        await receiving
        self.assertEqual(
            self.requests(delegate),
            [BACKEND_CONTEXT + QUESTION, BACKEND_CONTEXT + "And plus three?"],
        )
        self.assertEqual(
            live.commentary,
            [
                {"delegation_id": "first", "content": outcome("Twelve.")},
                {"delegation_id": "second", "content": outcome("Fifteen.")},
            ],
        )

    async def test_handoff_waits_for_the_last_word_and_a_backchannel_keeps_it(self):
        delegate = AsyncMock(side_effect=[("Twelve.", {}), ("15.", {})])
        live = FakeLive([words("input", 1000, " What is seven plus")])
        call = self.call(live, delegate)
        receiving = asyncio.create_task(call.receive())
        await asyncio.sleep(0.05)
        live.events.put_nowait(handoff("first", 1400))
        live.events.put_nowait(words("output", 1500, " Checking."))
        await asyncio.sleep(0.1)
        live.events.put_nowait(words("input", 1400, " five?"))
        await asyncio.sleep(0.05)
        await self.settle(call)
        live.events.put_nowait(words("input", 4000, " Plus three?"))
        live.events.put_nowait(handoff("second", 4400))
        await asyncio.sleep(0.05)
        await self.settle(call)
        await live.close()
        await receiving
        self.assertEqual(
            self.requests(delegate),
            [BACKEND_CONTEXT + QUESTION, BACKEND_CONTEXT + "Plus three?"],
        )

    async def test_words_after_a_handoff_withdraw_it_into_the_next(self):
        delegate = AsyncMock(return_value=("Done.", {}))
        live = FakeLive([words("input", 1000, " Turn the screen")])
        call = self.call(live, delegate)
        receiving = asyncio.create_task(call.receive())
        await asyncio.sleep(0.05)
        live.events.put_nowait(handoff("first", 1300))
        await asyncio.sleep(0.05)
        live.events.put_nowait(words("input", 2000, " blue please"))
        await asyncio.sleep(0.3)
        live.events.put_nowait(handoff("second", 2600))
        await asyncio.sleep(0.05)
        await self.settle(call)
        await live.close()
        await receiving
        self.assertEqual(
            self.requests(delegate), [BACKEND_CONTEXT + "Turn the screen blue please"]
        )
        self.assertEqual(
            live.thinking[0], {"delegation_id": "first", "content": WITHDRAWN}
        )
        self.assertEqual(
            live.commentary,
            [{"delegation_id": "second", "content": outcome("Done.")}],
        )
        self.assertTrue(call.delegations[0]["withdrawn"])

    async def test_agent_failure_still_answers_the_waiting_handoff(self):
        live = FakeLive(
            [words("input", 1000, " What time is it?"), handoff("only", 1400)]
        )
        call = self.call(live, AsyncMock(side_effect=RuntimeError))
        receiving = asyncio.create_task(call.receive())
        await asyncio.sleep(0.05)
        await self.settle(call)
        await live.close()
        await receiving
        self.assertEqual(
            live.commentary, [{"delegation_id": "only", "content": FAILED}]
        )
        self.assertEqual(call.delegations[0]["error"], "RuntimeError")

    async def test_spoken_yes_answers_the_agents_ok_question(self):
        approved = asyncio.Event()

        async def delegate(request, progress):
            await progress(
                {
                    "type": "approval.request",
                    "run_id": "run",
                    "request_id": "ask",
                    "command": "send an email to Sam",
                }
            )
            await approved.wait()
            return "Sent.", {}

        approve = AsyncMock(side_effect=lambda *args: approved.set())
        live = FakeLive(
            [words("input", 1000, " Email Sam hello"), handoff("first", 1400)]
        )
        call = self.call(live, delegate, approve)
        receiving = asyncio.create_task(call.receive())
        async with asyncio.timeout(5):
            while not live.commentary:
                await asyncio.sleep(0.02)
        await asyncio.sleep(0.5)
        live.events.put_nowait(words("input", 6000, " Yes."))
        live.events.put_nowait(handoff("second", 6400))
        await asyncio.sleep(0.05)
        await self.settle(call)
        await live.close()
        await receiving
        approve.assert_awaited_once_with("run", "ask", "once")
        self.assertEqual(
            live.commentary,
            [
                {
                    "delegation_id": "first",
                    "content": ok_question("send an email to Sam"),
                },
                {"delegation_id": "second", "content": ALLOWED},
                {"delegation_id": "first", "content": outcome("Sent.")},
            ],
        )

    async def test_updates_wait_for_a_free_line_and_unheard_ones_are_owed_again(self):
        live = FakeLive(voiced=False)
        call = self.call(live, AsyncMock())
        call.voice_ns = time.monotonic_ns()
        call.say(outcome("Twelve."), "only")
        await asyncio.sleep(0.3)
        self.assertEqual(live.commentary, [])
        await asyncio.sleep(0.7)
        self.assertEqual(
            live.commentary, [{"delegation_id": "only", "content": outcome("Twelve.")}]
        )
        receiving = asyncio.create_task(call.receive())
        live.events.put_nowait(words("input", 5000, " Hello?"))
        await asyncio.sleep(0.05)
        await live.close()
        await receiving
        await self.settle(call)
        self.assertEqual(
            live.commentary[1]["content"], f"{UNSAID} {outcome('Twelve.')}"
        )

    async def test_only_session_errors_end_the_call(self):
        def error(client_event_id):
            return event(
                "error",
                client_event_id=client_event_id,
                error=SimpleNamespace(code="bad", message="rejected"),
            )

        live = FakeLive([error("late-commentary")])
        await live.close()
        await self.call(live, AsyncMock()).receive()
        with self.assertRaisesRegex(RuntimeError, "slate.live: bad"):
            await self.call(FakeLive([error(None)]), AsyncMock()).receive()

    async def test_unrequested_close_ends_the_call_with_its_reason(self):
        call = self.call(FakeLive([closed("expired", 30.0)]), AsyncMock())
        with self.assertRaisesRegex(RuntimeError, "session closed: expired"):
            await call.receive()
        self.assertEqual(call.seconds, 30.0)

    async def test_reconnect_reseeds_the_conversation_and_says_it_dropped(self):
        live = FakeLive()
        call = self.call(live, AsyncMock())
        call.words = [
            Words("User", 0, 900, " What is seven plus five?"),
            Words("Slate", 1000, 1400, " Twelve."),
        ]
        call.session_seconds = 12.0
        call.started.set()
        dropped = SimpleNamespace(close_code=1006, attempt=1, max_attempts=5)
        call.reconnecting(dropped)
        call.reconnecting(SimpleNamespace(**{**vars(dropped), "attempt": 2}))
        await asyncio.gather(*call.tasks)
        self.assertFalse(call.started.is_set())
        self.assertEqual(len(live.starts), 1)
        self.assertEqual(
            live.starts[0]["input"],
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "What is seven plus five?"}
                    ],
                },
                {
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Twelve."}],
                },
            ],
        )
        await live.close()
        await call.receive()
        await self.settle(call)
        self.assertEqual(call.seconds, 16.0)
        self.assertEqual((call.restarting, call.generation), (False, 1))
        self.assertEqual(
            live.commentary, [{"delegation_id": None, "content": BACK_ON_THE_LINE}]
        )

    async def test_microphone_waits_for_the_session_and_hangup_closes_it(self):
        live = FakeLive()
        call = self.call(live, AsyncMock())
        sender = asyncio.create_task(call.send())
        call.push(b"\x01\x00" * 10)
        await asyncio.sleep(0)
        call.started.set()
        call.push(b"\x02\x00" * 10)
        call.hang_up()
        await sender
        self.assertEqual(bytes(live.heard), b"\x02\x00" * 10)
        self.assertEqual(live.events.get_nowait().type, "session.closed")

    async def test_webrtc_call_attaches_a_control_line_and_greets_once_audio_flows(
        self,
    ):
        live = FakeLive()
        call = LiveCall(
            delegate=AsyncMock(),
            approve=AsyncMock(),
            speak=Mock(),
            send=AsyncMock(),
            client=live.client(),
        )
        self.assertEqual(await call.open("offer-sdp"), "answer-sdp")
        created = live.created[0]
        self.assertEqual(created["transport"], {"type": "webrtc", "sdp": "offer-sdp"})
        self.assertNotIn("format", created["session"]["audio"])
        self.assertEqual(
            created["session"]["client"]["data_channel"]["allowed_client_events"], []
        )
        running = asyncio.create_task(call.run())
        quiet = base64.b64encode(bytes(960)).decode()
        live.events.put_nowait(event("session.input_audio.append", audio=quiet))
        live.events.put_nowait(event("session.input_audio.append", audio=quiet))
        live.events.put_nowait(
            event("session.output_audio.delta", delta=base64.b64encode(FILLER))
        )
        await asyncio.sleep(0.05)
        call.hang_up()
        await running
        self.assertEqual(call.transport, "webrtc")
        self.assertEqual(
            live.commentary, [{"delegation_id": None, "content": ON_THE_LINE}]
        )
        call.speak.assert_not_called()
        call.send_event.assert_any_await(
            {
                "type": "live",
                "call_id": call.id,
                "state": "started",
                "transport": "webrtc",
            }
        )

    async def test_webrtc_peer_leaving_ends_the_call_cleanly(self):
        live = FakeLive([closed("connection_lost", 9.0)])
        call = self.call(live, AsyncMock())
        call.session_id = "sess_rtc"
        await call.receive()
        self.assertEqual(call.seconds, 9.0)


class DeviceLiveCallTests(unittest.TestCase):
    def test_device_bridges_16_khz_audio_and_hermes_controls_it_during_a_handoff(
        self,
    ):
        agent = FakeAgent(reply="Twelve.\n---\n7 + 5 = 12", control=True)
        live = FakeLive(ask_after=RATE)

        def receipt(command):
            return {
                "type": "receipt",
                "request_id": command["request_id"],
                "operation": "set_orb",
                "revision": 3,
                "state": 1,
                "color": "#112233",
                "radius": 30.0,
                "text": "",
                "custom": True,
            }

        events, audio = [], b""
        with (
            cloud(agent) as client,
            patch(
                "slate.voice.session.LiveCall", partial(LiveCall, client=live.client())
            ),
        ):
            with client.websocket_connect("/api/device/socket", headers=AUTH) as socket:
                socket.send_text(
                    json.dumps({"type": "hello", "rate": RATE, "speaker_rate": RATE})
                )
                socket.send_text(json.dumps({"type": "live"}))
                started = json.loads(socket.receive_text())
                self.assertEqual(started["state"], "started")
                socket.send_text(json.dumps({"type": "start"}))
                for _ in range(40):
                    socket.send_bytes(b"\x10\x00" * 320)
                while not any("Twelve" in e.get("text", "") for e in events):
                    message = socket.receive()
                    if message.get("bytes") is not None:
                        audio += message["bytes"]
                        continue
                    events.append(json.loads(message["text"]))
                    if events[-1]["type"] == "command":
                        socket.send_text(json.dumps(receipt(events[-1])))
                socket.send_text(json.dumps({"type": "hangup"}))
                while events[-1]["type"] != "live":
                    message = socket.receive()
                    if message.get("text") is not None:
                        events.append(json.loads(message["text"]))
                reports = client.get("/api/voice/reports", headers=AUTH).json()
                report = reports[started["call_id"]]
                denied = client.get("/api/voice/reports").status_code
        self.assertEqual(denied, 401)
        self.assertEqual(live.starts[0]["audio"]["format"]["rate"], RATE)
        handed = report["delegations"][0]
        self.assertEqual(handed["reply"], "Twelve.")
        self.assertIn("agent", handed)
        self.assertEqual(report["seconds"], 4.0)
        ended = events[-1]
        self.assertEqual((ended["state"], ended["seconds"]), ("ended", 4.0))
        self.assertNotIn("error", ended)
        self.assertTrue(audio.startswith(FILLER))
        self.assertEqual(bytes(live.heard), b"\x10\x00" * 320 * 40)
        self.assertEqual(agent.messages, [BACKEND_CONTEXT + QUESTION])
        self.assertEqual(agent.receipts[0][0], 200)
        self.assertEqual(
            live.commentary,
            [
                {"delegation_id": None, "content": ON_THE_LINE},
                {"delegation_id": "only", "content": outcome("Twelve.")},
            ],
        )
        kinds = [e["type"] for e in events]
        self.assertNotIn("turn", kinds)
        self.assertEqual(kinds.count("heard"), 1)
        self.assertTrue(
            all(e["call_id"] == started["call_id"] for e in events if "call_id" in e)
        )
        tool = next(e for e in events if e["type"] == "tool")
        self.assertEqual(tool["tool"], "slate agent")

    def test_device_socket_carries_a_webrtc_offer_and_its_answer(self):
        live = FakeLive()
        with (
            cloud(FakeAgent()) as client,
            patch(
                "slate.voice.session.LiveCall", partial(LiveCall, client=live.client())
            ),
        ):
            with client.websocket_connect("/api/device/socket", headers=AUTH) as socket:
                socket.send_text(json.dumps({"type": "hello", "rate": 48_000}))
                socket.send_text(json.dumps({"type": "live", "sdp": "offer-sdp"}))
                answer = json.loads(socket.receive_text())
                started = json.loads(socket.receive_text())
                socket.send_bytes(b"\x10\x00" * 320)
                socket.send_text(json.dumps({"type": "hangup"}))
                ended = json.loads(socket.receive_text())
        self.assertEqual(
            {key: answer[key] for key in ("state", "sdp")},
            {"state": "answer", "sdp": "answer-sdp"},
        )
        self.assertEqual(
            (started["state"], started["transport"], started["call_id"]),
            ("started", "webrtc", answer["call_id"]),
        )
        self.assertEqual(live.created[0]["transport"]["sdp"], "offer-sdp")
        self.assertEqual(live.heard, bytearray())
        self.assertEqual(ended["state"], "ended")
        self.assertNotIn("error", ended)


if __name__ == "__main__":
    unittest.main()
