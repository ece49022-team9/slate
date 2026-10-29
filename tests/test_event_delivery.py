"""Delivery guarantees of the event stream: what a web app sees when it falls
behind, disconnects, or the server restarts."""

import asyncio
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st
from slate.api import events as events_module
from slate.api.events import EventBus, send_events
from slate.api.events import router as events_router


class CursorTests(unittest.TestCase):
    def test_restart_with_overlapping_ids_forces_resync(self):
        """Old bug: ids restarted at 1, so a cursor from before a restart
        could land inside the new buffer and hide events without a resync."""
        old = EventBus()
        for _ in range(300):
            cursor = old.publish("device.status", {})["id"]
        new = EventBus()
        for _ in range(400):
            new.publish("device.status", {})
        self.assertIsNone(new.since(cursor))

    def test_cursor_just_before_the_buffer_loses_nothing(self):
        bus = EventBus(size=3)
        first = bus.publish("agent.step", {"n": 0})
        rest = [bus.publish("agent.step", {"n": n}) for n in range(1, 4)]
        # first has left the buffer, but the client already has it.
        self.assertEqual(bus.since(first["id"]), rest)

    def test_bad_cursors_force_resync(self):
        bus = EventBus()
        bus.publish("agent.step", {})
        for cursor in ("", "evt_", "garbage", bus.prefix + "x", bus.prefix + "99"):
            self.assertIsNone(bus.since(cursor), cursor)

    def test_resets_and_depth_are_counted(self):
        bus = EventBus(queue_size=3)
        bus.subscribe()
        for _ in range(5):
            bus.publish("agent.step", {})
        stats = bus.snapshot()
        self.assertEqual(stats["published"], 5)
        self.assertEqual(stats["resets"], 1)
        self.assertEqual(stats["max_depth"], 3)
        self.assertEqual(stats["listeners"], 1)


class SimulatedClient:
    """Follows the web app protocol: read from the live queue, and after any
    disconnect reconnect with the last id seen."""

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self.last_id: str | None = None
        self.seen: list[int | str] = []  # event numbers, "resync" marks
        self.pending: list[dict] = []
        self.queue = None

    def connect(self) -> None:
        backlog = self.bus.since(self.last_id)
        if backlog is None:
            backlog = [self.bus.envelope("system.resync", {}), *self.bus.buffer]
        self.pending = list(backlog)
        self.queue = self.bus.subscribe()

    def disconnect(self) -> None:
        self.bus.unsubscribe(self.queue)
        self.queue = None

    def read(self, count: int) -> None:
        for _ in range(count):
            if self.pending:
                event = self.pending.pop(0)
            elif self.queue is not None and not self.queue.empty():
                event = self.queue.get_nowait()
                if event is None:  # server told us to reconnect (1013)
                    self.disconnect()
                    self.connect()
                    continue
            else:
                return
            self.last_id = event["id"]
            if event["type"] == "system.resync":
                self.seen.append("resync")
            else:
                self.seen.append(event["data"]["n"])


operations = st.lists(
    st.one_of(
        st.tuples(st.just("publish"), st.integers(1, 30)),
        st.tuples(st.just("read"), st.integers(1, 30)),
        st.tuples(st.just("drop"), st.just(0)),
        # Server restarts, publishes some events, then the client reconnects.
        st.tuples(st.just("restart"), st.integers(0, 30)),
    ),
    max_size=40,
)


class DeliveryPropertyTests(unittest.TestCase):
    @settings(max_examples=400, deadline=None)
    @given(operations, st.integers(2, 20), st.integers(1, 10))
    def test_never_a_silent_gap(self, ops, size, queue_size):
        """Under any mix of publishing, slow reading, dropped connections and
        server restarts, the client's view is in order with no repeats, and
        any jump in numbering comes right after a resync."""
        bus = EventBus(size=size, queue_size=queue_size)
        client = SimulatedClient(bus)
        client.connect()
        counter = 0
        for op, amount in ops:
            if op == "publish":
                for _ in range(amount):
                    bus.publish("agent.step", {"n": counter})
                    counter += 1
            elif op == "read":
                client.read(amount)
            elif op == "drop":
                client.disconnect()
                client.connect()
            else:
                client.disconnect()
                bus = EventBus(size=size, queue_size=queue_size)
                client.bus = bus
                for _ in range(amount):
                    bus.publish("agent.step", {"n": counter})
                    counter += 1
                client.connect()
        client.read(10_000)

        previous = None
        for item in client.seen:
            if item == "resync":
                previous = None
                continue
            if previous is not None:
                self.assertEqual(item, previous + 1, client.seen)
            previous = item


class StalledSendTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_client_that_stops_reading_is_dropped(self):
        class Stuck:
            async def send_json(self, event):
                await asyncio.Event().wait()  # TCP buffers full: never returns

            async def close(self, code):
                raise AssertionError("must not wait on a close frame")

        bus = EventBus()
        queue = bus.subscribe()
        event = bus.publish("agent.step", {})
        with mock.patch.object(events_module, "SEND_TIMEOUT", 0.05):
            await asyncio.wait_for(send_events(Stuck(), [event], queue, bus), 1)
        self.assertEqual(bus.stats["stalled"], 1)


class StreamTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(events_router, prefix="/api")
        app.state.events = EventBus()
        self.bus = app.state.events
        self.client = TestClient(app)
        patch = mock.patch.object(events_module, "LOCAL_HOSTS", ("testclient",))
        patch.start()
        self.addCleanup(patch.stop)

    def test_reconnect_with_cursor_replays_only_missed_events(self):
        seen = [self.bus.publish("agent.step", {"n": n}) for n in range(3)]
        missed = [self.bus.publish("agent.step", {"n": n}) for n in range(3, 6)]
        url = f"/api/ws/events?after={seen[-1]['id']}"
        with self.client.websocket_connect(url) as socket:
            received = [socket.receive_json() for _ in missed]
        self.assertEqual(received, missed)

    def test_stale_cursor_starts_with_resync(self):
        self.bus.publish("agent.step", {})
        url = "/api/ws/events?after=evt_000000_0000000001"
        with self.client.websocket_connect(url) as socket:
            self.assertEqual(socket.receive_json()["type"], "system.resync")

    def test_stats(self):
        self.bus.publish("agent.step", {})
        stats = self.client.get("/api/events/stats").json()
        self.assertEqual(stats["published"], 1)
        self.assertEqual(stats["boot"], self.bus.boot)

    def test_stats_reject_other_machines(self):
        with mock.patch.object(events_module, "LOCAL_HOSTS", ("127.0.0.1",)):
            self.assertEqual(self.client.get("/api/events/stats").status_code, 403)


if __name__ == "__main__":
    unittest.main()
