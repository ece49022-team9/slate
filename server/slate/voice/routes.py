import logging

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger("slate.voice.api")
router = APIRouter(prefix="/voice", tags=["voice"])


class SessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionResponse(BaseModel):
    session_id: str
    server_url: str
    participant_token: str
    worker_identity: str


def require_local(request: Request) -> None:
    if request.client is None or request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(
            403, "Microphone sessions are local-only during development"
        )
    origin = request.headers.get("origin")
    if origin and origin not in ("http://127.0.0.1:5173", "http://localhost:5173"):
        raise HTTPException(403, "Unknown browser origin")


@router.post("/sessions", response_model=SessionResponse)
async def create_session(body: SessionRequest, request: Request, response: Response):
    require_local(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        return await request.app.state.voice.create()
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    except Exception as error:
        logger.exception("Could not start microphone session")
        raise HTTPException(503, "LiveKit is unavailable. Run make livekit.") from error


@router.delete("/sessions/{session_id}", status_code=204)
async def close_session(session_id: str, request: Request) -> Response:
    require_local(request)
    await request.app.state.voice.close(session_id)
    return Response(status_code=204)
