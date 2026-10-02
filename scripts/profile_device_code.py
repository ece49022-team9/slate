import argparse
import asyncio
import hashlib
import json
import os
import re
import signal
import socket
import statistics
import subprocess
import sys
import time
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
from check_device_code import device_http, workers_runtime
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from slate.agent.agent import Agent
from slate.agent.code_mode import CloudflareExecutor, DeviceClient, MontyExecutor
from slate.board import ROOT
from slate.breadboard import breadboard, build_image
from slate.device import DeviceSDK, FirmwareDevice, agent_context

MODES = ("tools", "cloudflare", "monty")
HERMES_URL = "http://127.0.0.1:8642"
AGENT_LOG = ROOT / ".local/hermes-home/logs/agent.log"
COLORS = ("#0000ff", "#ff8800", "#00ff00", "#ff00ff", "#00ffff", "#ffff00")
SOURCES = (
    "server/slate/device.py",
    "server/slate/agent/code_mode.py",
    "server/slate/agent/device_mcp.py",
    "server/slate/agent/runtime.py",
    "firmware/main/main.cpp",
    "scripts/profile_device_code.py",
)


def task(index: int) -> SimpleNamespace:
    return SimpleNamespace(
        color=COLORS[index % len(COLORS)], radius=12 + index % 30, label=f"T{index}"
    )


def program(mode: str, work: SimpleNamespace) -> str:
    if mode == "monty":
        return (
            f"orb = await device.set_orb('{work.color}', {work.radius})\n"
            f"text = await device.show_text('{work.label}')\n"
            "status = await device.get_status()\n[orb, text, status]"
        )
    return (
        f"async () => {{ const orb = await device.set_orb('{work.color}', "
        f"{work.radius}); const text = await device.show_text('{work.label}'); "
        "return [orb, text, await device.get_status()]; }"
    )


def matches(status: dict, work: SimpleNamespace) -> bool:
    return (status["color"].lower(), status["radius"], status["text"]) == (
        work.color,
        work.radius,
        work.label,
    )


def code_status(result: dict) -> dict:
    if result.get("status") != "completed":
        raise RuntimeError(f"slate.profile: code execution failed: {result}")
    return result["result"][2]


async def timed(work) -> tuple[float, object]:
    started = time.perf_counter_ns()
    result = await work
    return (time.perf_counter_ns() - started) / 1_000_000, result


def quantiles(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    cuts = statistics.quantiles(ordered, n=10) if len(ordered) > 1 else ordered * 9
    return {
        "n": len(ordered),
        "median_ms": round(statistics.median(ordered), 2),
        "p10_ms": round(cuts[0], 2),
        "p90_ms": round(cuts[-1], 2),
    }


@asynccontextmanager
async def device_fixture():
    voice = SimpleNamespace(current=None)
    async with breadboard() as bench, device_http(voice) as url:
        turn = SimpleNamespace(scope=uuid4().hex, id=uuid4().hex)
        peer = FirmwareDevice(bench.link, lambda: turn.id)
        commands = []

        async def execute(command):
            commands.append(command)
            return (await peer.execute(command)).model_dump()

        session = SimpleNamespace(turn=turn)
        sdk = DeviceSDK(turn.scope, turn.id, execute, lambda: voice.current is session)
        session.device_sdk = lambda current: sdk
        voice.current = session
        async with (
            workers_runtime(url) as workers,
            httpx.AsyncClient(base_url=url) as client,
        ):
            yield SimpleNamespace(
                url=url,
                workers=workers,
                workers_url=str(workers.base_url),
                scope=turn.scope,
                device=DeviceClient(client),
                commands=commands,
            )


def mcp_environment(mode: str, fixture) -> dict[str, str]:
    return {
        **os.environ,
        "SLATE_DEVICE_MODE": mode,
        "SLATE_DEVICE_URL": fixture.url,
        "SLATE_CLOUDFLARE_CODE_URL": fixture.workers_url,
    }


def structured(result) -> dict:
    if result.is_error:
        raise RuntimeError(f"slate.profile: MCP tool failed: {result.content}")
    content = result.structured_content
    if "status" not in content and isinstance(content.get("result"), dict):
        return content["result"]
    return content


async def run_exec(args, fixture) -> list[dict]:
    scope = fixture.scope
    device = fixture.device
    async with AsyncExitStack() as stack:
        monty = MontyExecutor(device)
        stack.push_async_callback(monty.close)
        cloudflare = CloudflareExecutor(device, fixture.workers)
        sessions = {}
        for mode in MODES:
            read, write = await stack.enter_async_context(
                stdio_client(
                    StdioServerParameters(
                        command=sys.executable,
                        args=["-m", "slate.agent.device_mcp"],
                        env=mcp_environment(mode, fixture),
                        cwd=ROOT,
                    )
                )
            )
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            sessions[mode] = session

        async def direct(work):
            await device.set_orb(scope, work.color, work.radius)
            await device.show_text(scope, work.label)
            return await device.get_status(scope)

        async def mcp_tools(work):
            tools = sessions["tools"]
            arguments = {"scope": scope}
            structured(
                await tools.call_tool(
                    "device_set_orb",
                    {**arguments, "color": work.color, "radius": work.radius},
                )
            )
            structured(
                await tools.call_tool(
                    "device_show_text", {**arguments, "text": work.label}
                )
            )
            return structured(await tools.call_tool("device_get_status", arguments))

        def mcp_code(mode):
            async def run(work):
                result = await sessions[mode].call_tool(
                    "execute_device_code",
                    {"scope": scope, "code": program(mode, work)},
                )
                return code_status(structured(result))

            return run

        async def in_process(executor, mode):
            async def run(work):
                return code_status(await executor.execute(scope, program(mode, work)))

            return run

        arms = {
            "exec/tools": direct,
            "exec/monty": await in_process(monty, "monty"),
            "exec/cloudflare": await in_process(cloudflare, "cloudflare"),
            "mcp/tools": mcp_tools,
            "mcp/monty": mcp_code("monty"),
            "mcp/cloudflare": mcp_code("cloudflare"),
        }
        names = list(arms)
        rows = []
        index = 0
        for round_index in range(args.warmup + args.rounds):
            offset = round_index % len(names)
            for name in names[offset:] + names[:offset]:
                work = task(index)
                index += 1
                before = len(fixture.commands)
                elapsed, status = await timed(arms[name](work))
                rows.append(
                    {
                        "arm": name,
                        "round": round_index,
                        "warmup": round_index < args.warmup,
                        "ms": elapsed,
                        "correct": matches(status, work),
                        "firmware_commands": len(fixture.commands) - before,
                    }
                )
        for trial in range(args.cold):
            work = task(index)
            index += 1
            async with AsyncExitStack() as cold_stack:
                fresh = MontyExecutor(device)
                cold_stack.push_async_callback(fresh.close)
                elapsed, result = await timed(
                    fresh.execute(scope, program("monty", work))
                )
            rows.append(
                {
                    "arm": "exec/monty-cold",
                    "round": trial,
                    "warmup": False,
                    "ms": elapsed,
                    "correct": matches(code_status(result), work),
                }
            )
        return rows


def port_open(port: int) -> bool:
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) == 0


