import asyncio
import json
from pathlib import Path
from urllib.request import Request, urlopen

from livekit import rtc

from slate.voice.audio import SAMPLE_BYTES, SAMPLE_RATE, read_wav
from slate.voice.settings import TRANSCRIPT_TOPIC


def session_request(url: str, method: str, body: bytes | None = None) -> dict:
    request = Request(
        url, data=body, method=method, headers={"Content-Type": "application/json"}
    )
    with urlopen(request, timeout=15) as response:
        data = response.read()
        return json.loads(data) if data else {}


async def simulate(input_file: Path, api_url: str, sample_rate: int = 16_000) -> str:
    pcm = read_wav(input_file.read_bytes())
    session = await asyncio.to_thread(
        session_request, f"{api_url}/api/voice/sessions", "POST", b"{}"
    )
    room = rtc.Room()
    source = rtc.AudioSource(sample_rate, 1, queue_size_ms=100)
    transcript: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    turn = ""

    @room.on("data_received")
    def on_data(packet: rtc.DataPacket) -> None:
        if (
            packet.topic != TRANSCRIPT_TOPIC
            or packet.participant is None
            or packet.participant.identity != session["worker_identity"]
            or transcript.done()
        ):
            return
        event = json.loads(packet.data)
        if event["turn_id"] != turn:
            return
        if "error" in event:
            transcript.set_exception(RuntimeError(event["error"]))
        elif event.get("final"):
            transcript.set_result(event["text"])

    try:
        await room.connect(session["server_url"], session["participant_token"])
        track = rtc.LocalAudioTrack.create_audio_track("microphone", source)
        options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        await room.local_participant.publish_track(track, options)
        turn = await room.local_participant.perform_rpc(
            destination_identity=session["worker_identity"],
            method="start_turn",
            payload="",
        )
        resampler = rtc.AudioResampler(SAMPLE_RATE, sample_rate, num_channels=1)
        padding = bytes(SAMPLE_RATE * SAMPLE_BYTES // 5)
        audio = padding + pcm + padding
        frame_bytes = SAMPLE_RATE * SAMPLE_BYTES // 50
        for offset in range(0, len(audio), frame_bytes):
            chunk = audio[offset : offset + frame_bytes]
            frame = rtc.AudioFrame(chunk, SAMPLE_RATE, 1, len(chunk) // SAMPLE_BYTES)
            for converted in resampler.push(frame):
                await source.capture_frame(converted)
        for converted in resampler.flush():
            await source.capture_frame(converted)
        await source.wait_for_playout()
        await room.local_participant.perform_rpc(
            destination_identity=session["worker_identity"],
            method="end_turn",
            payload=turn,
        )
        return await asyncio.wait_for(transcript, timeout=360)
    finally:
        await source.aclose()
        await room.disconnect()
        await asyncio.to_thread(
            session_request,
            f"{api_url}/api/voice/sessions/{session['session_id']}",
            "DELETE",
        )
