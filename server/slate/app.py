from contextlib import asynccontextmanager

from fastapi import FastAPI

from slate.health import router as health_router
from slate.voice.routes import router as voice_router
from slate.voice.session import VoiceSessions


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.voice = VoiceSessions()
    yield
    await app.state.voice.close()


app = FastAPI(
    title="Slate",
    version="0.1.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    redoc_url=None,
    lifespan=lifespan,
)
app.include_router(health_router, prefix="/api")
app.include_router(voice_router, prefix="/api")
