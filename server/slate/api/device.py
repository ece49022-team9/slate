"""Device pairing (local web app only) and signed device commands.

Pairing, listing and revoking go through the local web app, like tasks.
Everything under /api/device/ must be signed by a paired device; see
slate.access.signing for the format. The device can do less than the web
app on purpose: check the link, answer an approval, cancel a task.
"""

from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from slate.access.devices import DeviceStore
from slate.access.signing import AuthError, Verifier
from slate.api.tasks import Tasks, cancel_or_raise, decide_or_raise, require_local

MAX_BODY = 16 * 1024

admin = APIRouter(tags=["devices"], dependencies=[Depends(require_local)])
device = APIRouter(prefix="/device", tags=["device"])


def devices(request: Request) -> DeviceStore:
    return request.app.state.devices


Devices = Annotated[DeviceStore, Depends(devices)]


# Pairing (web app)


class PairRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)


@admin.post("/devices", status_code=201)
def pair(body: PairRequest, store: Devices) -> dict[str, Any]:
    paired, secret = store.pair(body.name)
    return {
        "device": paired.to_dict(),
        # Shown once. Copy it to the device; it is never returned again.
        "secret": secret.hex(),
    }


@admin.get("/devices")
def list_devices(store: Devices) -> list[dict[str, Any]]:
    return [item.to_dict() for item in store.all()]


@admin.delete("/devices/{device_id}")
def revoke(device_id: str, store: Devices) -> dict[str, Any]:
    revoked = store.revoke(device_id)
    if revoked is None:
        raise HTTPException(404, "no such device")
    return revoked.to_dict()


@admin.get("/devices/auth-stats")
def auth_stats(request: Request) -> dict[str, int]:
    verifier: Verifier = request.app.state.verifier
    return {**verifier.stats, "remembered_nonces": verifier.remembered()}


# Signed commands (device)


@dataclass(frozen=True)
class Signed:
    device_id: str
    body: bytes


class AuthFailed(Exception):
    def __init__(self, error: AuthError, server_time: int) -> None:
        self.error = error
        self.server_time = server_time


async def signed(request: Request) -> Signed:
    length = request.headers.get("content-length")
    if length is not None and length.isdigit() and int(length) > MAX_BODY:
        raise HTTPException(413, "body too large")
    body = await request.body()
    if len(body) > MAX_BODY:
        raise HTTPException(413, "body too large")
    path = request.url.path
    if request.url.query:
        path += "?" + request.url.query
    verifier: Verifier = request.app.state.verifier
    try:
        device_id = verifier.verify(request.method, path, request.headers, body)
    except AuthError as error:
        raise AuthFailed(error, int(verifier.clock())) from None
    request.app.state.devices.touch(device_id)
    return Signed(device_id=device_id, body=body)


async def auth_failed(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AuthFailed)
    # server_time lets a device with a drifting clock fix itself and retry.
    return JSONResponse(
        {
            "error": "unauthorized",
            "reason": exc.error.reason,
            "server_time": exc.server_time,
        },
        status_code=exc.error.status,
    )


SignedRequest = Annotated[Signed, Depends(signed)]


def parse[T: BaseModel](model: type[T], body: bytes) -> T:
    # Parse exactly the bytes that were signed.
    try:
        return model.model_validate_json(body or b"{}")
    except ValidationError as bad:
        raise HTTPException(422, bad.errors(include_url=False)) from None


class DeviceDecision(BaseModel):
    decision: Literal["approve", "deny"]
    expected_version: int | None = None


@device.get("/ping")
def ping(request: Request, auth: SignedRequest) -> dict[str, Any]:
    return {
        "device_id": auth.device_id,
        "server_time": int(request.app.state.verifier.clock()),
    }


@device.post("/approvals/{approval_id}")
def device_decide(
    approval_id: str, auth: SignedRequest, tasks: Tasks
) -> dict[str, Any]:
    body = parse(DeviceDecision, auth.body)
    return decide_or_raise(
        tasks,
        approval_id,
        body.decision == "approve",
        actor=f"device:{auth.device_id}",
        expected_version=body.expected_version,
    )


@device.post("/tasks/{task_id}/cancel")
def device_cancel(task_id: str, auth: SignedRequest, tasks: Tasks) -> dict[str, Any]:
    return cancel_or_raise(tasks, task_id, actor=f"device:{auth.device_id}")


def include(app) -> None:
    app.include_router(admin, prefix="/api")
    app.include_router(device, prefix="/api")
    app.add_exception_handler(AuthFailed, auth_failed)
