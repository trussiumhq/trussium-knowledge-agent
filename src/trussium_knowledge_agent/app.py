"""HTTP application entry point."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from trussium_knowledge_agent.web import router as web_router

_STATIC_DIRECTORY = Path(__file__).with_name("static")

app = FastAPI(
    title="Trussium Knowledge Agent",
    description="Reference application for documentation search and bounded agent workflows.",
    version="0.1.0",
)
app.mount("/static", StaticFiles(directory=_STATIC_DIRECTORY), name="static")
app.include_router(web_router)


@app.get("/", include_in_schema=False)
async def question_interface() -> FileResponse:
    """Serve the small local browser interface."""
    return FileResponse(_STATIC_DIRECTORY / "index.html")


@app.get("/health/live", tags=["health"])
async def liveness() -> dict[str, str]:
    """Return a process liveness response without checking optional dependencies."""
    return {"status": "ok"}
