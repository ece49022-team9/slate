import re


class Sentences:
    def __init__(self, limit: int = 240) -> None:
        self.limit = limit
        self.pending = ""

    def feed(self, text: str, *, final: bool = False) -> list[str]:
        self.pending += text
        chunks = []
        while self.pending.strip():
            boundary = next(
                (
                    match.end()
                    for match in re.finditer(r"[.!?](?:\s+|$)|\n", self.pending)
                    if 20 <= match.end() <= self.limit
                ),
                None,
            )
            if boundary is None and len(self.pending) > self.limit:
                boundary = self.pending.rfind(" ", 0, self.limit + 1)
                if boundary <= 0:
                    boundary = self.limit
            if boundary is None:
                if not final:
                    break
                boundary = len(self.pending)
            piece = self.pending[:boundary].strip()
            self.pending = self.pending[boundary:].lstrip()
            if piece:
                chunks.append(piece)
        return chunks
