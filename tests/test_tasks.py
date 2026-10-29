import asyncio
import tempfile
import threading
import unittest
from collections import deque
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st
from slate.api.events import EventBus
from slate.api.tasks import require_local
from slate.api.tasks import router as tasks_router
from slate.tasks.machine import (
    ACTIVE,
    TERMINAL,
    TRANSITIONS,
    Event,
    InvalidTransition,
    State,
    next_state,
)
from slate.tasks.manager import TaskManager
from slate.tasks.store import (
    ApprovalClosed,
    StaleVersion,
    TaskStore,
)


class TempDB(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "tasks.sqlite3"
        self.store = TaskStore(self.path)

    def tearDown(self):
        self.dir.cleanup()

    def running_task(self, store=None):
        store = store or self.store
        task = store.create("Pay bill", now=0.0)
        return store.apply(task.id, Event.START, actor="agent", now=1.0)


class TransitionTableTests(unittest.TestCase):
    def test_every_state_event_pair(self):
        """All 7 x 8 = 56 pairs: allowed exactly when the table has them."""
        checked = 0
        for state in State:
            for event in Event:
                checked += 1
                if (state, event) in TRANSITIONS:
                    self.assertIn(next_state(state, event), State)
                else:
                    with self.assertRaises(InvalidTransition):
                        next_state(state, event)
        self.assertEqual(checked, len(State) * len(Event))

    def test_terminal_states_have_no_way_out(self):
        for state in TERMINAL:
            for event in Event:
                self.assertNotIn((state, event), TRANSITIONS)

    def test_every_state_is_reachable_from_queued(self):
        seen, todo = {State.QUEUED}, deque([State.QUEUED])
        while todo:
            state = todo.popleft()
            for (source, _), target in TRANSITIONS.items():
                if source is state and target not in seen:
                    seen.add(target)
                    todo.append(target)
        self.assertEqual(seen, set(State))

    def test_every_active_state_can_finish(self):
        """No dead ends: from any active state some path reaches a terminal."""
        for start in ACTIVE:
            seen, todo = {start}, deque([start])
            while todo:
                state = todo.popleft()
                for (source, _), target in TRANSITIONS.items():
                    if source is state and target not in seen:
                        seen.add(target)
                        todo.append(target)
            self.assertTrue(seen & TERMINAL, start)

    def test_finished_task_cannot_restart(self):
        with self.assertRaises(InvalidTransition):
            next_state(State.SUCCEEDED, Event.START)


class StoreTests(TempDB):
    def test_illegal_transition_changes_nothing(self):
        task = self.store.create("Pay bill", now=0.0)
        with self.assertRaises(InvalidTransition):
            self.store.apply(task.id, Event.SUCCEED, actor="agent", now=1.0)
        self.assertEqual(self.store.get(task.id), task)
        self.assertEqual(self.store.history(task.id), [])

    def test_history_records_every_edge(self):
        task = self.running_task()
        _, approval = self.store.request_approval(
            task.id, "Pay $84?", timeout=60, actor="agent", now=2.0
        )
        self.store.resolve(approval.id, "approved", actor="web", now=3.0)
        self.store.apply(task.id, Event.SUCCEED, actor="agent", now=4.0)
        edges = [
            (t.from_state, t.event, t.to_state) for t in self.store.history(task.id)
        ]
        self.assertEqual(
            edges,
            [
                ("queued", "start", "running"),
                ("running", "request_approval", "awaiting_approval"),
                ("awaiting_approval", "approve", "running"),
                ("running", "succeed", "succeeded"),
            ],
        )
        self.assertEqual(self.store.get(task.id).version, 4)

    def test_stale_version_is_refused(self):
        task = self.running_task()
        with self.assertRaises(StaleVersion):
            self.store.apply(
                task.id, Event.SUCCEED, actor="web", now=2.0, expected_version=0
            )
        self.assertIs(self.store.get(task.id).state, State.RUNNING)

    def test_approve_and_deny_only_through_resolve(self):
        task = self.running_task()
        self.store.request_approval(task.id, "ok?", timeout=60, actor="a", now=2.0)
        with self.assertRaises(ValueError):
            self.store.apply(task.id, Event.APPROVE, actor="web", now=3.0)

    def test_approval_is_single_use(self):
        task = self.running_task()
        _, approval = self.store.request_approval(
            task.id, "ok?", timeout=60, actor="agent", now=2.0
        )
        self.store.resolve(approval.id, "approved", actor="web", now=3.0)
        with self.assertRaises(ApprovalClosed) as closed:
            self.store.resolve(approval.id, "denied", actor="device", now=4.0)
        self.assertEqual(closed.exception.approval.decided_by, "web")

    def test_cancel_while_waiting_closes_the_approval(self):
        task = self.running_task()
        _, approval = self.store.request_approval(
            task.id, "ok?", timeout=60, actor="agent", now=2.0
        )
        self.store.apply(task.id, Event.CANCEL, actor="user", now=3.0)
        self.assertEqual(self.store.get_approval(approval.id).status, "cancelled")
        with self.assertRaises(ApprovalClosed):
            self.store.resolve(approval.id, "approved", actor="web", now=4.0)

    def test_survives_a_restart(self):
        task = self.running_task()
        reopened = TaskStore(self.path)
        self.assertEqual(reopened.get(task.id), self.store.get(task.id))
        self.assertEqual(len(reopened.history(task.id)), 1)

    def test_keeps_only_the_newest_finished_tasks(self):
        store = TaskStore(self.path, keep=5)
        active = [self.running_task(store) for _ in range(2)]
        finished = []
        for n in range(12):
            task = store.create(f"task {n}", now=float(n))
            store.apply(task.id, Event.CANCEL, actor="user", now=100.0 + n)
            finished.append(task.id)
        remaining = {task.id for task in store.recent(limit=200)}
        self.assertEqual(remaining, set(finished[-5:]) | {t.id for t in active})
        # Deleting a task also deletes its history.
        self.assertEqual(store.history(finished[0]), [])


class RaceTests(TempDB):
    def race(self, actions):
        """Run every action at the same moment on its own thread."""
        barrier = threading.Barrier(len(actions))
        results = [None] * len(actions)

        def run(index, action):
            barrier.wait()
            try:
                results[index] = ("ok", action())
            except Exception as error:
                results[index] = ("error", error)

        threads = [
            threading.Thread(target=run, args=(i, action))
            for i, action in enumerate(actions)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return results

    def test_only_one_decision_wins(self):
        outcomes = ["approved", "denied", "timed_out"] * 4
        for trial in range(25):
            task = self.running_task()
            _, approval = self.store.request_approval(
                task.id, "ok?", timeout=60, actor="agent", now=2.0
            )
            results = self.race(
                [
                    lambda a=approval.id, o=outcome, n=n: self.store.resolve(
                        a, o, actor=f"caller{n}", now=3.0
                    )
                    for n, outcome in enumerate(outcomes)
                ]
            )
            winners = [value for kind, value in results if kind == "ok"]
            losers = [value for kind, value in results if kind == "error"]
            self.assertEqual(len(winners), 1, f"trial {trial}")
            self.assertTrue(all(isinstance(e, ApprovalClosed) for e in losers))
            # Exactly one edge left the approval state, and it matches the winner.
            exits = [
                t
                for t in self.store.history(task.id)
                if t.from_state == "awaiting_approval"
            ]
            self.assertEqual(len(exits), 1)
            self.assertEqual(self.store.get(task.id), winners[0][0])

    def test_cancel_racing_approve(self):
        """Result must equal one of the two serial orders:
        cancel first: approval cancelled, approve refused.
        approve first: task runs again, then cancel stops it."""
        orders = {
            ("cancelled", ("start", "request_approval", "cancel")),
            ("approved", ("start", "request_approval", "approve", "cancel")),
        }
        for _ in range(25):
            task = self.running_task()
            _, approval = self.store.request_approval(
                task.id, "ok?", timeout=60, actor="agent", now=2.0
            )
            results = self.race(
                [
                    lambda t=task.id: self.store.apply(
                        t, Event.CANCEL, actor="device", now=3.0
                    ),
                    lambda a=approval.id: self.store.resolve(
                        a, "approved", actor="web", now=3.0
                    ),
                ]
            )
            self.assertEqual(results[0][0], "ok")  # cancel always lands
            self.assertIs(self.store.get(task.id).state, State.CANCELLED)
            status = self.store.get_approval(approval.id).status
            events = tuple(t.event for t in self.store.history(task.id))
            self.assertIn((status, events), orders)


class RandomSequenceTests(unittest.TestCase):
    @settings(max_examples=150, deadline=None)
    @given(st.lists(st.sampled_from([e for e in Event]), max_size=25))
    def test_store_always_agrees_with_the_table(self, events):
        """Feed random events. The stored state must match the pure FSM, and
        rejected events must leave no trace."""
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder) / "t.db")
            task = store.create("random", now=0.0)
            model, approval_id, accepted = State.QUEUED, None, 0
            for step, event in enumerate(events, start=1):
                expected = TRANSITIONS.get((model, event))
                try:
                    if event is Event.REQUEST_APPROVAL:
                        _, approval = store.request_approval(
                            task.id, "ok?", timeout=60, actor="t", now=step
                        )
                        approval_id = approval.id
                    elif event in (Event.APPROVE, Event.DENY) or (
                        event is Event.TIMEOUT and model is State.AWAITING_APPROVAL
                    ):
                        if model is not State.AWAITING_APPROVAL:
                            raise InvalidTransition(model, event)
                        outcome = {
                            Event.APPROVE: "approved",
                            Event.DENY: "denied",
                            Event.TIMEOUT: "timed_out",
                        }[event]
                        store.resolve(approval_id, outcome, actor="t", now=step)
                    else:
                        store.apply(task.id, event, actor="t", now=step)
                except InvalidTransition:
                    self.assertIsNone(expected)
                else:
                    self.assertIsNotNone(expected)
                    model = expected
                    accepted += 1
                stored = store.get(task.id)
                self.assertIs(stored.state, model)
                self.assertEqual(stored.version, accepted)
                self.assertEqual(len(store.history(task.id)), accepted)
            pending = [a for a in store.approvals(task.id) if a.status == "pending"]
            self.assertEqual(len(pending), int(model is State.AWAITING_APPROVAL))


class ManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "tasks.sqlite3"
        self.bus = EventBus()
        self.manager = TaskManager(TaskStore(self.path), self.bus)

    async def asyncTearDown(self):
        self.manager.close()
        self.dir.cleanup()

    def types(self):
        return [event["type"] for event in self.bus.buffer]

    async def test_unanswered_approval_times_out(self):
        task = self.manager.create("Pay bill")
        self.manager.start(task.id)
        approval = self.manager.request_approval(task.id, "Pay $84?", timeout=0.05)
        result = await asyncio.wait_for(
            self.manager.wait_for_decision(approval.id), timeout=1
        )
        self.assertEqual(result.status, "timed_out")
        self.assertEqual(result.decided_by, "timer")
        self.assertIs(self.manager.store.get(task.id).state, State.TIMED_OUT)
        self.assertIn("approval.requested", self.types())
        self.assertEqual(self.types()[-1], "approval.resolved")

    async def test_answer_stops_the_timer(self):
        task = self.manager.create("Pay bill")
        self.manager.start(task.id)
        approval = self.manager.request_approval(task.id, "Pay $84?", timeout=0.05)
        waiting = asyncio.create_task(self.manager.wait_for_decision(approval.id))
        self.manager.decide(approval.id, True, actor="web")
        self.assertEqual((await waiting).status, "approved")
        await asyncio.sleep(0.1)  # past the old deadline
        self.assertIs(self.manager.store.get(task.id).state, State.RUNNING)
        self.assertEqual(self.manager.timers, {})

    async def test_restart_fails_running_tasks(self):
        task = self.manager.create("Pay bill")
        self.manager.start(task.id)
        restarted = TaskManager(TaskStore(self.path), EventBus())
        restarted.recover()
        after = restarted.store.get(task.id)
        self.assertIs(after.state, State.FAILED)
        self.assertEqual(after.reason, "server restarted mid-task")

    async def test_restart_keeps_the_approval_deadline(self):
        task = self.manager.create("Pay bill")
        self.manager.start(task.id)
        approval = self.manager.request_approval(task.id, "ok?", timeout=0.1)
        self.manager.close()  # server goes down before the deadline
        restarted = TaskManager(TaskStore(self.path), EventBus())
        restarted.recover()
        self.assertIn(approval.id, restarted.timers)
        result = await asyncio.wait_for(
            restarted.wait_for_decision(approval.id), timeout=1
        )
        self.assertEqual(result.status, "timed_out")
        restarted.close()

    async def test_restart_after_deadline_times_out_at_once(self):
        task = self.manager.create("Pay bill")
        self.manager.start(task.id)
        approval = self.manager.request_approval(task.id, "ok?", timeout=30)
        self.manager.close()
        later = self.manager.clock() + 60
        restarted = TaskManager(TaskStore(self.path), EventBus(), clock=lambda: later)
        restarted.recover()
        self.assertEqual(restarted.store.get_approval(approval.id).status, "timed_out")
        self.assertIs(restarted.store.get(task.id).state, State.TIMED_OUT)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.app = FastAPI()
        self.app.include_router(tasks_router, prefix="/api")
        self.app.state.tasks = TaskManager(
            TaskStore(Path(self.dir.name) / "t.db"), EventBus()
        )
        self.store = self.app.state.tasks.store
        self.app.dependency_overrides[require_local] = lambda: None
        self.client = TestClient(self.app)

    def tearDown(self):
        self.dir.cleanup()

    def waiting_task(self):
        task = self.store.create("Pay bill", now=0.0)
        self.store.apply(task.id, Event.START, actor="agent", now=1.0)
        return self.store.request_approval(
            task.id, "Pay $84?", timeout=600, actor="agent", now=2.0
        )

    def test_list_and_detail(self):
        task, _ = self.waiting_task()
        listed = self.client.get("/api/tasks").json()
        self.assertEqual([t["id"] for t in listed], [task.id])
        detail = self.client.get(f"/api/tasks/{task.id}").json()
        self.assertEqual(detail["task"]["state"], "awaiting_approval")
        self.assertEqual(len(detail["history"]), 2)
        self.assertEqual(detail["approvals"][0]["status"], "pending")
        self.assertEqual(self.client.get("/api/tasks/tsk_nope").status_code, 404)

    def test_first_decision_wins_second_gets_409(self):
        _, approval = self.waiting_task()
        url = f"/api/approvals/{approval.id}"
        first = self.client.post(url, json={"decision": "approve", "source": "web"})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["task"]["state"], "running")
        second = self.client.post(url, json={"decision": "deny", "source": "device"})
        self.assertEqual(second.status_code, 409)
        self.assertEqual(
            second.json()["detail"],
            {"error": "approval_closed", "status": "approved", "decided_by": "web"},
        )

    def test_stale_version_gets_409(self):
        _, approval = self.waiting_task()
        response = self.client.post(
            f"/api/approvals/{approval.id}",
            json={"decision": "approve", "expected_version": 1},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["error"], "stale_version")

    def test_bad_input(self):
        _, approval = self.waiting_task()
        url = f"/api/approvals/{approval.id}"
        bad = self.client.post(url, json={"decision": "maybe"})
        self.assertEqual(bad.status_code, 422)
        missing = self.client.post("/api/approvals/apr_nope", json={"decision": "deny"})
        self.assertEqual(missing.status_code, 404)

    def test_cancel(self):
        task, _ = self.waiting_task()
        response = self.client.post(f"/api/tasks/{task.id}/cancel")
        self.assertEqual(response.json()["state"], "cancelled")
        again = self.client.post(f"/api/tasks/{task.id}/cancel")
        self.assertEqual(again.status_code, 409)

    def test_rejects_requests_from_other_machines(self):
        self.app.dependency_overrides.clear()
        self.assertEqual(self.client.get("/api/tasks").status_code, 403)


if __name__ == "__main__":
    unittest.main()
