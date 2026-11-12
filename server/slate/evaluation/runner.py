import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import os
import random
import secrets
import shutil
import socket
import sqlite3
import subprocess
import tarfile
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import urlopen
from uuid import uuid4

import httpx

from slate.agent.agent import Agent
from slate.agent.runtime import PROFILE, SOURCE, command
from slate.board import ROOT
from slate.evaluation.tau_runner import tau_pilot

LOG = logging.getLogger("slate.eval")
CACHE = ROOT / ".local/benchmarks"
RAW = ROOT / ".local/agent-runs"
HELPERS = Path(__file__).parent
READER_INSTRUCTIONS = (
    "You answer questions about the user's past conversations. Check every past "
    "conversation you can reach when you need to. Dates in them are when those "
    "conversations happened; the question tells you today's date. Give an exact, "
    "complete answer. If the past conversations don't have the answer, say you "
    "can't tell."
)


def manifest() -> dict:
    return tomllib.loads((ROOT / "experiments/agent.toml").read_text())


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def record(**fields) -> None:
    fields = {
        "at": datetime.now(UTC).isoformat(),
        "scope": "benchmark",
        "seconds": 0.0,
        "git_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "harness_revision": manifest()["hermes"]["revision"],
        "uncommitted": bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=ROOT, text=True
            ).strip()
        ),
        "evaluation_source_sha256": {
            str(path.relative_to(ROOT)): digest(path)
            for path in sorted(HELPERS.glob("*.py"))
        },
        **fields,
    }
    payload = (json.dumps(fields, separators=(",", ":")) + "\n").encode()
    fd = os.open(ROOT / "experiments/progress.jsonl", os.O_WRONLY | os.O_APPEND)
    try:
        if os.write(fd, payload) != len(payload):
            raise RuntimeError("Incomplete experiment receipt write")
    finally:
        os.close(fd)
    LOG.info("%s", json.dumps(fields))


