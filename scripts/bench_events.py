"""End-to-end latency and backpressure benchmark for /api/ws/events.

The real EventBus and WebSocket route run under uvicorn in a child process.
Real WebSocket clients run in other processes. Everything is on 127.0.0.1, so
this measures our server, not the network.

    uv run python scripts/bench_events.py           # full run, about 4 min
    uv run python scripts/bench_events.py --quick   # about 1 min

Latency is measured from when an event was *scheduled* to be published to
when a client parsed it. Measuring from the scheduled time (not from when the
publisher got around to it) keeps a stalled event loop from hiding its own
delay ("coordinated omission"). time.monotonic_ns is one clock shared by all
processes on Linux and macOS, so the two ends can be compared directly.

Results go to docs/bench/events.json, and docs/bench/events.png when
matplotlib is installed.
"""

import argparse
import asyncio
import json
import multiprocessing as mp
import os
import platform
import queue
import socket
import statistics
import subprocess
import sys
import time
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "bench"


# Server (child process)


def serve(port: int, size: int, queue_size: int, deflate: bool) -> None:
    import uvicorn
    from fastapi import FastAPI, Request
    from pydantic import BaseModel
    from slate.api.events import EventBus
    from slate.api.events import router as events_router

    class Load(BaseModel):
        rate: float
        duration: float
        pad: int = 0

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.events = EventBus(size=size, queue_size=queue_size)
        app.state.lag = []
        yield

    app = FastAPI(lifespan=lifespan)
    app.include_router(events_router, prefix="/api")

    async def generate(app: FastAPI, load: Load) -> None:
        bus: EventBus = app.state.events
        total = int(load.rate * load.duration)
        period = int(1e9 / load.rate)
        start = time.monotonic_ns() + 100_000_000
        for seq in range(total):
            due = start + seq * period
            wait = due - time.monotonic_ns()
            # Always yield, even when behind, so sends keep running.
            await asyncio.sleep(max(0, wait) / 1e9)
            data = {"seq": seq, "total": total, "due_ns": due}
            if load.pad:
                # Fresh random text per event, so compression cannot shrink it.
                data["pad"] = os.urandom(load.pad // 2).hex()
            bus.publish("bench.tick", data)
            app.state.lag.append(time.monotonic_ns() - due)

    @app.post("/bench/load")
    async def start_load(load: Load, request: Request) -> dict:
        request.app.state.lag = []
        request.app.state.task = asyncio.create_task(generate(request.app, load))
        return {"events": int(load.rate * load.duration)}

    @app.get("/bench/lag")
    async def publisher_lag(request: Request) -> dict:
        lag = sorted(request.app.state.lag)
        return {"count": len(lag), **summary(lag)}

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        ws_per_message_deflate=deflate,
    )


