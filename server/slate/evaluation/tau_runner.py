import asyncio
import contextlib
import hashlib
import json
import logging
import shutil
import socket
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import httpx

from slate.agent.agent import Agent
from slate.agent.runtime import SOURCE, command
from slate.board import ROOT

HELPERS = Path(__file__).parent
LOG = logging.getLogger("slate.eval.tau")


async def stop(process) -> None:
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            await asyncio.to_thread(process.wait, timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            await asyncio.to_thread(process.wait)


async def trial(args, arm, directory, profile_config, hermes_environment, wait_ready):
    for port in (args.fixture_port, args.port):
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", port))
    fixture = None
    gateway = None
    agent = None
    started = time.monotonic()
    turns = []
    fixture_url = f"http://127.0.0.1:{args.fixture_port}"
    frozen_helpers = directory.parent / "source"
    with contextlib.ExitStack() as stack:
        fixture_log = stack.enter_context((directory / "fixture.log").open("w"))
        event_log = stack.enter_context((directory / "native-events.jsonl").open("w"))
        try:
            fixture = subprocess.Popen(
                [
                    "uv",
                    "run",
                    "--frozen",
                    "--with",
                    "websockets==15.0.1",
                    "--no-sync",
                    "python",
                    str(frozen_helpers / "tau_fixture.py"),
                    "--task-id",
                    args.task_id,
                    "--seed",
                    str(args.seed),
                    "--user-model",
                    args.user_model,
                    "--max-steps",
                    str(args.max_steps),
                    "--port",
                    str(args.fixture_port),
                    "--output",
                    str(directory),
                ],
                cwd=ROOT / ".local/benchmarks/tau",
                stdout=fixture_log,
                stderr=subprocess.STDOUT,
            )
            async with httpx.AsyncClient(base_url=fixture_url, timeout=300) as boundary:
                await wait_ready(boundary, fixture)
                response = await boundary.post("/reset")
                response.raise_for_status()
                public = response.json()
                instructions = public["policy"]
                profile = directory / "profile"
                key = profile_config(
                    profile, "tau", args.model, "openai-codex", args.port
                )
                config = json.loads((profile / "config.yaml").read_text())
                config["platform_toolsets"]["api_server"] = ["mcp-tau"]
                config["agent"]["max_turns"] = args.max_steps
                config["terminal"] = {"cwd": str(directory)}
                config["mcp_servers"] = {
                    "tau": {
                        "command": shutil.which("uv"),
                        "args": [
                            "tool",
                            "run",
                            "--from",
                            "uv==0.12.22",
                            "uv",
                            "run",
                            "--project",
                            str(SOURCE),
                            "--no-sync",
                            "python",
                            str(frozen_helpers / "tau_mcp.py"),
                            str(directory / "public-fixture.json"),
                            "--url",
                            fixture_url,
                        ],
                    }
                }
                (profile / "config.yaml").write_text(json.dumps(config))
                env = hermes_environment(profile)
                env["TERMINAL_CWD"] = str(directory)
                gateway_log = stack.enter_context((directory / "gateway.log").open("w"))
                gateway = subprocess.Popen(
                    [*command(), "run", "--no-sync", "hermes", "gateway"],
                    cwd=SOURCE,
                    env=env,
                    stdout=gateway_log,
                    stderr=subprocess.STDOUT,
                )
                gateway_client = httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{args.port}",
                    headers={"Authorization": "Bearer " + key},
                    timeout=30,
                )
                await wait_ready(gateway_client, gateway)
                agent = Agent(
                    client=gateway_client,
                    model=args.model,
                    provider="openai-codex",
                    instructions=instructions,
                )
                observation = public["observation"]
                for turn in range(args.max_steps):
                    events = []

                    async def progress(event, captured=events):
                        captured.append(event)
                        event_log.write(json.dumps(event) + "\n")
                        event_log.flush()

                    output = await agent.run(observation, progress)
                    runtime = agent.last_run
                    turns.append(
                        {
                            "turn": turn,
                            "input": observation,
                            "output": output,
                            "events": events,
                            "runtime": runtime,
                        }
                    )
                    (directory / "reader-turns.json").write_text(
                        json.dumps(turns, indent=2)
                    )
                    response = await boundary.get("/status")
                    response.raise_for_status()
                    state = response.json()
                    if state["terminated"]:
                        break
                    if not output:
                        raise RuntimeError(
                            "No reader speech while official simulation remains active"
                        )
                    response = await boundary.post("/step", json={"message": output})
                    response.raise_for_status()
                    step = response.json()
                    if step["terminated"]:
                        break
                    observation = step["observation"]
                state = (await boundary.get("/status")).json()
                if not state["terminated"] or not state.get("simulation"):
                    raise RuntimeError(
                        "Trial did not reach an officially evaluated terminal state"
                    )
                return {
                    "reward": state["reward_info"]["reward"],
                    "reward_info": state["reward_info"],
                    "termination_reason": state["simulation"]["termination_reason"],
                    "tool_calls": sum(
                        "tool" in step["action"] for step in state["steps"]
                    ),
                    "turns": len(turns),
                    "seconds": time.monotonic() - started,
                    "policy_tools_sha256": hashlib.sha256(
                        json.dumps(
                            {"policy": public["policy"], "tools": public["tools"]},
                            sort_keys=True,
                        ).encode()
                    ).hexdigest(),
                    "initial_observation_sha256": hashlib.sha256(
                        public["observation"].encode()
                    ).hexdigest(),
                }
        finally:
            try:
                if agent is not None:
                    await agent.close()
                    await gateway_client.aclose()
            finally:
                try:
                    await stop(gateway)
                finally:
                    await stop(fixture)


