import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from slate.events import EventBus
from slate.tasks.manager import TaskManager
from slate.tasks.store import TaskStore
from slate.voice.audio import SAMPLE_RATE
from slate.voice.session import Hello, VoiceSession

RUN = "run_1"
REQUEST = "req_1"


class ScriptedAgent:
    def __init__(self, script=None) -> None:
        self.script = script
        self.pending = set()
        self.timings = {}
        self.last_run = {}
        self.choices = []
        self.decided = asyncio.Event()
        self.background_finished = AsyncMock()

    async def run(self, message, progress, *, device_context):
        if self.script is not None:
            return await self.script(self, progress)
        await progress({"type": "answer.complete", "text": "Done."})
        return "Done."

    async def approve(self, run_id, request_id, choice):
        if (run_id, request_id) != (RUN, REQUEST):
            raise ValueError("This agent run has ended")
        self.choices.append(choice)
        self.decided.set()

    async def close(self):
        pass


async def asks_to_pay(agent, progress):
    await progress({"type": "tool.started", "tool": "browser_click"})
    await progress(
        {
            "type": "approval.request",
            "run_id": RUN,
            "request_id": REQUEST,
            "command": "Pay $84 to Duke Energy",
        }
    )
    await agent.decided.wait()
    await progress({"type": "tool.completed", "tool": "browser_click"})
    answer = "Paid." if agent.choices == ["once"] else "I did not pay."
    await progress({"type": "answer.complete", "text": answer})
    return answer


async def gives_up(agent, progress):
    await progress(
        {
            "type": "approval.request",
            "run_id": RUN,
            "request_id": REQUEST,
            "command": "Send the email",
        }
    )
    await progress({"type": "answer.complete", "text": "I stopped waiting."})
    return "I stopped waiting."


async def fails(agent, progress):
    raise RuntimeError("model down")


async def silent(text, *, timings):
    if False:
        yield b""


class VoiceTaskTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.events = EventBus()
        self.manager = TaskManager(
            TaskStore(Path(self.dir.name) / "tasks.db"), self.events
        )
        self.store = self.manager.store
        self.heard = ""

        async def transcribe(chunks, timings=None):
            async for _ in chunks:
                pass
            if self.heard:
                yield self.heard

        patches = [
            patch("slate.voice.session.transcribe_stream", transcribe),
            patch("slate.voice.session.speak_stream", silent),
            patch("slate.voice.session.APPROVAL_SECONDS", 30),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    async def asyncTearDown(self):
        self.manager.close()
        self.dir.cleanup()

    def session(self, agent, device_id=None):
        socket = Mock(send_text=AsyncMock(), send_bytes=AsyncMock(), close=AsyncMock())
        with patch("slate.voice.session.create_agent", return_value=agent):
            return VoiceSession(
                socket,
                Hello(type="hello", rate=SAMPLE_RATE),
                self.events,
                self.manager,
                device_id,
            )

    async def talk(self, session, text):
        self.heard = text
        await session.start_turn()
        work = session.turn_task
        turn_id = session.turn.id
        await session.end_turn()
        return work, turn_id

    async def pending(self):
        async with asyncio.timeout(2):
            while not self.store.pending_approvals():
                await asyncio.sleep(0.01)
        return self.store.pending_approvals()[0]

    def sent(self, session):
        return [
            json.loads(call.args[0])
            for call in session.socket.send_text.await_args_list
        ]

    def of(self, kind):
        return [event for event in self.events.since(None) if event["type"] == kind]

    def states(self):
        return [event["data"]["state"] for event in self.of("device.status")]

    async def test_turn_without_speech_makes_no_task(self):
        session = self.session(ScriptedAgent())
        work, _ = await self.talk(session, "")
        await asyncio.wait_for(work, 2)
        await session.close()
        self.assertEqual(self.store.recent(), [])
        self.assertEqual(self.states(), ["listen", "transcribe", "idle", "idle"])
        self.assertFalse(self.of("device.status")[-1]["data"]["online"])

    async def test_spoken_request_becomes_a_task_that_succeeds(self):
        session = self.session(ScriptedAgent(), device_id="dev_kitchen")
        work, turn_id = await self.talk(session, "What is on my calendar?")
        await asyncio.wait_for(work, 2)
        [task] = self.store.recent()
        self.assertEqual(task.title, "What is on my calendar?")
        self.assertEqual(task.kind, "voice")
        self.assertEqual(task.session_id, turn_id)
        self.assertEqual(task.state.value, "succeeded")
        steps = [event["data"]["step_id"] for event in self.of("agent.step")]
        self.assertEqual(steps, ["request", "response"])
        device_events = self.of("device.status") + self.of("agent.step")
        self.assertEqual(
            {event["device_id"] for event in device_events}, {"dev_kitchen"}
        )
        self.assertEqual(self.states(), ["listen", "transcribe", "respond", "idle"])
        await session.close()

    async def test_web_approval_reaches_the_agent(self):
        agent = ScriptedAgent(asks_to_pay)
        session = self.session(agent)
        work, _ = await self.talk(session, "Pay my electric bill")
        approval = await self.pending()
        self.assertEqual(approval.summary, "Pay $84 to Duke Energy")
        [asked] = [event for event in self.sent(session) if event["type"] == "approval"]
        self.assertEqual(asked["approval_id"], approval.id)
        self.manager.decide(approval.id, True, actor="web")
        await asyncio.wait_for(work, 2)
        self.assertEqual(agent.choices, ["once"])
        [closed] = [e for e in self.sent(session) if e["type"] == "approval_resolved"]
        self.assertEqual(
            (closed["request_id"], closed["status"], closed["decided_by"]),
            (REQUEST, "approved", "web"),
        )
        task = self.store.get(approval.task_id)
        self.assertEqual(task.state.value, "succeeded")
        self.assertEqual(self.store.get_approval(approval.id).decided_by, "web")
        tool = [e["data"] for e in self.of("agent.step") if e["data"]["kind"] == "tool"]
        self.assertEqual(
            [(s["title"], s["status"]) for s in tool],
            [("browser_click", "running"), ("browser_click", "done")],
        )
        await session.close()

    async def test_device_answer_goes_through_the_task_store(self):
        agent = ScriptedAgent(asks_to_pay)
        session = self.session(agent)
        work, turn_id = await self.talk(session, "Pay my electric bill")
        approval = await self.pending()
        await session.control(
            {
                "type": "approve",
                "turn_id": turn_id,
                "run_id": RUN,
                "request_id": REQUEST,
                "choice": "deny",
            }
        )
        await asyncio.wait_for(work, 2)
        self.assertEqual(agent.choices, ["deny"])
        closed = self.store.get_approval(approval.id)
        self.assertEqual((closed.status, closed.decided_by), ("denied", "device"))
        self.assertEqual(self.store.get(approval.task_id).state.value, "cancelled")
        await session.close()

    async def test_second_answer_is_refused(self):
        agent = ScriptedAgent(asks_to_pay)
        session = self.session(agent)
        work, turn_id = await self.talk(session, "Pay my electric bill")
        approval = await self.pending()
        self.manager.decide(approval.id, True, actor="web")
        await asyncio.sleep(0)
        await session.approve(
            {
                "turn_id": turn_id,
                "run_id": RUN,
                "request_id": REQUEST,
                "choice": "deny",
            }
        )
        await asyncio.wait_for(work, 2)
        self.assertEqual(agent.choices, ["once"])
        errors = [event for event in self.sent(session) if event["type"] == "error"]
        self.assertTrue(errors)
        await session.close()

    async def test_unanswered_approval_times_out_and_is_denied(self):
        agent = ScriptedAgent(asks_to_pay)
        session = self.session(agent)
        with patch("slate.voice.session.APPROVAL_SECONDS", 0.05):
            work, _ = await self.talk(session, "Pay my electric bill")
            approval = await self.pending()
            await asyncio.wait_for(work, 2)
        self.assertEqual(agent.choices, ["deny"])
        self.assertEqual(self.store.get_approval(approval.id).status, "timed_out")
        self.assertEqual(self.store.get(approval.task_id).state.value, "timed_out")
        await session.close()

    async def test_cancel_from_the_web_stops_the_turn(self):
        agent = ScriptedAgent(asks_to_pay)
        session = self.session(agent)
        work, turn_id = await self.talk(session, "Pay my electric bill")
        approval = await self.pending()
        self.manager.cancel(approval.task_id, actor="web")
        await session.task_cancelled(approval.task_id)
        self.assertTrue(work.done())
        self.assertIsNone(session.turn)
        cancelled = [e for e in self.sent(session) if e["type"] == "cancelled"]
        self.assertEqual(cancelled[0]["turn_id"], turn_id)
        task = self.store.get(approval.task_id)
        self.assertEqual(
            (task.state.value, task.reason), ("cancelled", "cancelled by web")
        )
        await session.close()

    async def test_new_turn_cancels_the_unfinished_task(self):
        agent = ScriptedAgent(asks_to_pay)
        session = self.session(agent)
        await self.talk(session, "Pay my electric bill")
        approval = await self.pending()
        self.heard = ""
        await session.start_turn()
        task = self.store.get(approval.task_id)
        self.assertEqual(task.state.value, "cancelled")
        self.assertEqual(self.store.get_approval(approval.id).status, "cancelled")
        await session.close()

    async def test_run_that_ends_before_the_answer_closes_the_approval(self):
        session = self.session(ScriptedAgent(gives_up))
        work, _ = await self.talk(session, "Email my professor")
        await asyncio.wait_for(work, 2)
        [task] = self.store.recent()
        self.assertEqual(
            (task.state.value, task.reason), ("cancelled", "cancelled by agent")
        )
        [approval] = self.store.approvals(task.id)
        self.assertEqual(approval.status, "cancelled")
        await asyncio.sleep(0.05)
        closed = [e for e in self.sent(session) if e["type"] == "approval_resolved"]
        self.assertEqual(len(closed), 0)
        await session.close()

    async def test_failed_turn_fails_the_task(self):
        session = self.session(ScriptedAgent(fails))
        work, _ = await self.talk(session, "Book a table")
        await asyncio.wait_for(work, 2)
        [task] = self.store.recent()
        self.assertEqual(task.state.value, "failed")
        self.assertEqual(task.reason, "Assistant response failed")
        self.assertIn("error", self.states())
        await session.close()

    async def test_background_work_is_its_own_task(self):
        agent = ScriptedAgent()
        session = self.session(agent)
        session.turn = turn = Mock(id="turn_1")
        await session.agent_progress(
            turn,
            {
                "type": "subagent.start",
                "depth": 0,
                "delegation_id": "deleg_a",
                "goal": "Compare flight prices",
            },
        )
        [task] = self.store.recent()
        self.assertEqual((task.kind, task.state.value), ("background", "running"))
        self.assertEqual(task.title, "Compare flight prices")
        agent.pending = {"deleg_a"}

        async def finish():
            agent.pending.clear()
            return {"deleg_a"}

        agent.background_finished.side_effect = finish
        session.turn = None
        session.announce = AsyncMock()
        await asyncio.wait_for(session.report_background(), 2)
        self.assertEqual(self.store.get(task.id).state.value, "succeeded")
        await session.close()

    async def test_without_a_task_manager_approvals_go_straight_to_the_agent(self):
        agent = ScriptedAgent(asks_to_pay)
        socket = Mock(send_text=AsyncMock(), send_bytes=AsyncMock(), close=AsyncMock())
        with patch("slate.voice.session.create_agent", return_value=agent):
            session = VoiceSession(socket, Hello(type="hello", rate=SAMPLE_RATE))
        work, turn_id = await self.talk(session, "Pay my electric bill")
        async with asyncio.timeout(2):
            while not any(e["type"] == "approval" for e in self.sent(session)):
                await asyncio.sleep(0.01)
        await session.approve(
            {"turn_id": turn_id, "run_id": RUN, "request_id": REQUEST, "choice": "once"}
        )
        await asyncio.wait_for(work, 2)
        self.assertEqual(agent.choices, ["once"])
        await session.close()


if __name__ == "__main__":
    unittest.main()
