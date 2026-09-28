import asyncio
import struct
import tempfile
import unittest
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st
from slate.link import Link

lines = st.text(st.characters(min_codepoint=32, max_codepoint=126), max_size=40).map(
    lambda text: ("line", text)
)
frames = st.lists(st.integers(-32768, 32767), min_size=1, max_size=320).map(
    lambda samples: ("audio", struct.pack(f"<{len(samples)}h", *samples))
)


def encode(item) -> bytes:
    kind, value = item
    if kind == "line":
        return value.encode() + b"\r\n"
    return bytes([0, len(value) & 0xFF, len(value) >> 8]) + value


async def parse(stream: bytes, cuts: list[int]) -> Link:
    reader = asyncio.StreamReader()
    bounds = sorted({0, len(stream), *(cut % (len(stream) + 1) for cut in cuts)})
    for start, end in zip(bounds, bounds[1:], strict=False):
        reader.feed_data(stream[start:end])
    reader.feed_eof()
    link = Link(reader, None)
    with tempfile.TemporaryDirectory() as directory:
        await link.run(Path(directory) / "serial.log")
    return link


class LinkTests(unittest.TestCase):
    @settings(max_examples=200)
    @given(st.lists(st.one_of(lines, frames), max_size=12), st.lists(st.integers(0)))
    def test_recovers_lines_and_audio_at_any_chunking(self, items, cuts):
        link = asyncio.run(parse(b"".join(map(encode, items)), cuts))
        audio = []
        while not link.audio.empty():
            audio.append(link.audio.get_nowait())
        self.assertEqual(link.lines, [value for kind, value in items if kind == "line"])
        self.assertEqual(audio, [value for kind, value in items if kind == "audio"])
