import asyncio
from typing import Any

from slate.access.credentials import AccessDenied, CredentialStore
from slate.api.events import EventBus
from slate.tasks.manager import TaskManager

DEVICE_ID = "dev_01"
STATUS: dict[str, Any] = {
    "online": True,
    "state": "idle",
    "battery_percent": 82,
    "wifi_rssi": -54,
    "firmware_version": "0.1.0",
}


def device(state: str, battery: int = 82) -> tuple[str, dict[str, Any]]:
    return "device.status", {**STATUS, "state": state, "battery_percent": battery}


def step(
    step_id: str, kind: str, status: str, title: str, detail: str | None = None
) -> tuple[str, dict[str, Any]]:
    data: dict[str, Any] = {
        "step_id": step_id,
        "kind": kind,
        "status": status,
        "title": title,
    }
    if detail:
        data["detail"] = detail
    return "agent.step", data


SCRIPT = [
    device("idle"),
    device("listen"),
    device("transcribe"),
    step("st_1", "request", "done", "Order paper towels on Amazon"),
    step("st_2", "model", "running", "Planning the task"),
    step("st_2", "model", "done", "Planning the task"),
    step("st_3", "browser", "running", "Opening amazon.com"),
    step("st_3", "browser", "done", "Opening amazon.com"),
    step("st_4", "browser", "running", "Searching for paper towels"),
    step(
        "st_4",
        "browser",
        "done",
        "Searching for paper towels",
        "Picked the top result, $24.99",
    ),
    step("st_5", "tool", "done", "Used saved Amazon login"),
    device("respond"),
    step("st_6", "response", "done", "Found it. Waiting for your approval."),
    device("idle", battery=81),
]


async def run_demo(bus: EventBus, interval: float = 1.5) -> None:
    loop = 1
    while True:
        session = f"ses_server_{loop:02d}"
        for event_type, data in SCRIPT:
            await asyncio.sleep(interval)
            bus.publish(
                event_type,
                data,
                device_id=DEVICE_ID,
                session_id=session if event_type == "agent.step" else None,
            )
        loop += 1


async def run_task_demo(
    manager: TaskManager,
    approval_timeout: float = 30.0,
    credentials: CredentialStore | None = None,
) -> None:
    while True:
        task = manager.create("Pay the electric bill", kind="payment")
        manager.start(task.id)
        await asyncio.sleep(2)
        duke = None
        if credentials is not None:
            duke = next(
                (c for c in credentials.all() if c.service == "duke-energy.com"), None
            )
        approval = manager.request_approval(
            task.id,
            "Pay $84.12 to Duke Energy?",
            timeout=approval_timeout,
            credentials=[duke.id] if duke else (),
        )
        result = await manager.wait_for_decision(approval.id)
        if result.status == "approved":
            await asyncio.sleep(2)
            if credentials is not None and duke is not None:
                try:
                    secret = credentials.use(
                        duke.id, task_id=task.id, purpose="log in to pay the bill"
                    )
                except AccessDenied as denied:
                    manager.fail(
                        task.id, f"could not use the credential: {denied.reason}"
                    )
                    await asyncio.sleep(10)
                    continue
                del secret
            manager.succeed(task.id, "Paid $84.12, confirmation 55120")
        await asyncio.sleep(10)
