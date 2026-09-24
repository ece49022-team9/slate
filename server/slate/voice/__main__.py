import argparse
import asyncio
import re
import time
from pathlib import Path

from slate.voice.audio import read_wav
from slate.voice.client import speak, transcribe
from slate.voice.device import simulate

SMOKE_TEXT = "Slate is ready. The voice system is working."


def save_audio(output: Path, audio: bytes) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(audio)
    print(f"slate.tts: wrote {output}")


async def run(args: argparse.Namespace) -> None:
    started = time.monotonic()
    if args.command == "stt":
        async for piece in transcribe(read_wav(args.input.read_bytes())):
            print(piece, end="", flush=True)
        print()
    elif args.command == "tts":
        save_audio(args.output, await speak(args.text, args.max_seconds))
    elif args.command == "simulate":
        print(await simulate(args.input, args.api_url, args.sample_rate))
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
        "simulate", help="Send a WAV through LiveKit as a device"
    )
    device.add_argument("input", type=Path)
    device.add_argument("--api-url", default="http://127.0.0.1:8000")
    device.add_argument(
        "--sample-rate", type=int, choices=[16000, 24000, 48000], default=16000
    )
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
