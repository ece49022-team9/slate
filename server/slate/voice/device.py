import asyncio
import json
import struct
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path
from urllib.request import Request, urlopen

from livekit import rtc

from slate.voice.audio import SAMPLE_BYTES, SAMPLE_RATE, read_wav, write_wav
from slate.voice.settings import REPLY_TOPIC, TRANSCRIPT_TOPIC


@dataclass
class VoiceResult:
    transcript: str
    reply: str
    audio: bytes


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
    audio: AsyncIterator[bytes], api_url: str, sample_rate: int
) -> VoiceResult:
    session = await asyncio.to_thread(
        session_request, f"{api_url}/api/voice/sessions", "POST", b"{}"
    )
    room = rtc.Room()
    source = rtc.AudioSource(sample_rate, 1, queue_size_ms=100)
    result: asyncio.Future[tuple[str, str]] = asyncio.get_running_loop().create_future()
    transcript = ""
    reply = ""
    receiving_reply = False
    reply_audio = bytearray()
    reader: asyncio.Task | None = None
    turn = ""

    @room.on("track_subscribed")
    def on_track(track: rtc.RemoteTrack, _publication, participant) -> None:
        nonlocal reader
        if (
            participant.identity != session["worker_identity"]
            or track.kind != rtc.TrackKind.KIND_AUDIO
        ):
            return

        async def read_track() -> None:
            stream = rtc.AudioStream(
                track, sample_rate=SAMPLE_RATE, num_channels=1, frame_size_ms=20
            )
            try:
                async for event in stream:
                    if receiving_reply:
                        reply_audio.extend(event.frame.data.tobytes())
            finally:
                await stream.aclose()

        reader = asyncio.create_task(read_track())

    @room.on("data_received")
    def on_data(packet: rtc.DataPacket) -> None:
        nonlocal transcript, reply, receiving_reply
        if (
            packet.topic not in (TRANSCRIPT_TOPIC, REPLY_TOPIC)
            or packet.participant is None
            or packet.participant.identity != session["worker_identity"]
            or result.done()
        ):
            return
        event = json.loads(packet.data)
        if event["turn_id"] != turn:
            return
        if "error" in event:
            result.set_exception(RuntimeError(event["error"]))
        elif packet.topic == TRANSCRIPT_TOPIC:
            if event.get("final"):
                transcript = event["text"]
                if not transcript:
                    result.set_result(("", ""))
        else:
            reply = event["text"]
            receiving_reply = True
            if event.get("final"):
                result.set_result((transcript, reply))

    try:
        await room.connect(session["server_url"], session["participant_token"])
        track = rtc.LocalAudioTrack.create_audio_track("microphone", source)
        options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        publication = await room.local_participant.publish_track(track, options)
        await asyncio.wait_for(publication.wait_for_subscription(), timeout=10)
        turn = await room.local_participant.perform_rpc(
            destination_identity=session["worker_identity"],
            method="start_turn",
            payload="",
        )
        async with aclosing(aiter(audio)) as chunks:
            async for chunk in chunks:
                if not chunk or len(chunk) % SAMPLE_BYTES:
                    raise ValueError("slate.voice: incomplete PCM samples")
                frame = rtc.AudioFrame(
                    chunk, sample_rate, 1, len(chunk) // SAMPLE_BYTES
                )
                await source.capture_frame(frame)
        await source.wait_for_playout()
        await room.local_participant.perform_rpc(
            destination_identity=session["worker_identity"],
            method="end_turn",
            payload=turn,
        )
        transcript, reply = await asyncio.wait_for(result, timeout=360)
        if reply:
            await asyncio.sleep(0.5)
            if not reply_audio:
                raise RuntimeError("Slate spoke, but no LiveKit reply audio arrived")
        return VoiceResult(
            transcript,
            reply,
            write_wav(trim_transport_silence(bytes(reply_audio))) if reply else b"",
        )
    finally:
        if reader:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        await source.aclose()
        await room.disconnect()
        await asyncio.to_thread(
            session_request,
            f"{api_url}/api/voice/sessions/{session['session_id']}",
            "DELETE",
        )
