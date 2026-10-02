import asyncio
import json
import struct
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self
from urllib.request import Request, urlopen

from livekit import rtc

from slate.voice.audio import (
    AUDIBLE_PEAK,
    SAMPLE_BYTES,
    SAMPLE_RATE,
    SPEECH_PEAK,
    peak,
    read_wav,
    write_wav,
)
from slate.voice.settings import REPLY_TOPIC, TRANSCRIPT_TOPIC
from slate.voice.timing import Timeline


@dataclass
class VoiceResult:
    transcript: str
    reply: str
    audio: bytes
    timings: dict


def trim_transport_silence(pcm: bytes) -> bytes:
    first = last = None
    for index, (sample,) in enumerate(struct.iter_unpack("<h", pcm)):
        if abs(sample) > 96:
            if first is None:
                first = index
            last = index
    if first is None or last is None:
        raise RuntimeError("No audible reply arrived from LiveKit")
    padding = SAMPLE_RATE // 5
    return pcm[2 * max(0, first - padding) : 2 * min(len(pcm) // 2, last + padding)]


def session_request(url: str, method: str, body: bytes | None = None) -> dict:
    request = Request(
        url, data=body, method=method, headers={"Content-Type": "application/json"}
    )
    with urlopen(request, timeout=15) as response:
        data = response.read()
        return json.loads(data) if data else {}


async def simulate(
    input_file: Path, api_url: str, sample_rate: int = 16_000
) -> VoiceResult:
    pcm = read_wav(input_file.read_bytes())

    async def chunks() -> AsyncIterator[bytes]:
        resampler = rtc.AudioResampler(SAMPLE_RATE, sample_rate, num_channels=1)
        padding = bytes(SAMPLE_RATE * SAMPLE_BYTES // 5)
        audio = padding + pcm + padding
        frame_bytes = SAMPLE_RATE * SAMPLE_BYTES // 50
        for offset in range(0, len(audio), frame_bytes):
            chunk = audio[offset : offset + frame_bytes]
            frame = rtc.AudioFrame(chunk, SAMPLE_RATE, 1, len(chunk) // SAMPLE_BYTES)
            for converted in resampler.push(frame):
                yield converted.data.tobytes()
        for converted in resampler.flush():
            yield converted.data.tobytes()

    return await transcribe_audio(chunks(), api_url, sample_rate)


async def transcribe_audio(
    audio: AsyncIterator[bytes],
    api_url: str,
    sample_rate: int,
    *,
    profile: bool = False,
) -> VoiceResult:
    async with CascadeCall(api_url, sample_rate, profile=profile) as call:
        return await call.turn(audio)


@dataclass
class CascadeTurn:
    timing: Timeline
    result: asyncio.Future
    id: str = ""
    transcript: str = ""
    reply: str = ""
    receiving_reply: bool = False
    audio: bytearray = field(default_factory=bytearray)
    server: dict = field(default_factory=dict)


class CascadeCall:
    """The device's side of a push-to-talk session: one LiveKit connection that
    carries any number of start, speak, and end turns."""

    def __init__(self, api_url: str, sample_rate: int, *, profile: bool = False):
        self.api_url = api_url
        self.sample_rate = sample_rate
        self.profile = profile
        self.room = rtc.Room()
        self.source = rtc.AudioSource(sample_rate, 1, queue_size_ms=100)
        self.timing = Timeline()
        self.session: dict = {}
        self.current: CascadeTurn | None = None
        self.reader: asyncio.Task | None = None

    async def __aenter__(self) -> Self:
        self.timing.mark("session_requested")
        self.session = await asyncio.to_thread(
            session_request,
            f"{self.api_url}/api/voice/sessions",
            "POST",
            json.dumps({"profile": self.profile}).encode(),
        )
        self.timing.mark("session_ready")
        self.room.on("track_subscribed", self.track_subscribed)
        self.room.on("data_received", self.data_received)
        try:
            self.timing.mark("livekit_connect_requested")
            await self.room.connect(
                self.session["server_url"], self.session["participant_token"]
            )
            self.timing.mark("livekit_connected")
            track = rtc.LocalAudioTrack.create_audio_track("microphone", self.source)
            options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
            publication = await self.room.local_participant.publish_track(
                track, options
            )
            await asyncio.wait_for(publication.wait_for_subscription(), timeout=10)
            self.timing.mark("mic_subscribed")
        except BaseException:
            await self.close()
            raise
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()

    def track_subscribed(self, track: rtc.RemoteTrack, _publication, participant):
        if (
            participant.identity == self.session["worker_identity"]
            and track.kind == rtc.TrackKind.KIND_AUDIO
            and self.reader is None
        ):
            self.reader = asyncio.create_task(self.read_reply(track))

    async def read_reply(self, track: rtc.RemoteTrack) -> None:
        stream = rtc.AudioStream(
            track, sample_rate=SAMPLE_RATE, num_channels=1, frame_size_ms=20
        )
        try:
            async for event in stream:
                turn = self.current
                if turn is None or not turn.receiving_reply:
                    continue
                chunk = event.frame.data.tobytes()
                turn.timing.mark("reply_first_frame")
                if peak(chunk) > AUDIBLE_PEAK:
                    turn.timing.mark("reply_first_audible")
                turn.audio.extend(chunk)
        finally:
            await stream.aclose()

    def data_received(self, packet: rtc.DataPacket) -> None:
        turn = self.current
        if (
            turn is None
            or packet.topic not in (TRANSCRIPT_TOPIC, REPLY_TOPIC)
            or packet.participant is None
            or packet.participant.identity != self.session["worker_identity"]
            or turn.result.done()
        ):
            return
        event = json.loads(packet.data)
        if event["turn_id"] != turn.id:
            return
        if "error" in event:
            turn.result.set_exception(RuntimeError(event["error"]))
        elif packet.topic == TRANSCRIPT_TOPIC:
            if event.get("text", "").strip():
                turn.timing.mark("stt_first_text")
            if event.get("final"):
                turn.timing.mark("transcript_received")
                turn.transcript = event["text"]
                if not turn.transcript:
                    turn.result.set_result(None)
        else:
            turn.timing.mark("reply_text_received")
            turn.reply = event["text"]
            turn.receiving_reply = True
            if event.get("final"):
                turn.timing.mark("reply_complete_received")
                turn.server = event.get("timing", {})
                turn.result.set_result(None)

    async def turn(self, audio: AsyncIterator[bytes]) -> VoiceResult:
        turn = CascadeTurn(Timeline(), asyncio.get_running_loop().create_future())
        self.current = turn
        try:
            turn.timing.mark("turn_requested")
            turn.id = await self.room.local_participant.perform_rpc(
                destination_identity=self.session["worker_identity"],
                method="start_turn",
                payload="",
            )
            turn.timing.mark("turn_ready")
            async with aclosing(aiter(audio)) as chunks:
                async for chunk in chunks:
                    if not chunk or len(chunk) % SAMPLE_BYTES:
                        raise ValueError("slate.voice: incomplete PCM samples")
                    frame = rtc.AudioFrame(
                        chunk, self.sample_rate, 1, len(chunk) // SAMPLE_BYTES
                    )
                    turn.timing.mark("input_first_capture")
                    if peak(chunk) > SPEECH_PEAK:
                        turn.timing.mark("input_speech_last", replace=True)
                    await self.source.capture_frame(frame)
                    turn.timing.mark("input_last_capture", replace=True)
            turn.timing.mark("input_drain_requested")
            await self.source.wait_for_playout()
            turn.timing.mark("end_requested")
            await self.room.local_participant.perform_rpc(
                destination_identity=self.session["worker_identity"],
                method="end_turn",
                payload=turn.id,
            )
            turn.timing.mark("end_acknowledged")
            await asyncio.wait_for(turn.result, timeout=360)
            if turn.reply:
                await asyncio.sleep(0.5)
                turn.timing.mark("receive_drain_done")
                if not turn.audio:
                    raise RuntimeError("Slate spoke, but no reply audio arrived")
            device = turn.timing.snapshot()
            device["marks_ns"] = {**self.timing.marks, **device["marks_ns"]}
            return VoiceResult(
                turn.transcript,
                turn.reply,
                write_wav(trim_transport_silence(bytes(turn.audio)))
                if turn.reply
                else b"",
                {**turn.server, "device": device},
            )
        finally:
            self.current = None

    async def close(self) -> None:
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
        await self.source.aclose()
        await self.room.disconnect()
        if self.session:
            await asyncio.to_thread(
                session_request,
                f"{self.api_url}/api/voice/sessions/{self.session['session_id']}",
                "DELETE",
            )


class LiveCall:
    """The device's side of a duplex call. The microphone streams continuously;
    every mic chunk and reply frame is kept with its arrival time and peak."""

    def __init__(self, api_url: str, sample_rate: int) -> None:
        self.api_url = api_url
        self.sample_rate = sample_rate
        self.room = rtc.Room()
        self.source = rtc.AudioSource(sample_rate, 1, queue_size_ms=100)
        self.session: dict = {}
        self.mic: list[tuple[int, int]] = []
        self.reply: list[tuple[int, int]] = []
        self.reply_audio = bytearray()
        self.said: list[tuple[int, str]] = []
        self.errors: list[str] = []
        self.reader: asyncio.Task | None = None

    async def __aenter__(self) -> Self:
        self.session = await asyncio.to_thread(
            session_request,
            f"{self.api_url}/api/voice/sessions",
            "POST",
            json.dumps({"live": True}).encode(),
        )
        self.room.on("track_subscribed", self.track_subscribed)
        self.room.on("data_received", self.data_received)
        try:
            await self.room.connect(
                self.session["server_url"], self.session["participant_token"]
            )
            track = rtc.LocalAudioTrack.create_audio_track("microphone", self.source)
            options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
            publication = await self.room.local_participant.publish_track(
                track, options
            )
            await asyncio.wait_for(publication.wait_for_subscription(), timeout=10)
        except BaseException:
            await self.close()
            raise
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()

    def track_subscribed(self, track: rtc.RemoteTrack, _publication, participant):
        if (
            participant.identity == self.session["worker_identity"]
            and track.kind == rtc.TrackKind.KIND_AUDIO
            and self.reader is None
        ):
            self.reader = asyncio.create_task(self.read_reply(track))

    async def read_reply(self, track: rtc.RemoteTrack) -> None:
        stream = rtc.AudioStream(
            track, sample_rate=SAMPLE_RATE, num_channels=1, frame_size_ms=20
        )
        try:
            async for event in stream:
                chunk = event.frame.data.tobytes()
                self.reply.append((time.monotonic_ns(), peak(chunk)))
                self.reply_audio.extend(chunk)
        finally:
            await stream.aclose()

    def data_received(self, packet: rtc.DataPacket) -> None:
        if (
            packet.participant is None
            or packet.participant.identity != self.session["worker_identity"]
        ):
            return
        event = json.loads(packet.data)
        if "error" in event:
            self.errors.append(event["error"])
        elif packet.topic == REPLY_TOPIC:
            self.said.append((time.monotonic_ns(), event["text"]))

    async def send(self, chunk: bytes) -> None:
        if not chunk or len(chunk) % SAMPLE_BYTES:
            raise ValueError("slate.voice: incomplete PCM samples")
        self.mic.append((time.monotonic_ns(), peak(chunk)))
        await self.source.capture_frame(
            rtc.AudioFrame(chunk, self.sample_rate, 1, len(chunk) // SAMPLE_BYTES)
        )

    async def report(self) -> dict:
        return json.loads(
            await self.room.local_participant.perform_rpc(
                destination_identity=self.session["worker_identity"],
                method="live_report",
                payload="",
            )
        )

    async def finish(self) -> dict:
        """Close GPT-Live cleanly so the report includes its billed seconds."""
        return json.loads(
            await self.room.local_participant.perform_rpc(
                destination_identity=self.session["worker_identity"],
                method="live_finish",
                payload="",
                response_timeout=30,
            )
        )

    async def close(self) -> None:
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
        await self.source.aclose()
        await self.room.disconnect()
        if self.session:
            await asyncio.to_thread(
                session_request,
                f"{self.api_url}/api/voice/sessions/{self.session['session_id']}",
                "DELETE",
            )
