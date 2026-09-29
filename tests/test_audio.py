import io
import random
import unittest
import wave

from slate.voice.audio import MAX_AUDIO_SECONDS, SAMPLE_RATE, read_wav, write_wav


class AudioTests(unittest.TestCase):
    def test_preserves_samples(self):
        samples = random.Random(49022).randbytes(SAMPLE_RATE * 2)
        encoded = write_wav(samples)
        with wave.open(io.BytesIO(encoded), "rb") as audio:
            self.assertEqual(audio.getparams()[:4], (1, 2, SAMPLE_RATE, SAMPLE_RATE))
        self.assertEqual(read_wav(encoded), samples)

    def test_rejects_wrong_format(self):
        for channels, width, rate in [
            (2, 2, SAMPLE_RATE),
            (1, 1, SAMPLE_RATE),
            (1, 2, 16_000),
        ]:
            with self.subTest(channels=channels, width=width, rate=rate):
                output = io.BytesIO()
                with wave.open(output, "wb") as audio:
                    audio.setparams(
                        (channels, width, rate, 0, "NONE", "not compressed")
                    )
                    audio.writeframes(bytes(channels * width * rate))
                with self.assertRaisesRegex(ValueError, "mono, 24 kHz, 16-bit"):
                    read_wav(output.getvalue())

    def test_rejects_truncated_file(self):
        encoded = write_wav(bytes(200))
        with self.assertRaisesRegex(ValueError, "truncated"):
            read_wav(encoded[:-2])

    def test_enforces_duration_limit(self):
        samples = bytes(SAMPLE_RATE * MAX_AUDIO_SECONDS * 2)
        self.assertEqual(read_wav(write_wav(samples)), samples)
        with self.assertRaisesRegex(ValueError, "at most"):
            read_wav(write_wav(samples + b"\x00\x00"))

    def test_rejects_empty_or_partial_samples(self):
        for samples in [b"", b"\x00"]:
            with self.subTest(samples=samples):
                with self.assertRaisesRegex(ValueError, "complete 16-bit samples"):
                    write_wav(samples)


if __name__ == "__main__":
    unittest.main()