def download(url: str, path: Path, expected_sha256: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and digest(path) == expected_sha256:
        return
    temporary = path.with_suffix(path.suffix + ".part")
    with urlopen(url, timeout=120) as response, temporary.open("wb") as output:
        shutil.copyfileobj(response, output)
    if digest(temporary) != expected_sha256:
        raise RuntimeError(f"Pinned source checksum mismatch: {path.name}")
    temporary.replace(path)


def setup() -> None:
    cfg = manifest()["benchmarks"]
    for name in ("longmemeval", "tau"):
        benchmark = cfg[name]
        archive = CACHE / f"{name}.tar.gz"
        download(
            f"https://codeload.github.com/{benchmark['repository']}/tar.gz/"
            + benchmark["revision"],
            archive,
            benchmark["source_sha256"],
        )
        destination = CACHE / name
        marker = destination / ".slate-source-pin.json"
        pin = {"revision": benchmark["revision"], "sha256": benchmark["source_sha256"]}
        matches = False
        if destination.exists():
            with tarfile.open(archive) as source:
                matches = all(
                    (destination / Path(*Path(member.name).parts[1:])).is_file()
                    and hashlib.sha256(source.extractfile(member).read()).hexdigest()
                    == digest(destination / Path(*Path(member.name).parts[1:]))
                    for member in source.getmembers()
                    if member.isfile() and len(Path(member.name).parts) > 1
                )
        if not matches:
            shutil.rmtree(destination, ignore_errors=True)
            destination.mkdir()
            with tarfile.open(archive) as source:
                for member in source.getmembers():
                    parts = Path(member.name).parts
                    if len(parts) > 1:
                        member.name = str(Path(*parts[1:]))
                        source.extract(member, destination, filter="data")
        marker.write_text(json.dumps(pin))
    benchmark = cfg["longmemeval"]
    download(
        f"https://huggingface.co/datasets/{benchmark['dataset']}/resolve/"
        f"{benchmark['dataset_revision']}/{benchmark['file']}",
        CACHE / benchmark["file"],
        benchmark["file_sha256"],
    )
    subprocess.run(
        ["uv", "sync", "--frozen", "--python", "3.12", "--extra", "gym"],
        cwd=CACHE / "tau",
        check=True,
    )
    extras = [
        option
        for extra in cfg["tau"]["hermes_optional_extras"]
        for option in ("--extra", extra)
    ]
    subprocess.run(
        [*command(), "sync", "--frozen", "--python", "3.14", "--no-dev", *extras],
        cwd=SOURCE,
        check=True,
    )
    record(
        case="benchmark-setup",
        status="passed",
        dataset_sha256=benchmark["file_sha256"],
        questions=500,
    )


def verify_source_pins() -> None:
    for name in ("longmemeval", "tau"):
        benchmark = manifest()["benchmarks"][name]
        marker = CACHE / name / ".slate-source-pin.json"
        expected = {
            "revision": benchmark["revision"],
            "sha256": benchmark["source_sha256"],
        }
        if not marker.exists() or json.loads(marker.read_text()) != expected:
            raise RuntimeError(f"Source pin changed or missing for {name}; run setup")
        archive = CACHE / f"{name}.tar.gz"
        if not archive.exists() or digest(archive) != benchmark["source_sha256"]:
            raise RuntimeError(f"Pinned archive changed for {name}; run setup")
        with tarfile.open(archive) as source:
            for member in source.getmembers():
                if not member.isfile() or len(Path(member.name).parts) < 2:
                    continue
                destination = CACHE / name / Path(*Path(member.name).parts[1:])
                if (
                    not destination.is_file()
                    or digest(destination)
                    != hashlib.sha256(source.extractfile(member).read()).hexdigest()
                ):
                    raise RuntimeError(
                        f"Extracted upstream file changed: {destination}; run setup"
                    )


def hermes_environment(profile: Path, provider: str = "openai-codex") -> dict[str, str]:
    env = dict(os.environ, HERMES_HOME=str(profile), PYTHONPATH=str(SOURCE))
    for name in (
        "BROWSER_CDP_URL",
        "HERMES_MEMORY_PROVIDER",
        "HERMES_PROFILE",
    ):
        env.pop(name, None)
    if provider == "openai-codex":
        env.pop("OPENAI_API_KEY", None)
        env.pop("OPENROUTER_API_KEY", None)
    return env


def profile_config(
    profile: Path, arm: str, model: str, provider: str, port: int
) -> str:
    profile.mkdir(mode=0o700)
    shutil.copy2(PROFILE / "auth.json", profile / "auth.json")
    key = secrets.token_urlsafe(32)
    config = {
        "model": {"default": model, "provider": provider},
        "platform_toolsets": {
            "api_server": ["memory", "session_search"]
            if arm == "session-search"
            else []
        },
        "agent": {"max_turns": "unlimited"},
        "sessions": {"auto_prune": False, "auto_archive": False},
        "memory": {
            "memory_enabled": arm == "session-search",
            "user_profile_enabled": arm == "session-search",
        },
        "browser": {"backend": "off"},
        "auth": {"adopt_external_logins": False},
        "gateway": {
            "api_server": {
                "enabled": True,
                "host": "127.0.0.1",
                "port": port,
                "key": key,
            }
        },
    }
    (profile / "config.yaml").write_text(json.dumps(config, indent=2))
    return key


def history_only(row: dict) -> dict:
    return {
        "haystack_dates": row["haystack_dates"],
        "haystack_sessions": [
            [
                {"role": message["role"], "content": message["content"]}
                for message in session
            ]
            for session in row["haystack_sessions"]
        ],
    }


def full_context(row: dict) -> str:
    blocks = []
    for date, messages in zip(
        row["haystack_dates"], row["haystack_sessions"], strict=True
    ):
        blocks.append(
            f"Conversation date: {date}\n"
            + "\n".join(
                f"{message['role']}: {message['content']}" for message in messages
            )
        )
    return "Historical conversations:\n\n" + "\n\n".join(blocks) + "\n\n"


def verify_history(profile: Path, row: dict) -> dict:
    expected = []
    for index, (date, messages) in enumerate(
        zip(row["haystack_dates"], row["haystack_sessions"], strict=True)
    ):
        timestamp = (
            datetime.strptime(date, "%Y/%m/%d (%a) %H:%M")
            .replace(tzinfo=UTC)
            .timestamp()
        )
        expected.extend(
            (f"history_{index:04}", message["role"], message["content"], timestamp)
            for message in messages
        )
    with sqlite3.connect(f"file:{profile / 'state.db'}?mode=ro", uri=True) as db:
        actual = db.execute(
            "SELECT session_id, role, content, timestamp FROM messages "
            "WHERE session_id GLOB 'history_*' ORDER BY session_id, id"
        ).fetchall()
    if actual != expected:
        raise RuntimeError(
            f"Historical archive changed: expected {len(expected)} messages, "
            f"found {len(actual)}; histories must survive gateway startup unchanged"
        )
    return {
        "verified_messages": len(actual),
        "sha256": hashlib.sha256(
            json.dumps(actual, ensure_ascii=False).encode()
        ).hexdigest(),
    }


async def wait_ready(client: httpx.AsyncClient, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"Isolated Hermes gateway exited with {process.returncode}"
            )
        try:
            response = await client.get("/health")
            if response.status_code == 200:
                return
        except httpx.TransportError:
            pass
        await asyncio.sleep(0.5)
    raise RuntimeError("Isolated Hermes gateway did not become healthy")


async def evaluate_question(row: dict, arm: str, run: Path, args) -> dict:
    directory = run / f"{row['question_id']}-{arm}"
    directory.mkdir()
    profile = directory / "profile"
    key = profile_config(profile, arm, args.model, args.provider, args.port)
    env = hermes_environment(profile, args.provider)
    history = directory / "history.json"
    history.write_text(json.dumps(history_only(row)))
    if arm == "session-search":
        subprocess.run(
            [
                *command(),
                "run",
                "--no-sync",
                "python",
                str(HELPERS / "import_history.py"),
                str(history),
                str(directory / "ingestion.json"),
            ],
            cwd=SOURCE,
            env=env,
            check=True,
        )
    process = None
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", args.port))
    with (directory / "gateway.log").open("w") as gateway_log:
        try:
            process = subprocess.Popen(
                [*command(), "run", "--no-sync", "hermes", "gateway"],
                cwd=SOURCE,
                env=env,
                stdout=gateway_log,
                stderr=subprocess.STDOUT,
            )
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{args.port}",
                headers={"Authorization": "Bearer " + key},
                timeout=httpx.Timeout(30, read=30),
            ) as client:
                await wait_ready(client, process)
                archive_before = (
                    verify_history(profile, row) if arm == "session-search" else None
                )
                agent = Agent(
                    client=client,
                    model=args.model,
                    provider=args.provider,
                    instructions=READER_INSTRUCTIONS,
                )
                events = []

                async def progress(event: dict) -> None:
                    events.append(event)

                prefix = full_context(row) if arm == "full-context" else ""
                question = prefix + (
                    "Reference date: "
                    + row["question_date"]
                    + "\nQuestion: "
                    + row["question"]
                )
                started = time.monotonic()
                answer = await agent.run(question, progress)
                archive_after = (
                    verify_history(profile, row) if arm == "session-search" else None
                )
                receipt = {
                    "question_id": row["question_id"],
                    "question_type": row["question_type"],
                    "arm": arm,
                    "hypothesis": answer,
                    "query_seconds": time.monotonic() - started,
                    "runtime": agent.last_run["runtime"],
                    "run": agent.last_run,
                    "events": events,
                    "history_sessions": len(row["haystack_sessions"]),
                    "history_messages": sum(len(s) for s in row["haystack_sessions"]),
                    "archive_before": archive_before,
                    "archive_after": archive_after,
                }
                (directory / "answer.json").write_text(json.dumps(receipt, indent=2))
                return receipt
        finally:
            if process is not None:
                process.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    await asyncio.to_thread(process.wait, timeout=15)
                if process.poll() is None:
                    process.kill()
                    await asyncio.to_thread(process.wait)


