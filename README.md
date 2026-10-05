# Trussium Knowledge Agent

An open-source reference application for asking grounded questions about a
Markdown repository and reviewing documentation maintenance findings. It
demonstrates retrieval augmented generation (RAG) and bounded, explicitly
registered agent tools using Trussium.

The project is at its foundation stage. The first change establishes the
application shell, local development environment, architecture, security
boundaries, and roadmap. Document indexing, cited answers, and the audit agent
are planned milestones.

## Planned architecture

- FastAPI application and a small browser interface, with a CLI for indexing.
- An existing Trussium runtime for chat, embeddings, and optional reranking.
- PostgreSQL with pgvector for document chunks, source metadata, and vectors.
- Local Markdown repositories as the first ingestion source.
- An opt-in Trussium runtime with registered tools for bounded documentation
  audits. External issue or pull request creation requires human approval.

The app owns ingestion, retrieval, citations, and user experience. Trussium
provides model capabilities and controlled execution. See
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and
[`docs/ROADMAP.md`](docs/ROADMAP.md).

## Local development

### Prerequisites

- Python 3.12 or newer
- [`uv`](https://docs.astral.sh/uv/)
- Docker Compose
- A reachable Trussium runtime for inference features (planned; the runtime is
  not installed or started by this application)

### Start the development environment

```bash
cp .env.example .env
uv sync --all-groups
docker compose up -d postgres
uv run uvicorn trussium_knowledge_agent.app:app --reload --port 8000
```

The starter service exposes `GET http://127.0.0.1:8000/health/live`. PostgreSQL
with pgvector is available at `127.0.0.1:5433`; its data is stored in a named
Docker volume. The starter service does not yet connect to the database.

### Development checks

```bash
uv run ruff format --check .
uv run ruff check src tests
uv run mypy src tests
uv run pytest
```

## Project status

See the [roadmap](docs/ROADMAP.md) for planned milestones and acceptance
criteria. Security issues should be reported privately as described in
[`SECURITY.md`](SECURITY.md).
