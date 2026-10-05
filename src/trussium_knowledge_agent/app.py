"""HTTP application entry point."""

from fastapi import FastAPI

app = FastAPI(
    title="Trussium Knowledge Agent",
    description="Reference application for documentation search and bounded agent workflows.",
    version="0.1.0",
)


@app.get("/health/live", tags=["health"])
async def liveness() -> dict[str, str]:
    """Return a process liveness response without checking optional dependencies."""
    return {"status": "ok"}
