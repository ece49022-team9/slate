import argparse
import asyncio
import re
import time
from pathlib import Path

from slate.voice.audio import read_wav
from slate.voice.client import speak, transcribe
from slate.voice.device import VoiceResult, simulate
from slate.voice.firmware import FirmwareTurn, board_firmware, simulate_firmware

SMOKE_TEXT = "Slate is ready. The voice system is working."


def save_audio(output: Path, audio: bytes) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(audio)
    print(f"slate.tts: wrote {output}")


def show_turn(result: VoiceResult, output: Path) -> None:
    print(f"slate.stt: {result.transcript}")
    print(f"slate.agent: {result.reply}")
    if result.audio:
        save_audio(output, result.audio)


def show_firmware_turn(result: FirmwareTurn) -> None:
    print(f"slate.stt: {result.transcript}")
    print(f"slate.agent: {result.reply}")
    print(f"slate.voice: firmware received {result.reply_audio_bytes} bytes of speech")


async def run(args: argparse.Namespace) -> None:
    started = time.monotonic()
    if args.command == "stt":
        async for piece in transcribe(read_wav(args.input.read_bytes())):
            print(piece, end="", flush=True)
        print()
    elif args.command == "tts":
        save_audio(args.output, await speak(args.text, args.max_seconds))
    elif args.command == "simulate":
        show_turn(await simulate(args.input), args.output)
    elif args.command == "firmware":
        show_firmware_turn(
            await board_firmware(args.input, args.channel, args.port)
            if args.port
            else await simulate_firmware(args.input, args.channel)
        )
    else:
        audio = await speak(SMOKE_TEXT)
        save_audio(args.output, audio)
        transcript = "".join(
            [piece async for piece in transcribe(read_wav(audio))]
        ).strip()
        print(f"slate.stt: {transcript}")
        words = set(re.findall(r"[a-z]+", transcript.lower()))
        if not {"ready", "voice", "working"} <= words:
            raise RuntimeError(
                "slate.voice: speech round trip did not match the test text"
            )
        print("slate.voice: speech round trip passed")
    print(f"slate.voice: finished in {time.monotonic() - started:.1f}s")


def main() -> None:
    parser = argparse.ArgumentParser(prog="slate.voice")
    commands = parser.add_subparsers(dest="command", required=True)
    stt = commands.add_parser("stt", help="Transcribe a mono 24 kHz PCM WAV")
    stt.add_argument("input", type=Path)
    tts = commands.add_parser("tts", help="Generate a spoken WAV")
    tts.add_argument("text")
    tts.add_argument("--output", type=Path, default=Path(".local/speech.wav"))
    tts.add_argument("--max-seconds", type=int, default=15)
    check = commands.add_parser("check", help="Generate speech and transcribe it")
    check.add_argument("--output", type=Path, default=Path(".local/voice-check.wav"))
    device = commands.add_parser(
        "simulate", help="Send a 24 kHz WAV to the cloud as a device would"
    )
    device.add_argument("input", type=Path)
    device.add_argument("--output", type=Path, default=Path(".local/reply.wav"))
    firmware = commands.add_parser(
        "firmware", help="Speak a WAV into the simulated or real board's mic"
    )
    firmware.add_argument("input", type=Path)
    firmware.add_argument("--channel", choices=["left", "right", "mix"], default="left")
    firmware.add_argument(
        "--port", help="Use the board on this serial port and play the WAV aloud"
    )
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
