import hmac
import logging
import os
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket
from fastapi.datastructures import Headers
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocketDisconnect

from slate.access.credentials import BadSecret, CredentialNotFound, CredentialStore
from slate.access.devices import DeviceStore

logger = logging.getLogger("slate.access")
BROWSER_PROTOCOL = "slate"
MAX_BODY = 32 * 1024
OWNER = "owner"


def bearer(headers: Headers) -> str:
    scheme, _, token = headers.get("authorization", "").partition(" ")
    return token if scheme.lower() == "bearer" else ""


def socket_token(socket: WebSocket) -> tuple[str, str | None]:
    if token := bearer(socket.headers):
        return token, None
    offered = [
        part.strip()
        for part in socket.headers.get("sec-websocket-protocol", "").split(",")
    ]
    if len(offered) == 2 and offered[0] == BROWSER_PROTOCOL:
        return offered[1], BROWSER_PROTOCOL
    return "", None


def is_owner(token: str) -> bool:
    expected = os.environ.get("SLATE_DEVICE_TOKEN", "")
    return bool(expected) and hmac.compare_digest(token.encode(), expected.encode())


def identify(connection: HTTPConnection, token: str) -> str | None:
    if is_owner(token):
        return OWNER
    devices: DeviceStore | None = getattr(connection.app.state, "devices", None)
    if devices is not None and (device := devices.identify(token)):
        return device.id
    return None


def require_owner(request: Request) -> None:
    if not is_owner(bearer(request.headers)):
        raise HTTPException(401, "Send the owner token as a bearer token")


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PairRequest(Body):
    name: str = Field(min_length=1, max_length=64)


class NewCredential(Body):
    service: str = Field(min_length=1, max_length=120)
    label: str = Field(min_length=1, max_length=120)
    kind: str
    secret: dict[str, str]
    username: str | None = Field(default=None, min_length=1, max_length=120)


class CredentialNames(Body):
    label: str | None = Field(default=None, min_length=1, max_length=120)
    username: str | None = Field(default=None, min_length=1, max_length=120)


class NewSecret(Body):
    secret: dict[str, str]


def parsed[T: Body](model: type[T]):
    async def parse(request: Request) -> T:
        raw = await request.body()
        if len(raw) > MAX_BODY:
            raise HTTPException(413, "The request body is too large")
        try:
            return model.model_validate_json(raw)
        except ValidationError as error:
            problems = error.errors(
                include_url=False, include_input=False, include_context=False
            )
            raise HTTPException(
                422, [{"loc": item["loc"], "msg": item["msg"]} for item in problems]
            ) from None

    return Depends(parse)


router = APIRouter(tags=["access"], dependencies=[Depends(require_owner)])


def devices(request: Request) -> DeviceStore:
    return request.app.state.devices


def credentials(request: Request) -> CredentialStore:
    return request.app.state.credentials


Devices = Annotated[DeviceStore, Depends(devices)]
Credentials = Annotated[CredentialStore, Depends(credentials)]


@router.post("/devices", status_code=201)
def pair(
    body: Annotated[PairRequest, parsed(PairRequest)], store: Devices
) -> dict[str, Any]:
    device, token = store.pair(body.name)
    logger.info("Paired device %s (%s)", device.id, device.name)
    return {"device": device.to_dict(), "token": token}


@router.get("/devices")
def list_devices(store: Devices) -> list[dict[str, Any]]:
    return [device.to_dict() for device in store.all()]


@router.delete("/devices/{device_id}")
async def revoke(device_id: str, request: Request, store: Devices) -> dict[str, Any]:
    device = store.revoke(device_id)
    if device is None:
        raise HTTPException(404, "This device does not exist")
    session = request.app.state.voice.current
    if session is not None and session.device_id == device_id:
        logger.info("Disconnecting revoked device %s", device_id)
        await session.close()
        try:
            await session.socket.close(4401, "Device revoked")
        except (WebSocketDisconnect, RuntimeError):
            logger.info("Revoked device %s socket was already gone", device_id)
    return device.to_dict()


def found(store: CredentialStore, credential_id: str) -> None:
    try:
        store.get(credential_id)
    except CredentialNotFound:
        raise HTTPException(404, "This credential does not exist") from None


@router.post("/credentials", status_code=201)
def add_credential(
    body: Annotated[NewCredential, parsed(NewCredential)], store: Credentials
) -> dict[str, Any]:
    try:
        return store.add(
            service=body.service,
            label=body.label,
            kind=body.kind,
            username=body.username,
            secret=body.secret,
        ).to_dict()
    except BadSecret as bad:
        raise HTTPException(422, str(bad)) from None


@router.get("/credentials")
def list_credentials(store: Credentials) -> list[dict[str, Any]]:
    return [item.to_dict() for item in store.all()]


@router.get("/credentials/{credential_id}")
def credential(
    credential_id: str,
    store: Credentials,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> dict[str, Any]:
    found(store, credential_id)
    return {
        "credential": store.get(credential_id).to_dict(),
        "uses": [use.to_dict() for use in store.uses(credential_id, limit)],
    }


@router.patch("/credentials/{credential_id}")
def rename(
    credential_id: str,
    body: Annotated[CredentialNames, parsed(CredentialNames)],
    store: Credentials,
) -> dict[str, Any]:
    found(store, credential_id)
    return store.rename(
        credential_id, label=body.label, username=body.username
    ).to_dict()


@router.put("/credentials/{credential_id}/secret")
def replace(
    credential_id: str,
    body: Annotated[NewSecret, parsed(NewSecret)],
    store: Credentials,
) -> dict[str, Any]:
    found(store, credential_id)
    try:
        return store.replace_secret(credential_id, body.secret).to_dict()
    except BadSecret as bad:
        raise HTTPException(422, str(bad)) from None


@router.delete("/credentials/{credential_id}", status_code=204)
def delete(credential_id: str, store: Credentials) -> None:
    found(store, credential_id)
    store.delete(credential_id)
