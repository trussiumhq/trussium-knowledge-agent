# Trussium Knowledge Agent

An open-source reference application for asking grounded questions about a
Markdown repository and reviewing documentation maintenance findings. It
demonstrates retrieval augmented generation (RAG) and bounded, explicitly
registered agent tools using Trussium.

The first milestone currently supports safe Markdown indexing and semantic
retrieval. Grounded answer generation, a browser interface, and bounded agent
tools are planned follow-on work. See the
[roadmap](docs/ROADMAP.md) for current status.

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
- A reachable Trussium runtime with an embeddings-capable model enabled (the
  runtime is managed separately and is not installed or started by this app)

### Start the development environment

```bash
cp .env.example .env
uv sync --all-groups
docker compose up -d postgres
uv run uvicorn trussium_knowledge_agent.app:app --reload --port 8000
```

The starter service exposes `GET http://127.0.0.1:8000/health/live`. PostgreSQL
with pgvector is available at `127.0.0.1:5433`; its data is stored in a named
Docker volume. The FastAPI shell currently provides liveness only. The indexing
and semantic search commands call the separately configured Trussium embeddings
API.

### Index a Markdown repository

Set `DATABASE_URL` in `.env` to the local database DSN shown in
`.env.example`. Set `TRUSSIUM_EMBEDDING_MODEL` to a model enabled for embeddings
on the runtime at `TRUSSIUM_URL`. Set `TRUSSIUM_API_KEY` only if runtime bearer
authentication is enabled; keep that credential in your ignored local `.env`
or secret manager. The indexer sends document chunks to that configured runtime
for embedding, so select a runtime whose data handling is appropriate for the
documents you choose.

Index, search, and remove a repository:

```bash
uv run --env-file .env trussium-knowledge-agent index /path/to/markdown-repo
uv run --env-file .env trussium-knowledge-agent search "how do I configure retries?" --limit 5
uv run --env-file .env trussium-knowledge-agent remove /path/to/markdown-repo
```

The CLI applies pending versioned schema migrations automatically.
Indexing is explicit and local. It skips symlinks and does not read outside the
selected root. Re-indexing replaces that source's chunks transactionally;
removing an index deletes its stored source and chunks. Markdown files over
5 MiB or invalid UTF-8 abort the operation before database state changes. Git
metadata is optional; when available the checked-out `HEAD` is recorded. The
runtime timeout defaults to 30 seconds and accepts values from 1 through 120
seconds via `TRUSSIUM_TIMEOUT_SECONDS`. The search limit is bounded from 1 to
20 and results include cosine scores and source paths/headings. Search uses
exact cosine distance, filtered to the same resolved provider, model, and
vector dimension used at indexing. If that model
identity changes, re-index the source before searching with the new identity.
Natural-language answer generation, reranking, and the browser interface are
still planned.

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
