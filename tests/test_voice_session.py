import asyncio
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from livekit import api
from slate.app import app
from slate.voice.audio import MAX_AUDIO_SECONDS, SAMPLE_BYTES, SAMPLE_RATE
from slate.voice.settings import VoiceSettings
from slate.voice.turn import Turn


class TurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_keeps_audio_in_order_and_ignores_late_frames(self):
        turn = Turn()
        turn.push(b"\x01\x00")
        turn.push(b"\x02\x00")
        turn.finish()
        turn.push(b"\x03\x00")
        turn.finish()
        async with asyncio.timeout(1):
            audio = b"".join([chunk async for chunk in turn.chunks()])
        self.assertEqual(audio, b"\x01\x00\x02\x00")

    async def test_limits_audio_in_memory(self):
        turn = Turn()
        limit = MAX_AUDIO_SECONDS * SAMPLE_RATE * SAMPLE_BYTES
        turn.push(bytes(limit))
        with self.assertRaisesRegex(ValueError, "limited"):
            turn.push(b"\x00\x00")
        async with asyncio.timeout(1):
            size = sum([len(chunk) async for chunk in turn.chunks()])
        self.assertEqual(size, limit)

    async def test_rejects_partial_samples(self):
        turn = Turn()
        with self.assertRaisesRegex(ValueError, "complete"):
            turn.push(b"\x00")


class VoiceAccessTests(unittest.TestCase):
    def test_device_token_is_scoped_to_one_room_and_microphone(self):
        settings = VoiceSettings("ws://127.0.0.1:7880", "key", "s" * 32)
        token = settings.token("room-a", "device-a")
        claims = api.TokenVerifier("key", "s" * 32).verify(token)
        self.assertEqual(claims.identity, "device-a")
        self.assertEqual(claims.video.room, "room-a")
        self.assertEqual(claims.video.can_publish_sources, ["microphone"])
        self.assertFalse(claims.video.room_admin)

    def test_requires_complete_livekit_configuration(self):
        with patch.dict("os.environ", {"LIVEKIT_URL": "wss://example.com"}, clear=True):
            with self.assertRaisesRegex(ValueError, "together"):
                VoiceSettings.from_env()

    def test_rejects_foreign_browser_origin(self):
        with TestClient(app, client=("127.0.0.1", 1234)) as client:
            response = client.post(
                "/api/voice/sessions",
                json={},
                headers={"Origin": "https://example.com"},
            )
        self.assertEqual(response.status_code, 403)

    def test_rejects_remote_session_creation(self):
        with TestClient(app, client=("192.0.2.1", 1234)) as client:
            response = client.post("/api/voice/sessions", json={})
        self.assertEqual(response.status_code, 403)

    def test_rejects_invalid_session_request_before_connecting(self):
        with TestClient(app, client=("127.0.0.1", 1234)) as client:
            response = client.post("/api/voice/sessions", json={"room": "other-room"})
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
