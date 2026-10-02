import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from slate.voice.live import BACKEND_CONTEXT, FAILED_REPLY, LiveConversation


def words(kind: str, start_ms: int, text: str) -> SimpleNamespace:
    return SimpleNamespace(
        type=f"session.{kind}_transcript.delta",
        start_ms=start_ms,
        end_ms=start_ms + 200,
        delta=text,
    )


def delegation(delegation_id: str, offset_ms: int) -> SimpleNamespace:
    return SimpleNamespace(
        type="session.delegation.created",
        offset_ms=offset_ms,
        delegation=SimpleNamespace(id=delegation_id, target="client"),
    )


class FakeConnection:
    def __init__(self, events: list) -> None:
        self.events = events
        self.session = SimpleNamespace(commentary=SimpleNamespace(append=AsyncMock()))

    def __aiter__(self):
        return self.stream()

    async def stream(self):
        for event in self.events:
            yield event
        yield SimpleNamespace(type="session.closed", usage=None)


class LiveConversationTests(unittest.IsolatedAsyncioTestCase):
    def conversation(self, events: list, agent) -> LiveConversation:
        live = LiveConversation(agent, speak=Mock(), publish=AsyncMock())
        live.connection = FakeConnection(events)
        return live

    async def finish(self, live: LiveConversation) -> None:
        await live.receive()
        await asyncio.gather(*live.tasks)

    async def test_each_delegation_sends_new_speech_and_returns_under_its_id(self):
        agent = Mock(
            run=AsyncMock(side_effect=["Twelve.", "Fifteen."]),
            timings={},
            last_run={},
        )
        live = self.conversation(
            [
                words("input", 1000, " What is seven"),
                words("input", 1200, " plus five?"),
                delegation("first", 1600),
                words("output", 1800, " Twelve."),
                words("input", 3000, " And plus three?"),
                delegation("second", 3400),
            ],
            agent,
        )
        await self.finish(live)
        self.assertEqual(
            [call.args[0] for call in agent.run.await_args_list],
            [
                BACKEND_CONTEXT + "User: What is seven plus five?",
                BACKEND_CONTEXT + "Slate: Twelve.\nUser: And plus three?",
            ],
        )
        sent = live.connection.session.commentary.append.await_args_list
        self.assertEqual(
            [(call.kwargs["delegation_id"], call.kwargs["content"]) for call in sent],
            [("first", "Twelve."), ("second", "Fifteen.")],
        )

    async def test_agent_failure_still_answers_the_waiting_delegation(self):
        agent = Mock(run=AsyncMock(side_effect=RuntimeError), timings={}, last_run={})
        live = self.conversation(
            [words("input", 1000, " What time is it?"), delegation("only", 1400)],
            agent,
        )
        await self.finish(live)
        live.connection.session.commentary.append.assert_awaited_once_with(
            event_id="result-0", delegation_id="only", content=FAILED_REPLY
        )
        self.assertEqual(live.delegations[0]["error"], "RuntimeError")

    async def test_only_session_errors_end_the_conversation(self):
        def error(client_event_id):
            return SimpleNamespace(
                type="error",
                client_event_id=client_event_id,
                error=SimpleNamespace(code="bad", message="rejected"),
            )

        agent = Mock(timings={}, last_run={})
        await self.conversation([error("result-0")], agent).receive()
        with self.assertRaisesRegex(RuntimeError, "slate.live: bad"):
            await self.conversation([error(None)], agent).receive()
