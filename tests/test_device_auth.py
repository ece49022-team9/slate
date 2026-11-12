import hashlib
import hmac
import json
import tempfile
import threading
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st
from slate.access.devices import DeviceStore
from slate.access.signing import (
    WINDOW,
    AuthError,
    Verifier,
    canonical,
    sign,
    signed_headers,
)
from slate.api import device as device_api
from slate.api.events import EventBus
from slate.api.tasks import require_local
from slate.api.tasks import router as tasks_router
from slate.tasks.machine import Event
from slate.tasks.manager import TaskManager
from slate.tasks.store import TaskStore

T0 = 1_800_000_000  # a fixed "now" for every test


class Clock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def lower(headers: dict[str, str]) -> dict[str, str]:
    return {key.lower(): value for key, value in headers.items()}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        # The server started a while before "now", so slightly old requests
        # are judged by the clock check, not the restart check.
        self.clock = Clock(T0 - 100)
        self.devices = DeviceStore(Path(self.dir.name) / "d.db", clock=self.clock)
        self.verifier = Verifier(self.devices, clock=self.clock)
        self.clock.now = T0
        self.device, self.secret = self.devices.pair("Slate 1")

    def tearDown(self):
        self.devices.close()
        self.dir.cleanup()

    def headers(self, method="POST", path="/api/x", body=b"", **kw):
        kw.setdefault("timestamp", int(self.clock()))
        return lower(
            signed_headers(self.device.id, self.secret, method, path, body, **kw)
        )

    def verify(self, method="POST", path="/api/x", body=b"", headers=None):
        headers = headers if headers is not None else self.headers(method, path, body)
        return self.verifier.verify(method, path, headers, body)

    def rejected(self, reason, *args, **kw):
        with self.assertRaises(AuthError) as caught:
            self.verify(*args, **kw)
        self.assertEqual(caught.exception.reason, reason)


class FormatTests(unittest.TestCase):
    def test_known_answer(self):
        """Fixed vector for checking the firmware's C version (docs/device-auth.md)."""
        secret = bytes(range(32))
        body = b'{"decision":"approve"}'
        message = canonical(
            "POST",
            "/api/device/approvals/apr_1",
            1_800_000_000,
            "00112233445566778899aabbccddeeff",
            "dev_0123456789abcdef",
            body,
        )
        self.assertEqual(
            message.decode(),
            "SLATE-HMAC-SHA256-V1\nPOST\n/api/device/approvals/apr_1\n1800000000\n"
            "00112233445566778899aabbccddeeff\ndev_0123456789abcdef\n"
            + hashlib.sha256(body).hexdigest(),
        )
        self.assertEqual(
            sign(
                secret,
                "POST",
                "/api/device/approvals/apr_1",
                1_800_000_000,
                "00112233445566778899aabbccddeeff",
                "dev_0123456789abcdef",
                body,
            ),
            hmac.new(secret, message, hashlib.sha256).hexdigest(),
        )
        self.assertEqual(
            sign(
                secret,
                "POST",
                "/api/device/approvals/apr_1",
                1_800_000_000,
                "00112233445566778899aabbccddeeff",
                "dev_0123456789abcdef",
                body,
            ),
            KNOWN_SIGNATURE,
        )


KNOWN_SIGNATURE = "aa3d79384a1d8d108e808ff6c912e3d50892b90b21ca202c86acd57c0d717cb3"


