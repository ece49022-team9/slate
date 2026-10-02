import argparse
import asyncio
import json
import struct
from contextlib import AsyncExitStack, aclosing
from pathlib import Path
from uuid import uuid4

from livekit import rtc
from slate.board import ROOT
from slate.breadboard import breadboard, build_image
from slate.device import DeviceCommand, DeviceSDK, FirmwareDevice, TextRequest
from slate.voice.audio import SAMPLE_RATE
from slate.voice.client import speak
from slate.voice.device import session_request
from slate.voice.firmware import RATE, capture, stereo_pcm
from slate.voice.settings import REPLY_TOPIC, TRANSCRIPT_TOPIC


async def check(args: argparse.Namespace) -> dict:
    first_input = args.first_input
    if first_input is None:
        first_input = ROOT / ".local/agent-runs/interruption-input.wav"
        first_input.parent.mkdir(parents=True, exist_ok=True)
        first_input.write_bytes(
            await speak(
                "Tell me a story in at least twenty sentences. "
                "Start with Once upon a time, a tiny robot explored a forest."
            )
        )
    await asyncio.to_thread(build_image)
    async with breadboard(realtime=True) as bench:
        session = await asyncio.to_thread(
            session_request, f"{args.api_url}/api/voice/sessions", "POST", b"{}"
        )
        room = rtc.Room()
        source = rtc.AudioSource(RATE, 1, queue_size_ms=100)
        active = ""
        first = ""
        second = ""
        boundary = False
        events = []
        errors = []
        audible = asyncio.Event()
        finished = asyncio.Event()
        reader = None
        final_reply = ""
        second_audio = 0
        first_complete = False
        second_reply_started = False
        peer = FirmwareDevice(bench.link, lambda: active)

        @room.on("track_subscribed")
        def on_track(track, _publication, participant):
            nonlocal reader
            if participant.identity != session["worker_identity"]:
                return
            if track.kind != rtc.TrackKind.KIND_AUDIO:
                return

            async def listen():
                nonlocal second_audio
                async with aclosing(
                    rtc.AudioStream(
                        track, sample_rate=SAMPLE_RATE, num_channels=1, frame_size_ms=20
                    )
                ) as stream:
                    async for packet in stream:
                        pcm = packet.frame.data.tobytes()
                        if any(
                            abs(sample) > 96
                            for (sample,) in struct.iter_unpack("<h", pcm)
                        ):
                            if active == first and first:
                                audible.set()
                            elif active == second and second and second_reply_started:
                                second_audio += len(pcm)

            reader = asyncio.create_task(listen())

        @room.on("data_received")
        def on_data(packet):
            nonlocal final_reply, first_complete, second_reply_started
            if packet.participant is None:
                return
            if packet.participant.identity != session["worker_identity"]:
                return
            if packet.topic not in (TRANSCRIPT_TOPIC, REPLY_TOPIC, "slate.agent"):
                return
            event = json.loads(packet.data)
            events.append({"topic": packet.topic, **event})
            if (
                event.get("turn_id") == first
                and packet.topic == REPLY_TOPIC
                and event.get("final")
            ):
                first_complete = True
            if (
                event.get("turn_id") == second
                and packet.topic == REPLY_TOPIC
                and event.get("text")
            ):
                second_reply_started = True
            if (
                boundary
                and event.get("turn_id") == first
                and not event.get("cancelled")
            ):
                errors.append("Old turn published after the new start receipt")
            if event.get("turn_id") == active and event.get("error"):
                errors.append(event["error"])
                finished.set()
            if (
                event.get("turn_id") == second
                and packet.topic == REPLY_TOPIC
                and event.get("final")
            ):
                final_reply = event.get("text", "")
                finished.set()

        async def device_rpc(data):
            command = DeviceCommand.model_validate_json(data.payload)
            result = await peer.rpc(data)
            if boundary and command.turn_id == first:
                errors.append("Old turn received a successful device receipt")
            return result

        async def rpc(method, payload=""):
            return await room.local_participant.perform_rpc(
                destination_identity=session["worker_identity"],
                method=method,
                payload=payload,
                response_timeout=15,
            )

        async def feed(path):
            stereo = stereo_pcm(path, bench.slot)
            async with aclosing(
                capture(bench.link, "left", len(stereo) // 4, bench.play(stereo))
            ) as audio:
                async for pcm in audio:
                    await source.capture_frame(
                        rtc.AudioFrame(pcm, RATE, 1, len(pcm) // 2)
                    )
            await source.wait_for_playout()
            await rpc("end_turn", active)

        try:
            await room.connect(session["server_url"], session["participant_token"])
            room.local_participant.register_rpc_method("device.command", device_rpc)
            publication = await room.local_participant.publish_track(
                rtc.LocalAudioTrack.create_audio_track("microphone", source),
                rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
            )
            await asyncio.wait_for(publication.wait_for_subscription(), 10)
            first = active = await rpc("start_turn")
            await feed(first_input)
            waits = [
                asyncio.create_task(audible.wait()),
                asyncio.create_task(finished.wait()),
            ]
            try:
                done, _ = await asyncio.wait(
                    waits, timeout=360, return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    raise TimeoutError("First reply did not produce audio")
                if errors:
                    raise AssertionError(str(errors))
            finally:
                for waiting in waits:
                    waiting.cancel()
                await asyncio.gather(*waits, return_exceptions=True)
            if first_complete:
                raise AssertionError("First reply finished before interruption")
            print(
                json.dumps({"stage": "first_reply_audible", "status": "passed"}),
                flush=True,
            )
            active = ""
            second = await rpc("start_turn")
            active = second
            boundary = True
            try:
                await rpc("cancel_turn", first)
            except rtc.RpcError as error:
                if error.code != 1502:
                    raise
            else:
                raise AssertionError("Delayed old cancellation was accepted")
            print(
                json.dumps({"stage": "old_cancel_rejected", "status": "passed"}),
                flush=True,
            )
            await feed(args.second_input)
            await asyncio.wait_for(finished.wait(), 360)
            await asyncio.sleep(0.5)
            if errors or not final_reply or not second_audio:
                raise AssertionError(
                    str(errors or "New turn did not return text and audio")
                )
            if not any(
                expected.casefold() in final_reply.casefold()
                for expected in args.expect
            ):
                raise AssertionError("New reply did not contain the expected answer")

            active = "fixture-old"

            async def execute(command):
                return (await peer.execute(command)).model_dump()

            sdk = DeviceSDK("fixture", active, execute, lambda: active == "fixture-old")

            before = await sdk.get_status()
            pending = asyncio.create_task(sdk.show_text(TextRequest(text="STALE")))
            await asyncio.sleep(0)
            active = "fixture-new"
            try:
                await pending
            except ValueError:
                pass
            else:
                raise AssertionError("In-flight old SDK write was admitted")
            current = DeviceSDK(
                "fixture", active, execute, lambda: active == "fixture-new"
            )
            after = await current.get_status()
            if after.revision != before.revision or after.text != before.text:
                raise AssertionError("Invalidated SDK write changed QEMU firmware")
            try:
                await peer.execute(
                    DeviceCommand(
                        request_id=uuid4().hex,
                        turn_id="fixture-old",
                        operation="show_text",
                        arguments={"text": "LATE"},
                    )
                )
            except ValueError:
                pass
            else:
                raise AssertionError("Late firmware command was accepted")
            if not any(
                event.get("turn_id") == first and event.get("cancelled")
                for event in events
            ):
                raise AssertionError("Old turn cancellation receipt was not received")
            return {
                "status": "passed",
                "old_turn_cancelled": any(
                    event.get("turn_id") == first and event.get("cancelled")
                    for event in events
                ),
                "second_reply": final_reply,
                "second_audio_bytes": second_audio,
                "stale_sdk_revision_unchanged": True,
            }
        finally:
            async with AsyncExitStack() as cleanup:
                cleanup.push_async_callback(
                    asyncio.to_thread,
                    session_request,
                    f"{args.api_url}/api/voice/sessions/{session['session_id']}",
                    "DELETE",
                )
                cleanup.push_async_callback(room.disconnect)
                cleanup.push_async_callback(source.aclose)
                if reader:
                    reader.cancel()
                    await asyncio.gather(reader, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--first-input", type=Path)
    parser.add_argument(
        "--second-input", type=Path, default=ROOT / ".local/agent-runs/spoken-input.wav"
    )
    parser.add_argument("--expect", action="append")
    args = parser.parse_args()
    args.expect = args.expect or ["twelve", "12"]
    print(json.dumps(asyncio.run(check(args))))


if __name__ == "__main__":
    main()
