import asyncio
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from slate.access.credentials import (
    BadOrigin,
    Credential,
    CredentialStore,
    Secret,
    origin_of,
)
from slate.access.fill import (
    CannotFill,
    Releases,
    check_fillable,
    identifier_type,
    vault_secret,
)
from slate.access.vault import Keyring
from slate.agent.vault import HermesVault, VaultError
from test_agent_credentials import FakeVault


def credential(kind="password", username="minjoo", origin=None):
    return Credential(
        id="cred_1",
        service="duke-energy.com",
        label="Electric",
        kind=kind,
        username=username,
        origin=origin,
        hint=None,
        key_id="k1",
        created_at=0.0,
        updated_at=0.0,
        last_used_at=None,
        use_count=0,
    )


OLD_TABLE = """
CREATE TABLE credentials (
    id TEXT PRIMARY KEY, service TEXT NOT NULL, label TEXT NOT NULL,
    kind TEXT NOT NULL, username TEXT, hint TEXT, key_id TEXT NOT NULL,
    nonce BLOB NOT NULL, ciphertext BLOB NOT NULL, created_at REAL NOT NULL,
    updated_at REAL NOT NULL, last_used_at REAL,
    use_count INTEGER NOT NULL DEFAULT 0
)
"""
CARD = Secret(
    "cred_1", None, {"number": "4111 1111 1111 1234", "expiry": "8/29", "cvc": "737"}
)


class OriginTests(unittest.TestCase):
    def test_keeps_only_scheme_host_and_port(self):
        cases = {
            "https://WWW.Duke-Energy.com/login?next=/pay": "https://www.duke-energy.com",
            "https://shop.example:443/cart": "https://shop.example",
            "https://shop.example:8443/cart": "https://shop.example:8443",
            "http://127.0.0.1:8080/fixture": "http://127.0.0.1:8080",
        }
        for address, origin in cases.items():
            with self.subTest(address):
                self.assertEqual(origin_of(address), origin)

    def test_refuses_plain_http_and_non_addresses(self):
        for address in (
            "http://duke-energy.com",
            "duke-energy.com",
            "javascript:alert(1)",
            "file:///etc/passwd",
            "https://shop.example:99999",
        ):
            with self.subTest(address), self.assertRaises(BadOrigin):
                origin_of(address)


class VaultSecretTests(unittest.TestCase):
    def test_login_identifier_kinds(self):
        self.assertEqual(identifier_type("minjoo@purdue.edu"), "email")
        self.assertEqual(identifier_type("+1 (765) 555-0100"), "phone")
        self.assertEqual(identifier_type("park1525"), "username")

    def test_card_fields_match_hermes_names(self):
        values = vault_secret(credential("card", None), CARD)
        self.assertEqual(
            values,
            {
                "card_number": "4111111111111234",
                "exp_month": "08",
                "exp_year": "2029",
                "cvc": "737",
            },
        )
        named = Secret(
            "c", None, {**CARD.values, "expiry": "12/2031", "name": "M Park"}
        )
        values = vault_secret(credential("card", None), named)
        self.assertEqual((values["exp_month"], values["exp_year"]), ("12", "2031"))
        self.assertEqual(values["cardholder_name"], "M Park")

    def test_unreadable_expiry_is_refused(self):
        odd = Secret("c", None, {**CARD.values, "expiry": "August"})
        with self.assertRaises(CannotFill):
            vault_secret(credential("card", None), odd)

    def test_what_can_be_filled(self):
        site = "https://www.duke-energy.com"
        check_fillable(credential(origin=site), site)
        check_fillable(credential("card", None), "https://any.shop")
        for item, where in (
            (credential("token"), site),
            (credential(username=None), site),
            (credential(origin=site), "https://duke-energy.example"),
        ):
            with self.subTest(item.kind), self.assertRaises(CannotFill):
                check_fillable(item, where)


class ReleaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_vault_item_per_task_and_removed_when_it_ends(self):
        vault = FakeVault()
        held = Releases(vault)
        login = credential(origin="https://a.example")
        secret = Secret("cred_1", "minjoo", {"password": "pw"})
        first = await held.fill("tsk_1", login, secret, "https://a.example")
        again = await held.fill("tsk_1", login, secret, "https://a.example")
        other = await held.fill("tsk_2", login, secret, "https://a.example")
        self.assertEqual((first, again, other), ("vault_1", "vault_1", "vault_2"))
        self.assertEqual(held.site("tsk_1", "cred_1"), "https://a.example")
        held.finish("tsk_1")
        held.finish("tsk_unknown")
        await asyncio.gather(*held.cleanup)
        self.assertEqual(vault.removed, ["vault_1"])
        self.assertIsNone(held.site("tsk_1", "cred_1"))
        await held.close()
        self.assertEqual(vault.removed, ["vault_1", "vault_2"])

    async def test_failed_removal_is_logged_not_raised(self):
        vault = FakeVault()

        async def broken(handle):
            raise VaultError("Hermes is not installed here")

        vault.remove = broken
        held = Releases(vault)
        login = credential(origin="https://a.example")
        await held.fill(
            "tsk_1", login, Secret("c", "m", {"password": "pw"}), "https://a.example"
        )
        with self.assertLogs("slate.access.fill", "ERROR"):
            await held.close()


class HermesVaultTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_hermes_is_a_clear_error(self):
        vault = HermesVault(python=Path("/nonexistent/python"), home=Path("/tmp"))
        with self.assertRaisesRegex(VaultError, "make agent-setup"):
            await vault.add("login", "x", "https://a.example", {"password": "pw"})


class StoredOriginTests(unittest.TestCase):
    def test_origin_is_saved_normalized_and_old_databases_gain_it(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "cred.db"
            with sqlite3.connect(path) as conn:
                conn.execute(OLD_TABLE)
            store = CredentialStore(path, Keyring([os.urandom(32)]))
            saved = store.add(
                "duke-energy.com",
                "Electric",
                "password",
                {"password": "pw"},
                username="minjoo",
                origin="https://www.duke-energy.com/login",
            )
            self.assertEqual(saved.origin, "https://www.duke-energy.com")
            moved = store.rename(saved.id, origin="https://duke-energy.com:443/")
            self.assertEqual(moved.origin, "https://duke-energy.com")
            self.assertEqual(store.rename(saved.id, label="Bills").origin, moved.origin)
            with self.assertRaises(BadOrigin):
                store.rename(saved.id, origin="http://duke-energy.com")
            store.close()


if __name__ == "__main__":
    unittest.main()
