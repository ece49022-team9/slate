import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from livekit.agents import ChatMessage
from livekit.plugins.openai.realtime import GPTLiveDelegation
from slate.voice.worker import BACKEND_CONTEXT, FAILED_REPLY, Handoffs


def said(role: str, text: str) -> SimpleNamespace:
    return SimpleNamespace(item=ChatMessage(role=role, content=[text]))


class HandoffTests(unittest.IsolatedAsyncioTestCase):
    def handoffs(self, run: AsyncMock) -> tuple[Handoffs, Mock]:
        agent = Mock(run=run, timings={}, last_run={}, close=AsyncMock())
        return Handoffs(agent), Mock()

    async def test_each_handoff_sends_only_new_speech_under_its_own_id(self):
        replies = AsyncMock(side_effect=["Twelve.", "Fifteen."])
        handoffs, session = self.handoffs(replies)
        question = "What is seven plus five?"
        handoffs.delegate(session, GPTLiveDelegation("first", question))
        await asyncio.gather(*handoffs.tasks)
        handoffs.heard(said("user", question))
        handoffs.heard(said("assistant", "Twelve."))
        handoffs.delegate(session, GPTLiveDelegation("second", "And plus three?"))
        await asyncio.gather(*handoffs.tasks)
        self.assertEqual(
            [call.args[0] for call in handoffs.agent.run.await_args_list],
            [
                BACKEND_CONTEXT + "User: What is seven plus five?",
                BACKEND_CONTEXT + "Slate: Twelve.\nUser: And plus three?",
            ],
        )
        sent = session.append_commentary.call_args_list
        self.assertEqual(
            [(call.kwargs["delegation_id"], call.args[0]) for call in sent],
            [("first", "Twelve."), ("second", "Fifteen.")],
        )

    async def test_handoffs_reach_hermes_in_the_order_they_arrived(self):
        started = []

        async def run(message: str) -> str:
            started.append(message.rsplit("User: ", 1)[1])
            await asyncio.sleep(0.01 if message.endswith("first?") else 0)
            return "ok"

        handoffs, session = self.handoffs(AsyncMock(side_effect=run))
        handoffs.delegate(session, GPTLiveDelegation("a", "first?"))
        handoffs.delegate(session, GPTLiveDelegation("b", "second?"))
        await asyncio.gather(*handoffs.tasks)
        self.assertEqual(started, ["first?", "second?"])

    async def test_hermes_failure_still_answers_the_waiting_delegation(self):
        handoffs, session = self.handoffs(AsyncMock(side_effect=RuntimeError))
        handoffs.delegate(session, GPTLiveDelegation("only", "What time is it?"))
        await asyncio.gather(*handoffs.tasks)
        session.append_commentary.assert_called_once_with(
            FAILED_REPLY, delegation_id="only"
        )
        self.assertEqual(handoffs.records[0]["error"], "RuntimeError")
