import asyncio
from typing import Any

from slate.api.events import EventBus

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


# One request, start to finish. Same story as web/src/events/mock.ts.
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
    """Replay the sample request forever so the dashboard has live data."""
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