async def tau_pilot(args, cfg, record, profile_config, hermes_environment, wait_ready):
    if args.max_steps < 1:
        raise ValueError("--max-steps must be positive")
    run = ROOT / ".local/agent-runs" / ("tau-airline-" + uuid4().hex)
    run.mkdir(parents=True, mode=0o700)
    source = run / "source"
    source.mkdir()
    for path in HELPERS.glob("*.py"):
        shutil.copy2(path, source / path.name)
    protocol = {
        "benchmark_revision": cfg["benchmarks"]["tau"]["revision"],
        "domain": "airline",
        "split": "base",
        "task_ids": [args.task_id],
        "seed": args.seed,
        "reader_model": args.model,
        "user_model": "openai/" + args.user_model,
        "user_args": {"seed": args.seed, "reasoning_effort": "low"},
        "max_steps": args.max_steps,
        "reader_turn_timeout_seconds": 300,
        "trial_timeout_seconds": 1800,
        "reader_route": "openai-codex",
        "arms": args.arms,
        "trials_per_arm": 1,
        "evaluation": "official AgentGymEnv ALL; custom reader and user models",
        "native_tool_transport": "MCP 2.0.0 stdio",
        "source_sha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in source.glob("*.py")
        },
    }
    (run / "protocol.json").write_text(json.dumps(protocol, indent=2))
    failures = []
    results = {}
    for arm in args.arms:
        directory = run / arm
        directory.mkdir()
        started = time.monotonic()
        fields = {
            "case": f"tau-airline-{arm}",
            "arm": arm,
            "task_ids": [args.task_id],
            "planned": 1,
            "benchmark_revision": protocol["benchmark_revision"],
            "protocol": protocol["evaluation"],
            "artifact": str(directory.relative_to(ROOT)),
            "frozen_evaluation_source_sha256": protocol["source_sha256"],
        }
        try:
            async with asyncio.timeout(1800):
                result = await trial(
                    args, arm, directory, profile_config, hermes_environment, wait_ready
                )
            (directory / "result.json").write_text(json.dumps(result, indent=2))
            results[arm] = result
            record(
                **fields,
                status="passed",
                scored=1,
                correct=int(result["reward"] == 1),
                metrics={
                    "success_rate": {"correct": int(result["reward"] == 1), "scored": 1}
                },
                **result,
            )
        except Exception as error:
            LOG.exception("Native tau trial failed: %s", arm)
            failures.append(arm)
            failure = {"error_type": type(error).__name__, "error": str(error)}
            (directory / "failure.json").write_text(json.dumps(failure, indent=2))
            record(
                **fields,
                status="failed",
                scored=0,
                seconds=time.monotonic() - started,
                **failure,
            )
    LOG.info("Native tau artifacts: %s", run)
    record(
        case="tau-agent-evaluation",
        status="failed" if failures else "passed",
        task_ids=[args.task_id],
        dataset_tasks=50,
        planned=len(args.arms),
        scored=len(results),
        failed=len(failures),
        metrics={
            arm: {"correct": int(result["reward"] == 1), "scored": 1}
            for arm, result in results.items()
        },
        protocol=protocol["evaluation"],
        benchmark_revision=protocol["benchmark_revision"],
        frozen_evaluation_source_sha256=protocol["source_sha256"],
        artifact=str(run.relative_to(ROOT)),
    )
    if failures:
        raise RuntimeError("Native tau trials failed: " + ", ".join(failures))
