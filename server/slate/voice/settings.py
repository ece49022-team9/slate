import os
from dataclasses import dataclass
from datetime import timedelta
from typing import Self

from livekit import api

WORKER_IDENTITY = "slate-transcriber"
TRANSCRIPT_TOPIC = "slate.transcript"


@dataclass(frozen=True)
class VoiceSettings:
    url: str
    api_key: str
    api_secret: str

    @classmethod
    def from_env(cls) -> Self:
        values = [
            os.getenv(name)
            for name in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")
        ]
        if not any(values):
            return cls("ws://127.0.0.1:7880", "devkey", "secret")
        if not all(values):
            raise ValueError(
                "Set LIVEKIT_URL, LIVEKIT_API_KEY and LIVEKIT_API_SECRET together"
            )
        return cls(*values)

    def token(self, room: str, identity: str, *, worker: bool = False) -> str:
        grants = api.VideoGrants(
            room_join=True,
            room=room,
            can_publish=not worker,
            can_publish_sources=[] if worker else ["microphone"],
            can_subscribe=True,
            can_publish_data=True,
        )
        return (
            api.AccessToken(self.api_key, self.api_secret)
            .with_identity(identity)
            .with_ttl(timedelta(minutes=10))
            .with_grants(grants)
            .to_jwt()
        )
