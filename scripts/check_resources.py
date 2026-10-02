import argparse
import asyncio
import re
import struct
import subprocess
import tomllib
from datetime import UTC, datetime

from slate.board import ROOT, load
from slate.breadboard import breadboard, build_image, tone
from slate.link import Link, open_board

CALIBRATION = ROOT / "firmware/sim_calibration.toml"
SERIAL_LOG = ROOT / ".local/resources-serial.log"
TIMERS = ("render", "spi", "audio")
STACKS = ("loop", "control", "oled", "pdm")
FRAME_US = 30_000
HEAP_MIN = 32_768
HEAP_BLOCK = 16_384
STACK_MIN = 512
BLOCK_SAMPLES = 320


def parse(line: str) -> dict:
    report = {}
    for key, value in re.findall(r"(\w+)=([\d/]+)", line.split("slate.perf:", 1)[1]):
        if "/" in value:
            average, peak = value.split("/")
            report[key] = (int(average), int(peak))
        else:
            report[key] = int(value)
    return report


def merge(reports: list[dict]) -> dict:
    merged = {}
    for timer in TIMERS:
        key = f"{timer}_us"
        merged[key] = (
            round(sum(report[key][0] for report in reports) / len(reports)),
            max(report[key][1] for report in reports),
        )
    for key in ("heap_free", "heap_min", "heap_block") + tuple(
        f"stack_{task}" for task in STACKS
    ):
        merged[key] = min(report[key] for report in reports)
    merged["heap_total"] = reports[-1]["heap_total"]
    return merged


async def measure(link: Link, key: str, reports: int) -> dict:
    for state in "123450":
        link.type(state)
        await link.wait_for(f"slate.state: {state}", seconds=3)
    link.type(f"{key}1a")
    await link.wait_for("slate.state: 1", seconds=3)
    await link.wait_for("slate.perf:", seconds=5)
    lines = [await link.wait_for("slate.perf:", seconds=5) for _ in range(reports)]
    link.type("x0")
    await link.wait_for("slate.state: 0", seconds=3)
    return merge([parse(line) for line in lines])


async def simulated(key: str, reports: int) -> dict:
    board, _ = load()
    rate = board["device"]["mic"]["sample_hz"]
    async with breadboard() as bench:
        samples = tone(440, 12000, 2.5 * reports + 4, rate)
        stereo = [0, 0] * len(samples)
        stereo[bench.slot :: 2] = samples
        bench.feed(struct.pack(f"<{len(stereo)}h", *stereo))
        return await measure(bench.link, key, reports)


async def hardware(port: str, key: str, reports: int) -> dict:
    link, task = await open_board(port, SERIAL_LOG)
    try:
        return await measure(link, key, reports)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def factors() -> dict[str, float] | None:
    if not CALIBRATION.exists():
        return None
    return tomllib.loads(CALIBRATION.read_text())["timing"]


def check(target: str, report: dict, scale: dict[str, float] | None) -> list[str]:
    board, _ = load()
    mic = board["device"]["mic"]
    audio_us = 1_000_000 * BLOCK_SAMPLES // mic["sample_hz"]
    scale = scale or dict.fromkeys(TIMERS, 1.0)
    peak = {timer: report[f"{timer}_us"][1] * scale[timer] for timer in TIMERS}
    failures = []
    frame = peak["render"] + peak["spi"]
    if frame > FRAME_US:
        failures.append(f"frame takes {frame:.0f} us of a {FRAME_US} us budget")
    if peak["audio"] > audio_us:
        failures.append(f"audio block takes {peak['audio']:.0f} us of {audio_us} us")
    if report["heap_min"] < HEAP_MIN:
        failures.append(f"heap fell to {report['heap_min']} bytes")
    if report["heap_block"] < HEAP_BLOCK:
        failures.append(f"largest free block is {report['heap_block']} bytes")
    for task in STACKS:
        if report[f"stack_{task}"] < STACK_MIN:
            failures.append(f"{task} stack has {report[f'stack_{task}']} bytes left")
    print(
        f"slate.resources: {target} frame peak {frame:.0f}/{FRAME_US} us, audio peak "
        f"{peak['audio']:.0f}/{audio_us} us, heap min {report['heap_min']} of "
        f"{report['heap_total']} bytes, largest block {report['heap_block']}, "
        "stack left "
        + ", ".join(f"{task}={report[f'stack_{task}']}" for task in STACKS)
    )
    return failures


def calibrate(real: dict, sim: dict) -> dict[str, float]:
    ratios = {}
    for timer in TIMERS:
        real_average, sim_average = real[f"{timer}_us"][0], sim[f"{timer}_us"][0]
        if not sim_average:
            raise RuntimeError(f"slate.resources: QEMU measured no {timer} time")
        ratios[timer] = round(real_average / sim_average, 3)
    revision = subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True
    ).strip()
    board, _ = load()
    CALIBRATION.write_text(
        f"# Measured {datetime.now(UTC).date()} on {board['mcu']} at {revision}: "
        "hardware average / QEMU average\n[timing]\n"
        + "".join(f"{timer} = {ratios[timer]}\n" for timer in TIMERS)
    )
    return ratios


async def run(args: argparse.Namespace) -> None:
    board, _ = load()
    key = "lr"[["left", "right"].index(board["device"]["mic"]["slot"])]
    failures = []
    if args.port:
        real = await hardware(args.port, key, args.reports)
        failures += check("hardware", real, None)
    if not args.port or args.calibrate:
        build_image()
        sim = await simulated(key, args.reports)
        if args.calibrate:
            print(f"slate.resources: QEMU timing factors {calibrate(real, sim)}")
        scale = factors()
        if scale is None:
            print(
                "slate.resources: QEMU timing is uncalibrated; run make calibrate-sim"
            )
        failures += check("qemu", sim, scale)
    if failures:
        raise SystemExit("slate.resources: " + "; ".join(failures))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port")
    parser.add_argument("--calibrate", action="store_true")
    parser.add_argument("--reports", type=int, default=3)
    args = parser.parse_args()
    if args.calibrate and not args.port:
        parser.error("--calibrate needs --port")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
