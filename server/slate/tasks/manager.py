"""Runs the task FSM inside the server: stores every change, pushes it to the
web app as an event, and enforces approval deadlines.

Event types sent on /api/ws/events:
- task.updated        data = the task after the change
- approval.requested  data = the new approval (summary, deadline)
- approval.resolved   data = the closed approval (status, decided_by)
"""

import asyncio
import time
from collections.abc import Callable

from slate.api.events import EventBus
from slate.tasks.machine import Event, State
from slate.tasks.store import (
    PENDING,
    Approval,
    ApprovalClosed,
    Task,
    TaskStore,
)

DEFAULT_APPROVAL_TIMEOUT = 60.0


class TaskManager:
    def __init__(
        self,
        store: TaskStore,
        bus: EventBus,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.bus = bus
        self.clock = clock
        self.timers: dict[str, asyncio.TimerHandle] = {}
        self.waiters: dict[str, asyncio.Future[Approval]] = {}

    # Lifecycle

    def recover(self) -> None:
        """Call once at startup, inside the event loop.

        A task that was running when the server stopped lost its work, so it
        fails. An approval whose deadline passed while we were down times
        out now; the rest get their timers back with the time they have left.
        """
        now = self.clock()
        for task in self.store.in_state(State.RUNNING):
            self._apply(task.id, Event.FAIL, "server", "server restarted mid-task")
        for approval in self.store.pending_approvals():
            if approval.deadline <= now:
                self._expire(approval.id)
            else:
                self._arm(approval)

    def close(self) -> None:
        for handle in self.timers.values():
            handle.cancel()
        self.timers.clear()
        for waiter in self.waiters.values():
            waiter.cancel()
        self.waiters.clear()

    # Calls from the agent

    def create(
        self, title: str, *, kind: str = "request", session_id: str | None = None
    ) -> Task:
        task = self.store.create(
            title, kind=kind, session_id=session_id, now=self.clock()
        )
        self._publish_task(task)
        return task

    def start(self, task_id: str) -> Task:
        return self._apply(task_id, Event.START, "agent")

    def succeed(self, task_id: str, detail: str | None = None) -> Task:
        return self._apply(task_id, Event.SUCCEED, "agent", detail)

    def fail(self, task_id: str, reason: str) -> Task:
        return self._apply(task_id, Event.FAIL, "agent", reason)

    def cancel(self, task_id: str, actor: str = "user") -> Task:
        return self._apply(task_id, Event.CANCEL, actor, f"cancelled by {actor}")

    def request_approval(
        self,
        task_id: str,
        summary: str,
        *,
        timeout: float = DEFAULT_APPROVAL_TIMEOUT,
    ) -> Approval:
        task, approval = self.store.request_approval(
            task_id, summary, timeout=timeout, actor="agent", now=self.clock()
        )
        self._arm(approval)
        self._publish_task(task)
        self.bus.publish(
            "approval.requested",
            approval.to_dict(),
            session_id=task.session_id,
        )
        return approval

    async def wait_for_decision(self, approval_id: str) -> Approval:
        """Block the agent until the user answers or the deadline passes."""
        approval = self.store.get_approval(approval_id)
        if approval.status != PENDING:
            return approval
        waiter = self.waiters.get(approval_id)
        if waiter is None:
            waiter = asyncio.get_running_loop().create_future()
            self.waiters[approval_id] = waiter
        return await asyncio.shield(waiter)

    # Calls from the user (web app or device button)

    def decide(
        self,
        approval_id: str,
        approve: bool,
        *,
        actor: str,
        expected_version: int | None = None,
    ) -> tuple[Task, Approval]:
        """Raises ApprovalClosed if someone else (or the timer) got there first."""
        outcome = "approved" if approve else "denied"
        task, approval = self.store.resolve(
            approval_id,
            outcome,
            actor=actor,
            now=self.clock(),
            expected_version=expected_version,
        )
        self._closed(task, approval)
        return task, approval

    # Internals

    def _apply(
        self, task_id: str, event: Event, actor: str, reason: str | None = None
    ) -> Task:
        pending = [
            approval
            for approval in self.store.approvals(task_id)
            if approval.status == PENDING
        ]
        task = self.store.apply(
            task_id, event, actor=actor, now=self.clock(), reason=reason
        )
        self._publish_task(task)
        if task.state is not State.AWAITING_APPROVAL:
            for approval in pending:
                closed = self.store.get_approval(approval.id)
                self._closed(task, closed, publish_task=False)
        return task

    def _arm(self, approval: Approval) -> None:
        delay = max(0.0, approval.deadline - self.clock())
        loop = asyncio.get_running_loop()
        self.timers[approval.id] = loop.call_later(delay, self._expire, approval.id)

    def _expire(self, approval_id: str) -> None:
        self.timers.pop(approval_id, None)
        try:
            task, approval = self.store.resolve(
                approval_id, "timed_out", actor="timer", now=self.clock()
            )
        except ApprovalClosed:
            # The user answered in the same instant and won the race. That is
            # the expected outcome, not an error.
            return
        self._closed(task, approval)

    def _closed(
        self, task: Task, approval: Approval, *, publish_task: bool = True
    ) -> None:
        handle = self.timers.pop(approval.id, None)
        if handle:
            handle.cancel()
        if publish_task:
            self._publish_task(task)
        self.bus.publish(
            "approval.resolved", approval.to_dict(), session_id=task.session_id
        )
        waiter = self.waiters.pop(approval.id, None)
        if waiter and not waiter.done():
            waiter.set_result(approval)

    def _publish_task(self, task: Task) -> None:
        self.bus.publish("task.updated", task.to_dict(), session_id=task.session_id)
