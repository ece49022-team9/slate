import hmac
import logging
import os

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
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


@router.websocket("/device/socket")
async def device_socket(socket: WebSocket) -> None:
    expected = os.environ.get("SLATE_DEVICE_TOKEN", "")
    token, protocol = credential(socket)
    if not expected or not hmac.compare_digest(token.encode(), expected.encode()):
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
