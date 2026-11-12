import argparse
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PARTS = ROOT / "hardware/parts.toml"
BOARD = ROOT / "hardware/board.toml"
HEADER = ROOT / "firmware/main/board.h"
PDM_OVERSAMPLE = 64
RINGING_CM = 10


@dataclass(frozen=True)
class Problem:
    level: str
    device: str
    message: str

    def __str__(self) -> str:
        return f"slate.board: {self.level}: {self.device}: {self.message}"


def load(path: Path = BOARD) -> tuple[dict, dict]:
    return tomllib.loads(path.read_text()), tomllib.loads(PARTS.read_text())


def check(board: dict, parts: dict) -> list[Problem]:
    mcu = parts["mcu"].get(board["mcu"])
    if mcu is None:
        return [Problem("error", "board", f"unknown MCU {board['mcu']}")]
    problems: list[Problem] = []
    used: dict[int, str] = {}
    shared: dict[int, tuple] = {}
    for name, device in board["device"].items():

        def report(level: str, message: str, name: str = name) -> None:
            problems.append(Problem(level, name, message))

        part = parts["part"].get(device["part"])
        if part is None:
            report("error", f"unknown part {device['part']}")
            continue
        low, high = part["supply_v"]
        supply = board["supply_v"]
        if not low <= supply <= high:
            report("error", f"needs {low}-{high} V; board supplies {supply} V")
        if set(device["pins"]) != set(part["pins"]):
            report("error", f"pins must be {sorted(part['pins'])}")
        for pin, gpio in (device["pins"] | device.get("i2c", {})).items():
            where = f"{pin} on GPIO{gpio}"
            if gpio not in mcu["gpio"]:
                report("error", f"{where}: {mcu['name']} has no GPIO{gpio}")
                continue
            console = (
                name == "console"
                and {"usb_dm": 19, "usb_dp": 20, "uart_tx": 43, "uart_rx": 44}.get(pin)
                == gpio
            )
            if (reason := mcu["reserved"].get(str(gpio))) and not console:
                report("error", f"{where}: reserved for {reason}")
            if part["pins"].get(pin) != "out" and gpio in mcu["input_only"]:
                report("error", f"{where}: input-only pin cannot drive {pin}")
            if reason := mcu["strapping"].get(str(gpio)):
                report("warning", f"{where}: boot pin; {reason}")
            bus = device.get("bus")
            share = (bus, pin) if bus and pin in {"clk", "data", "sda", "scl"} else None
            if gpio in used and not (share and shared.get(gpio) == share):
                report("error", f"{where}: already used by {used[gpio]}")
            used[gpio] = f"{name}.{pin}"
            shared[gpio] = share
        if device.get("spi_hz", 0) > part.get("spi_max_hz", float("inf")):
            report(
                "warning",
                f"SPI at {device['spi_hz'] / 1e6:g} MHz is above the datasheet limit "
                f"of {part['spi_max_hz'] / 1e6:g} MHz; confirm on the bench",
            )
        if "sample_hz" in device:
            clock = device["sample_hz"] * PDM_OVERSAMPLE
            low, high = part["clock_hz"]
            if not low <= clock <= high:
                report(
                    "error",
                    f"PDM clock {clock / 1e6:g} MHz is outside the mic's "
                    f"{low / 1e6:g}-{high / 1e6:g} MHz range",
                )
        if device.get("wire_cm", 0) > RINGING_CM:
            report(
                "warning",
                f"{device['wire_cm']} cm wires are longer than {RINGING_CM} cm; "
                "fast clock edges may ring and double-clock the part",
            )
    return problems


def header(board: dict) -> str:
    lines = [
        "#pragma once",
        "// Generated from hardware/board.toml by make board",
        f'constexpr char BOARD_MCU[] = "{board["mcu"]}";',
    ]
    for name, device in board["device"].items():
        prefix = name.upper()
        for pin, gpio in (device["pins"] | device.get("i2c", {})).items():
            lines.append(f"constexpr int {prefix}_{pin.upper()} = {gpio};")
        for key, value in device.items():
            if key.endswith("_hz"):
                lines.append(f"constexpr unsigned {prefix}_{key.upper()} = {value};")
    enabled = int(board["device"]["power"].get("enabled", False))
    lines.extend(
        ["#ifndef POWER_ENABLED", f"#define POWER_ENABLED {enabled}", "#endif"]
    )
    return "\n".join(lines) + "\n"


def generated_header() -> str:
    final, _ = load()
    prototype, _ = load(BOARD.with_name("board-prototype.toml"))
    return (
        "#pragma once\n#ifdef SLATE_BOARD_S3\n"
        + header(final)
        + "#else\n"
        + header(prototype)
        + "#endif\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="slate.board")
    parser.add_argument(
        "--check", action="store_true", help="fail if board.h is out of date"
    )
    args = parser.parse_args()
    board, parts = load()
    prototype, _ = load(BOARD.with_name("board-prototype.toml"))
    problems = check(board, parts) + check(prototype, parts)
    for problem in problems:
        print(problem, file=sys.stderr)
    if any(problem.level == "error" for problem in problems):
        sys.exit("slate.board: fix the wiring errors above before building")
    text = generated_header()
    if args.check:
        if HEADER.read_text() != text:
            sys.exit("slate.board: firmware/main/board.h is stale; run make board")
    else:
        HEADER.write_text(text)
    print(f"slate.board: {board['mcu']} wiring ok, {len(problems)} warnings")


if __name__ == "__main__":
    main()