class Server:
    def __init__(self, size=500, queue_size=100, deflate=True) -> None:
        self.port = free_port()
        self.base = f"127.0.0.1:{self.port}"
        args = [str(self.port), str(size), str(queue_size), str(int(deflate))]
        self.process = subprocess.Popen(
            [sys.executable, __file__, "serve", *args], env=os.environ.copy()
        )
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                self.get("/api/events/stats")
                return
            except OSError:
                time.sleep(0.1)
        self.process.kill()
        raise RuntimeError("bench server did not start")

    def get(self, path: str) -> dict:
        with urllib.request.urlopen(f"http://{self.base}{path}", timeout=5) as reply:
            return json.load(reply)

    def post(self, path: str, body: dict) -> dict:
        request = urllib.request.Request(
            f"http://{self.base}{path}",
            data=json.dumps(body).encode(),
            headers={"content-type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as reply:
            return json.load(reply)

    def close(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(5)
        except subprocess.TimeoutExpired:
            self.process.kill()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# Clients (worker processes)


async def client(base: str, spec: dict, stop_ns: int) -> dict:
    """One web app. kinds:
    fast     reads as fast as it can
    slow     sleeps read_delay after every event
    stalled  connects and never reads
    flaky    drops its connection every drop_every s, waits reconnect_delay,
             then resumes from its cursor like the real web app
    """
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed

    kind = spec["kind"]
    latency: list[int] = []
    result = {
        "kind": kind,
        "received": 0,
        "gaps": 0,
        "duplicates": 0,
        "resyncs": 0,
        "reconnects": 0,
        "closes": {},
        "closed_after_s": None,
    }
    last_id = None
    last_seq = -1  # nothing seen yet, so the first event must be seq 0
    total = None
    opened = time.monotonic_ns()

    def finished() -> bool:
        return total is not None and last_seq == total - 1

    while time.monotonic_ns() < stop_ns and not finished():
        url = f"ws://{base}/api/ws/events"
        if last_id:
            url += f"?after={last_id}"
        drop_at = None
        if kind == "flaky":
            drop_at = time.monotonic_ns() + int(spec["drop_every"] * 1e9)
        try:
            # Fast apps buffer without limit so they measure the server, not
            # themselves. Slow and flaky apps keep websockets' default of 16
            # frames, so when they lag the TCP window closes and the server
            # feels it. A stalled app stops reading after one frame.
            max_queue = {"fast": None, "stalled": 1}.get(kind, 16)
            async with connect(url, max_queue=max_queue, open_timeout=5) as ws:
                if kind == "stalled":
                    remaining = (stop_ns - time.monotonic_ns()) / 1e9
                    try:
                        await asyncio.wait_for(ws.wait_closed(), remaining)
                        result["closed_after_s"] = (time.monotonic_ns() - opened) / 1e9
                    except TimeoutError:
                        pass
                    break
                while time.monotonic_ns() < stop_ns:
                    timeout = (stop_ns - time.monotonic_ns()) / 1e9
                    if drop_at:
                        timeout = min(timeout, (drop_at - time.monotonic_ns()) / 1e9)
                    try:
                        message = await asyncio.wait_for(ws.recv(), max(timeout, 0))
                    except TimeoutError:
                        break  # time to drop (flaky) or the run is over
                    now = time.monotonic_ns()
                    event = json.loads(message)
                    last_id = event["id"]
                    if event["type"] == "system.resync":
                        result["resyncs"] += 1
                        last_seq = None  # unknown until the next event
                        continue
                    if event["type"] != "bench.tick":
                        continue
                    seq = event["data"]["seq"]
                    total = event["data"]["total"]
                    if last_seq is not None and seq <= last_seq:
                        result["duplicates"] += 1
                    elif last_seq is not None and seq > last_seq + 1:
                        result["gaps"] += seq - last_seq - 1
                    last_seq = seq if last_seq is None else max(seq, last_seq)
                    result["received"] += 1
                    latency.append(now - event["data"]["due_ns"])
                    if finished():
                        break
                    if kind == "slow":
                        await asyncio.sleep(spec["read_delay"])
        except ConnectionClosed as closed:
            code = str(closed.rcvd.code if closed.rcvd else "none")
            result["closes"][code] = result["closes"].get(code, 0) + 1
        except OSError:
            result["closes"]["oserror"] = result["closes"].get("oserror", 0) + 1
        if finished() or time.monotonic_ns() >= stop_ns:
            break
        result["reconnects"] += 1
        await asyncio.sleep(spec.get("reconnect_delay", 0))

    result["complete"] = finished()
    result["latency_ms"] = summary(sorted(latency))
    result["samples_ns"] = latency
    return result


def worker(base: str, specs: list[dict], stop_ns: int, out) -> None:
    async def run() -> list[dict]:
        return await asyncio.gather(*(client(base, spec, stop_ns) for spec in specs))

    out.put(asyncio.run(run()))


def summary(ordered: list[int]) -> dict:
    if not ordered:
        return {}

    def at(p: float) -> float:
        return ordered[min(len(ordered) - 1, int(p * len(ordered)))] / 1e6

    return {
        "p50": at(0.50),
        "p95": at(0.95),
        "p99": at(0.99),
        "max": ordered[-1] / 1e6,
        "mean": statistics.fmean(ordered) / 1e6,
    }


# Experiments


def trial(
    specs: list[dict],
    rate: float,
    duration: float,
    *,
    pad: int = 0,
    size: int = 500,
    queue_size: int = 100,
    deflate: bool = True,
    procs: int = 4,
) -> dict:
    server = Server(size, queue_size, deflate)
    try:
        ctx = mp.get_context("spawn")
        out = ctx.Queue()
        stop_ns = time.monotonic_ns() + int((duration + 6) * 1e9)
        groups = [specs[i::procs] for i in range(min(procs, len(specs)))]
        workers = [
            ctx.Process(target=worker, args=(server.base, group, stop_ns, out))
            for group in groups
        ]
        for process in workers:
            process.start()
        # Let every client connect before any event is published.
        deadline = time.time() + 15
        while server.get("/api/events/stats")["listeners"] < len(specs):
            if time.time() > deadline:
                raise RuntimeError("clients did not connect")
            time.sleep(0.05)
        server.post("/bench/load", {"rate": rate, "duration": duration, "pad": pad})
        load_start = time.monotonic() + 0.1  # the generator's start offset
        # Sample server counters while the clients run.
        clients, timeline = [], []
        while len(clients) < len(specs):
            try:
                clients.extend(out.get(timeout=0.25))
            except queue.Empty:
                stats = server.get("/api/events/stats")
                timeline.append({"t": round(time.monotonic() - load_start, 2), **stats})
        for process in workers:
            process.join()
        stats = server.get("/api/events/stats")
        lag = server.get("/bench/lag")
    finally:
        server.close()

    def pooled(kind: str) -> dict:
        samples = sorted(
            s for c in clients if c["kind"] == kind for s in c["samples_ns"]
        )
        return summary(samples)

    latency = pooled("fast")
    for result in clients:
        del result["samples_ns"]
    return {
        "rate": rate,
        "duration": duration,
        "clients": len(specs),
        "deflate": deflate,
        "buffer_size": size,
        "queue_size": queue_size,
        "fast_latency_ms": latency,
        "publisher_lag_ms": lag,
        "server": stats,
        "timeline": timeline,
        "stalled_dropped_at_s": next(
            (point["t"] for point in timeline if point["stalled"] > 0), None
        ),
        "silent_gaps": sum(c["gaps"] for c in clients),
        "duplicates": sum(c["duplicates"] for c in clients),
        "detail": clients,
    }


def fast(n: int) -> list[dict]:
    return [{"kind": "fast"} for _ in range(n)]


def show(label: str, result: dict) -> None:
    lat = result["fast_latency_ms"]
    print(
        f"  {label:<26} p50 {lat.get('p50', 0):7.2f}  p95 {lat.get('p95', 0):7.2f}"
        f"  p99 {lat.get('p99', 0):7.2f}  max {lat.get('max', 0):8.2f} ms"
        f"  | resets {result['server']['resets']:>3}"
        f"  gaps {result['silent_gaps']}  dups {result['duplicates']}"
    )


def run(quick: bool) -> dict:
    seconds = 3.0 if quick else 8.0
    results: dict = {"env": environment(), "quick": quick}

    print("A. Latency vs publish rate (3 web apps)")
    results["rate"] = []
    for rate in [10, 50, 100, 200, 500, 1000, 2000, 5000, 10000]:
        result = trial(fast(3), rate, seconds)
        results["rate"].append(result)
        show(f"{rate} ev/s", result)

    print("B. Fan-out, 100 ev/s, compression on vs off")
    results["fanout"] = []
    for deflate in (True, False):
        for clients in [1, 10, 25, 50]:
            result = trial(fast(clients), 100, seconds, deflate=deflate)
            results["fanout"].append(result)
            show(f"{clients} apps, deflate={'on' if deflate else 'off'}", result)

    print("C. One slow web app (reads 20 ev/s) among 3 fast ones, 200 ev/s of 1 KB")
    slow = {"kind": "slow", "read_delay": 0.05, "reconnect_delay": 0}
    slow_seconds = max(seconds, 8.0)
    results["slow"] = {
        "baseline": trial(fast(3), 200, slow_seconds, pad=1000),
        "with_slow": trial([*fast(3), slow], 200, slow_seconds, pad=1000),
    }
    show("baseline", results["slow"]["baseline"])
    show("with a slow app", results["slow"]["with_slow"])
    detail = [c for c in results["slow"]["with_slow"]["detail"] if c["kind"] == "slow"]
    print(f"    slow app: {json.dumps({k: detail[0][k] for k in KEYS})}")

    print("D. One stalled web app (never reads), 500 ev/s of 4 KB events")
    stalled_seconds = 12.0 if quick else 15.0
    stalled = {"kind": "stalled"}
    results["stalled"] = {
        "baseline": trial(fast(3), 500, stalled_seconds, pad=4000),
        "with_stalled": trial([*fast(3), stalled], 500, stalled_seconds, pad=4000),
    }
    show("baseline", results["stalled"]["baseline"])
    show("with a stalled app", results["stalled"]["with_stalled"])
    stalled_run = results["stalled"]["with_stalled"]
    stuck = [c for c in stalled_run["detail"] if c["kind"] == "stalled"][0]
    print(
        f"    server dropped it at t = {stalled_run['stalled_dropped_at_s']} s "
        f"(stalled count {stalled_run['server']['stalled']}, "
        f"resets before that {stalled_run['server']['resets']}); "
        f"client saw the socket close at {stuck['closed_after_s']} s"
    )

    print("E. Buffer sizing: web app offline 1 s every 2 s, buffer 500")
    results["buffer"] = []
    flaky = {"kind": "flaky", "drop_every": 2.0, "reconnect_delay": 1.0}
    for rate in [100, 250, 400, 600, 1000]:
        result = trial([*fast(1), flaky], rate, max(seconds, 6.0))
        app = [c for c in result["detail"] if c["kind"] == "flaky"][0]
        result["flaky"] = {k: app[k] for k in KEYS}
        result["predicted_resync"] = rate * 1.0 > 500
        results["buffer"].append(result)
        print(
            f"  {rate:>5} ev/s  R x d = {rate:>4} events"
            f"  reconnects {app['reconnects']}  resyncs {app['resyncs']}"
            f"  gaps {app['gaps']}"
            f"  (predicted resync: {result['predicted_resync']})"
        )
    return results


KEYS = ("received", "reconnects", "resyncs", "gaps", "duplicates", "complete")


def environment() -> dict:
    import uvicorn
    import websockets

    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpus": os.cpu_count(),
        "python": platform.python_version(),
        "uvicorn": uvicorn.__version__,
        "websockets": websockets.__version__,
        "when": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def check(results: dict) -> list[str]:
    problems = []
    every = [
        *results["rate"],
        *results["fanout"],
        *results["slow"].values(),
        *results["stalled"].values(),
        *results["buffer"],
    ]
    if any(r["silent_gaps"] or r["duplicates"] for r in every):
        problems.append("a client saw a silent gap or a duplicate")
    if results["stalled"]["with_stalled"]["server"]["stalled"] < 1:
        problems.append("the stalled client was never dropped")
    for result in results["buffer"]:
        if (result["flaky"]["resyncs"] > 0) != result["predicted_resync"]:
            problems.append(f"buffer sizing prediction wrong at {result['rate']}")
    return problems


def plot(results: dict) -> None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("(matplotlib not installed, skipping the plot)")
        return
    fig, (left, right) = plt.subplots(1, 2, figsize=(11, 4.2))
    rates = [r["rate"] for r in results["rate"]]
    for key, style in (("p50", "o-"), ("p95", "s-"), ("p99", "^-")):
        values = [r["fast_latency_ms"][key] for r in results["rate"]]
        left.plot(rates, values, style, label=key)
    left.set(
        xscale="log",
        yscale="log",
        xlabel="events per second",
        ylabel="end-to-end latency (ms)",
        title="A. Latency vs publish rate (3 web apps)",
    )
    left.grid(True, which="both", alpha=0.3)
    left.legend()
    for deflate, style in ((True, "o-"), (False, "s--")):
        rows = [r for r in results["fanout"] if r["deflate"] is deflate]
        right.plot(
            [r["clients"] for r in rows],
            [r["fast_latency_ms"]["p99"] for r in rows],
            style,
            label=f"p99, compression {'on' if deflate else 'off'}",
        )
    right.set(
        xlabel="connected web apps",
        ylabel="p99 latency (ms)",
        title="B. Fan-out at 100 ev/s",
    )
    right.grid(True, alpha=0.3)
    right.legend()
    fig.tight_layout()
    fig.savefig(OUT / "events.png", dpi=160)
    print(f"plot: {OUT / 'events.png'}")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        port, size, queue_size, deflate = (int(a) for a in sys.argv[2:6])
        serve(port, size, queue_size, bool(deflate))
        return
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--plot-only", action="store_true")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if args.plot_only:
        plot(json.loads((OUT / "events.json").read_text()))
        return
    results = run(args.quick)
    problems = check(results)
    results["problems"] = problems
    (OUT / "events.json").write_text(json.dumps(results, indent=1))
    print(f"results: {OUT / 'events.json'}")
    plot(results)
    print("PASS" if not problems else "FAIL: " + "; ".join(problems))
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
