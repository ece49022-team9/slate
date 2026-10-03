import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import modal
from slate.agent.code_mode import DeviceClient, MontyExecutor

COLORS = ("#0000ff", "#ff8800", "#00ff00", "#ff00ff", "#00ffff", "#ffff00")
app = modal.App("slate-device-code-bench")
image = (
    modal.Image.debian_slim(python_version="3.12")
    .uv_sync(str(Path(__file__).resolve().parents[1]))
    .add_local_python_source("slate")
)


def task(index: int) -> SimpleNamespace:
    return SimpleNamespace(
        color=COLORS[index % len(COLORS)], radius=12 + index % 30, label=f"T{index}"
    )


def program(work: SimpleNamespace) -> str:
    return (
        f"orb = await device.set_orb('{work.color}', {work.radius})\n"
        f"text = await device.show_text('{work.label}')\n"
        "status = await device.get_status()\n[orb, text, status]"
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


def stub_transport() -> httpx.MockTransport:
    state = {"revision": 0, "color": "#ffffff", "radius": 27.0, "text": ""}

    def handle(request: httpx.Request) -> httpx.Response:
        operation = request.url.path.rsplit("/", 1)[-1]
        operation = "get_status" if operation == "status" else operation
        if operation != "get_status":
            state.update(httpx.Response(200, content=request.content).json())
            state["revision"] += 1
        return httpx.Response(
            200,
            json={
                "request_id": uuid4().hex,
                "operation": operation,
                "state": 0,
                "custom": True,
                **state,
            },
        )

    return httpx.MockTransport(handle)


@app.function(image=image, cpu=1.0, memory=1024, timeout=900)
async def remote_profile(rounds: int, warmup: int, cold: int) -> list[dict]:
    target = "stub"
    scope = "stub"
    client = httpx.AsyncClient(base_url="http://stub.local", transport=stub_transport())
    rows = []
    async with client:
        device = DeviceClient(client)
        monty = MontyExecutor(device)

        async def direct(work):
            await device.set_orb(scope, work.color, work.radius)
            await device.show_text(scope, work.label)
            return await device.get_status(scope)

        async def warm(work):
            return code_status(await monty.execute(scope, program(work)))

        async def fresh(work):
            executor = MontyExecutor(device)
            try:
                return code_status(await executor.execute(scope, program(work)))
            finally:
                await executor.close()

        arms = {f"modal-{target}/tools": direct, f"modal-{target}/monty": warm}
        index = 50_000
        try:
            for round_index in range(warmup + rounds):
                for name, run in arms.items():
                    work = task(index)
                    index += 1
                    elapsed, status = await timed(run(work))
                    rows.append(
                        {
                            "arm": name,
                            "round": round_index,
                            "warmup": round_index < warmup,
                            "ms": elapsed,
                            "correct": matches(status, work),
                        }
                    )
            for trial in range(cold):
                work = task(index)
                index += 1
                elapsed, status = await timed(fresh(work))
                rows.append(
                    {
                        "arm": f"modal-{target}/monty-new-process",
                        "round": trial,
                        "warmup": False,
                        "ms": elapsed,
                        "correct": matches(status, work),
                    }
                )
        finally:
            await monty.close()
    return rows
