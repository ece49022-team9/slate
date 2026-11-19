import json
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import httpx
from slate.app import app
from test_voice_session import AUTH, cloud

PASSWORD = "correct-horse-battery-staple"
CARD = "4111111111111234"
DUKE = "https://www.duke-energy.com"


class FakeVault:
    def __init__(self) -> None:
        self.added = []
        self.removed = []
        self.purged = []

    async def add(self, kind, label, origin, secret):
        handle = f"vault_{len(self.added) + 1}"
        self.added.append(
            {
                "handle": handle,
                "kind": kind,
                "label": label,
                "origin": origin,
                "secret": secret,
            }
        )
        return handle

    async def remove(self, handle):
        self.removed.append(handle)

    async def purge(self, prefix):
        self.purged.append(prefix)
        return 0


class CredentialAgent:
    def __init__(self, uses) -> None:
        self.uses = uses
        self.pending = set()
        self.timings = {}
        self.last_run = {}
        self.listed = None
        self.replies = []

    async def run(self, message, progress, *, device_context):
        scope = device_context.split("opaque scope is ", 1)[1].split(".", 1)[0]
        headers = {"Authorization": f"Bearer {scope}"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://slate"
        ) as client:
            listed = await client.get("/api/agent/credentials", headers=headers)
            self.listed = (listed.status_code, listed.json())
            for credential_id, site, purpose in self.uses:
                used = await client.post(
                    f"/api/agent/credentials/{credential_id}/use",
                    json={"purpose": purpose, "site": site},
                    headers=headers,
                    timeout=10,
                )
                self.replies.append((used.status_code, used.json()))
        reply = "Done." if self.replies[0][0] == 200 else "I could not log in."
        await progress({"type": "answer.complete", "text": reply})
        return reply

    async def close(self):
        pass


@contextmanager
def slate(agent):
    vault = FakeVault()
    with patch("slate.app.HermesVault", return_value=vault), cloud(agent) as client:
        yield client, vault


def save(client, **fields):
    reply = client.post("/api/credentials", headers=AUTH, json=fields)
    assert reply.status_code == 201, reply.text
    return reply.json()["id"]


def save_password(client, origin=DUKE):
    return save(
        client,
        service="duke-energy.com",
        label="Electric",
        kind="password",
        username="minjoo@example.com",
        origin=origin,
        secret={"password": PASSWORD},
    )


def save_card(client):
    return save(
        client,
        service="visa",
        label="Main card",
        kind="card",
        secret={"number": CARD, "expiry": "08/29", "cvc": "737"},
    )


def talk(client, answer):
    events = []
    with client.websocket_connect("/api/device/socket", headers=AUTH) as socket:
        socket.send_text(json.dumps({"type": "hello"}))
        socket.send_text(json.dumps({"type": "start"}))
        socket.receive_text()
        socket.send_bytes(b"\x10\x00" * 480)
        socket.send_text(json.dumps({"type": "end"}))
        while True:
            message = socket.receive()
            if message.get("bytes") is not None:
                continue
            event = json.loads(message["text"])
            events.append(event)
            if event["type"] == "approval" and answer is not None:
                socket.send_text(
                    json.dumps(
                        {
                            "type": "approve",
                            "turn_id": event["turn_id"],
                            "run_id": event["run_id"],
                            "request_id": event["request_id"],
                            "choice": answer,
                        }
                    )
                )
            if event["type"] == "reply" and event["final"]:
                return events


