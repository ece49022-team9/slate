import logging

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from slate.access.routes import OWNER, bearer, identify, socket_token
from slate.voice.session import Hello

logger = logging.getLogger("slate.voice.api")
router = APIRouter(tags=["voice"])


@router.get("/voice/reports")
async def reports(request: Request) -> dict[str, dict]:
    if identify(request, bearer(request.headers)) is None:
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
    token, protocol = socket_token(socket)
    who = identify(socket, token)
    if who is None:
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
    session = await socket.app.state.voice.connect(
        socket, hello, None if who == OWNER else who
    )
    logger.info("Device %s connected at %s Hz", session.id, hello.rate)
    await session.serve()
