# Architecture

## Purpose

Trussium Knowledge Agent is a self-hostable reference application for searching
and maintaining Markdown documentation. The initial product answers questions
from a user's indexed documents and links each answer to its source. A later
bounded agent workflow audits the same corpus and prepares evidence-backed
maintenance issues for review.

## System boundaries

```text
Markdown source
    │ explicit indexing
    ▼
extract → chunk with heading/path metadata → Trussium embeddings → PostgreSQL/pgvector
                                                               │
Question → Trussium embeddings → similarity search → optional Trussium reranking
                                                               │
                                                               ▼
                                             Trussium chat with cited passages

Documentation audit → registered, bounded tools → report → human review
```

The application owns source access, parsing, chunking, storage, retrieval,
citation formatting, and the user interface. The Trussium runtime supplies
provider-neutral chat, embedding, and reranking APIs. PostgreSQL with pgvector
stores vectors alongside source identity and metadata. Provider credentials
remain configured on the runtime.

## Retrieval flow

1. A user explicitly chooses a local Markdown repository and starts indexing.
2. The indexer reads supported files beneath that configured root, records the
   repository revision and content hash, and splits content while preserving
   file paths and heading anchors.
3. The application requests embeddings from the configured Trussium runtime and
   stores chunks, metadata, and vectors in PostgreSQL.
4. For a question, the application embeds the query, retrieves a bounded set of
   matching chunks, and may ask Trussium to rerank that set.
5. The app sends only the selected passages and question to chat. Its response
   includes links to source files and headings; if evidence is insufficient it
   should say so.

Ingestion is repeatable and idempotent by source revision and content hash.
Only Markdown is in the first ingestion milestone. Other formats and external
connectors require separate parser, access-control, and deletion decisions.

## Agent workflow

The first automation audits documentation and returns a report with source
evidence. Its operations are explicitly registered, bounded, and audited. The
application coordinates retrieval and model calls; Trussium's workflow endpoint
executes only registered tool invocations and is enabled only when the
application composes a tool executor. The standard Trussium Python SDK does
not currently expose the workflow endpoint, so the implementation milestone
must decide whether to add that SDK method or use a narrowly scoped typed HTTP
client.

Tools begin read-only. Creating an issue or pull request is a later opt-in
operation that requires a human approval decision and a narrowly scoped GitHub
credential. No agent tool may execute arbitrary code or choose an unrestricted
filesystem path, URL, or shell command.

## Trust and privacy

- Indexed content and retrieved passages are untrusted data, never system
  instructions or authorization.
- File access stays beneath a user-selected source root; symbolic links and
  unsupported file types are handled explicitly.
- Prompts, retrieved content, credentials, and vectors are excluded from logs.
- Answers expose only citations for indexed source material available to the
  current user.
- External writes remain disabled until an explicit human approval step.
- Database and runtime endpoints are configurable; local defaults bind only to
  loopback interfaces.

## Local services

The starter Compose file runs PostgreSQL with the pgvector extension on
`127.0.0.1:5433`. The Trussium runtime remains an independently configured
service, normally on port `9000`. The FastAPI application runs on port `8000`.
Production deployments must supply managed secrets, backups, TLS, and a
network policy appropriate to their environment.
