import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from slate.access.credentials import CredentialStore
from slate.access.devices import DeviceStore
from slate.access.signing import Verifier
from slate.access.vault import Keyring
from slate.api import device as device_api
from slate.api.credentials import router as credentials_router
from slate.api.demo import run_demo, run_task_demo
from slate.api.events import EventBus
from slate.api.events import router as events_router
from slate.api.tasks import router as tasks_router
from slate.device import router as device_router
from slate.health import router as health_router
from slate.tasks.manager import TaskManager
from slate.tasks.store import TaskStore
from slate.voice.routes import router as voice_router
from slate.voice.session import VoiceSessions

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.events = EventBus()
    app.state.voice = VoiceSessions(app.state.events)
    home = Path.home() / ".slate"
    keyring = Keyring.load()
    tasks = TaskStore(os.getenv("SLATE_TASKS_DB") or home / "tasks.sqlite3")
    # A task may use a credential only while running, and only if an approved
    # approval named it (TaskStore.credential_access).
    credentials_db = os.getenv("SLATE_CREDENTIALS_DB") or home / "credentials.sqlite3"
    app.state.credentials = CredentialStore(
        credentials_db, keyring, authorize=tasks.credential_access
    )
    app.state.tasks = TaskManager(
        tasks, app.state.events, describe_credential=app.state.credentials.describe
    )
    app.state.tasks.recover()
    devices_db = os.getenv("SLATE_DEVICES_DB") or home / "devices.sqlite3"
    app.state.devices = DeviceStore(devices_db, keyring=keyring)
    app.state.verifier = Verifier(app.state.devices)
    demos = []
    if os.getenv("SLATE_DEMO_EVENTS") == "1":
        demos.append(asyncio.create_task(run_demo(app.state.events)))
    if os.getenv("SLATE_DEMO_TASKS") == "1":
        demos.append(
            asyncio.create_task(
                run_task_demo(app.state.tasks, credentials=app.state.credentials)
            )
        )
    yield
    for demo in demos:
        demo.cancel()
    app.state.tasks.close()
    app.state.devices.close()
    app.state.credentials.close()
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
app.include_router(device_router, prefix="/api")
app.include_router(events_router, prefix="/api")
app.include_router(tasks_router, prefix="/api")
app.include_router(credentials_router, prefix="/api")
device_api.include(app)
