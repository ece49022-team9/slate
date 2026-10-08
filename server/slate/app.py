import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from slate.api.demo import run_demo
from slate.api.events import EventBus
from slate.api.events import router as events_router
from slate.health import router as health_router
from slate.voice.routes import router as voice_router
from slate.voice.session import VoiceSessions


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.events = EventBus()
    app.state.voice = VoiceSessions(app.state.events)
    demo = None
    if os.getenv("SLATE_DEMO_EVENTS") == "1":
        demo = asyncio.create_task(run_demo(app.state.events))
    yield
    if demo:
        demo.cancel()
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
app.include_router(events_router, prefix="/api")