class AgentCredentialTests(unittest.TestCase):
    def test_allowed_login_goes_to_the_browser_vault_not_the_model(self):
        agent = CredentialAgent([])
        with slate(agent) as (client, vault):
            credential_id = save_password(client)
            agent.uses = [
                (credential_id, f"{DUKE}/login?next=/pay", "pay the October bill"),
                (credential_id, f"{DUKE}/account", "check the balance"),
            ]
            events = talk(client, "once")
            [asked] = [e for e in events if e["type"] == "approval"]
            self.assertIn("duke-energy.com login (Electric)", asked["command"])
            self.assertIn(f"on {DUKE} to pay the October bill", asked["command"])
            [closed] = [e for e in events if e["type"] == "approval_resolved"]
            self.assertEqual(
                (closed["status"], closed["decided_by"]), ("approved", "device")
            )
            status, listed = agent.listed
            self.assertEqual(status, 200)
            self.assertEqual(listed[0]["origin"], DUKE)
            [(first, released), (second, again)] = agent.replies
            self.assertEqual((first, second), (200, 200))
            self.assertEqual(released, again)
            self.assertEqual(released["handle"], "vault_1")
            self.assertEqual(released["origin"], DUKE)
            self.assertEqual(released["identifier"], "minjoo@example.com")
            self.assertIn("browser_vault_fill", released["next"])
            self.assertNotIn(PASSWORD, json.dumps([listed, released, events]))
            [stored] = vault.added
            self.assertEqual(
                (stored["kind"], stored["label"], stored["origin"]),
                ("login", "Slate: Electric", DUKE),
            )
            self.assertEqual(
                stored["secret"],
                {
                    "identifier_type": "email",
                    "identifier": "minjoo@example.com",
                    "password": PASSWORD,
                },
            )
            uses = client.get(f"/api/credentials/{credential_id}", headers=AUTH)
            uses = uses.json()["uses"]
            self.assertEqual([use["allowed"] for use in uses], [True, True, False])
            [task] = client.get("/api/tasks", headers=AUTH).json()
            self.assertEqual(task["state"], "succeeded")
            self.assertEqual([use["task_id"] for use in uses], [task["id"]] * 3)
        self.assertEqual(vault.removed, ["vault_1"])
        self.assertEqual(vault.purged, ["Slate: "])

    def test_denied_request_puts_nothing_in_the_vault(self):
        agent = CredentialAgent([])
        with slate(agent) as (client, vault):
            credential_id = save_password(client)
            agent.uses = [(credential_id, DUKE, "pay the bill")]
            events = talk(client, "deny")
            [(status, body)] = agent.replies
            self.assertEqual(status, 403)
            self.assertEqual(vault.added, [])
            self.assertNotIn(PASSWORD, json.dumps([body, events]))
            self.assertEqual(events[-1]["text"], "I could not log in.")

    def test_login_is_filled_only_on_its_own_site(self):
        agent = CredentialAgent([])
        with slate(agent) as (client, vault):
            credential_id = save_password(client)
            agent.uses = [(credential_id, "https://duke-energy.example", "log in")]
            events = talk(client, "once")
            [(status, body)] = agent.replies
            self.assertEqual(status, 422)
            self.assertIn(DUKE, body["detail"])
            self.assertFalse([e for e in events if e["type"] == "approval"])
            self.assertEqual(vault.added, [])

    def test_card_stays_on_the_site_the_user_allowed(self):
        agent = CredentialAgent([])
        with slate(agent) as (client, vault):
            card = save_card(client)
            shop = "https://pay.duke-energy.com"
            agent.uses = [
                (card, f"{shop}/checkout", "pay $84"),
                (card, "https://other-shop.example/checkout", "pay $84"),
            ]
            talk(client, "once")
            [(first, released), (second, refused)] = agent.replies
            self.assertEqual((first, second), (200, 403))
            self.assertIn(shop, refused["detail"])
            [stored] = vault.added
            self.assertEqual((stored["kind"], stored["origin"]), ("payment", shop))
            self.assertEqual(
                stored["secret"],
                {
                    "card_number": CARD,
                    "exp_month": "08",
                    "exp_year": "2029",
                    "cvc": "737",
                },
            )
            self.assertNotIn(CARD, json.dumps(agent.replies))

    def test_tokens_are_not_offered_to_the_agent(self):
        agent = CredentialAgent([])
        with slate(agent) as (client, vault):
            token = save(
                client,
                service="github",
                label="Deploy",
                kind="token",
                secret={"token": "ghp_secret"},
            )
            agent.uses = [(token, "https://github.com", "deploy")]
            events = talk(client, "once")
            self.assertEqual(agent.listed, (200, []))
            [(status, body)] = agent.replies
            self.assertEqual(status, 422)
            self.assertNotIn("ghp_secret", json.dumps([body, events]))
            self.assertFalse([e for e in events if e["type"] == "approval"])

    def test_plain_http_sites_are_refused(self):
        agent = CredentialAgent([])
        with slate(agent) as (client, vault):
            credential_id = save_password(client, origin=None)
            agent.uses = [(credential_id, "http://duke-energy.com", "log in")]
            talk(client, "once")
            [(status, body)] = agent.replies
            self.assertEqual(status, 422)
            self.assertIn("https", body["detail"])
            self.assertEqual(vault.added, [])

    def test_needs_the_active_turn_scope(self):
        with slate(CredentialAgent([])) as (client, vault):
            credential_id = save_password(client)
            for headers in ({}, AUTH, {"Authorization": "Bearer old-scope"}):
                listed = client.get("/api/agent/credentials", headers=headers)
                self.assertIn(listed.status_code, (401, 409))
                used = client.post(
                    f"/api/agent/credentials/{credential_id}/use",
                    json={"purpose": "pay", "site": DUKE},
                    headers=headers,
                )
                self.assertIn(used.status_code, (401, 409))
                self.assertNotIn(PASSWORD, used.text)
            self.assertEqual(vault.added, [])


if __name__ == "__main__":
    unittest.main()
