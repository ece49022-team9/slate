import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from openai import NotFoundError
from openai.lib.beta.agents._result import (
    AgentTurnResultCollection,
    AgentTurnResultError,
)
from openai.types.beta.agent_session import Agent as SessionAgent
from openai.types.beta.agent_session import AgentSession
from openai.types.beta.agent_session_assistant_message import (
    AgentSessionAssistantMessage,
)
from openai.types.beta.agent_session_created_event import AgentSessionCreatedEvent
from openai.types.beta.agent_session_idle_event import AgentSessionIdleEvent
from openai.types.beta.agent_session_message import AgentSessionMessage
from openai.types.beta.agent_session_turn_completed_event import (
    AgentSessionTurnCompletedEvent,
)
from openai.types.beta.agent_session_turn_created_event import (
    AgentSessionTurnCreatedEvent,
)
from openai.types.beta.agent_session_turn_failed_event import (
    AgentSessionTurnFailedEvent,
)
from openai.types.beta.agent_session_turn_item_added_event import (
    AgentSessionTurnItemAddedEvent,
)
from openai.types.beta.agent_session_turn_item_done_event import (
    AgentSessionTurnItemDoneEvent,
)
from openai.types.beta.agent_session_turn_output_text_delta_event import (
    AgentSessionTurnOutputTextDeltaEvent,
)
from openai.types.beta.agent_session_turn_output_text_done_event import (
    AgentSessionTurnOutputTextDoneEvent,
)
from openai.types.beta.agent_session_turn_reasoning_summary_text_delta_event import (
    AgentSessionTurnReasoningSummaryTextDeltaEvent,
)
from openai.types.beta.agents.sessions.turn import Turn as ManagedTurn
from openai.types.beta.output_text import OutputText
from slate.agent.managed import ManagedAgent


def session(model="gpt-6.1-sol", required_actions=None):
    return AgentSession.model_construct(
        id="sess-a",
        agent=SessionAgent.model_construct(model=model),
        status="idle",
        required_actions=required_actions or [],
    )


def turn(status, turn_id="turn-a"):
    return ManagedTurn(
        id=turn_id,
        agent_id="agent-a",
        created_at=0,
        object="agent.session.turn",
        session_id="sess-a",
        status=status,
    )


def events(*, text="Final answer", status="completed", commentary=True):
    result = [
        AgentSessionCreatedEvent(
            type="agent.session.created", event_id="created", session=session()
        ),
        AgentSessionIdleEvent(
            type="agent.session.idle", event_id="initial-idle", session=session()
        ),
        AgentSessionTurnCreatedEvent(
            type="agent.session.turn.created",
            event_id="turn-created",
            session_id="sess-a",
            turn_id="turn-a",
            turn=turn("in_progress"),
        ),
    ]
    for index, (phase, content) in enumerate(
        [("commentary", "Do not speak this progress"), ("final_answer", text)]
    ):
        if phase == "commentary" and not commentary:
            continue
        result.append(
            AgentSessionTurnItemDoneEvent.model_construct(
                type="agent.session.turn.item.done",
                event_id=f"message-{index}",
                session_id="sess-a",
                turn_id="turn-a",
                output_index=index,
                item=AgentSessionAssistantMessage(
                    id=f"message-{index}",
                    type="message",
                    turn_id="turn-a",
                    role="assistant",
                    phase=phase,
                    status="completed",
                    content=[
                        OutputText(type="output_text", text=content, annotations=[])
                    ],
                ),
            )
        )
    cls = (
        AgentSessionTurnCompletedEvent
        if status == "completed"
        else AgentSessionTurnFailedEvent
    )
    result.extend(
        [
            cls(
                type=f"agent.session.turn.{status}",
                event_id="terminal",
                session_id="sess-a",
                turn_id="turn-a",
                turn=turn(status),
            ),
            AgentSessionIdleEvent(
                type="agent.session.idle", event_id="final-idle", session=session()
            ),
        ]
    )
    return result


