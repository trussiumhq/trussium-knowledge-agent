"""HTTP application entry point."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from trussium_knowledge_agent.github_webhook import router as github_webhook_router
from trussium_knowledge_agent.mcp import router as mcp_router
from trussium_knowledge_agent.public_demo import (
    PublicDemoConfigurationError,
    PublicDemoSafetyMiddleware,
    load_public_demo_config,
)
from trussium_knowledge_agent.store import validate_public_demo_sources
from trussium_knowledge_agent.web import router as web_router

_STATIC_DIRECTORY = Path(__file__).with_name("static")


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Validate public-demo controls and the source allowlist before serving."""
    config = load_public_demo_config()
    application.state.public_demo_config = config
    if config is not None:
        database_url = os.environ.get("DATABASE_URL", "").strip()
        if not database_url:
            raise PublicDemoConfigurationError("DATABASE_URL is required in public demo mode")
        validate_public_demo_sources(database_url, config.allowed_source_ids)
    yield


app = FastAPI(
    title="Trussium Knowledge Agent",
    description="Reference application for documentation search and bounded agent workflows.",
    version="0.1.0",
    lifespan=lifespan,
)
app.add_middleware(PublicDemoSafetyMiddleware)
app.mount("/static", StaticFiles(directory=_STATIC_DIRECTORY), name="static")
app.include_router(web_router)
app.include_router(mcp_router)
app.include_router(github_webhook_router)


@app.get("/", include_in_schema=False)
async def question_interface() -> FileResponse:
    """Serve the small local browser interface."""
    return FileResponse(_STATIC_DIRECTORY / "index.html")


@app.get("/health/live", tags=["health"])
async def liveness() -> dict[str, str]:
    """Return a process liveness response without checking optional dependencies."""
    return {"status": "ok"}