async def pilot(args) -> None:
    cfg = manifest()
    benchmark = cfg["benchmarks"]["longmemeval"]
    dataset_path = CACHE / benchmark["file"]
    if digest(dataset_path) != benchmark["file_sha256"]:
        raise RuntimeError("LongMemEval dataset checksum mismatch; run setup")
    data = json.loads(dataset_path.read_text())
    ordered = sorted(data, key=lambda row: row["question_id"])
    selected = random.Random(args.seed).sample(ordered, args.limit)
    run = RAW / ("longmemeval-" + uuid4().hex)
    run.mkdir(parents=True, mode=0o700)
    frozen = {
        "benchmark": benchmark,
        "reader_model": args.model,
        "provider": args.provider,
        "reader_instructions": READER_INSTRUCTIONS,
        "seed": args.seed,
        "selection": "seeded sample of question-id-sorted full cleaned S dataset",
        "question_ids": [row["question_id"] for row in selected],
        "arms": args.arms,
        "judge_model": args.judge_model,
        "judge_protocol": "upstream-rubric-custom-sol",
        "judge_parameters": {"reasoning_effort": "low", "max_output_tokens": 512},
        "historical_date_interpretation": (
            "UTC for indexing; original date strings retained"
        ),
        "dataset_questions": len(data),
        "planned_answers": len(selected) * len(args.arms),
        "script_sha256": digest(Path(__file__)),
        "harness_revision": cfg["hermes"]["revision"],
        "git_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
    }
    (run / "protocol.json").write_text(json.dumps(frozen, indent=2))
    source = run / "source"
    source.mkdir()
    for file in HELPERS.glob("*.py"):
        shutil.copy2(file, source / file.name)
    hypotheses = run / "hypotheses.jsonl"
    completed = 0
    failures = 0
    with hypotheses.open("x") as output:
        for row in selected:
            for arm in args.arms:
                try:
                    receipt = await evaluate_question(row, arm, run, args)
                    output.write(
                        json.dumps(
                            {
                                name: receipt[name]
                                for name in (
                                    "question_id",
                                    "question_type",
                                    "arm",
                                    "hypothesis",
                                    "query_seconds",
                                )
                            }
                        )
                        + "\n"
                    )
                    output.flush()
                    completed += 1
                    record(
                        case="longmemeval-reader",
                        status="unscored",
                        arm=arm,
                        question_id=row["question_id"],
                        seconds=receipt["query_seconds"],
                        query_seconds=receipt["query_seconds"],
                        runtime=receipt["runtime"],
                        artifact=str(run.relative_to(ROOT)),
                    )
                except Exception as exc:
                    failures += 1
                    LOG.exception(
                        "LongMemEval reader failed: %s %s", row["question_id"], arm
                    )
                    record(
                        case="longmemeval-reader",
                        status="failed",
                        arm=arm,
                        question_id=row["question_id"],
                        error_type=type(exc).__name__,
                        artifact=str(run.relative_to(ROOT)),
                    )
    record(
        case="longmemeval-pilot",
        status="unscored",
        completed=completed,
        failed=failures,
        planned=frozen["planned_answers"],
        dataset_questions=len(data),
        artifact=str(run.relative_to(ROOT)),
    )
    print(f"slate.eval: hypotheses saved to {hypotheses}")
    if args.grade and completed:
        grade(run, args.judge_model)
    if failures:
        raise RuntimeError(
            f"Pilot has {failures} failed answers; see the saved receipts"
        )


