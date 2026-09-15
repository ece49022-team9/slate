from fastapi import FastAPI

from slate.health import router as health_router

app = FastAPI(
    title="Slate",
    version="0.1.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    redoc_url=None,
)
app.include_router(health_router, prefix="/api")
