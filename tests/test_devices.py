import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from slate.access.devices import DeviceStore
from slate.app import app
from starlette.websockets import WebSocketDisconnect
from test_voice_session import AUTH, FakeAgent, cloud


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "devices.db"
        self.now = 100.0
        self.store = DeviceStore(self.path, clock=lambda: self.now)

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_only_a_hash_of_the_token_is_kept(self):
        device, token = self.store.pair("Kitchen")
        self.assertTrue(token.startswith("sdt_"))
        self.assertNotIn(token.encode(), self.path.read_bytes())
        self.assertNotIn("token", json.dumps(device.to_dict()))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_identify_marks_the_device_as_seen(self):
        device, token = self.store.pair("Kitchen")
        self.now = 200.0
        self.assertEqual(self.store.identify(token).id, device.id)
        self.assertEqual(self.store.get(device.id).last_seen_at, 200.0)

    def test_unknown_wrong_and_revoked_tokens_are_refused(self):
        device, token = self.store.pair("Kitchen")
        _, other = self.store.pair("Desk")
        for wrong in ("", "owner-token", token[:-1] + "x", "sdt_"):
            self.assertIsNone(self.store.identify(wrong))
        self.store.revoke(device.id)
        self.assertIsNone(self.store.identify(token))
        self.assertIsNotNone(self.store.identify(other))

    def test_revoke_keeps_the_first_time(self):
        device, _ = self.store.pair("Kitchen")
        self.store.revoke(device.id)
        self.now = 300.0
        self.assertEqual(self.store.revoke(device.id).revoked_at, 100.0)
        self.assertIsNone(self.store.revoke("dev_nope"))

    def test_old_signing_table_is_replaced(self):
        self.store.close()
        self.path.unlink()
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "CREATE TABLE devices (id TEXT PRIMARY KEY, name TEXT NOT NULL, "
                "secret BLOB NOT NULL, created_at REAL NOT NULL, revoked_at REAL, "
                "last_seen_at REAL)"
            )
        self.store = DeviceStore(self.path)
        device, token = self.store.pair("Kitchen")
        self.assertEqual(self.store.identify(token).id, device.id)


class PairingTests(unittest.TestCase):
    def pair(self, client, name="Kitchen"):
        reply = client.post("/api/devices", json={"name": name}, headers=AUTH)
        self.assertEqual(reply.status_code, 201, reply.text)
        return reply.json()

    def test_paired_device_connects_with_its_own_token(self):
        with cloud(FakeAgent()) as client:
            paired = self.pair(client)
            headers = {"Authorization": f"Bearer {paired['token']}"}
            with client.websocket_connect(
                "/api/device/socket", headers=headers
            ) as socket:
                socket.send_text(json.dumps({"type": "hello"}))
                socket.send_text(json.dumps({"type": "start"}))
                self.assertEqual(json.loads(socket.receive_text())["type"], "turn")
                self.assertEqual(
                    app.state.voice.current.device_id, paired["device"]["id"]
                )
            listed = client.get("/api/devices", headers=AUTH).json()
            self.assertIsNotNone(listed[0]["last_seen_at"])
            self.assertNotIn(paired["token"], json.dumps(listed))

    def test_device_token_only_opens_the_device_socket(self):
        with cloud(FakeAgent()) as client:
            headers = {"Authorization": f"Bearer {self.pair(client)['token']}"}
            for path in ("/api/tasks", "/api/devices", "/api/credentials"):
                self.assertEqual(client.get(path, headers=headers).status_code, 401)
            reply = client.post("/api/devices", json={"name": "x"}, headers=headers)
            self.assertEqual(reply.status_code, 401)
            with self.assertRaises(WebSocketDisconnect) as refused:
                with client.websocket_connect(
                    "/api/events/socket", headers=headers
                ) as socket:
                    socket.receive_json()
            self.assertEqual(refused.exception.code, 4401)

    def test_revoking_disconnects_the_device_and_locks_it_out(self):
        with cloud(FakeAgent()) as client:
            paired = self.pair(client)
            device_id = paired["device"]["id"]
            headers = {"Authorization": f"Bearer {paired['token']}"}
            with client.websocket_connect(
                "/api/device/socket", headers=headers
            ) as socket:
                socket.send_text(json.dumps({"type": "hello"}))
                socket.send_text(json.dumps({"type": "start"}))
                socket.receive_text()
                reply = client.delete(f"/api/devices/{device_id}", headers=AUTH)
                self.assertEqual(reply.status_code, 200)
                self.assertIsNotNone(reply.json()["revoked_at"])
                with self.assertRaises(WebSocketDisconnect) as closed:
                    while True:
                        socket.receive_text()
                self.assertEqual(closed.exception.code, 4401)
            with self.assertRaises(WebSocketDisconnect) as refused:
                with client.websocket_connect("/api/device/socket", headers=headers):
                    pass
            self.assertEqual(refused.exception.code, 4401)
            missing = client.delete("/api/devices/dev_nope", headers=AUTH)
            self.assertEqual(missing.status_code, 404)

    def test_pairing_needs_a_name_and_the_owner_token(self):
        with cloud(FakeAgent()) as client:
            for body in ({}, {"name": ""}, {"name": "x" * 65}, {"name": 5}):
                reply = client.post("/api/devices", json=body, headers=AUTH)
                self.assertEqual(reply.status_code, 422)
            self.assertEqual(
                client.post("/api/devices", json={"name": "x"}).status_code, 401
            )


if __name__ == "__main__":
    unittest.main()
