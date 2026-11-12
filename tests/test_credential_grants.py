import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st
from slate.access.credentials import AccessDenied, CredentialNotFound, CredentialStore
from slate.access.vault import Keyring
from slate.api.events import EventBus
from slate.api.tasks import require_local
from slate.api.tasks import router as tasks_router
from slate.tasks.machine import Event, State
from slate.tasks.manager import TaskManager
from slate.tasks.store import CredentialRef, TaskStore


class Setup:
    def make(self, root: Path):
        self.tasks = TaskStore(root / "tasks.db")
        self.creds = CredentialStore(
            root / "cred.db",
            Keyring([os.urandom(32)]),
            authorize=self.tasks.credential_access,
        )
        self.duke = self.creds.add(
            "duke-energy.com", "Electric bill", "password", {"password": "pw-duke"}
        )
        self.gmail = self.creds.add(
            "gmail.com", "Personal email", "password", {"password": "pw-gmail"}
        )
        self.visa = self.creds.add(
            "visa",
            "Main card",
            "card",
            {"number": "4111111111111234", "expiry": "08/29", "cvc": "737"},
        )

    def running(self):
        task = self.tasks.create("Pay bill", now=0.0)
        return self.tasks.apply(task.id, Event.START, actor="agent", now=1.0)

    def ask(self, task_id, *creds):
        refs = [self.creds.describe(c.id) for c in creds]
        _, approval = self.tasks.request_approval(
            task_id, "Pay $84?", timeout=60, actor="agent", now=2.0, credentials=refs
        )
        return approval

    def use(self, task_id, cred):
        return self.creds.use(cred.id, task_id=task_id, purpose="log in")

    def refused(self, task_id, cred) -> str:
        try:
            self.use(task_id, cred)
        except AccessDenied as denied:
            return denied.reason
        return "allowed"


