import asyncio
import json
import unittest
from collections.abc import AsyncIterator, Callable
from unittest.mock import AsyncMock, Mock

import httpx
from slate.agent.agent import Agent
from slate.voice.session import VoiceSession
from slate.voice.turn import Turn

MODEL = "gpt-6.1-sol"
PROVIDER = "openai-codex"


def completed(**fields) -> dict:
    return {
        "run_id": "run-a",
        "status": "completed",
        "runtime": {"model": MODEL, "provider": PROVIDER},
        "output": "The task is finished.",
        **fields,
    }


def events(*payloads: dict) -> bytes:
    return b"".join(
        f"id: {event.get('seq', index)}\ndata: {json.dumps(event)}\n\n".encode()
        for index, event in enumerate(payloads)
    )


class EventStream(httpx.AsyncByteStream):
    def __init__(
        self,
        content: bytes = b"",
        *,
        disconnected: bool = False,
        blocked: asyncio.Event | None = None,
    ) -> None:
        self.content = content
        self.disconnected = disconnected
        self.blocked = blocked
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self.content:
            yield self.content
        if self.disconnected:
            raise httpx.ReadError("Event connection lost")
        if self.blocked:
            self.blocked.set()
            await asyncio.Future()

    async def aclose(self) -> None:
        self.closed = True


