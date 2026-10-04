import hmac
import logging
import os

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from slate.voice.session import Hello

logger = logging.getLogger("slate.voice.api")
router = APIRouter(tags=["voice"])
BROWSER_PROTOCOL = "slate"


def credential(socket: WebSocket) -> tuple[str, str | None]:
    scheme, _, token = socket.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and token:
        return token, None
    offered = [
        part.strip()
        for part in socket.headers.get("sec-websocket-protocol", "").split(",")
    ]
    if len(offered) == 2 and offered[0] == BROWSER_PROTOCOL:
        return offered[1], BROWSER_PROTOCOL
    return "", None


def device_token(token: str) -> bool:
    expected = os.environ.get("SLATE_DEVICE_TOKEN", "")
    return bool(expected) and hmac.compare_digest(token.encode(), expected.encode())


@router.get("/voice/reports")
async def reports(request: Request) -> dict[str, dict]:
    """Timing, usage and words for the connected device's recent turns and live
    calls, oldest first and including a call in progress, for profiling."""
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not device_token(token):
        raise HTTPException(401, "Send the device token as a bearer token")
    session = request.app.state.voice.current
    if session is None:
        return {}
    found = dict(session.reports)
    if session.call is not None:
        found[session.call.id] = session.call.report()
    return found


@router.websocket("/device/socket")
async def device_socket(socket: WebSocket) -> None:
    token, protocol = credential(socket)
    if not device_token(token):
        logger.warning("Rejected device socket: missing or wrong device token")
        await socket.close(4401, "Unauthorized device")
        return
    await socket.accept(subprotocol=protocol)
    try:
        hello = Hello.model_validate_json(await socket.receive_text())
    except WebSocketDisconnect:
        logger.info("Device socket closed before hello")
        return
    except (ValidationError, ValueError) as error:
        logger.warning("Rejected device socket: invalid hello: %s", error)
        await socket.close(4400, "Send hello first")
        return
    session = await socket.app.state.voice.connect(socket, hello)
    logger.info("Device %s connected at %s Hz", session.id, hello.rate)
    await session.serve()
