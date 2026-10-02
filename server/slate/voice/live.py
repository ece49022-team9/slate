from uuid import uuid4

from livekit import api

from slate.voice.settings import LIVE_AGENT, WORKER_IDENTITY, VoiceSettings


class LiveRoom:
    """An always-listening call. The device's token asks LiveKit to send the
    GPT-Live worker into the room; this server only hands out that token and
    ends the room afterward."""

    def __init__(self, settings: VoiceSettings) -> None:
        self.settings = settings
        self.id = uuid4().hex
        self.room_name = f"slate-live-{self.id}"
        self.device_identity = f"device-{self.id}"
        self.closed = False

    async def connect(self) -> dict[str, str]:
        return {
            "session_id": self.id,
            "server_url": self.settings.url,
            "participant_token": self.settings.token(
                self.room_name, self.device_identity, agent=LIVE_AGENT
            ),
            "worker_identity": WORKER_IDENTITY,
        }

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        livekit = api.LiveKitAPI(
            self.settings.url, self.settings.api_key, self.settings.api_secret
        )
        try:
            await livekit.room.delete_room(api.DeleteRoomRequest(room=self.room_name))
        finally:
            await livekit.aclose()
