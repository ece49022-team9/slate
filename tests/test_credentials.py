import json
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st
from slate.access.credentials import (
    AccessDenied,
    BadSecret,
    CredentialStore,
)
from slate.access.routes import router as access_router
from slate.access.vault import DecryptError, Keyring, Sealed
from slate.tasks.machine import Event
from slate.tasks.store import TaskStore

PASSWORD = "hunter2-correct-horse-battery"
CARD = {"number": "4111 1111 1111 1234", "expiry": "08/29", "cvc": "737"}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.ring = Keyring([os.urandom(32)])
        self.tasks = TaskStore(self.root / "tasks.db")
        self.store = CredentialStore(
            self.root / "cred.db", self.ring, authorize=self.tasks.credential_access
        )

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def granted(self, *credential_ids):
        task = self.task()
        refs = [self.store.describe(cid) for cid in credential_ids]
        _, approval = self.tasks.request_approval(
            task.id, "ok?", timeout=60, actor="agent", now=2.0, credentials=refs
        )
        self.tasks.resolve(approval.id, "approved", actor="web", now=3.0)
        return task

    def task(self, running=True):
        task = self.tasks.create("Pay bill", now=0.0)
        if running:
            task = self.tasks.apply(task.id, Event.START, actor="agent", now=1.0)
        return task

    def duke(self):
        return self.store.add(
            "duke-energy.com",
            "Electric bill",
            "password",
            {"password": PASSWORD},
            username="minjoo",
        )

    def raw_file(self) -> bytes:
        data = (self.root / "cred.db").read_bytes()
        wal = self.root / "cred.db-wal"
        return data + (wal.read_bytes() if wal.exists() else b"")


