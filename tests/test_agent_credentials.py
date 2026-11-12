import json
import unittest

import httpx
from slate.app import app
from test_voice_session import AUTH, cloud

PASSWORD = "correct-horse-battery-staple"


class CredentialAgent:
    def __init__(self, purpose="pay the October electric bill") -> None:
        self.purpose = purpose
        self.pending = set()
        self.timings = {}
        self.last_run = {}
        self.listed = None
        self.used = None
        self.scope = None

    async def run(self, message, progress, *, device_context):
        self.scope = device_context.split("opaque scope is ", 1)[1].split(".", 1)[0]
        headers = {"Authorization": f"Bearer {self.scope}"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://slate"
        ) as client:
            listed = await client.get("/api/agent/credentials", headers=headers)
            self.listed = (listed.status_code, listed.json())
            credential_id = listed.json()[0]["id"]
            used = await client.post(
                f"/api/agent/credentials/{credential_id}/use",
                json={"purpose": self.purpose},
                headers=headers,
                timeout=10,
            )
            self.used = (used.status_code, used.json())
            again = await client.post(
                f"/api/agent/credentials/{credential_id}/use",
                json={"purpose": "check the balance"},
                headers=headers,
                timeout=10,
            )
            self.again = again.status_code
        reply = "Paid." if self.used[0] == 200 else "I could not log in."
        await progress({"type": "answer.complete", "text": reply})
        return reply

    async def close(self):
        pass


def save_password(client):
    reply = client.post(
        "/api/credentials",
        headers=AUTH,
        json={
            "service": "duke-energy.com",
            "label": "Electric",
            "kind": "password",
            "username": "minjoo",
            "secret": {"password": PASSWORD},
        },
    )
    assert reply.status_code == 201, reply.text
    return reply.json()["id"]


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
    def test_agent_gets_the_secret_only_after_the_user_allows_it(self):
        agent = CredentialAgent()
        with cloud(agent) as client:
            credential_id = save_password(client)
            events = talk(client, "once")
            [asked] = [e for e in events if e["type"] == "approval"]
            self.assertIn("duke-energy.com", asked["command"])
            self.assertIn("pay the October electric bill", asked["command"])
            self.assertNotIn(PASSWORD, json.dumps(events))
            [closed] = [e for e in events if e["type"] == "approval_resolved"]
            self.assertEqual(
                (closed["status"], closed["decided_by"]), ("approved", "device")
            )
            status, listed = agent.listed
            self.assertEqual(status, 200)
            self.assertNotIn(PASSWORD, json.dumps(listed))
            self.assertEqual(agent.used[0], 200)
            self.assertEqual(agent.used[1]["values"], {"password": PASSWORD})
            self.assertEqual(agent.used[1]["username"], "minjoo")
            self.assertEqual(agent.again, 200)
            detail = client.get(f"/api/credentials/{credential_id}", headers=AUTH)
            uses = detail.json()["uses"]
            self.assertEqual([use["allowed"] for use in uses], [True, True, False])
            [task] = client.get("/api/tasks", headers=AUTH).json()
            self.assertEqual(task["state"], "succeeded")
            self.assertEqual([use["task_id"] for use in uses], [task["id"]] * 3)

    def test_denied_request_gets_no_secret(self):
        agent = CredentialAgent()
        with cloud(agent) as client:
            save_password(client)
            events = talk(client, "deny")
            self.assertEqual(agent.used[0], 403)
            self.assertNotIn(PASSWORD, json.dumps(agent.used[1]))
            self.assertNotIn(PASSWORD, json.dumps(events))
            self.assertEqual(events[-1]["text"], "I could not log in.")

    def test_needs_the_active_turn_scope(self):
        with cloud(CredentialAgent()) as client:
            credential_id = save_password(client)
            for headers in ({}, AUTH, {"Authorization": "Bearer old-scope"}):
                listed = client.get("/api/agent/credentials", headers=headers)
                self.assertIn(listed.status_code, (401, 409))
                used = client.post(
                    f"/api/agent/credentials/{credential_id}/use",
                    json={"purpose": "pay"},
                    headers=headers,
                )
                self.assertIn(used.status_code, (401, 409))
                self.assertNotIn(PASSWORD, used.text)


if __name__ == "__main__":
    unittest.main()