def grade(run: Path, model: str) -> None:
    benchmark = manifest()["benchmarks"]["longmemeval"]
    rubric = CACHE / "longmemeval/src/evaluation/evaluate_qa.py"
    if digest(rubric) != benchmark["rubric_sha256"]:
        raise RuntimeError("Pinned upstream grading rubric checksum mismatch")
    if digest(CACHE / benchmark["file"]) != benchmark["file_sha256"]:
        raise RuntimeError("Pinned grading references checksum mismatch")
    protocol = json.loads((run / "protocol.json").read_text())
    if model != protocol["judge_model"]:
        raise ValueError("Judge model must match the frozen pilot protocol")
    env = dict(os.environ, PYTHONPATH=str(CACHE / "longmemeval/src/evaluation"))
    output = run / "grades.jsonl"
    try:
        subprocess.run(
            [
                "uv",
                "run",
                "--with",
                "tqdm",
                "--with",
                "backoff",
                "--with",
                "numpy",
                "python",
                str(HELPERS / "grade.py"),
                str(CACHE / benchmark["file"]),
                str(run / "hypotheses.jsonl"),
                str(output),
                "--model",
                model,
            ],
            cwd=ROOT,
            env=env,
            check=True,
        )
    except subprocess.CalledProcessError:
        record(
            case="longmemeval-grading",
            status="failed",
            judge_model=model,
            artifact=str(run.relative_to(ROOT)),
        )
        raise
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    for arm in sorted({row["arm"] for row in rows}):
        subset = [row for row in rows if row["arm"] == arm]
        groups = {
            kind: [row for row in subset if row["question_type"] == kind]
            for kind in sorted({row["question_type"] for row in subset})
        }
        abstentions = [row for row in subset if "_abs" in row["question_id"]]
        record(
            case="longmemeval-custom-judge",
            status="passed",
            arm=arm,
            correct=sum(row["correct"] for row in subset),
            scored=len(subset),
            planned=len(protocol["question_ids"]),
            metrics={
                "accuracy_by_question_type": {
                    kind: {
                        "correct": sum(row["correct"] for row in group),
                        "scored": len(group),
                    }
                    for kind, group in groups.items()
                },
                "abstention_accuracy": {
                    "correct": sum(row["correct"] for row in abstentions),
                    "scored": len(abstentions),
                },
                "query_latency_seconds": [row["query_seconds"] for row in subset],
            },
            dataset_questions=500,
            judge_model=model,
            protocol="pinned-upstream-rubric-custom-sol-responses-judge",
            artifact=str(run.relative_to(ROOT)),
        )


