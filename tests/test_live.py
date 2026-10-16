import asyncio
import base64
import json
import unittest
from contextlib import asynccontextmanager
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from slate.voice.audio import SAMPLE_RATE
from slate.voice.live import BACKEND_CONTEXT, FAILED_REPLY, LiveCall, Words
from test_voice_session import AUTH, FakeAgent, cloud

QUESTION = "What is seven plus five?"


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


class FakeLive:
    """Stands in for GPT-Live's WebSocket. Once it has heard `ask_after` bytes of
    microphone audio it says a filler, transcribes the question and hands it to
    the client; commentary it receives is spoken back as Slate's words."""

    def __init__(self, script: list | None = None, ask_after: int = 0) -> None:
        self.events: asyncio.Queue = asyncio.Queue()
        for item in script or []:
            self.events.put_nowait(item)
        self.ask_after = ask_after
        self.asked = False
        self.heard = bytearray()
        self.starts: list[dict] = []
        self.commentary: list[dict] = []
        self.session = SimpleNamespace(
            start=self.start,
            close=self.close,
            input_audio=SimpleNamespace(append=self.append),
            commentary=SimpleNamespace(append=self.comment),
        )

    def client(self) -> SimpleNamespace:
        @asynccontextmanager
        async def connect(**options):
            yield self

        return SimpleNamespace(live=SimpleNamespace(connect=connect))

    async def start(self, *, session: dict) -> None:
        self.starts.append(session)
        self.events.put_nowait(
            event("session.started", session=SimpleNamespace(id="sess"))
        )

    async def append(self, *, audio: str) -> None:
        self.heard += base64.b64decode(audio)
        if self.ask_after and len(self.heard) >= self.ask_after and not self.asked:
            self.asked = True
            filler = b"\x00\x10" * (SAMPLE_RATE // 25)
            self.events.put_nowait(
                event("session.output_audio.delta", delta=base64.b64encode(filler))
            )
            self.events.put_nowait(words("input", 0, QUESTION))
            self.events.put_nowait(handoff("only", 1000))

    async def comment(self, *, content: str, delegation_id: str | None = None):
        self.commentary.append({"delegation_id": delegation_id, "content": content})
        self.events.put_nowait(words("output", 2000, " " + content))

    async def close(self) -> None:
        self.events.put_nowait(
            event(
                "session.closed",
                reason="close_requested",
                usage=SimpleNamespace(seconds=4.0),
            )
        )

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.events.get()


class LiveCallTests(unittest.IsolatedAsyncioTestCase):
    def call(self, live: FakeLive, delegate) -> LiveCall:
        call = LiveCall(
            delegate=delegate, speak=lambda pcm: None, send=AsyncMock(), client=None
        )
        call.connection = live
        return call

    async def finish(self, call: LiveCall) -> None:
        await call.receive()
        await asyncio.gather(*call.tasks)

    async def test_each_handoff_sends_new_speech_and_answers_under_its_id(self):
        delegate = AsyncMock(side_effect=[("Twelve.", {}), ("Fifteen.", {})])
        live = FakeLive(
            [
                words("input", 1000, " What is seven"),
                words("input", 1200, " plus five?"),
                handoff("first", 1600),
                words("output", 1800, " Twelve."),
                words("input", 3000, " And plus three?"),
                handoff("second", 3400),
            ]
        )
        call = self.call(live, delegate)
        await live.close()
        await self.finish(call)
        self.assertEqual(
            [entry.args[0] for entry in delegate.await_args_list],
            [
                BACKEND_CONTEXT + "User: What is seven plus five?",
                BACKEND_CONTEXT + "Slate: Twelve.\nUser: And plus three?",
            ],
        )
        self.assertEqual(
            live.commentary,
            [
                {"delegation_id": "first", "content": "Twelve."},
                {"delegation_id": "second", "content": "Fifteen."},
            ],
        )

    async def test_handoff_waits_for_the_last_word_and_keeps_the_filler_for_later(
        self,
    ):
        live = FakeLive([words("input", 1000, " What is seven plus")])
        call = self.call(live, AsyncMock(side_effect=[("Twelve.", {}), ("15.", {})]))
        receiving = asyncio.create_task(call.receive())
        await asyncio.sleep(0.05)
        live.events.put_nowait(handoff("first", 1400))
        live.events.put_nowait(words("output", 1500, " Checking."))
        await asyncio.sleep(0.1)
        live.events.put_nowait(words("input", 1400, " five?"))
        await asyncio.sleep(0.5)
        live.events.put_nowait(words("input", 4000, " Plus three?"))
        live.events.put_nowait(handoff("second", 4400))
        await asyncio.sleep(0.5)
        await live.close()
        await receiving
        await asyncio.gather(*call.tasks)
        self.assertEqual(
            [entry.args[0] for entry in call.delegate.await_args_list],
            [
                BACKEND_CONTEXT + "User: What is seven plus five?",
                BACKEND_CONTEXT + "Slate: Checking. Twelve.\nUser: Plus three?",
            ],
        )

    async def test_agent_failure_still_answers_the_waiting_handoff(self):
        live = FakeLive(
            [words("input", 1000, " What time is it?"), handoff("only", 1400)]
        )
        call = self.call(live, AsyncMock(side_effect=RuntimeError))
        await live.close()
        await self.finish(call)
        self.assertEqual(
            live.commentary, [{"delegation_id": "only", "content": FAILED_REPLY}]
        )
        self.assertEqual(call.delegations[0]["error"], "RuntimeError")

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
        live = FakeLive(
            [
                event(
                    "session.closed",
                    reason="expired",
                    usage=SimpleNamespace(seconds=30.0),
                )
            ]
        )
        call = self.call(live, AsyncMock())
        with self.assertRaisesRegex(RuntimeError, "session closed: expired"):
            await call.receive()
        self.assertEqual(call.seconds, 30.0)

    async def test_reconnect_starts_a_new_session_seeded_with_the_conversation(self):
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
        self.assertEqual(call.seconds, 16.0)
        self.assertFalse(call.restarting)

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


class DeviceLiveCallTests(unittest.TestCase):
    def test_device_holds_a_live_call_and_hermes_controls_it_during_a_handoff(self):
        agent = FakeAgent(reply="Twelve.\n---\n7 + 5 = 12", control=True)
        live = FakeLive(ask_after=SAMPLE_RATE)

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
                socket.send_text(json.dumps({"type": "hello", "rate": 16000}))
                socket.send_text(json.dumps({"type": "live"}))
                started = json.loads(socket.receive_text())
                self.assertEqual(started["state"], "started")
                socket.send_text(json.dumps({"type": "start"}))
                for _ in range(40):
                    socket.send_bytes(b"\x10\x00" * 320)
                while not any(e["type"] == "said" for e in events):
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
        handed = report["delegations"][0]
        self.assertEqual(handed["reply"], "Twelve.")
        self.assertIn("agent", handed)
        self.assertEqual(report["seconds"], 4.0)
        ended = events[-1]
        self.assertEqual((ended["state"], ended["seconds"]), ("ended", 4.0))
        self.assertNotIn("error", ended)
        self.assertEqual(audio, b"\x00\x10" * (SAMPLE_RATE // 25))
        self.assertEqual(agent.messages, [BACKEND_CONTEXT + "User: " + QUESTION])
        self.assertEqual(agent.receipts[0][0], 200)
        self.assertEqual(
            live.commentary, [{"delegation_id": "only", "content": "Twelve."}]
        )
        self.assertGreaterEqual(len(live.heard), SAMPLE_RATE)
        kinds = [e["type"] for e in events]
        self.assertNotIn("turn", kinds)
        self.assertEqual(kinds.count("heard"), 1)
        self.assertTrue(
            all(e["call_id"] == started["call_id"] for e in events if "call_id" in e)
        )
        tool = next(e for e in events if e["type"] == "tool")
        self.assertEqual(tool["tool"], "slate agent")


if __name__ == "__main__":
    unittest.main()