class VaultTests(unittest.TestCase):
    def test_round_trip_and_fresh_nonce(self):
        ring = Keyring([os.urandom(32)])
        a = ring.seal(b"secret", b"row1")
        b = ring.seal(b"secret", b"row1")
        self.assertNotEqual(a.nonce, b.nonce)
        self.assertNotEqual(a.ciphertext, b.ciphertext)
        self.assertEqual(ring.open(a, b"row1"), b"secret")

    def test_any_changed_byte_is_caught(self):
        ring = Keyring([os.urandom(32)])
        sealed = ring.seal(b"secret", b"row1")
        for i in range(len(sealed.ciphertext)):
            bad = bytearray(sealed.ciphertext)
            bad[i] ^= 1
            with self.assertRaises(DecryptError) as caught:
                ring.open(Sealed(sealed.key_id, sealed.nonce, bytes(bad)), b"row1")
            self.assertEqual(caught.exception.reason, "tampered")

    def test_wrong_place_or_wrong_key(self):
        ring = Keyring([os.urandom(32)])
        sealed = ring.seal(b"secret", b"row1")
        with self.assertRaises(DecryptError):
            ring.open(sealed, b"row2")
        other = Keyring([os.urandom(32)])
        with self.assertRaises(DecryptError) as caught:
            other.open(sealed, b"row1")
        self.assertEqual(caught.exception.reason, "unknown_key")

    def test_key_file_is_private_and_reused(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("SLATE_MASTER_KEY", None)
            path = Path(tmp) / "cfg" / "master.key"
            first = Keyring.load(path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            again = Keyring.load(path)
            self.assertEqual(first.keys, again.keys)

    def test_env_key_wins(self):
        key = os.urandom(32)
        with mock.patch.dict(os.environ, {"SLATE_MASTER_KEY": key.hex()}):
            ring = Keyring.load(Path("/nonexistent/should-not-be-made"))
        self.assertEqual(list(ring.keys.values()), [key])
        self.assertFalse(Path("/nonexistent").exists())

    @settings(max_examples=100, deadline=None)
    @given(st.binary(max_size=512), st.binary(max_size=64))
    def test_any_value_round_trips(self, plain, where):
        ring = Keyring([bytes(32)])
        self.assertEqual(ring.open(ring.seal(plain, where), where), plain)


class StoreTests(Base):
    def test_database_file_has_no_plain_secret(self):
        self.duke()
        self.store.add("visa", "Main card", "card", CARD)
        raw = self.raw_file()
        for secret in (PASSWORD, CARD["number"], "1111 1234", CARD["cvc"] + '"'):
            self.assertNotIn(secret.encode(), raw)
        self.assertIn(b"duke-energy.com", raw)

    def test_list_never_has_values(self):
        self.duke()
        card = self.store.add("visa", "Main card", "card", CARD)
        self.assertEqual(card.hint, "•••• 1234")
        shown = json.dumps([c.to_dict() for c in self.store.all()])
        self.assertNotIn(PASSWORD, shown)
        self.assertNotIn(CARD["number"], shown)

    def test_running_task_can_use_it(self):
        cred = self.duke()
        task = self.granted(cred.id)
        secret = self.store.use(cred.id, task_id=task.id, purpose="log in to pay bill")
        self.assertEqual(secret["password"], PASSWORD)
        self.assertEqual(secret.username, "minjoo")
        self.assertNotIn(PASSWORD, repr(secret))
        self.assertEqual(self.store.get(cred.id).use_count, 1)

    def test_other_tasks_are_refused_and_logged(self):
        cred = self.duke()
        waiting = self.task()
        self.tasks.request_approval(
            waiting.id, "Pay?", timeout=60, actor="agent", now=2.0
        )
        queued = self.task(running=False)
        done = self.task()
        self.tasks.apply(done.id, Event.SUCCEED, actor="agent", now=3.0)
        cases = {
            waiting.id: "task_awaiting_approval",
            queued.id: "task_queued",
            done.id: "task_succeeded",
            "tsk_nope": "no_such_task",
        }
        for task_id, reason in cases.items():
            with self.subTest(reason):
                with self.assertRaises(AccessDenied) as caught:
                    self.store.use(cred.id, task_id=task_id, purpose="x")
                self.assertEqual(caught.exception.reason, reason)
        uses = self.store.uses(cred.id)
        self.assertEqual(len(uses), 4)
        self.assertFalse(any(u.allowed for u in uses))
        self.assertEqual(self.store.get(cred.id).use_count, 0)

    def test_audit_log_survives_delete(self):
        cred = self.duke()
        self.store.use(cred.id, task_id=self.granted(cred.id).id, purpose="pay")
        self.store.delete(cred.id)
        self.assertEqual([u.purpose for u in self.store.uses(cred.id)], ["pay"])
        with self.assertRaises(AccessDenied) as caught:
            self.store.use(cred.id, task_id=self.granted().id, purpose="pay")
        self.assertEqual(caught.exception.reason, "not_found")

    def test_tampered_row_is_refused(self):
        cred = self.duke()
        with sqlite3.connect(self.root / "cred.db") as conn:
            blob = bytearray(
                conn.execute(
                    "SELECT ciphertext FROM credentials WHERE id = ?", (cred.id,)
                ).fetchone()[0]
            )
            blob[0] ^= 1
            conn.execute(
                "UPDATE credentials SET ciphertext = ? WHERE id = ?",
                (bytes(blob), cred.id),
            )
        with self.assertRaises(AccessDenied) as caught:
            self.store.use(cred.id, task_id=self.granted(cred.id).id, purpose="x")
        self.assertEqual(caught.exception.reason, "tampered")

    def test_swapping_rows_does_not_work(self):
        duke = self.duke()
        card = self.store.add("visa", "Main card", "card", CARD)
        with sqlite3.connect(self.root / "cred.db") as conn:
            row = conn.execute(
                "SELECT key_id, nonce, ciphertext FROM credentials WHERE id = ?",
                (card.id,),
            ).fetchone()
            conn.execute(
                "UPDATE credentials SET key_id = ?, nonce = ?, ciphertext = ? "
                "WHERE id = ?",
                (*row, duke.id),
            )
        with self.assertRaises(AccessDenied) as caught:
            self.store.use(duke.id, task_id=self.granted(duke.id).id, purpose="x")
        self.assertEqual(caught.exception.reason, "tampered")

    def test_replace_secret(self):
        cred = self.duke()
        self.store.replace_secret(cred.id, {"password": "new-one"})
        secret = self.store.use(cred.id, task_id=self.granted(cred.id).id, purpose="x")
        self.assertEqual(secret["password"], "new-one")

    def test_bad_secret_messages_do_not_show_values(self):
        bad_inputs = [
            ("card", {"number": PASSWORD}),
            ("password", {"pass": PASSWORD}),
            ("password", {"password": 12345}),
            ("password", PASSWORD),
            ("nope", {"password": PASSWORD}),
        ]
        for kind, secret in bad_inputs:
            with self.subTest(kind=kind, secret=type(secret).__name__):
                with self.assertRaises(BadSecret) as caught:
                    self.store.add("s", "l", kind, secret)
                self.assertNotIn(PASSWORD, str(caught.exception))
                self.assertNotIn("12345", str(caught.exception))

    def test_rotation(self):
        cred = self.duke()
        old = self.ring.current
        new = self.ring.add_key()
        self.assertEqual(self.store.rekey(), 1)
        self.assertEqual(self.store.key_ids_in_use(), {new})
        self.ring.retire(old)
        secret = self.store.use(cred.id, task_id=self.granted(cred.id).id, purpose="x")
        self.assertEqual(secret["password"], PASSWORD)
        with self.assertRaises(ValueError):
            self.ring.retire(new)

    def test_wrong_master_key_reads_nothing(self):
        cred = self.duke()
        self.store.close()
        thief = CredentialStore(
            self.root / "cred.db",
            Keyring([os.urandom(32)]),
            authorize=lambda t, c: ("apr_x", None),
        )
        with self.assertRaises(AccessDenied) as caught:
            thief.use(cred.id, task_id="any", purpose="steal")
        self.assertEqual(caught.exception.reason, "unknown_key")
        thief.close()
        self.store = CredentialStore(self.root / "cred.db", self.ring)


class ApiTests(Base):
    def setUp(self):
        super().setUp()
        app = FastAPI()
        app.include_router(access_router, prefix="/api")
        app.state.credentials = self.store
        patch = mock.patch.dict(os.environ, {"SLATE_DEVICE_TOKEN": "owner"})
        patch.start()
        self.addCleanup(patch.stop)
        self.app = app
        self.client = TestClient(app, headers={"Authorization": "Bearer owner"})

    def test_add_list_detail_never_return_the_secret(self):
        made = self.client.post(
            "/api/credentials",
            json={
                "service": "duke-energy.com",
                "label": "Electric",
                "kind": "password",
                "username": "minjoo",
                "secret": {"password": PASSWORD},
            },
        )
        self.assertEqual(made.status_code, 201, made.text)
        cid = made.json()["id"]
        self.store.use(cid, task_id=self.granted(cid).id, purpose="pay")
        for reply in (
            made,
            self.client.get("/api/credentials"),
            self.client.get(f"/api/credentials/{cid}"),
        ):
            self.assertNotIn(PASSWORD, reply.text)
        detail = self.client.get(f"/api/credentials/{cid}").json()
        self.assertEqual(detail["uses"][0]["purpose"], "pay")

    def test_there_is_no_way_to_read_it_back(self):
        cid = self.duke().id
        for method, path in [
            ("GET", f"/api/credentials/{cid}/secret"),
            ("POST", f"/api/credentials/{cid}/reveal"),
        ]:
            reply = self.client.request(method, path)
            self.assertIn(reply.status_code, (404, 405))
            self.assertNotIn(PASSWORD, reply.text)

    def test_errors_do_not_echo_the_secret(self):
        bodies = [
            {
                "service": "s",
                "label": "l",
                "kind": "card",
                "secret": {"number": PASSWORD},
            },
            {
                "service": "s",
                "label": "l",
                "kind": "password",
                "secret": {"password": [PASSWORD]},
            },
            {
                "service": "",
                "label": "l",
                "kind": "password",
                "secret": {"password": PASSWORD},
            },
        ]
        for body in bodies:
            reply = self.client.post("/api/credentials", json=body)
            self.assertEqual(reply.status_code, 422)
            self.assertNotIn(PASSWORD, reply.text)
        reply = self.client.post(
            "/api/credentials",
            content=b'{"secret": {"password": "' + PASSWORD.encode() + b'"',
        )
        self.assertEqual(reply.status_code, 422)
        self.assertNotIn(PASSWORD, reply.text)

    def test_rename_replace_delete(self):
        cid = self.duke().id
        self.assertEqual(
            self.client.patch(
                f"/api/credentials/{cid}", json={"label": "Power"}
            ).json()["label"],
            "Power",
        )
        replaced = self.client.put(
            f"/api/credentials/{cid}/secret", json={"secret": {"password": "new"}}
        )
        self.assertEqual(replaced.status_code, 200)
        self.assertEqual(self.client.delete(f"/api/credentials/{cid}").status_code, 204)
        self.assertEqual(self.client.get(f"/api/credentials/{cid}").status_code, 404)

    def test_needs_the_owner_token(self):
        self.client.headers.pop("Authorization")
        self.assertEqual(self.client.get("/api/credentials").status_code, 401)
        reply = self.client.post(
            "/api/credentials",
            json={
                "service": "s",
                "label": "l",
                "kind": "password",
                "secret": {"password": PASSWORD},
            },
        )
        self.assertEqual(reply.status_code, 401)
        self.assertEqual(self.store.all(), [])


if __name__ == "__main__":
    unittest.main()
