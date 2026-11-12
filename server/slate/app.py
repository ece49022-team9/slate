import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from slate.access import home
from slate.access.credentials import CredentialStore
from slate.access.devices import DeviceStore
from slate.access.routes import agent_router
from slate.access.routes import router as access_router
from slate.access.vault import Keyring
from slate.device import router as device_router
from slate.events import EventBus
from slate.events import router as events_router
from slate.health import router as health_router
from slate.tasks.manager import TaskManager
from slate.tasks.routes import router as tasks_router
from slate.tasks.store import TaskStore
from slate.voice.routes import router as voice_router
from slate.voice.session import VoiceSessions

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
WEB_ORIGINS = ("http://127.0.0.1:5173", "http://localhost:5173")


@asynccontextmanager
async def lifespan(app: FastAPI):
    data = home()
    tasks = TaskStore(data / "tasks.sqlite3")
    app.state.events = EventBus()
    app.state.credentials = CredentialStore(
        data / "credentials.sqlite3", Keyring.load(), authorize=tasks.credential_access
    )
    app.state.tasks = TaskManager(
        tasks, app.state.events, describe_credential=app.state.credentials.describe
    )
    app.state.tasks.recover()
    app.state.devices = DeviceStore(data / "devices.sqlite3")
    app.state.voice = VoiceSessions(app.state.events, app.state.tasks)
    yield
    await app.state.voice.close()
    app.state.tasks.close()
    app.state.devices.close()
    app.state.credentials.close()


app = FastAPI(
    title="Slate",
    version="0.1.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    redoc_url=None,
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("SLATE_WEB_ORIGINS", ",".join(WEB_ORIGINS)).split(","),
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)
app.include_router(health_router, prefix="/api")
app.include_router(voice_router, prefix="/api")
app.include_router(device_router, prefix="/api")
app.include_router(events_router, prefix="/api")
app.include_router(tasks_router, prefix="/api")
app.include_router(access_router, prefix="/api")
app.include_router(agent_router, prefix="/api")
