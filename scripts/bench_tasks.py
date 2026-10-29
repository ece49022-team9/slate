"""Measure the task state machine: transition latency, race safety,
approval timeout accuracy, and history retention.

    uv run python scripts/bench_tasks.py
"""

import asyncio
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

from slate.api.events import EventBus
from slate.tasks.machine import Event
from slate.tasks.manager import TaskManager
from slate.tasks.store import ApprovalClosed, TaskStore


def percentiles(samples: list[float]) -> str:
    ordered = sorted(samples)

    def at(p: float) -> float:
        return ordered[min(len(ordered) - 1, int(p * len(ordered)))]

    return f"p50 {at(0.50):.2f} ms | p95 {at(0.95):.2f} ms | p99 {at(0.99):.2f} ms"


def transition_latency(store: TaskStore, tasks: int = 300) -> list[float]:
    samples = []
    for n in range(tasks):
        task = store.create(f"bench {n}", now=time.time())
        for event in (Event.START, Event.SUCCEED):
            start = time.perf_counter()
            store.apply(task.id, event, actor="bench", now=time.time())
            samples.append((time.perf_counter() - start) * 1000)
    return samples


def race_trials(store: TaskStore, trials: int = 200, callers: int = 8) -> int:
    """Return how many trials had exactly one winner."""
    clean = 0
    outcomes = ["approved", "denied", "timed_out"]
    for _ in range(trials):
        task = store.create("race", now=time.time())
        store.apply(task.id, Event.START, actor="bench", now=time.time())
        _, approval = store.request_approval(
            task.id, "ok?", timeout=60, actor="bench", now=time.time()
        )
        barrier = threading.Barrier(callers)
        wins = []

        def call(n: int, approval_id=approval.id, barrier=barrier, wins=wins) -> None:
            barrier.wait()
            try:
                store.resolve(
                    approval_id, outcomes[n % 3], actor=f"c{n}", now=time.time()
                )
                wins.append(n)
            except ApprovalClosed:
                pass

        threads = [threading.Thread(target=call, args=(n,)) for n in range(callers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        clean += len(wins) == 1
    return clean


async def timeout_error(store: TaskStore, count: int = 100) -> list[float]:
    """How late each approval timer fires compared with its deadline.
    The 1 s base gives setup (300 blocking writes) time to finish first,
    so we measure the timer, not the setup."""
    manager = TaskManager(store, EventBus())
    approvals = []
    for n in range(count):
        task = manager.create(f"timeout {n}")
        manager.start(task.id)
        approvals.append(
            manager.request_approval(task.id, "ok?", timeout=1.0 + n * 0.005)
        )
    results = await asyncio.gather(
        *(manager.wait_for_decision(a.id) for a in approvals)
    )
    manager.close()
    return [(r.decided_at - r.deadline) * 1000 for r in results]


def retention(folder: Path, keep: int = 100, total: int = 1000) -> tuple[int, int]:
    store = TaskStore(folder / "retention.db", keep=keep)
    for n in range(total):
        task = store.create(f"old {n}", now=float(n))
        store.apply(task.id, Event.CANCEL, actor="bench", now=float(n))
    return len(store.recent(limit=total)), keep


def main() -> None:
    with tempfile.TemporaryDirectory() as name:
        folder = Path(name)
        store = TaskStore(folder / "bench.db", keep=10_000)

        latency = transition_latency(store)
        print(f"Transition latency ({len(latency)} writes): {percentiles(latency)}")

        trials = 200
        clean = race_trials(store, trials)
        print(f"Race: {clean}/{trials} trials had exactly one winner (8 callers)")

        late = asyncio.run(timeout_error(store))
        print(f"Timeout firing delay ({len(late)} approvals): {percentiles(late)}")
        print(f"  never early: {min(late) >= 0}, max {max(late):.2f} ms")

        kept, keep = retention(folder)
        print(f"Retention: {kept} finished tasks kept after 1000 (limit {keep})")

        ok = clean == trials and min(late) >= 0 and kept == keep
        print("PASS" if ok else "FAIL")
        mean = statistics.mean(latency)
        print(f"python {sys.version.split()[0]}, mean write {mean:.2f} ms")
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
