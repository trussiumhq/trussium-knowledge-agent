# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.11.31 AS uv

FROM python:3.12-slim-bookworm AS build

COPY --from=uv /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --locked --no-dev --no-editable

FROM python:3.12-slim-bookworm AS runtime

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    KNOWLEDGE_AGENT_LIFECYCLE_LEDGER_PATH=/var/lib/knowledge-agent/source-lifecycle.jsonl

WORKDIR /app

COPY --from=build --chown=10001:10001 /opt/venv /opt/venv
COPY --from=build --chown=10001:10001 /app/src ./src
COPY --chown=10001:10001 fixtures ./fixtures
RUN mkdir -p /var/lib/knowledge-agent \
    && chown -R 10001:10001 /var/lib/knowledge-agent

USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2)"]

CMD ["uvicorn", "trussium_knowledge_agent.app:app", "--host", "0.0.0.0", "--port", "8000"]
