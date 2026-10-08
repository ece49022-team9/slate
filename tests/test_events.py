import unittest

from slate.api.events import EventBus


class EventBusTests(unittest.TestCase):
    def test_events_follow_the_schema_envelope(self):
        bus = EventBus()
        event = bus.publish("device.status", {"online": True}, device_id="dev_01")
        self.assertEqual(
            set(event),
            {"v", "id", "type", "ts", "device_id", "session_id", "data"},
        )
        self.assertEqual(event["v"], 1)
        self.assertTrue(event["ts"].endswith("Z"))

    def test_ids_sort_in_publish_order(self):
        bus = EventBus()
        ids = [bus.publish("agent.step", {})["id"] for _ in range(12)]
        self.assertEqual(ids, sorted(ids))

    def test_replays_only_events_after_the_cursor(self):
        bus = EventBus()
        first, second, third = [bus.publish("agent.step", {"n": n}) for n in range(3)]
        self.assertEqual(bus.since(first["id"]), [second, third])
        self.assertEqual(bus.since(third["id"]), [])
        self.assertEqual(bus.since(None), [first, second, third])

    def test_asks_for_resync_when_cursor_is_gone(self):
        bus = EventBus(size=2)
        old = bus.publish("agent.step", {})
        bus.publish("agent.step", {})
        bus.publish("agent.step", {})
        self.assertIsNone(bus.since(old["id"]))
        # After a server restart the buffer is empty, so any cursor is stale.
        self.assertIsNone(EventBus().since("evt_0000000005"))

    def test_slow_listener_is_told_to_reconnect(self):
        bus = EventBus(queue_size=2)
        queue = bus.subscribe()
        for _ in range(3):
            bus.publish("agent.step", {})
        self.assertIsNone(queue.get_nowait())
        self.assertTrue(queue.empty())
