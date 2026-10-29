import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from slate.api.demo import run_demo, run_task_demo
from slate.api.events import EventBus
from slate.api.events import router as events_router
from slate.api.tasks import router as tasks_router
from slate.health import router as health_router
from slate.tasks.manager import TaskManager
from slate.tasks.store import TaskStore
from slate.voice.routes import router as voice_router
from slate.voice.session import VoiceSessions


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.events = EventBus()
    app.state.voice = VoiceSessions(app.state.events)
    db = os.getenv("SLATE_TASKS_DB") or Path.home() / ".slate" / "tasks.sqlite3"
    app.state.tasks = TaskManager(TaskStore(db), app.state.events)
    app.state.tasks.recover()
    demos = []
    if os.getenv("SLATE_DEMO_EVENTS") == "1":
        demos.append(asyncio.create_task(run_demo(app.state.events)))
    if os.getenv("SLATE_DEMO_TASKS") == "1":
        demos.append(asyncio.create_task(run_task_demo(app.state.tasks)))
    yield
    for demo in demos:
        demo.cancel()
    app.state.tasks.close()
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
app.include_router(tasks_router, prefix="/api")
