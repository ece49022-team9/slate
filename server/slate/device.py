import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from livekit import rtc
from pydantic import BaseModel, ConfigDict, Field

from slate.link import Link
from slate.voice.routes import require_local
from slate.voice.settings import WORKER_IDENTITY

logger = logging.getLogger("slate.device")
router = APIRouter(prefix="/device", tags=["device"])


class DeviceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class OrbRequest(DeviceModel):
    color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    radius: float = Field(ge=10, le=45, allow_inf_nan=False)


class TextRequest(DeviceModel):
    text: str = Field(max_length=64, pattern=r"^[\x20-\x7e]*$")


class DeviceCommand(DeviceModel):
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    turn_id: str
    operation: Literal["set_orb", "show_text", "get_status"]
    arguments: dict


class DeviceStatus(DeviceModel):
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    operation: Literal["set_orb", "show_text", "get_status"]
    revision: int = Field(ge=0)
    state: int = Field(ge=0, le=5)
    color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    radius: float = Field(ge=10, le=45, allow_inf_nan=False)
    text: str = Field(max_length=64, pattern=r"^[\x20-\x7e]*$")
    custom: bool


def agent_context(scope: str) -> str:
    return (
        f"This turn controls a Slate device. Its opaque scope is {scope}. "
        "Use the slate-device MCP SDK to control it when requested. "
        "Only report device changes after a successful acknowledgment. "
        "Device text is printable ASCII, at most 64 characters. "
        "Orb color is #RRGGBB and radius is 10 through 45 pixels. "
        "The scope expires when this turn ends."
    )


class DeviceSDK:
    def __init__(
        self,
        scope: str,
        turn_id: str,
        execute: Callable[[DeviceCommand], Awaitable[dict]],
        active: Callable[[], bool],
    ) -> None:
        self.scope = scope
        self.turn_id = turn_id
        self.execute = execute
        self.active = active

    async def command(self, operation: str, arguments: dict) -> DeviceStatus:
        if not self.active():
            raise ValueError("This device turn has ended")
        command = DeviceCommand(
            request_id=uuid4().hex,
            turn_id=self.turn_id,
            operation=operation,
            arguments=arguments,
        )
        receipt = DeviceStatus.model_validate(await self.execute(command))
        if not self.active():
            raise ValueError("This device turn has ended")
        if (receipt.request_id, receipt.operation) != (
            command.request_id,
            command.operation,
        ):
            raise RuntimeError("Device acknowledgment does not match the command")
        logger.info(
            "Acknowledged %s: turn=%s request=%s revision=%s",
            operation,
            self.turn_id,
            receipt.request_id,
            receipt.revision,
        )
        return receipt

    async def set_orb(self, request: OrbRequest) -> DeviceStatus:
        return await self.command("set_orb", request.model_dump())

    async def show_text(self, request: TextRequest) -> DeviceStatus:
        return await self.command("show_text", request.model_dump())

    async def get_status(self) -> DeviceStatus:
        return await self.command("get_status", {})


class FirmwareDevice:
    def __init__(self, link: Link, active_turn: Callable[[], str]) -> None:
        self.link = link
        self.active_turn = active_turn
        self.lock = asyncio.Lock()

    async def execute(self, command: DeviceCommand) -> DeviceStatus:
        async with self.lock:
            if command.turn_id != self.active_turn():
                raise ValueError("This device turn has ended")
            if command.operation == "set_orb":
                request = OrbRequest.model_validate(command.arguments)
                wire = f"orb {request.color[1:]} {request.radius:g}"
            elif command.operation == "show_text":
                request = TextRequest.model_validate(command.arguments)
                wire = "text " + request.text.encode("ascii").hex()
            else:
                if command.arguments:
                    raise ValueError("Status takes no arguments")
                wire = "status"
            prefix = f"slate.device:{command.request_id} "
            waiting = asyncio.create_task(self.link.wait_for(prefix, seconds=5))
            try:
                await asyncio.sleep(0)
                if command.turn_id != self.active_turn():
                    raise ValueError("This device turn has ended")
                self.link.type(f"@{command.request_id} {wire}\n")
                async with asyncio.timeout(6):
                    line = await waiting
            finally:
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
            receipt = json.loads(line.split(prefix, 1)[1])
            if "error" in receipt:
                raise RuntimeError(f"Firmware rejected command: {receipt['error']}")
            receipt["text"] = bytes.fromhex(receipt.pop("text_hex")).decode("ascii")
            return DeviceStatus.model_validate(receipt)

    async def rpc(self, data: rtc.RpcInvocationData) -> str:
        if data.caller_identity != WORKER_IDENTITY:
            raise rtc.RpcError(1501, "Only the Slate worker can control this device")
        try:
            command = DeviceCommand.model_validate_json(data.payload)
            return (await self.execute(command)).model_dump_json()
        except (ValueError, RuntimeError, TimeoutError) as error:
            logger.warning("Device command failed: %s", error)
            raise rtc.RpcError(1505, str(error)) from error


def scoped_device(request: Request, scope: str) -> DeviceSDK:
    require_local(request)
    session = request.app.state.voice.current
    if session is None or session.turn is None or session.turn.scope != scope:
        raise HTTPException(409, "This device turn has ended")
    return session.device_sdk(session.turn)


async def receipt(work: Awaitable[DeviceStatus]) -> DeviceStatus:
    try:
        return await work
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    except (rtc.RpcError, RuntimeError, TimeoutError) as error:
        logger.warning("Device unavailable: %s", error)
        raise HTTPException(503, "Device did not acknowledge the command") from error


@router.post("/{scope}/set_orb", response_model=DeviceStatus)
async def set_orb(scope: str, body: OrbRequest, request: Request):
    return await receipt(scoped_device(request, scope).set_orb(body))


@router.post("/{scope}/show_text", response_model=DeviceStatus)
async def show_text(scope: str, body: TextRequest, request: Request):
    return await receipt(scoped_device(request, scope).show_text(body))


@router.get("/{scope}/status", response_model=DeviceStatus)
async def get_status(scope: str, request: Request):
    return await receipt(scoped_device(request, scope).get_status())
