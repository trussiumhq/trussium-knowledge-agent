# ADR 0001: Application and RAG boundaries

## Status

Accepted for the initial reference application.

## Context

Trussium provides provider-neutral inference capabilities, controlled tool
execution, and a bounded workflow endpoint. It intentionally does not provide
document parsing, vector storage, indexing, or retrieval orchestration. The
reference project needs to demonstrate those pieces without moving application
data and user experience into the runtime.

## Decision

- Build the reference application in the public `trussium-knowledge-agent`
  repository with Python 3.12+, `uv`, and FastAPI.
- Use a separately configured Trussium runtime for chat, embeddings, and
  optional reranking.
- Use PostgreSQL with pgvector for vectors, source metadata, and chunks.
- Start with explicitly selected local Markdown repositories.
- Keep the application responsible for parsing, chunking, retrieval, citation
  formatting, and the user interface.
- Introduce agent automation as deterministic, bounded workflows over
  explicitly registered tools. Start read-only and require human approval for
  external writes.
- Expose cross-process tools only through an opt-in authenticated MCP endpoint
  with fixed tool names, schemas, and read-only behavior; do not accept
  request-selected destinations or dynamic registration.
- Run the database locally through Docker Compose and bind its published port
  to loopback.

## Consequences

PostgreSQL stores relational source metadata together with vectors and can
support ordinary text search alongside vector search. Operators run a database
in addition to the Trussium runtime. The first release supports Markdown only.
The first cross-process workflow slice is implemented: a custom runtime
application registers the Knowledge Agent's fixed read-only MCP tools, and the
Python SDK submits declared workflows to that configured runtime. The runtime
and SDK remain separate services/packages; the Knowledge Agent does not depend
on runtime internals. See
[`TRUSSIUM_WORKFLOW.md`](../TRUSSIUM_WORKFLOW.md).

## Alternatives considered

- **Put retrieval and vector storage in Trussium:** rejected because these are
  application-owned concerns and outside the runtime's current capability
  contract.
- **Use an in-memory index only:** rejected because persisted indexing,
  reproducible reindexing, and restart behavior are part of the useful example.
- **Add a hosted vector database:** deferred because local reproducibility and
  open deployment are the first goal.
- **Allow autonomous arbitrary tools:** rejected because tool access must remain
  explicit, bounded, and reviewable.

## References

- [Trussium embeddings contract](https://github.com/trussiumhq/trussium/blob/main/docs/EMBEDDINGS.md)
- [Trussium workflow lifecycle](https://github.com/trussiumhq/trussium/blob/main/docs/AGENT_RUNTIME_WORKFLOWS.md)
- [pgvector](https://github.com/pgvector/pgvector)
