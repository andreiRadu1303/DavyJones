"""DavyJones Central API — entry point."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from cloud.config import settings
from cloud.db import engine
from cloud.models.base import Base


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create database tables on startup."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


app = FastAPI(
    title="DavyJones Cloud API",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
from cloud.api.auth import router as auth_router
from cloud.api.vaults import router as vaults_router
from cloud.api.tasks import router as tasks_router
from cloud.api.billing import router as billing_router

app.include_router(auth_router, prefix="/api/v1")
app.include_router(vaults_router, prefix="/api/v1")
app.include_router(tasks_router, prefix="/api/v1")
app.include_router(billing_router, prefix="/api/v1")


@app.get("/api/v1/health")
async def health():
    return {"status": "ok", "service": "davyjones-cloud-api"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("cloud.main:app", host="0.0.0.0", port=8000, reload=True)