class VerifierTests(Base):
    def test_good_request_passes(self):
        self.assertEqual(self.verify(body=b'{"a":1}'), self.device.id)
        self.assertEqual(self.verifier.stats["accepted"], 1)

    def test_any_changed_part_breaks_the_signature(self):
        body = b'{"decision":"approve"}'
        good = self.headers("POST", "/api/x?y=1", body)
        cases = {
            "method": ("PUT", "/api/x?y=1", body, good),
            "path": ("POST", "/api/z?y=1", body, good),
            "query": ("POST", "/api/x?y=2", body, good),
            "body": ("POST", "/api/x?y=1", b'{"decision":"deny"}', good),
            "time": (
                "POST",
                "/api/x?y=1",
                body,
                {**good, "x-slate-timestamp": str(T0 - 1)},
            ),
            "nonce": (
                "POST",
                "/api/x?y=1",
                body,
                {**good, "x-slate-nonce": "f" * 32},
            ),
        }
        for name, (method, path, sent, headers) in cases.items():
            with self.subTest(name):
                with self.assertRaises(AuthError) as caught:
                    self.verifier.verify(method, path, headers, sent)
                self.assertEqual(caught.exception.reason, "bad_signature")

    def test_cannot_claim_to_be_another_device(self):
        other, _ = self.devices.pair("Slate 2")
        headers = {**self.headers(), "x-slate-device": other.id}
        self.rejected("bad_signature", headers=headers)

    def test_wrong_key(self):
        headers = lower(
            signed_headers(self.device.id, b"\x00" * 32, "POST", "/api/x", timestamp=T0)
        )
        self.rejected("bad_signature", headers=headers)

    def test_replay_is_refused(self):
        headers = self.headers()
        self.verify(headers=headers)
        self.rejected("replay", headers=headers)

    def test_forged_request_does_not_burn_the_nonce(self):
        real = self.headers()
        forged = {**real, "x-slate-signature": "0" * 64}
        self.rejected("bad_signature", headers=forged)
        self.assertEqual(self.verify(headers=real), self.device.id)

    def test_same_nonce_is_fine_on_another_device(self):
        other, other_secret = self.devices.pair("Slate 2")
        nonce = "ab" * 16
        self.verify(headers=self.headers(nonce=nonce))
        headers = lower(
            signed_headers(
                other.id, other_secret, "POST", "/api/x", timestamp=T0, nonce=nonce
            )
        )
        self.assertEqual(self.verifier.verify("POST", "/api/x", headers, b""), other.id)

    def test_clock_skew(self):
        for offset, ok in [
            (-WINDOW, True),
            (WINDOW, True),
            (-WINDOW - 1, False),
            (WINDOW + 1, False),
        ]:
            with self.subTest(offset=offset):
                headers = self.headers(timestamp=T0 + offset)
                if ok:
                    self.verify(headers=headers)
                else:
                    self.rejected("clock_skew", headers=headers)

    def test_restart_refuses_requests_signed_before_it(self):
        """Nonces live in memory, so a restart forgets them. A request captured
        just before the restart must still not work after it."""
        headers = self.headers()
        self.verify(headers=headers)
        self.clock.now += 5
        restarted = Verifier(self.devices, clock=self.clock)
        with self.assertRaises(AuthError) as caught:
            restarted.verify("POST", "/api/x", headers, b"")
        self.assertEqual(caught.exception.reason, "before_boot")
        fresh = self.headers()
        self.assertEqual(restarted.verify("POST", "/api/x", fresh, b""), self.device.id)

    def test_unknown_and_revoked_devices(self):
        headers = {**self.headers(), "x-slate-device": "dev_" + "0" * 16}
        self.rejected("unknown_device", headers=headers)
        self.devices.revoke(self.device.id)
        self.rejected("revoked")

    def test_malformed_headers(self):
        good = self.headers()
        cases = {
            "missing": {k: v for k, v in good.items() if k != "x-slate-nonce"},
            "upper case hex": {
                **good,
                "x-slate-signature": good["x-slate-signature"].upper(),
            },
            "short nonce": {**good, "x-slate-nonce": "ab"},
            "negative time": {**good, "x-slate-timestamp": "-5"},
            "bad device id": {**good, "x-slate-device": "../etc"},
        }
        for name, headers in cases.items():
            with self.subTest(name):
                self.rejected("bad_format", headers=headers)

    def test_old_nonces_are_forgotten_but_still_refused(self):
        headers = self.headers()
        self.verify(headers=headers)
        self.assertEqual(self.verifier.remembered(), 1)
        self.clock.now += WINDOW + 1
        self.verify()  # any request clears what can no longer pass the time check
        self.assertEqual(self.verifier.remembered(), 1)
        self.rejected("clock_skew", headers=headers)

    def test_rate_limit(self):
        verifier = Verifier(self.devices, clock=self.clock, max_nonces=5)
        for _ in range(5):
            verifier.verify("POST", "/api/x", self.headers(), b"")
        with self.assertRaises(AuthError) as caught:
            verifier.verify("POST", "/api/x", self.headers(), b"")
        self.assertEqual(
            (caught.exception.reason, caught.exception.status), ("rate_limited", 429)
        )
        self.clock.now += WINDOW + 1
        verifier.verify("POST", "/api/x", self.headers(), b"")

    def test_simultaneous_copies_only_one_wins(self):
        headers = self.headers()
        barrier = threading.Barrier(16)
        results = []

        def send():
            barrier.wait()
            try:
                self.verify(headers=headers)
                results.append("ok")
            except AuthError as error:
                results.append(error.reason)

        threads = [threading.Thread(target=send) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), ["ok"] + ["replay"] * 15)

    @settings(max_examples=300, deadline=None)
    @given(data=st.data())
    def test_random_single_change_is_always_refused(self, data):
        """Change one byte anywhere in the request: it must never pass."""
        body = data.draw(st.binary(max_size=64))
        path = "/api/device/approvals/apr_" + data.draw(
            st.text("abc123", min_size=1, max_size=8)
        )
        headers = self.headers("POST", path, body)
        parts = {
            "path": path.encode(),
            "body": body,
            "signature": headers["x-slate-signature"].encode(),
        }
        part = data.draw(st.sampled_from(sorted(k for k, v in parts.items() if v)))
        raw = bytearray(parts[part])
        i = data.draw(st.integers(0, len(raw) - 1))
        raw[i] = data.draw(st.integers(0, 255).filter(lambda b: b != raw[i]))
        changed = bytes(raw)
        sent_path = changed.decode("latin-1") if part == "path" else path
        sent_body = changed if part == "body" else body
        sent = dict(headers)
        if part == "signature":
            sent["x-slate-signature"] = changed.decode("latin-1")
        with self.assertRaises(AuthError):
            self.verifier.verify("POST", sent_path, sent, sent_body)


