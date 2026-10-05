# Roadmap

This roadmap tracks the public reference application. Each milestone should
ship as a small, reviewable change with tests and user documentation.

## Milestone 0 — Repository foundation

Status: in progress.

- Establish project purpose, architecture, trust boundaries, and ADRs.
- Provide a Python 3.12+ `uv` project and a minimal FastAPI health endpoint.
- Provide local PostgreSQL with pgvector through Docker Compose.
- Add CI, dependency/source security checks, contribution guidance, and
  reproducible local setup instructions.

Exit criteria: fresh clone can install, start PostgreSQL and the app, receive a
successful liveness response, and pass all CI checks.

## Milestone 1 — Markdown RAG

Planned:

- Index an explicitly selected local Markdown repository.
- Preserve repository revision, relative file path, heading anchor, and content
  hash for each chunk.
- Make ingestion idempotent and support deleting/rebuilding an index.
- Use Trussium embeddings and PostgreSQL/pgvector retrieval; expose retrieval
  scores and source links.
- Answer with grounded citations and a clear insufficient-evidence response.
- Provide CLI commands and a usable browser question/search interface.
- Include a small evaluation corpus for retrieval relevance and citation
  correctness.

Exit criteria: a user can index the sample documentation, ask questions, open
each cited source, and rebuild or remove the index using documented commands.

## Milestone 2 — Bounded documentation audit agent

Planned:

- Define a deterministic documentation audit that uses retrieved evidence and
  registered read-only tools.
- Run the bounded workflow through an opt-in Trussium runtime composition.
- Report suspected stale or missing guidance with citations and confidence;
  never modify a repository automatically.
- Add human-reviewed issue creation only after a clear approval interaction,
  scoped credential setup, audit events, and failure recovery are documented.
- Test prompt injection, denied tools, approval timeout, cancellation, and
  source access boundaries.

Exit criteria: no external write occurs without a recorded approval, and each
finding can be traced to source evidence and a deterministic check.

## Milestone 3 — Evaluation and deployment guidance

Planned:

- Publish reproducible retrieval and answer-quality evaluation fixtures.
- Document self-hosted deployment, database backup/restore, configuration,
  health checks, and troubleshooting.
- Add optional source connectors only when their access and deletion semantics
  are documented and tested.

## Out of scope until separately proposed

- Multi-tenant hosted service, billing, or managed storage.
- Unrestricted autonomous agents, unbounded loops, or implicit tool discovery.
- Arbitrary shell execution or broad filesystem/network access.
- Multi-agent coordination, durable workflow scheduling, or agent memory.
- Non-Markdown ingestion before access control and parser risks are reviewed.
