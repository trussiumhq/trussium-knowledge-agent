# Trussium Knowledge Agent

An open-source reference application for asking grounded questions about a
Markdown repository and reviewing documentation maintenance findings. It
demonstrates retrieval augmented generation (RAG) and bounded, explicitly
registered agent tools using Trussium.

The reference app supports safe Markdown indexing, semantic retrieval,
grounded answers with validated source citations, retrieval evaluation, and a
minimal browser question interface. It also exposes authenticated, fixed
read-only tools that a custom Trussium runtime can register in a bounded
workflow. The first workflow slice supports document search and a deterministic
Markdown link audit; it does not autonomously edit repositories. See the
[workflow guide](docs/TRUSSIUM_WORKFLOW.md) and
[roadmap](docs/ROADMAP.md) for scope and remaining work.

## Architecture

- FastAPI browser interface for grounded questions, with a CLI for indexing,
  searching, asking questions, and evaluating retrieval.
- An existing Trussium runtime for chat and embeddings; reranking integration
  remains future work.
- PostgreSQL with pgvector for document chunks, source metadata, and vectors.
- Local Markdown repositories as the first ingestion source.
- An opt-in Trussium runtime with explicitly registered tools for bounded
  documentation search and deterministic audits. External issue or pull
  request creation is not implemented.

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
uv run --env-file .env uvicorn trussium_knowledge_agent.app:app --reload --port 8000
```

Open `http://127.0.0.1:8000/` for the browser question interface. It requires
an indexed corpus, PostgreSQL with pgvector at `127.0.0.1:5433`, and a reachable
Trussium runtime. The service also exposes
`GET http://127.0.0.1:8000/health/live`. PostgreSQL data is stored in a named
Docker volume. The browser and CLI call the separately configured Trussium
runtime APIs.

### Read-only MCP tool endpoint

The application includes an opt-in authenticated MCP endpoint at
`POST /v1/mcp`. It is unavailable unless `KNOWLEDGE_AGENT_TOOL_TOKEN` is set.
Generate a unique high-entropy secret (for example, `openssl rand -hex 32`),
store it in a secret manager or ignored local `.env`, and configure the same
value in the trusted Trussium runtime composition. Never commit the secret.

The fixed `docs.search` tool accepts a query up to 4,000 characters and a result
limit from 1 to 10, returning retrieved passages with source paths, headings,
revisions, and scores. The fixed `docs.audit_links` tool performs a deterministic
read-only link check over the operator-configured `KNOWLEDGE_AGENT_AUDIT_ROOT`;
its only argument is a bounded `max_findings` value. `docs.review_guidance`
compares a supplied excerpt (up to 4,000 characters) with up to 10 independent
indexed passages and returns a cited potential-conflict assessment. It requires
PostgreSQL, an embedding model, and a chat model on the configured Trussium
runtime. This is advisory: revisions are opaque, freshness/authority are not
inferred, and confidence is qualitative and uncalibrated. It never reads the
caller-supplied source path or changes files. See
[`docs/TRUSSIUM_WORKFLOW.md`](docs/TRUSSIUM_WORKFLOW.md) for invocation and
privacy details. The link-audit tool does not accept paths or URLs from callers,
fetch network destinations, follow symlinks, or change files. It reports local
missing targets, unsafe paths, and missing Markdown anchors. External URLs are
ignored. Reference-style Markdown links and raw HTML links are not currently
checked. Scans are bounded by file count, bytes, link count, and findings. See
[`docs/DOCUMENTATION_AUDIT.md`](docs/DOCUMENTATION_AUDIT.md) for limits and
examples. Unknown tools and extra arguments are rejected. Indexed passages are
untrusted evidence; this endpoint does not interpret them as instructions or
perform writes. It does not expose issue creation, shell execution, arbitrary
URLs, or dynamic tool discovery. Request bodies are capped at 1 MiB, and
provider/database errors are returned as bounded JSON-RPC failures without
logging query or passage content.

The endpoint is intended for an explicitly registered fixed remote MCP tool in
a custom Trussium application. The packaged runtime command remains tool-free
unless its application composes that adapter. See
[`docs/adr/0004-cross-process-tool-boundary.md`](docs/adr/0004-cross-process-tool-boundary.md)
for the trust and deployment boundary.

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
Reranking remains planned. The browser sends questions to the local FastAPI
service; it shows the validated answer and source path/heading citations without
exposing local source files as public URLs. See
[`docs/WEB_INTERFACE.md`](docs/WEB_INTERFACE.md) for the HTTP contract,
configuration, error states, and deployment boundary.

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
