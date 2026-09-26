import io
import wave

SAMPLE_RATE = 24_000
SAMPLE_BYTES = 2
MAX_AUDIO_SECONDS = 120


def read_wav(data: bytes) -> bytes:
    with wave.open(io.BytesIO(data), "rb") as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (
            1,
            SAMPLE_BYTES,
            SAMPLE_RATE,
        ):
            raise ValueError("Audio must be mono, 24 kHz, 16-bit PCM WAV")
        frames = audio.getnframes()
        if not 0 < frames <= SAMPLE_RATE * MAX_AUDIO_SECONDS:
            raise ValueError(f"Audio must be nonempty and at most {MAX_AUDIO_SECONDS}s")
        pcm = audio.readframes(frames)
        if len(pcm) != frames * SAMPLE_BYTES:
            raise ValueError("WAV data is truncated")
        return pcm


def write_wav(pcm: bytes) -> bytes:
    if not pcm or len(pcm) % SAMPLE_BYTES:
        raise ValueError("Audio must contain complete 16-bit samples")
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(SAMPLE_BYTES)
        audio.setframerate(SAMPLE_RATE)
        audio.writeframes(pcm)
    return output.getvalue()