class ApiTests(Base):
    def setUp(self):
        super().setUp()
        self.app = FastAPI()
        self.app.include_router(tasks_router, prefix="/api")
        device_api.include(self.app)
        self.app.state.devices = self.devices
        self.app.state.verifier = self.verifier
        self.app.state.tasks = TaskManager(
            TaskStore(Path(self.dir.name) / "t.db"), EventBus(), clock=self.clock
        )
        self.store = self.app.state.tasks.store
        self.app.dependency_overrides[require_local] = lambda: None
        self.client = TestClient(self.app)

    def waiting_task(self):
        task = self.store.create("Pay bill", now=T0)
        self.store.apply(task.id, Event.START, actor="agent", now=T0)
        return self.store.request_approval(
            task.id, "Pay $84?", timeout=600, actor="agent", now=T0
        )

    def send(self, method, path, payload=None, headers=None):
        body = b"" if payload is None else json.dumps(payload).encode()
        headers = headers or self.headers(method, path, body)
        return self.client.request(method, path, content=body, headers=headers)

    def test_pairing_shows_the_secret_once(self):
        made = self.client.post("/api/devices", json={"name": "Desk"})
        self.assertEqual(made.status_code, 201)
        self.assertEqual(len(bytes.fromhex(made.json()["secret"])), 32)
        listed = self.client.get("/api/devices").text
        self.assertNotIn(made.json()["secret"], listed)
        self.assertNotIn("secret", listed)

    def test_ping(self):
        reply = self.send("GET", "/api/device/ping")
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(reply.json()["device_id"], self.device.id)
        self.assertEqual(self.devices.get(self.device.id).last_seen_at, T0)

    def test_signed_approval_records_the_device(self):
        task, approval = self.waiting_task()
        path = f"/api/device/approvals/{approval.id}"
        reply = self.send("POST", path, {"decision": "approve"})
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(
            reply.json()["approval"]["decided_by"], f"device:{self.device.id}"
        )
        self.assertEqual(reply.json()["task"]["state"], "running")

    def test_captured_request_cannot_be_sent_again(self):
        task, _ = self.waiting_task()
        path = f"/api/device/tasks/{task.id}/cancel"
        headers = self.headers("POST", path)
        self.assertEqual(self.send("POST", path, headers=headers).status_code, 200)
        again = self.send("POST", path, headers=headers)
        self.assertEqual(again.status_code, 401)
        self.assertEqual(
            again.json(),
            {"error": "unauthorized", "reason": "replay", "server_time": T0},
        )

    def test_body_swapped_in_transit(self):
        _, approval = self.waiting_task()
        path = f"/api/device/approvals/{approval.id}"
        headers = self.headers("POST", path, b'{"decision":"deny"}')
        reply = self.client.post(
            path, content=b'{"decision":"approve"}', headers=headers
        )
        self.assertEqual(reply.json()["reason"], "bad_signature")
        self.assertEqual(self.store.get_approval(approval.id).status, "pending")

    def test_unsigned_and_revoked(self):
        self.assertEqual(
            self.client.get("/api/device/ping").json()["reason"], "bad_format"
        )
        self.client.delete(f"/api/devices/{self.device.id}")
        self.assertEqual(
            self.send("GET", "/api/device/ping").json()["reason"], "revoked"
        )

    def test_bad_body_after_good_signature(self):
        _, approval = self.waiting_task()
        reply = self.send(
            "POST", f"/api/device/approvals/{approval.id}", {"decision": "maybe"}
        )
        self.assertEqual(reply.status_code, 422)

    def test_web_app_cannot_pretend_to_be_the_device(self):
        _, approval = self.waiting_task()
        reply = self.client.post(
            f"/api/approvals/{approval.id}",
            json={"decision": "approve", "source": "device"},
        )
        self.assertEqual(reply.json()["approval"]["decided_by"], "web")

    def test_pairing_is_local_only(self):
        del self.app.dependency_overrides[require_local]
        self.assertEqual(
            self.client.post("/api/devices", json={"name": "x"}).status_code, 403
        )
        self.assertEqual(self.client.get("/api/devices").status_code, 403)
        # The device routes are reachable from the network; the signature is the check.
        self.assertEqual(self.send("GET", "/api/device/ping").status_code, 200)

    def test_large_body_refused_before_reading(self):
        reply = self.client.post("/api/device/approvals/apr_1", content=b"x" * 20000)
        self.assertEqual(reply.status_code, 413)


if __name__ == "__main__":
    unittest.main()
