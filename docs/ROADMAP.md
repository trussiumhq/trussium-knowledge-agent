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

Status: complete. Safe Markdown indexing, transactional chunk storage,
embeddings, semantic retrieval, grounded cited answers, a retrieval evaluation
corpus, and a minimal browser question interface are complete.

- [x] Safely ingest explicitly selected local Markdown repositories and
  transactionally persist heading-aware chunks and source metadata.
- [x] Add deterministic migrations and documented index, re-index, and removal
  commands.
- [x] Call Trussium embeddings and store vectors with provider/model/dimension
  identity; add bounded exact-cosine retrieval with source citations.
- [x] Preserve each chunk's own vector during bulk persistence and guard the
  mapping with a database-backed regression test.
- [x] Generate grounded answers with citations validated against retrieved
  sources and a clear insufficient-evidence response.
- [x] Include a bounded evaluation corpus and report retrieval Hit@k, Recall@k,
  MRR@k, and per-query expected/retrieved citations.
- [x] Provide a browser question interface with bounded requests, safe answer
  rendering, citation metadata, and insufficient-evidence/error states.

Exit criteria: a user can index the sample documentation, ask questions,
inspect each citation's source path and heading, and rebuild or remove the
index using documented commands. Browser citations do not serve or link to
arbitrary local source files.

## Milestone 2 — Bounded documentation audit workflow

Status: first read-only workflow slice complete; semantic audit and approved
write capabilities remain future work.

- [x] Add a bounded local Markdown link audit with stable findings, source
  locations, no caller-controlled path, and no automatic source changes.
- [x] Expose fixed, authenticated MCP tools for indexed document search and
  deterministic link auditing.
- [x] Register the remote tools explicitly in a custom Trussium runtime
  application and call them through the typed Python SDK workflow API.
- [x] Validate the local SDK-to-runtime-to-Knowledge-Agent audit workflow.
- [x] Add evidence-backed candidate conflict review for supplied guidance
  excerpts, with validated citations and a read-only review boundary. This does
  not determine which source is newer or authoritative; confidence is
  qualitative and uncalibrated.
- [ ] Consider human-approved issue creation only after approval UX, narrowly
  scoped credentials, audit events, and recovery behavior are designed.
- [ ] Expand end-to-end checks for denied tools, cancellation, timeout, and
  source-access boundaries as workflow behavior grows.

Exit criteria for the completed slice: the runtime invokes only explicitly
registered tools; the audit is bounded and read-only; findings use
root-relative source locations; and the workflow can be traced through runtime
execution results. Candidate conflict review is advisory and does not establish
freshness. External writes are not part of this completed slice. See
[`TRUSSIUM_WORKFLOW.md`](TRUSSIUM_WORKFLOW.md) for setup and limits.

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
