import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

from slate.voice.audio import MAX_AUDIO_SECONDS, SAMPLE_BYTES, SAMPLE_RATE


class Turn:
    def __init__(self) -> None:
        self.id = uuid4().hex
        self.audio: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.receiving = True
        self.ending = False
        self.received = 0

    def push(self, pcm: bytes) -> None:
        if not self.receiving:
            return
        if not pcm or len(pcm) % SAMPLE_BYTES:
            raise ValueError("Audio must contain complete 16-bit samples")
        if self.received + len(pcm) > SAMPLE_RATE * SAMPLE_BYTES * MAX_AUDIO_SECONDS:
            self.finish()
            raise ValueError(f"Recordings are limited to {MAX_AUDIO_SECONDS} seconds")
        self.received += len(pcm)
        self.audio.put_nowait(pcm)

    def finish(self) -> None:
        if self.receiving:
            self.receiving = False
            self.audio.put_nowait(None)

    async def chunks(self) -> AsyncIterator[bytes]:
        while (chunk := await self.audio.get()) is not None:
            yield chunk
