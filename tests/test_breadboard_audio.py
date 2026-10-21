import unittest
from array import array

from slate.breadboard import Breadboard, mix


def bench() -> Breadboard:
    board = object.__new__(Breadboard)
    board.realtime = True
    board.uart = bytearray()
    board.audio = bytearray()
    board.echo = bytearray()
    board.echo_gain = 0.0
    board.rate = 16000
    board.lead = 12800
    board.buffered = 0
    board.frames_drained = 0
    board.audio_bytes = 0
    board.now = 0
    board.played = []
    board.speaker_listeners = set()
    return board


class BreadboardAudioTests(unittest.TestCase):
    def test_mixing_clips_and_keeps_unmatched_samples(self):
        first = array("h", (30000, -30000, 7)).tobytes()
        second = array("h", (10000, -10000)).tobytes()
        self.assertEqual(array("h", mix(first, second)).tolist(), [32767, -32768, 7])

    def test_pacing_does_not_refill_lead_when_browser_chunks_pause(self):
        board = bench()
        board.audio = bytearray(12800)
        board.reply()
        board.feed(bytes(12800))
        board.now = 1_000_000
        board.reply()
        self.assertEqual(board.audio_bytes, 12864)
        board.feed_echo(array("h", [7, 7]).tobytes())
        board.now = 2_000_000
        self.assertIn(array("h", [7, 7]).tobytes(), board.reply())

    def test_speaker_output_reaches_the_mic_scaled_by_echo_gain(self):
        board = bench()
        board.capture_speaker(0, array("h", [1000, -1000, 32767, -32768]).tobytes())
        self.assertEqual(board.echo, b"")
        board.echo_gain = 0.5
        board.capture_speaker(0, array("h", [1000, -1000, 32767, -32768]).tobytes())
        self.assertEqual(array("h", board.echo).tolist(), [500, -500, 16384, -16384])
