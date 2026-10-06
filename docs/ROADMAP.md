# Roadmap

This roadmap tracks the public reference application. Each milestone should
ship as a small, reviewable change with tests and user documentation.

## Milestone 0 — Repository foundation

Status: complete.

- Establish project purpose, architecture, trust boundaries, and ADRs.
- Provide a Python 3.12+ `uv` project and a minimal FastAPI health endpoint.
- Provide local PostgreSQL with pgvector through Docker Compose.
- Add CI, dependency/source security checks, contribution guidance, and
  reproducible local setup instructions.

Exit criteria: a fresh clone can install, configure PostgreSQL and the app,
receive a successful liveness response, and pass all CI checks. PostgreSQL
container startup was validated in CI; local validation depends on Docker being
available.

## Milestone 1 — Markdown RAG

In progress. Safe Markdown indexing, transactional chunk storage, embeddings,
semantic retrieval, grounded cited answers, and a retrieval evaluation corpus
are complete. The remaining work is the browser question/search interface.

- [x] Safely ingest explicitly selected local Markdown repositories and
  transactionally persist heading-aware chunks and source metadata.
- [x] Add deterministic migrations and documented index, re-index, and removal
  commands.
- [x] Call Trussium embeddings and store vectors with provider/model/dimension
  identity; add bounded exact-cosine retrieval with source citations.
- [x] Generate grounded answers with citations validated against retrieved
  sources and a clear insufficient-evidence response.
- [x] Include a bounded evaluation corpus and report retrieval Hit@k, Recall@k,
  MRR@k, and per-query expected/retrieved citations.

Remaining planned work:

- Provide a usable browser question/search interface.

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

- Extend evaluation with reviewed answer-quality examples only when a safe,
  reproducible scoring method is defined; current retrieval metrics do not
  assess generated answer truth or completeness.
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