@asynccontextmanager
async def hermes(mode: str, fixture, log_path):
    if port_open(8642):
        raise RuntimeError(
            "slate.profile: stop the running Hermes gateway before the agent layer"
        )
    with log_path.open("ab") as log:
        process = await asyncio.create_subprocess_exec(
            "uv",
            "run",
            "python",
            "-m",
            "slate.agent.runtime",
            "start",
            cwd=ROOT,
            env=mcp_environment(mode, fixture),
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    try:
        async with httpx.AsyncClient(base_url=HERMES_URL) as client:
            async with asyncio.timeout(120):
                while True:
                    if process.returncode is not None:
                        raise RuntimeError(
                            f"slate.profile: Hermes exited during startup ({mode})"
                        )
                    try:
                        if (await client.get("/health")).status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(0.5)
        yield
    finally:
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 30)
            except TimeoutError:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
        async with asyncio.timeout(30):
            while port_open(8642):
                await asyncio.sleep(0.2)


def hermes_trace(session_id: str) -> dict:
    marker = f"[{session_id}]"
    calls, tools, ended = [], [], None
    for line in AGENT_LOG.read_text(errors="replace").splitlines():
        if marker not in line:
            continue
        if match := re.search(r"API call #\d+: .* latency=([\d.]+)s", line):
            calls.append(float(match[1]))
        elif match := re.search(r"tool (\S+) completed \(([\d.]+)s", line):
            tools.append({"name": match[1], "seconds": float(match[2])})
        elif match := re.search(r"api_calls=(\d+)/\d+ .* tool_turns=(\d+)", line):
            ended = {"api_calls": int(match[1]), "tool_turns": int(match[2])}
    return {"model_call_seconds": calls, "tool_calls": tools, "turn": ended}


async def agent_turn(fixture, work) -> dict:
    prompt = (
        f"Set the orb to {work.color} with radius {work.radius}, show "
        f"{work.label} on the screen, then check the device status and tell me "
        "the result."
    )
    agent = Agent()
    before = len(fixture.commands)
    try:
        elapsed, reply = await timed(
            agent.run(prompt, device_context=agent_context(fixture.scope))
        )
        run = agent.last_run
        timings = agent.timings
    finally:
        await agent.close()
    commands = len(fixture.commands) - before
    status = await fixture.device.get_status(fixture.scope)
    await asyncio.sleep(0.5)
    first_text = timings.get("first_text")
    return {
        "ms": elapsed,
        "reply": reply,
        "correct": matches(status, work),
        "firmware_commands": commands,
        "ttft_ms": (first_text - timings["requested"]) / 1_000_000
        if first_text
        else None,
        "usage": run.get("usage"),
        **hermes_trace(run["session_id"]),
    }


