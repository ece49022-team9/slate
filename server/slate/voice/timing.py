import socket
import time
from dataclasses import dataclass, field


@dataclass
class Timeline:
    marks: dict[str, int] = field(default_factory=dict)

    def mark(self, name: str, *, replace: bool = False) -> None:
        if replace or name not in self.marks:
            self.marks[name] = time.monotonic_ns()

    def snapshot(self) -> dict:
        return {"host": socket.gethostname(), "marks_ns": dict(self.marks)}


def milliseconds(marks: dict[str, int | None], start: str, end: str) -> float | None:
    begin, finish = marks.get(start), marks.get(end)
    if begin is None or finish is None:
        return None
    elapsed = finish - begin
    if elapsed < 0:
        raise ValueError(f"Timing order is invalid: {start} follows {end}")
    return elapsed / 1_000_000