class FakeStream:
    def __init__(self, items, *, failure=None, wait=False):
        self.items = iter(items)
        self.failure = failure
        self.wait = wait
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.collection = AgentTurnResultCollection()
        self.enabled = False
        self.closed = False
        self.final_calls = 0

    def with_result_collection(self):
        self.enabled = True
        self.collection.enable()
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.enabled:
            raise AssertionError("Collection must start before iteration")
        try:
            event = next(self.items)
        except StopIteration:
            self.started.set()
            if self.wait:
                await self.release.wait()
            if self.failure:
                raise self.failure from None
            raise StopAsyncIteration from None
        self.collection.accept(event)
        return event

    async def get_final_result(self):
        self.final_calls += 1
        return self.collection.result()


def client_for(*streams):
    sessions = SimpleNamespace(
        create=AsyncMock(return_value=streams[0]),
        stream=Mock(side_effect=streams[1:]),
        retrieve=AsyncMock(return_value=session()),
        delete=AsyncMock(),
        events=SimpleNamespace(create=AsyncMock()),
    )
    return SimpleNamespace(
        beta=SimpleNamespace(agents=SimpleNamespace(sessions=sessions)),
        close=AsyncMock(),
    )


class ManagedAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_reply_deltas_require_known_final_answer_items_of_the_root_turn(self):
        items = events()
        extra = []
        for index, (phase, turn_id, delta) in enumerate(
            [
                ("commentary", "turn-a", "I'll use tools."),
                (None, "turn-a", "Unclassified"),
                ("final_answer", "turn-child", "Child answer"),
                ("final_answer", "turn-a", "Final"),
            ]
        ):
            item_id = "message-1" if index == 3 else f"extra-message-{index}"
            extra.extend(
                [
                    AgentSessionTurnItemAddedEvent.model_construct(
                        type="agent.session.turn.item.added",
                        event_id=f"added-{index}",
                        session_id="sess-a",
                        turn_id=turn_id,
                        output_index=index,
                        item=AgentSessionMessage(
                            id=item_id,
                            type="message",
                            role="assistant",
                            turn_id=turn_id,
                            phase=phase,
                            status="in_progress",
                            content=[],
                        ),
                    ),
                    AgentSessionTurnOutputTextDeltaEvent(
                        type="agent.session.turn.output_text.delta",
                        event_id=f"delta-{index}",
                        session_id="sess-a",
                        turn_id=turn_id,
                        item_id=item_id,
                        output_index=index,
                        content_index=0,
                        delta=delta,
                    ),
                ]
            )
        extra.append(
            AgentSessionTurnOutputTextDeltaEvent(
                type="agent.session.turn.output_text.delta",
                event_id="final-space",
                session_id="sess-a",
                turn_id="turn-a",
                item_id="message-1",
                output_index=3,
                content_index=0,
                delta=" ",
            )
        )
        extra.append(
            AgentSessionTurnOutputTextDeltaEvent(
                type="agent.session.turn.output_text.delta",
                event_id="final-tail",
                session_id="sess-a",
                turn_id="turn-a",
                item_id="message-1",
                output_index=3,
                content_index=0,
                delta="answer",
            )
        )
        items[3:3] = extra
        progress = AsyncMock()
        client = client_for(FakeStream(items), FakeStream(events()[1:]))
        agent = ManagedAgent(client=client)
        self.assertEqual(
            await agent.run("Change it", progress, device_context="scope=current"),
            "Final answer",
        )
        replies = [
            call.args[0]
            for call in progress.await_args_list
            if call.args[0]["type"] in ("reply.delta", "answer.complete")
        ]
        self.assertEqual(
            replies,
            [
                {"type": "reply.delta", "delta": "Final"},
                {"type": "reply.delta", "delta": " "},
                {"type": "reply.delta", "delta": "answer"},
                {"type": "answer.complete", "text": "Final answer"},
            ],
        )
        submitted = client.beta.agents.sessions.create.call_args.kwargs
        self.assertIn("scope=current", submitted["input"])
        self.assertTrue(submitted["input"].endswith("Change it"))
        self.assertNotIn("scope=current", submitted["agent"]["instructions"])
        await agent.run("Again")
        self.assertEqual(
            client.beta.agents.sessions.stream.call_args.kwargs["input"], "Again"
        )
        await agent.close()

    async def test_reply_callback_failure_cancels_the_remote_turn(self):
        items = events()
        message = AgentSessionMessage(
            id="message-1",
            type="message",
            role="assistant",
            turn_id="turn-a",
            phase="final_answer",
            status="in_progress",
            content=[],
        )
        items[3:3] = [
            AgentSessionTurnItemAddedEvent.model_construct(
                type="agent.session.turn.item.added",
                event_id="added-final",
                session_id="sess-a",
                turn_id="turn-a",
                output_index=1,
                item=message,
            ),
            AgentSessionTurnOutputTextDeltaEvent(
                type="agent.session.turn.output_text.delta",
                event_id="delta-final",
                session_id="sess-a",
                turn_id="turn-a",
                item_id="message-1",
                output_index=1,
                content_index=0,
                delta="Final",
            ),
        ]
        client = client_for(FakeStream(items))
        agent = ManagedAgent(client=client)

        async def progress(event):
            if event["type"] == "reply.delta":
                raise ConnectionError("Reply sink closed")

        with self.assertRaisesRegex(ConnectionError, "Reply sink closed"):
            await agent.run("Hello", progress)
        client.beta.agents.sessions.events.create.assert_awaited_once()
        client.beta.agents.sessions.delete.assert_awaited_once()
        self.assertIsNone(agent.session_id)
        await agent.close()

    async def test_timings_use_root_visible_deltas_not_reasoning_done_or_subagents(
        self,
    ):
        items = events()
        extra = [
            AgentSessionTurnReasoningSummaryTextDeltaEvent(
                type="agent.session.turn.reasoning_summary_text.delta",
                event_id="reasoning",
                session_id="sess-a",
                turn_id="turn-a",
                item_id="reasoning-a",
                output_index=0,
                summary_index=0,
                delta="Private",
            ),
            AgentSessionTurnOutputTextDoneEvent(
                type="agent.session.turn.output_text.done",
                event_id="text-done",
                session_id="sess-a",
                turn_id="turn-a",
                item_id="message-1",
                output_index=1,
                content_index=0,
                text="Whole text",
            ),
            AgentSessionTurnCompletedEvent(
                type="agent.session.turn.completed",
                event_id="child-terminal",
                session_id="sess-a",
                turn_id="turn-child",
                turn=turn("completed", "turn-child"),
            ),
        ]
        for index, (turn_id, delta) in enumerate(
            [
                ("turn-child", "Child"),
                ("turn-a", ""),
                ("turn-a", " "),
                ("turn-a", "Hello"),
                ("turn-a", " again"),
            ]
        ):
            extra.append(
                AgentSessionTurnOutputTextDeltaEvent(
                    type="agent.session.turn.output_text.delta",
                    event_id=f"delta-{index}",
                    session_id="sess-a",
                    turn_id=turn_id,
                    item_id="message-1",
                    output_index=1,
                    content_index=0,
                    delta=delta,
                )
            )
        items[3:3] = extra
        snapshots = {}
        client = client_for(FakeStream(items), FakeStream(events()[1:]))
        agent = ManagedAgent(client=client)

        async def progress(event):
            if "event_id" in event:
                snapshots[event["event_id"]] = agent.timings

        await agent.run("Hello", progress)
        measured = agent.timings
        self.assertEqual(
            set(measured),
            {
                "requested",
                "lock_acquired",
                "session_ready",
                "admitted",
                "first_event",
                "first_text",
                "last_text",
                "terminal",
                "completed",
            },
        )
        self.assertTrue(all(isinstance(value, int) for value in measured.values()))
        self.assertEqual(list(measured.values()), sorted(measured.values()))
        for event_id in (
            "reasoning",
            "text-done",
            "child-terminal",
            "delta-0",
            "delta-1",
            "delta-2",
        ):
            self.assertNotIn("first_text", snapshots[event_id])
        self.assertNotIn("terminal", snapshots["child-terminal"])
        self.assertEqual(measured["first_event"], snapshots["created"]["first_event"])
        self.assertEqual(measured["first_text"], snapshots["delta-3"]["first_text"])
        self.assertEqual(measured["last_text"], snapshots["delta-4"]["last_text"])
        self.assertGreater(measured["last_text"], measured["first_text"])
        self.assertEqual(measured["terminal"], snapshots["terminal"]["terminal"])
        measured.clear()
        self.assertIn("completed", agent.timings)
        await agent.run("Again")
        self.assertNotIn("first_text", agent.timings)
        self.assertNotIn("last_text", agent.timings)
        await agent.close()

    async def test_final_result_excludes_commentary_and_keeps_session(self):
        first = FakeStream(events(text="Remember copper"))
        second = FakeStream(events(text="copper")[1:])
        client = client_for(first, second)
        agent = ManagedAgent(client=client)
        progress = AsyncMock()
        self.assertEqual(
            await agent.run("Remember copper", progress), "Remember copper"
        )
        self.assertEqual(await agent.run("Recall it"), "copper")
        self.assertEqual(agent.session_id, "sess-a")
        self.assertEqual(first.final_calls, 1)
        self.assertEqual(second.final_calls, 1)
        self.assertEqual(client.beta.agents.sessions.create.await_count, 1)
        client.beta.agents.sessions.stream.assert_called_once_with(
            "sess-a", input="Recall it", timeout=300
        )
        self.assertEqual(agent.last_run["runtime"]["model"], "gpt-6.1-sol")
        self.assertTrue(
            any("commentary" in str(call) for call in progress.call_args_list)
        )
        await agent.close()
        client.beta.agents.sessions.delete.assert_awaited_once_with("sess-a", timeout=5)
        self.assertIsNone(agent.session_id)

    async def test_idle_without_completed_root_turn_is_not_a_reply(self):
        stream = FakeStream(events()[:2])
        client = client_for(stream)
        agent = ManagedAgent(client=client)
        with self.assertRaises(AgentTurnResultError) as raised:
            await agent.run("Hello")
        self.assertEqual(raised.exception.reason, "incomplete")
        self.assertIsNone(agent.session_id)
        self.assertTrue(stream.closed)

    async def test_failed_turn_does_not_return_partial_output(self):
        client = client_for(FakeStream(events(status="failed")))
        agent = ManagedAgent(client=client)
        with self.assertRaises(AgentTurnResultError) as raised:
            await agent.run("Hello")
        self.assertEqual(raised.exception.reason, "failed")
        self.assertEqual(agent.last_run["status"], "failed")
        self.assertIsNone(agent.session_id)
        self.assertIn("terminal", agent.timings)
        self.assertIn("completed", agent.timings)
        self.assertNotIn("first_text", agent.timings)

    async def test_broken_stream_cancels_and_deletes_session(self):
        stream = FakeStream(events()[:3], failure=ConnectionError("disconnected"))
        client = client_for(stream)
        agent = ManagedAgent(client=client)
        with self.assertRaises(ConnectionError):
            await agent.run("Hello")
        cancel = client.beta.agents.sessions.events.create.call_args.kwargs["events"]
        self.assertEqual(cancel, [{"type": "agent.session.input.cancel"}])
        client.beta.agents.sessions.delete.assert_awaited_once()
        self.assertIsNone(agent.session_id)
        self.assertTrue(stream.closed)

    async def test_timeout_and_task_cancellation_cleanup_remote_turn(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                stream = FakeStream(events()[:3], wait=True)
                client = client_for(stream)
                agent = ManagedAgent(client=client, timeout=0.01 if not cancel else 5)
                task = asyncio.create_task(agent.run("Hello"))
                await stream.started.wait()
                if cancel:
                    task.cancel()
                with self.assertRaises(
                    asyncio.CancelledError if cancel else TimeoutError
                ):
                    await task
                client.beta.agents.sessions.events.create.assert_awaited_once()
                client.beta.agents.sessions.delete.assert_awaited_once()
                self.assertIsNone(agent.session_id)
                self.assertIsNone(agent.run_id)

    async def test_close_cancels_active_task_and_prevents_new_turns(self):
        stream = FakeStream(events()[:3], wait=True)
        client = client_for(stream)
        agent = ManagedAgent(client=client)
        task = asyncio.create_task(agent.run("Hello"))
        await stream.started.wait()
        await agent.close()
        self.assertTrue(task.cancelled())
        client.close.assert_awaited_once()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await agent.run("Again")

    async def test_rejects_model_route_change_and_empty_reply(self):
        for wrong_model in (True, False):
            with self.subTest(wrong_model=wrong_model):
                items = events(text="")
                if wrong_model:
                    items[0] = AgentSessionCreatedEvent(
                        type="agent.session.created",
                        event_id="created",
                        session=session(model="other-model"),
                    )
                client = client_for(FakeStream(items))
                agent = ManagedAgent(client=client)
                with self.assertRaisesRegex(
                    RuntimeError, "model route" if wrong_model else "spoken reply"
                ):
                    await agent.run("Hello")
                self.assertIsNone(agent.session_id)

    async def test_approval_requires_current_run_and_exact_request(self):
        client = client_for(FakeStream(events()))
        agent = ManagedAgent(client=client, browser=True)
        agent.session_id = "sess-a"
        agent.run_id = "turn-a"
        approval = {
            "type": "computer_use_approval_request",
            "turn_id": "turn-a",
            "request_id": "request-a",
            "request": {
                "type": "browser_origin_access",
                "origin": "https://example.com",
            },
        }
        progress = AsyncMock()
        event = {
            "type": "agent.session.requires_action",
            "session": {"required_actions": [approval]},
        }
        await agent._progress(event, progress)
        self.assertEqual(progress.call_args.args[0]["type"], "approval.request")
        for run_id, request_id, choice in (
            ("turn-old", "request-a", "once"),
            ("turn-a", "request-old", "once"),
            ("turn-a", "request-a", "always"),
        ):
            with self.assertRaises(ValueError):
                await agent.approve(run_id, request_id, choice)
        await agent.approve("turn-a", "request-a", "once")
        submitted = client.beta.agents.sessions.events.create.call_args.kwargs["events"]
        self.assertEqual(submitted[0]["request_id"], "request-a")
        self.assertEqual(
            submitted[0]["response"],
            {"type": "browser_origin_access", "decision": "approve"},
        )
        with self.assertRaises(ValueError):
            await agent.approve("turn-a", "request-a", "once")

    async def test_pending_origin_with_no_callback_fails_instead_of_hanging(self):
        client = client_for(FakeStream(events()))
        agent = ManagedAgent(client=client)
        agent.run_id = "turn-a"
        with self.assertRaisesRegex(RuntimeError, "external action"):
            await agent._progress(
                {
                    "type": "agent.session.requires_action",
                    "session": {
                        "required_actions": [
                            {
                                "type": "computer_use_approval_request",
                                "turn_id": "turn-a",
                                "request_id": "request-a",
                                "request": {"type": "browser_origin_access"},
                            }
                        ]
                    },
                },
                None,
            )

    async def test_failed_deletion_is_reported_and_retried_before_reuse(self):
        stream = FakeStream(events()[:3], failure=ConnectionError("disconnected"))
        client = client_for(stream)
        client.beta.agents.sessions.delete.side_effect = ConnectionError("offline")
        agent = ManagedAgent(client=client)
        with self.assertRaises(ConnectionError):
            await agent.run("Hello")
        self.assertEqual(agent.session_id, "sess-a")
        with self.assertRaisesRegex(RuntimeError, "cleanup"):
            await agent.run("Again")
        self.assertEqual(client.beta.agents.sessions.create.await_count, 1)
        with self.assertRaisesRegex(RuntimeError, "could not be deleted"):
            await agent.close()
        client.close.assert_not_awaited()
        client.beta.agents.sessions.delete.side_effect = None
        await agent.close()
        self.assertIsNone(agent.session_id)
        client.close.assert_awaited_once()

    async def test_browser_uses_hosted_desktop_and_caller_instructions(self):
        client = client_for(FakeStream(events()))
        agent = ManagedAgent(client=client, browser=True, instructions="Read this page")
        await agent.run("Hello")
        payload = client.beta.agents.sessions.create.call_args.kwargs
        self.assertEqual(payload["agent"]["instructions"], "Read this page")
        self.assertEqual(payload["environment"]["type"], "openai_hosted")
        self.assertTrue(payload["environment"]["desktop"]["enabled"])
        self.assertEqual(
            payload["agent"]["tools"],
            [{"type": "computer_use", "include_screenshots": True}],
        )
        await agent.close()

    async def test_already_deleted_session_is_successful_cleanup(self):
        client = client_for(FakeStream(events()))
        client.beta.agents.sessions.delete.side_effect = NotFoundError(
            "Session absent",
            response=Mock(status_code=404, headers={}, request=Mock()),
            body=None,
        )
        agent = ManagedAgent(client=client, session_id="sess-a")
        await agent.close()
        self.assertIsNone(agent.session_id)
        client.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