class GrantTests(Setup, unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.make(Path(self.dir.name))

    def tearDown(self):
        self.creds.close()
        self.dir.cleanup()

    def test_only_the_named_credential_is_granted(self):
        task = self.running()
        approval = self.ask(task.id, self.duke)
        self.tasks.resolve(approval.id, "approved", actor="web", now=3.0)
        self.assertEqual(self.use(task.id, self.duke)["password"], "pw-duke")
        self.assertEqual(self.refused(task.id, self.gmail), "not_granted")
        self.assertEqual(self.refused(task.id, self.visa), "not_granted")

    def test_running_alone_is_not_enough(self):
        task = self.running()
        self.assertEqual(self.refused(task.id, self.duke), "not_granted")

    def test_nothing_before_the_decision(self):
        task = self.running()
        self.ask(task.id, self.duke)
        self.assertEqual(self.refused(task.id, self.duke), "task_awaiting_approval")

    def test_denied_timed_out_or_cancelled_grant_nothing(self):
        for outcome in ("denied", "timed_out", "cancelled"):
            with self.subTest(outcome):
                task = self.running()
                approval = self.ask(task.id, self.duke)
                if outcome == "cancelled":
                    self.tasks.apply(task.id, Event.CANCEL, actor="web", now=3.0)
                else:
                    self.tasks.resolve(approval.id, outcome, actor="web", now=3.0)
                state = self.tasks.get(task.id).state.value
                self.assertEqual(self.refused(task.id, self.duke), f"task_{state}")

    def test_grant_ends_when_the_task_ends(self):
        for event in (Event.SUCCEED, Event.FAIL, Event.CANCEL, Event.TIMEOUT):
            with self.subTest(event.value):
                task = self.running()
                approval = self.ask(task.id, self.duke)
                self.tasks.resolve(approval.id, "approved", actor="web", now=3.0)
                self.use(task.id, self.duke)
                self.tasks.apply(task.id, event, actor="agent", now=4.0)
                state = self.tasks.get(task.id).state.value
                self.assertEqual(self.refused(task.id, self.duke), f"task_{state}")

    def test_grant_belongs_to_one_task(self):
        first, second = self.running(), self.running()
        approval = self.ask(first.id, self.duke)
        self.tasks.resolve(approval.id, "approved", actor="web", now=3.0)
        self.assertEqual(self.refused(second.id, self.duke), "not_granted")

    def test_grants_from_several_approvals_add_up(self):
        task = self.running()
        first = self.ask(task.id, self.duke)
        self.tasks.resolve(first.id, "approved", actor="web", now=3.0)
        second = self.ask(task.id, self.visa)
        self.tasks.resolve(second.id, "approved", actor="web", now=4.0)
        self.use(task.id, self.duke)
        self.use(task.id, self.visa)
        self.assertEqual(self.refused(task.id, self.gmail), "not_granted")

    def test_while_waiting_again_earlier_grants_pause(self):
        task = self.running()
        first = self.ask(task.id, self.duke)
        self.tasks.resolve(first.id, "approved", actor="web", now=3.0)
        self.ask(task.id, self.visa)
        self.assertEqual(self.refused(task.id, self.duke), "task_awaiting_approval")

    def test_only_approved_approvals_grant_even_if_state_is_wrong(self):
        for status in ("pending", "denied", "timed_out", "cancelled"):
            with self.subTest(status):
                task = self.running()
                approval = self.ask(task.id, self.duke)
                with sqlite3.connect(Path(self.dir.name) / "tasks.db") as conn:
                    conn.execute(
                        "UPDATE approvals SET status = ? WHERE id = ?",
                        (status, approval.id),
                    )
                    conn.execute(
                        "UPDATE tasks SET state = 'running' WHERE id = ?", (task.id,)
                    )
                self.assertEqual(self.refused(task.id, self.duke), "not_granted")

    def test_audit_log_names_the_approval(self):
        task = self.running()
        approval = self.ask(task.id, self.duke)
        self.tasks.resolve(approval.id, "approved", actor="web", now=3.0)
        self.use(task.id, self.duke)
        self.refused(task.id, self.gmail)
        [use] = self.creds.uses(self.duke.id)
        self.assertEqual((use.allowed, use.approval_id), (True, approval.id))
        [refusal] = self.creds.uses(self.gmail.id)
        self.assertEqual((refusal.allowed, refusal.reason), (False, "not_granted"))
        self.assertIsNone(refusal.approval_id)

    def test_approval_keeps_the_name_the_user_saw(self):
        task = self.running()
        approval = self.ask(task.id, self.duke)
        self.creds.rename(self.duke.id, label="Renamed later")
        shown = self.tasks.get_approval(approval.id).credentials
        self.assertEqual(
            shown, (CredentialRef(self.duke.id, "duke-energy.com", "Electric bill"),)
        )
        self.assertEqual(
            self.tasks.get_approval(approval.id).to_dict()["credentials"],
            [
                {
                    "id": self.duke.id,
                    "service": "duke-energy.com",
                    "label": "Electric bill",
                }
            ],
        )

    def test_same_credential_twice_is_one_grant(self):
        task = self.running()
        approval = self.ask(task.id, self.duke, self.duke)
        self.assertEqual(len(approval.credentials), 1)

    def test_limit_on_credentials_per_approval(self):
        task = self.running()
        refs = [CredentialRef(f"cred_{i:016x}", "s", "l") for i in range(11)]
        with self.assertRaises(ValueError):
            self.tasks.request_approval(
                task.id, "x", timeout=60, actor="agent", now=2.0, credentials=refs
            )
        self.assertIs(self.tasks.get(task.id).state, State.RUNNING)

    def test_grants_survive_a_restart(self):
        task = self.running()
        approval = self.ask(task.id, self.duke)
        self.tasks.resolve(approval.id, "approved", actor="web", now=3.0)
        self.creds.authorize = TaskStore(
            Path(self.dir.name) / "tasks.db"
        ).credential_access
        self.use(task.id, self.duke)

    def test_deleted_credential_cannot_be_used_even_if_granted(self):
        task = self.running()
        approval = self.ask(task.id, self.duke)
        self.tasks.resolve(approval.id, "approved", actor="web", now=3.0)
        self.creds.delete(self.duke.id)
        self.assertEqual(self.refused(task.id, self.duke), "not_found")

    def test_old_credentials_database_gets_the_new_column(self):
        path = Path(self.dir.name) / "old.db"
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE credential_uses (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "credential_id TEXT NOT NULL, task_id TEXT, purpose TEXT NOT NULL, "
                "at REAL NOT NULL, allowed INTEGER NOT NULL, reason TEXT)"
            )
            conn.execute(
                "INSERT INTO credential_uses (credential_id, task_id, purpose, at, "
                "allowed, reason) VALUES ('cred_x', 't', 'old', 0, 0, 'x')"
            )
        store = CredentialStore(path, Keyring([os.urandom(32)]))
        [old] = store.uses("cred_x")
        self.assertEqual((old.purpose, old.approval_id), ("old", None))
        store.close()


class ManagerTests(Setup, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.make(Path(self.dir.name))
        self.bus = EventBus()
        self.manager = TaskManager(
            self.tasks, self.bus, describe_credential=self.creds.describe
        )

    async def asyncTearDown(self):
        self.manager.close()
        self.creds.close()
        self.dir.cleanup()

    async def test_end_to_end(self):
        task = self.manager.create("Pay the electric bill", kind="payment")
        self.manager.start(task.id)
        approval = self.manager.request_approval(
            task.id, "Pay $84.12 to Duke Energy?", credentials=[self.duke.id]
        )
        event = [e for e in self.bus.buffer if e["type"] == "approval.requested"][-1]
        self.assertEqual(
            event["data"]["credentials"],
            [
                {
                    "id": self.duke.id,
                    "service": "duke-energy.com",
                    "label": "Electric bill",
                }
            ],
        )
        self.manager.decide(approval.id, True, actor="web")
        self.assertEqual(self.use(task.id, self.duke)["password"], "pw-duke")

    async def test_unknown_credential_changes_nothing(self):
        task = self.manager.create("Pay")
        self.manager.start(task.id)
        with self.assertRaises(CredentialNotFound):
            self.manager.request_approval(
                task.id, "Pay?", credentials=[self.duke.id, "cred_doesnotexist0"]
            )
        self.assertIs(self.tasks.get(task.id).state, State.RUNNING)
        self.assertEqual(self.tasks.approvals(task.id), [])

    async def test_credentials_need_a_store(self):
        manager = TaskManager(self.tasks, EventBus())
        task = manager.create("Pay")
        manager.start(task.id)
        with self.assertRaises(RuntimeError):
            manager.request_approval(task.id, "Pay?", credentials=[self.duke.id])
        manager.request_approval(task.id, "No credentials is fine")
        manager.close()


class ApiTests(Setup, unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.make(Path(self.dir.name))
        app = FastAPI()
        app.include_router(tasks_router, prefix="/api")
        app.state.tasks = TaskManager(self.tasks, EventBus())
        app.dependency_overrides[require_local] = lambda: None
        self.client = TestClient(app)

    def tearDown(self):
        self.creds.close()
        self.dir.cleanup()

    def test_task_detail_shows_what_the_approval_unlocks(self):
        task = self.running()
        self.ask(task.id, self.duke, self.visa)
        detail = self.client.get(f"/api/tasks/{task.id}").json()
        names = [c["label"] for c in detail["approvals"][0]["credentials"]]
        self.assertEqual(names, ["Electric bill", "Main card"])
        self.assertNotIn("pw-duke", str(detail))


CREDS = ("duke", "gmail", "visa")
step = st.one_of(
    st.tuples(st.just("start"), st.integers(0, 1)),
    st.tuples(st.just("ask"), st.integers(0, 1), st.sets(st.sampled_from(CREDS))),
    st.tuples(
        st.just("decide"),
        st.integers(0, 1),
        st.sampled_from(["approved", "denied", "timed_out"]),
    ),
    st.tuples(
        st.just("end"), st.integers(0, 1), st.sampled_from(["succeed", "cancel"])
    ),
    st.tuples(st.just("use"), st.integers(0, 1), st.sampled_from(CREDS)),
)


class ModelTests(Setup, unittest.TestCase):
    @settings(max_examples=150, deadline=None)
    @given(st.lists(step, max_size=25))
    def test_allowed_exactly_when_the_model_says(self, steps):
        with tempfile.TemporaryDirectory() as tmp:
            self.make(Path(tmp))
            creds = {"duke": self.duke, "gmail": self.gmail, "visa": self.visa}
            ids = [self.tasks.create(f"t{i}", now=0.0).id for i in range(2)]
            state = ["queued", "queued"]
            granted: list[set[str]] = [set(), set()]
            pending: list[tuple[str, set[str]] | None] = [None, None]
            for op, t, *args in steps:
                tid = ids[t]
                if op == "start" and state[t] == "queued":
                    self.tasks.apply(tid, Event.START, actor="a", now=1.0)
                    state[t] = "running"
                elif op == "ask" and state[t] == "running":
                    approval = self.ask(tid, *(creds[c] for c in args[0]))
                    pending[t] = (approval.id, set(args[0]))
                    state[t] = "awaiting_approval"
                elif op == "decide" and state[t] == "awaiting_approval":
                    approval_id, named = pending[t]
                    self.tasks.resolve(approval_id, args[0], actor="web", now=3.0)
                    if args[0] == "approved":
                        granted[t] |= named
                        state[t] = "running"
                    else:
                        state[t] = {"denied": "cancelled", "timed_out": "timed_out"}[
                            args[0]
                        ]
                elif op == "end" and state[t] == "running":
                    event = Event.SUCCEED if args[0] == "succeed" else Event.CANCEL
                    self.tasks.apply(tid, event, actor="a", now=4.0)
                    state[t] = "succeeded" if args[0] == "succeed" else "cancelled"
                elif op == "use":
                    name = args[0]
                    if state[t] != "running":
                        expected = f"task_{state[t]}"
                    elif name not in granted[t]:
                        expected = "not_granted"
                    else:
                        expected = "allowed"
                    self.assertEqual(self.refused(tid, creds[name]), expected)
            self.creds.close()


if __name__ == "__main__":
    unittest.main()
