"""SQLite storage for tasks, their transition history, and approvals.

Every change runs inside BEGIN IMMEDIATE, which takes SQLite's write lock
before reading. So "read the current state, check the FSM, write the new
state" is one atomic step, even with several threads or processes using the
same file. Each operation opens its own connection, so nothing is shared
between threads.

Two extra guards sit on top of that:
- tasks.version goes up by one on every transition. A caller can pass the
  version it last saw, and the write is refused if the task moved since.
- an approval can only leave "pending" once. Whichever decision (approve,
  deny, timeout, cancel) commits first wins, and every later one gets
  ApprovalClosed.
"""

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from slate.tasks.machine import TERMINAL, Event, State, next_state

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    kind        TEXT NOT NULL,
    session_id  TEXT,
    state       TEXT NOT NULL,
    version     INTEGER NOT NULL,
    reason      TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_by_state ON tasks (state, updated_at);

CREATE TABLE IF NOT EXISTS transitions (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
    from_state  TEXT NOT NULL,
    event       TEXT NOT NULL,
    to_state    TEXT NOT NULL,
    actor       TEXT NOT NULL,
    at          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS transitions_by_task ON transitions (task_id, seq);

CREATE TABLE IF NOT EXISTS approvals (
    id          TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
    summary     TEXT NOT NULL,
    status      TEXT NOT NULL,
    deadline    REAL NOT NULL,
    decided_by  TEXT,
    decided_at  REAL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS approvals_by_status ON approvals (status, deadline);
"""

PENDING = "pending"

# How each approval outcome moves the task.
APPROVAL_EVENTS = {
    "approved": Event.APPROVE,
    "denied": Event.DENY,
    "timed_out": Event.TIMEOUT,
}


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    moment = datetime.fromtimestamp(ts, UTC).isoformat(timespec="milliseconds")
    return moment.replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    kind: str
    session_id: str | None
    state: State
    version: int
    reason: str | None
    created_at: float
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["state"] = self.state.value
        data["created_at"] = iso(self.created_at)
        data["updated_at"] = iso(self.updated_at)
        return data


@dataclass(frozen=True)
class Approval:
    id: str
    task_id: str
    summary: str
    status: str
    deadline: float
    decided_by: str | None
    decided_at: float | None
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("deadline", "decided_at", "created_at"):
            data[key] = iso(data[key])
        return data


@dataclass(frozen=True)
class Transition:
    seq: int
    task_id: str
    from_state: str
    event: str
    to_state: str
    actor: str
    at: float

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["at"] = iso(self.at)
        return data


class TaskNotFound(LookupError):
    pass


class ApprovalNotFound(LookupError):
    pass


class StaleVersion(Exception):
    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(f"task is at version {actual}, not {expected}")
        self.expected = expected
        self.actual = actual


class ApprovalClosed(Exception):
    def __init__(self, approval: Approval) -> None:
        super().__init__(f"approval is already {approval.status}")
        self.approval = approval


def to_task(row: sqlite3.Row) -> Task:
    return Task(**{**dict(row), "state": State(row["state"])})


def to_approval(row: sqlite3.Row) -> Approval:
    return Approval(**dict(row))


class TaskStore:
    def __init__(self, path: str | Path, keep: int = 100) -> None:
        """keep: how many finished tasks to remember. Active tasks are never
        deleted, so the table holds at most keep + (active tasks) rows."""
        self.path = str(path)
        self.keep = keep
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    # Reads

    def get(self, task_id: str) -> Task:
        with self.read() as conn:
            return self._task(conn, task_id)

    def recent(self, limit: int = 50) -> list[Task]:
        with self.read() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [to_task(row) for row in rows]

    def history(self, task_id: str) -> list[Transition]:
        with self.read() as conn:
            rows = conn.execute(
                "SELECT * FROM transitions WHERE task_id = ? ORDER BY seq",
                (task_id,),
            ).fetchall()
        return [Transition(**dict(row)) for row in rows]

    def approvals(self, task_id: str) -> list[Approval]:
        with self.read() as conn:
            rows = conn.execute(
                "SELECT * FROM approvals WHERE task_id = ? ORDER BY created_at",
                (task_id,),
            ).fetchall()
        return [to_approval(row) for row in rows]

    def get_approval(self, approval_id: str) -> Approval:
        with self.read() as conn:
            return self._approval(conn, approval_id)

    def in_state(self, state: State) -> list[Task]:
        with self.read() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE state = ?", (state.value,)
            ).fetchall()
        return [to_task(row) for row in rows]

    def pending_approvals(self) -> list[Approval]:
        with self.read() as conn:
            rows = conn.execute(
                "SELECT * FROM approvals WHERE status = ? ORDER BY deadline",
                (PENDING,),
            ).fetchall()
        return [to_approval(row) for row in rows]

    # Writes

    def create(
        self,
        title: str,
        *,
        kind: str = "request",
        session_id: str | None = None,
        now: float,
    ) -> Task:
        task = Task(
            id=new_id("tsk"),
            title=title,
            kind=kind,
            session_id=session_id,
            state=State.QUEUED,
            version=0,
            reason=None,
            created_at=now,
            updated_at=now,
        )
        with self.write() as conn:
            conn.execute(
                "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task.id,
                    task.title,
                    task.kind,
                    task.session_id,
                    task.state.value,
                    task.version,
                    task.reason,
                    task.created_at,
                    task.updated_at,
                ),
            )
        return task

    def apply(
        self,
        task_id: str,
        event: Event,
        *,
        actor: str,
        now: float,
        reason: str | None = None,
        expected_version: int | None = None,
    ) -> Task:
        """Move a task along one FSM edge. Raises InvalidTransition,
        StaleVersion, or TaskNotFound and changes nothing in that case.

        Approve and deny go through resolve(), so they always close the
        matching approval in the same transaction."""
        if event in (Event.APPROVE, Event.DENY):
            raise ValueError(f"use resolve() to {event.value} a task")
        with self.write() as conn:
            task = self._transition(
                conn, task_id, event, actor, now, reason, expected_version
            )
            if task.state is not State.AWAITING_APPROVAL:
                # Leaving the approval state any other way closes the approval.
                status = "timed_out" if event is Event.TIMEOUT else "cancelled"
                conn.execute(
                    "UPDATE approvals SET status = ?, decided_by = ?, "
                    "decided_at = ? WHERE task_id = ? AND status = ?",
                    (status, actor, now, task_id, PENDING),
                )
        return task

    def request_approval(
        self,
        task_id: str,
        summary: str,
        *,
        timeout: float,
        actor: str,
        now: float,
    ) -> tuple[Task, Approval]:
        approval = Approval(
            id=new_id("apr"),
            task_id=task_id,
            summary=summary,
            status=PENDING,
            deadline=now + timeout,
            decided_by=None,
            decided_at=None,
            created_at=now,
        )
        with self.write() as conn:
            task = self._transition(
                conn, task_id, Event.REQUEST_APPROVAL, actor, now, None, None
            )
            conn.execute(
                "INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    approval.id,
                    approval.task_id,
                    approval.summary,
                    approval.status,
                    approval.deadline,
                    None,
                    None,
                    approval.created_at,
                ),
            )
        return task, approval

    def resolve(
        self,
        approval_id: str,
        outcome: str,
        *,
        actor: str,
        now: float,
        expected_version: int | None = None,
    ) -> tuple[Task, Approval]:
        """Close a pending approval as approved, denied or timed_out.
        Only the first call for an approval succeeds."""
        event = APPROVAL_EVENTS[outcome]
        with self.write() as conn:
            approval = self._approval(conn, approval_id)
            if approval.status != PENDING:
                raise ApprovalClosed(approval)
            reason = {
                "denied": f"denied by {actor}",
                "timed_out": "no answer before the approval deadline",
            }.get(outcome)
            task = self._transition(
                conn,
                approval.task_id,
                event,
                actor,
                now,
                reason,
                expected_version,
            )
            closed = conn.execute(
                "UPDATE approvals SET status = ?, decided_by = ?, decided_at = ? "
                "WHERE id = ? AND status = ?",
                (outcome, actor, now, approval_id, PENDING),
            )
            # Cannot happen while we hold the write lock; checked anyway so a
            # future change that drops BEGIN IMMEDIATE fails loudly.
            assert closed.rowcount == 1
            approval = self._approval(conn, approval_id)
        return task, approval

    # Helpers that run inside an open transaction

    def _task(self, conn: sqlite3.Connection, task_id: str) -> Task:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskNotFound(task_id)
        return to_task(row)

    def _approval(self, conn: sqlite3.Connection, approval_id: str) -> Approval:
        row = conn.execute(
            "SELECT * FROM approvals WHERE id = ?", (approval_id,)
        ).fetchone()
        if row is None:
            raise ApprovalNotFound(approval_id)
        return to_approval(row)

    def _transition(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        event: Event,
        actor: str,
        now: float,
        reason: str | None,
        expected_version: int | None,
    ) -> Task:
        task = self._task(conn, task_id)
        if expected_version is not None and expected_version != task.version:
            raise StaleVersion(expected_version, task.version)
        target = next_state(task.state, event)
        updated = conn.execute(
            "UPDATE tasks SET state = ?, version = version + 1, reason = ?, "
            "updated_at = ? WHERE id = ? AND version = ?",
            (target.value, reason, now, task_id, task.version),
        )
        assert updated.rowcount == 1
        conn.execute(
            "INSERT INTO transitions (task_id, from_state, event, to_state, actor, at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, task.state.value, event.value, target.value, actor, now),
        )
        if target in TERMINAL:
            self._prune(conn)
        return self._task(conn, task_id)

    def _prune(self, conn: sqlite3.Connection) -> None:
        """Forget the oldest finished tasks beyond self.keep."""
        finished = tuple(state.value for state in TERMINAL)
        marks = ", ".join("?" for _ in finished)
        conn.execute(
            f"DELETE FROM tasks WHERE id IN ("
            f"SELECT id FROM tasks WHERE state IN ({marks}) "
            f"ORDER BY updated_at DESC, rowid DESC LIMIT -1 OFFSET ?)",
            (*finished, self.keep),
        )
