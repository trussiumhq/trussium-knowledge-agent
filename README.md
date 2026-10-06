# Trussium Knowledge Agent

An open-source reference application for asking grounded questions about a
Markdown repository and reviewing documentation maintenance findings. It
demonstrates retrieval augmented generation (RAG) and bounded, explicitly
registered agent tools using Trussium.

The first milestone currently supports safe Markdown indexing, semantic
retrieval, grounded answers with validated source citations, and a repeatable
retrieval evaluation fixture. A browser interface and bounded agent tools are
planned follow-on work. See the
[roadmap](docs/ROADMAP.md) for current status.

## Architecture

- FastAPI application and a planned browser interface, with a CLI for indexing,
  searching, and asking grounded questions.
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
- A reachable Trussium runtime with an embeddings-enabled model for indexing
  and search; `ask` additionally requires a chat-enabled model. The runtime is
  managed separately and is not installed or started by this app.

### Start the development environment

```bash
cp .env.example .env
uv sync --all-groups
docker compose up -d postgres
uv run uvicorn trussium_knowledge_agent.app:app --reload --port 8000
```

The starter service exposes `GET http://127.0.0.1:8000/health/live`. PostgreSQL
with pgvector is available at `127.0.0.1:5433`; its data is stored in a named
Docker volume. The FastAPI shell currently provides liveness only. The indexing,
semantic search, and answer commands call the separately configured Trussium
runtime APIs.

### Index and query a Markdown repository

Set `DATABASE_URL` in `.env` to the local database DSN shown in
`.env.example`. Set `TRUSSIUM_EMBEDDING_MODEL` to a model enabled for embeddings
on the runtime at `TRUSSIUM_URL`. Set `TRUSSIUM_API_KEY` only if runtime bearer
authentication is enabled; keep that credential in your ignored local `.env`
or secret manager. The indexer sends document chunks to that configured runtime
for embedding, so select a runtime whose data handling is appropriate for the
documents you choose. For `ask`, set `TRUSSIUM_CHAT_MODEL` to a chat-enabled
model on that runtime. The question and retrieved passages are sent to its
chat endpoint for answer generation.

Index, search, ask a grounded question, and remove a repository:

```bash
uv run --env-file .env trussium-knowledge-agent index /path/to/markdown-repo
uv run --env-file .env trussium-knowledge-agent search "how do I configure retries?" --limit 5
uv run --env-file .env trussium-knowledge-agent ask "How do I configure retries?" --limit 5
uv run --env-file .env trussium-knowledge-agent index fixtures/evaluation-corpus
uv run --env-file .env trussium-knowledge-agent evaluate fixtures/evaluation-queries.json --limit 5
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
20 and results include cosine scores and source paths/headings. `ask` accepts a
question up to 4,000 characters, uses at most 10 passages, and returns an answer
only when citations map to retrieved source metadata. Empty retrieval,
malformed/truncated output, unknown citations, or generated URLs fail closed to
a fixed insufficient-evidence response. This does not guarantee factual
correctness. Search uses
exact cosine distance, filtered to the same resolved provider, model, and
vector dimension used at indexing. If that model
identity changes, re-index the source before searching with the new identity.
Reranking and the browser interface remain planned.

The included retrieval evaluation fixture compares ranked source locations
against expected `path#heading-anchor` citations. It reports Hit@k, Recall@k,
and MRR@k; it does not judge generated answer quality. See
[`docs/EVALUATION.md`](docs/EVALUATION.md) for setup, dataset limits, and
interpretation guidance.

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