class AgentContractTests(unittest.IsolatedAsyncioTestCase):
    def agent(
        self,
        handler: Callable,
        *,
        session_id: str | None = "session-a",
    ) -> Agent:
        client = httpx.AsyncClient(
            base_url="http://hermes.test", transport=httpx.MockTransport(handler)
        )
        agent = Agent(
            client=client, session_id=session_id, model=MODEL, provider=PROVIDER
        )
        self.addAsyncCleanup(agent.close)
        return agent

    async def test_lost_creation_ack_retries_same_work_and_new_turn_has_new_key(self):
        admitted: dict[str, str] = {}
        attempts: list[tuple[str, dict]] = []

        def handle(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path == "/v1/runs":
                key = request.headers["Idempotency-Key"]
                attempts.append((key, json.loads(request.content)))
                admitted.setdefault(key, f"run-{len(admitted)}")
                if len(attempts) == 1:
                    raise httpx.ReadError("Acknowledgment lost", request=request)
                return httpx.Response(202, json={"run_id": admitted[key]})
            if request.url.path.endswith("/events"):
                return httpx.Response(200, content=b": stream closed\n\n")
            if request.method == "GET":
                return httpx.Response(200, json=completed())
            self.fail(f"Unexpected request: {request.method} {request.url.path}")

        agent = self.agent(handle)
        self.assertEqual(await agent.run("Open the dashboard"), "The task is finished.")
        self.assertEqual(await agent.run("Read the heading"), "The task is finished.")
        self.assertEqual(len(admitted), 2)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(attempts[0], attempts[1])
        self.assertNotEqual(attempts[1][0], attempts[2][0])
        self.assertEqual(attempts[0][1]["provider"], PROVIDER)
        self.assertEqual(attempts[0][1]["model"], MODEL)

    async def test_cancel_during_admission_recovers_receipt_and_stops_accepted_run(
        self,
    ):
        accepted = asyncio.Event()
        receipt = asyncio.Event()
        stopped = []
        attempts = []

        async def handle(request):
            if request.url.path == "/v1/runs":
                attempts.append(request.headers["Idempotency-Key"])
                accepted.set()
                await receipt.wait()
                return httpx.Response(202, json={"run_id": "run-a"})
            if request.url.path.endswith("/stop"):
                stopped.append(request.url.path)
                return httpx.Response(200, json={})
            self.fail(f"Unexpected request: {request.url.path}")

        agent = self.agent(handle)
        running = asyncio.create_task(agent.run("Apply the synthetic test setting"))
        self.addAsyncCleanup(self.cancel_task, running)
        await asyncio.wait_for(accepted.wait(), 1)
        running.cancel()
        receipt.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(running, 2)
        self.assertEqual(stopped, ["/v1/runs/run-a/stop"])
        self.assertEqual(len(attempts), 1)
        self.assertIsNone(agent.run_id)

    async def test_cancel_recovers_stalled_receipt_with_same_key(self):
        accepted = asyncio.Event()
        attempts = []
        stopped = []

        async def handle(request):
            if request.url.path == "/v1/runs":
                attempts.append(
                    (request.headers["Idempotency-Key"], json.loads(request.content))
                )
                if len(attempts) == 1:
                    accepted.set()
                    await asyncio.Future()
                return httpx.Response(202, json={"run_id": "run-a"})
            if request.url.path.endswith("/stop"):
                stopped.append(request.url.path)
                return httpx.Response(200, json={})
            self.fail(f"Unexpected request: {request.url.path}")

        agent = self.agent(handle)
        running = asyncio.create_task(agent.run("Apply the synthetic test setting"))
        self.addAsyncCleanup(self.cancel_task, running)
        await asyncio.wait_for(accepted.wait(), 1)
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(running, 7)
        self.assertEqual(stopped, ["/v1/runs/run-a/stop"])
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0], attempts[1])
        self.assertIsNone(agent.run_id)

    async def test_new_agents_create_unique_sessions_and_keep_their_own_history(self):
        titles: list[str] = []
        run_sessions: list[str] = []

        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/sessions":
                titles.append(json.loads(request.content)["title"])
                return httpx.Response(
                    201, json={"session": {"id": f"session-{len(titles)}"}}
                )
            if request.url.path == "/v1/runs":
                run_sessions.append(json.loads(request.content)["session_id"])
                return httpx.Response(202, json={"run_id": "run-a"})
            if request.url.path.endswith("/events"):
                return httpx.Response(200, content=b": stream closed\n\n")
            return httpx.Response(200, json=completed())

        first = self.agent(handle, session_id=None)
        second = self.agent(handle, session_id=None)
        await first.run("First conversation")
        await second.run("Another conversation")
        await first.run("Continue my conversation")
        self.assertEqual(len(titles), 2)
        self.assertNotEqual(titles[0], titles[1])
        self.assertEqual(run_sessions, ["session-1", "session-2", "session-1"])

    async def test_commentary_is_progress_and_only_final_status_output_is_spoken(self):
        progress = AsyncMock()
        content = events(
            {"event": "tool.started", "seq": 0, "tool": "browser_navigate"},
            {"event": "assistant.message", "seq": 1, "text": "Still looking."},
            {"event": "run.completed", "seq": 2, "output": "Stream text."},
        )

        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/runs":
                return httpx.Response(202, json={"run_id": "run-a"})
            if request.url.path.endswith("/events"):
                return httpx.Response(200, content=content)
            return httpx.Response(
                200, json=completed(output="  The heading is Slate.  ")
            )

        agent = self.agent(handle)
        self.assertEqual(
            await agent.run("Read the heading", progress), "The heading is Slate."
        )
        self.assertEqual(
            [call.args[0]["type"] for call in progress.await_args_list],
            ["tool.started", "assistant.message", "run.completed"],
        )
        self.assertEqual(agent.last_run["output"], "  The heading is Slate.  ")

    async def test_disconnected_events_resume_same_run_and_ignore_replay_duplicates(
        self,
    ):
        progress = AsyncMock()
        subscriptions: list[httpx.Request] = []
        creation: list[httpx.Request] = []
        statuses = 0
        first = {"event": "tool.started", "seq": 0, "tool": "browser_navigate"}
        second = {"event": "tool.completed", "seq": 1, "tool": "browser_navigate"}

        def handle(request: httpx.Request) -> httpx.Response:
            nonlocal statuses
            if request.url.path == "/v1/runs":
                creation.append(request)
                return httpx.Response(202, json={"run_id": "run-a"})
            if request.url.path.endswith("/events"):
                subscriptions.append(request)
                stream = (
                    EventStream(events(first), disconnected=True)
                    if len(subscriptions) == 1
                    else EventStream(events(first, second))
                )
                return httpx.Response(200, stream=stream)
            statuses += 1
            return httpx.Response(
                200, json=completed(status="running") if statuses == 1 else completed()
            )

        agent = self.agent(handle)
        self.assertEqual(
            await agent.run("Open the dashboard", progress), "The task is finished."
        )
        self.assertEqual(len(creation), 1)
        self.assertEqual(len(subscriptions), 2)
        self.assertEqual(subscriptions[1].headers["Last-Event-ID"], "0")
        self.assertEqual(
            [request.url.path for request in subscriptions],
            ["/v1/runs/run-a/events", "/v1/runs/run-a/events"],
        )
        self.assertEqual(
            [call.args[0]["seq"] for call in progress.await_args_list], [0, 1]
        )

    async def test_server_route_mismatch_is_rejected_and_remote_run_is_stopped(self):
        for runtime in (
            {"model": "another-model", "provider": PROVIDER},
            {"model": MODEL, "provider": "another-provider"},
            {},
        ):
            with self.subTest(runtime=runtime):
                stopped: list[str] = []

                def handle(
                    request: httpx.Request, runtime=runtime, stopped=stopped
                ) -> httpx.Response:
                    if request.url.path == "/v1/runs":
                        return httpx.Response(202, json={"run_id": "run-a"})
                    if request.url.path.endswith("/events"):
                        return httpx.Response(200, content=b": stream closed\n\n")
                    if request.url.path.endswith("/stop"):
                        stopped.append(request.url.path)
                        return httpx.Response(200, json={})
                    return httpx.Response(200, json=completed(runtime=runtime))

                agent = self.agent(handle)
                with self.assertRaisesRegex(RuntimeError, "changed the model route"):
                    await agent.run("Open the dashboard")
                self.assertEqual(stopped, ["/v1/runs/run-a/stop"])
                self.assertIsNone(agent.run_id)

    async def test_failed_or_cancelled_run_cannot_return_partial_speech(self):
        for status in ("failed", "cancelled", "interrupted"):
            with self.subTest(status=status):
                stopped: list[str] = []

                def handle(
                    request: httpx.Request, status=status, stopped=stopped
                ) -> httpx.Response:
                    if request.url.path == "/v1/runs":
                        return httpx.Response(202, json={"run_id": "run-a"})
                    if request.url.path.endswith("/events"):
                        return httpx.Response(200, content=b": stream closed\n\n")
                    if request.url.path.endswith("/stop"):
                        stopped.append(request.url.path)
                        return httpx.Response(200, json={})
                    return httpx.Response(
                        200, json=completed(status=status, error="Browser failed")
                    )

                agent = self.agent(handle)
                with self.assertRaisesRegex(RuntimeError, f"{status}: Browser failed"):
                    async with asyncio.timeout(2):
                        await agent.run("Open the dashboard")
                self.assertEqual(stopped, ["/v1/runs/run-a/stop"])
                self.assertIsNone(agent.run_id)

    async def test_empty_completed_output_cannot_fall_back_to_commentary(self):
        stopped: list[str] = []

        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/runs":
                return httpx.Response(202, json={"run_id": "run-a"})
            if request.url.path.endswith("/events"):
                return httpx.Response(
                    200,
                    content=events({"event": "assistant.message", "text": "Looking."}),
                )
            if request.url.path.endswith("/stop"):
                stopped.append(request.url.path)
                return httpx.Response(200, json={})
            return httpx.Response(200, json=completed(output=" \n "))

        agent = self.agent(handle)
        with self.assertRaisesRegex(RuntimeError, "without a spoken reply"):
            await agent.run("Open the dashboard")
        self.assertEqual(stopped, ["/v1/runs/run-a/stop"])

    async def test_cancel_stops_remote_run_and_bounds_unresponsive_stop(self):
        entered = asyncio.Event()
        stop_entered = asyncio.Event()
        stream = EventStream(blocked=entered)
        stopped: list[str] = []

        async def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/runs":
                return httpx.Response(202, json={"run_id": "run-a"})
            if request.url.path.endswith("/events"):
                return httpx.Response(200, stream=stream)
            if request.url.path.endswith("/stop"):
                stopped.append(request.url.path)
                stop_entered.set()
                await asyncio.Future()
            self.fail(f"Unexpected request: {request.method} {request.url.path}")

        agent = self.agent(handle)
        run = asyncio.create_task(agent.run("Open the dashboard"))
        self.addAsyncCleanup(self.cancel_task, run)
        await asyncio.wait_for(entered.wait(), 1)
        run.cancel()
        await asyncio.wait_for(stop_entered.wait(), 1)
        with self.assertLogs("slate.agent", level="ERROR"):
            with self.assertRaises(asyncio.CancelledError):
                async with asyncio.timeout(6):
                    await run
        self.assertEqual(stopped, ["/v1/runs/run-a/stop"])
        self.assertIsNone(agent.run_id)
        self.assertTrue(stream.closed)

    @staticmethod
    async def cancel_task(task: asyncio.Task) -> None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_approval_binds_exact_run_and_request_and_refuses_broad_grants(self):
        approvals: list[tuple[str, dict]] = []

        def handle(request: httpx.Request) -> httpx.Response:
            approvals.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, json={"resolved": 1})

        agent = self.agent(handle)
        agent.run_id = "run-a"
        for run_id, request_id, choice in (
            ("run-b", "approval-a", "once"),
            ("run-a", "", "once"),
            ("run-a", "approval-a", "always"),
            ("run-a", "approval-a", "session"),
            ("run-a", "approval-a", "allow"),
        ):
            with self.subTest(run_id=run_id, request_id=request_id, choice=choice):
                with self.assertRaises(ValueError):
                    await agent.approve(run_id, request_id, choice)
        self.assertEqual(approvals, [])
        await agent.approve("run-a", "approval-a", "once")
        await agent.approve("run-a", "approval-b", "deny")
        self.assertEqual(
            approvals,
            [
                (
                    "/v1/runs/run-a/approval",
                    {"request_id": "approval-a", "choice": "once"},
                ),
                (
                    "/v1/runs/run-a/approval",
                    {"request_id": "approval-b", "choice": "deny"},
                ),
            ],
        )
        agent.run_id = None
        with self.assertRaises(ValueError):
            await agent.approve("run-a", "approval-a", "once")
        self.assertEqual(len(approvals), 2)

    async def test_approval_rejection_is_reported_without_retrying_or_widening_scope(
        self,
    ):
        approvals: list[dict] = []

        def handle(request: httpx.Request) -> httpx.Response:
            approvals.append(json.loads(request.content))
            return httpx.Response(409, json={"error": {"code": "approval_not_pending"}})

        agent = self.agent(handle)
        agent.run_id = "run-a"
        with self.assertRaises(httpx.HTTPStatusError):
            await agent.approve("run-a", "stale-request", "once")
        self.assertEqual(approvals, [{"request_id": "stale-request", "choice": "once"}])


class VoiceSessionCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_abort_clears_queued_audio_before_waiting_for_agent_cancellation(
        self,
    ):
        session = object.__new__(VoiceSession)
        order: list[str] = []
        running = asyncio.Event()
        cancelled = asyncio.Event()
        release = asyncio.Event()

        async def reply() -> None:
            running.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                order.append("cancel")
                cancelled.set()
                await release.wait()
                raise

        session.turn = Turn()
        session.turn_task = asyncio.create_task(reply())
        session.speaker = Mock()
        session.speaker.clear_queue.side_effect = lambda: order.append("clear")
        await asyncio.wait_for(running.wait(), 1)
        abort = asyncio.create_task(session.abort())
        try:
            await asyncio.wait_for(cancelled.wait(), 1)
            self.assertEqual(order, ["clear", "cancel"])
            self.assertFalse(abort.done())
        finally:
            release.set()
            await asyncio.wait_for(abort, 1)
        self.assertIsNone(session.turn)

    async def test_close_releases_agent_client_and_native_audio_resources_once(self):
        session = object.__new__(VoiceSession)
        session.closed = False
        session.close_done = asyncio.Event()
        session.turn = None
        session.tasks = set()
        session.agent = Mock(close=AsyncMock())
        session.speaker = Mock(aclose=AsyncMock())
        session.room = Mock(disconnect=AsyncMock())
        await session.close()
        await session.close()
        session.agent.close.assert_awaited_once()
        session.speaker.aclose.assert_awaited_once()
        session.room.disconnect.assert_awaited_once()
        self.assertTrue(session.close_done.is_set())

    async def test_close_releases_audio_when_remote_agent_cleanup_fails(self):
        session = object.__new__(VoiceSession)
        session.closed = False
        session.close_done = asyncio.Event()
        session.turn = None
        session.tasks = set()
        session.agent = Mock(
            close=AsyncMock(side_effect=RuntimeError("remote cleanup failed"))
        )
        session.speaker = Mock(aclose=AsyncMock())
        session.room = Mock(disconnect=AsyncMock())
        with self.assertRaisesRegex(RuntimeError, "remote cleanup failed"):
            await session.close()
        session.speaker.aclose.assert_awaited_once()
        session.room.disconnect.assert_awaited_once()
        self.assertTrue(session.close_done.is_set())


if __name__ == "__main__":
    unittest.main()