async def run_agent(args, fixture, directory) -> list[dict]:
    rows = []
    index = 10_000
    for block, mode in enumerate(MODES + MODES[::-1]):
        async with hermes(mode, fixture, directory / "hermes.log"):
            for trial in range(-1, args.turns):
                work = task(index)
                index += 1
                try:
                    row = await agent_turn(fixture, work)
                except Exception as error:
                    row = {"error": f"{type(error).__name__}: {error}"}
                print(
                    f"slate.profile: agent/{mode} block={block} trial={trial} "
                    f"ms={row.get('ms', 0):.0f} "
                    f"calls={(row.get('turn') or {}).get('api_calls')} "
                    f"correct={row.get('correct')}",
                    flush=True,
                )
                rows.append(
                    {
                        "arm": f"agent/{mode}",
                        "block": block,
                        "round": trial,
                        "warmup": trial < 0,
                        **row,
                    }
                )
    return rows


def summarize(rows: list[dict]) -> dict:
    summary = {}
    for arm in dict.fromkeys(row["arm"] for row in rows):
        group = [row for row in rows if row["arm"] == arm and not row["warmup"]]
        timed_rows = [row for row in group if "ms" in row]
        entry = {
            **quantiles([row["ms"] for row in timed_rows]),
            "correct": sum(bool(row.get("correct")) for row in group),
            "errors": sum("error" in row for row in group),
        }
        if arm.startswith("agent/"):
            turns = [row["turn"] for row in timed_rows if row.get("turn")]
            entry["median_model_calls"] = (
                statistics.median(turn["api_calls"] for turn in turns)
                if turns
                else None
            )
            entry["median_ttft_ms"] = (
                statistics.median(
                    row["ttft_ms"] for row in timed_rows if row.get("ttft_ms")
                )
                if any(row.get("ttft_ms") for row in timed_rows)
                else None
            )
            entry["median_model_seconds"] = (
                statistics.median(sum(row["model_call_seconds"]) for row in timed_rows)
                if timed_rows
                else None
            )
            entry["median_tool_seconds"] = (
                statistics.median(
                    sum(tool["seconds"] for tool in row["tool_calls"])
                    for row in timed_rows
                )
                if timed_rows
                else None
            )
            entry["median_input_tokens"] = (
                statistics.median(
                    row["usage"]["input_tokens"]
                    for row in timed_rows
                    if row.get("usage")
                )
                if any(row.get("usage") for row in timed_rows)
                else None
            )
        summary[arm] = entry
    return summary


async def run(args) -> None:
    profile_id = uuid4().hex
    directory = ROOT / ".local/agent-runs" / profile_id
    directory.mkdir(parents=True)
    rows = []
    async with device_fixture() as fixture:
        if args.layer in ("exec", "all"):
            rows += await run_exec(args, fixture)
        if args.layer in ("agent", "all"):
            rows += await run_agent(args, fixture, directory)
    summary = summarize(rows)
    (directory / "device-code-profile.json").write_text(
        json.dumps({"rows": rows, "summary": summary}, indent=2)
    )
    record = {
        "at": datetime.now(UTC).isoformat(),
        "scope": "latency",
        "case": f"device-code-profile-{args.layer}",
        "status": "passed"
        if all(row.get("correct") for row in rows if not row["warmup"])
        else "failed",
        "profile_id": profile_id,
        "git_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "uncommitted": bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=ROOT, text=True
            ).strip()
        ),
        "source_sha256": {
            path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
            for path in SOURCES
        },
        "artifact": str((directory / "device-code-profile.json").relative_to(ROOT)),
        "definition": (
            "One composed device task: set_orb, show_text, get_status against QEMU "
            "firmware over the fixture HTTP device route. exec/* times the runtime "
            "call in-process; mcp/* adds the stdio MCP boundary Hermes uses; "
            "agent/* is a full Hermes turn with a fresh session. Code-mode arms "
            "include their scope status preflight. QEMU runs unpaced, so firmware "
            "round trips are emulator time, not LiveKit RPC or physical hardware."
        ),
        "summary": summary,
    }
    with (ROOT / "experiments/progress.jsonl").open("a") as log:
        log.write(json.dumps(record) + "\n")
    print(json.dumps(summary, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--layer", choices=("exec", "agent", "all"), default="exec")
    parser.add_argument("--rounds", type=int, default=30)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--cold", type=int, default=5)
    parser.add_argument("--turns", type=int, default=3)
    args = parser.parse_args()
    build_image()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