def audit_tau() -> None:
    run = RAW / ("tau-airline-audit-" + uuid4().hex)
    run.mkdir(parents=True, mode=0o700)
    output = run / "audit.json"
    started = time.monotonic()
    subprocess.run(
        [
            "uv",
            "run",
            "--frozen",
            "--with",
            "websockets==15.0.1",
            "--no-sync",
            "python",
            str(HELPERS / "audit_tau.py"),
            str(output),
        ],
        cwd=CACHE / "tau",
        check=True,
    )
    receipt = json.loads(output.read_text())
    record(
        case="tau-airline-reference-audit",
        status="passed" if receipt["passed"] == receipt["tasks"] else "failed",
        seconds=time.monotonic() - started,
        passed=receipt["passed"],
        audited=receipt["tasks"],
        protocol="reference-audit-only-no-agent-score",
        benchmark_revision=manifest()["benchmarks"]["tau"]["revision"],
        artifact=str(run.relative_to(ROOT)),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup")
    commands.add_parser("tau-audit")
    tau = commands.add_parser("tau")
    tau.add_argument("--task-id", default="18")
    tau.add_argument("--seed", type=int, default=20261002)
    tau.add_argument("--max-steps", type=int, default=100)
    tau.add_argument("--model", default=manifest()["hermes"]["model"])
    tau.add_argument("--user-model", default="gpt-6.1-sol")
    tau.add_argument(
        "--arms",
        nargs="+",
        choices=["hermes", "managed"],
        default=["hermes", "managed"],
    )
    tau.add_argument("--port", type=int, default=8643)
    tau.add_argument("--fixture-port", type=int, default=8644)
    run = commands.add_parser("longmemeval")
    run.add_argument("--limit", type=int, default=1)
    run.add_argument("--seed", type=int, default=20261002)
    run.add_argument(
        "--arms",
        nargs="+",
        choices=["session-search", "full-context", "no-memory"],
        default=["session-search", "full-context", "no-memory"],
    )
    run.add_argument("--model", default=manifest()["hermes"]["model"])
    run.add_argument("--provider", default=manifest()["hermes"]["provider"])
    run.add_argument("--port", type=int, default=8643)
    run.add_argument("--judge-model", default="gpt-6.1-sol")
    run.add_argument("--grade", action="store_true")
    judge = commands.add_parser("grade")
    judge.add_argument("run", type=Path)
    judge.add_argument("--judge-model", default="gpt-6.1-sol")
    args = parser.parse_args()
    if args.command != "setup":
        verify_source_pins()
    if args.command == "setup":
        setup()
    elif args.command == "tau-audit":
        audit_tau()
    elif args.command == "tau":
        asyncio.run(
            tau_pilot(
                args, manifest(), record, profile_config, hermes_environment, wait_ready
            )
        )
    elif args.command == "grade":
        grade(args.run.resolve(), args.judge_model)
    else:
        if args.limit < 1 or args.limit > 500:
            parser.error("--limit must be between 1 and 500")
        asyncio.run(pilot(args))
