# Architecture

## Purpose

Trussium Knowledge Agent is a self-hostable reference application for searching
and maintaining Markdown documentation. The initial product answers questions
from a user's indexed documents and identifies each cited source by path and
heading. Its first bounded workflow slice can run explicitly registered,
read-only search and documentation-audit tools; it does not automatically
propose or apply maintenance changes.

## System boundaries

```text
Markdown source
    │ explicit indexing
    ▼
extract → chunk with heading/path metadata → Trussium embeddings → PostgreSQL/pgvector
                                                               │
Question → Trussium embeddings → exact cosine similarity search
                                                               │
                                                               ▼
                                             Trussium chat with cited passages
                                                               │
                                                               ▼
                                           FastAPI answer API → browser citations

Documentation audit → registered, bounded tools → report → human review
```

The application owns source access, parsing, chunking, storage, retrieval,
citation formatting, and the user interface. It uses Trussium's
provider-neutral chat and embedding APIs; integrating the runtime's reranking
API remains future work. PostgreSQL with pgvector stores vectors alongside
source identity and metadata. Provider credentials remain configured on the
runtime.

## Retrieval flow

1. A user explicitly chooses a local Markdown repository and starts indexing.
2. The indexer reads Markdown files beneath that configured root without
   following symlinks, records the optional Git revision and source content
   hash, and splits content while preserving relative paths and heading
   anchors.
3. Source metadata and chunks are replaced in a single PostgreSQL transaction.
   Re-indexing unchanged content does not create duplicates; a failed index
   leaves the last successful corpus intact.
4. The indexer batches chunk text to the configured Trussium embeddings API,
   validates response order and vector identity, and stores vectors together
   with provider, resolved model, and dimension metadata.
5. For a query, the application requests one embedding and retrieves up to 20
   nearest passages by exact cosine similarity. Results are filtered to the
   same provider/model/dimension and include source metadata and a score.
6. The `ask` flow sends the question and at most ten retrieved passages to
   Trussium chat. The application validates a bounded JSON answer, citation
   markers, and IDs; it formats source details from database metadata rather
   than trusting model-generated links. Empty retrieval or invalid output
   produces a fixed insufficient-evidence message.
7. The browser uses the same bounded answer flow through `POST /api/ask`. The
   API returns only the validated answer and stored citation metadata. Browser
   rendering treats answer and source strings as text, not HTML; it displays
   source paths and heading anchors without serving arbitrary local files.

Ingestion is repeatable and idempotent for an explicitly selected local source.
Only Markdown is supported initially. Other formats and external connectors
require separate parser, access-control, and deletion decisions.

The first implementation stores only a SHA-256 identifier derived from the
canonical local source path, not the absolute path itself. It records the
repository name, optional Git `HEAD` revision, per-file content hashes,
relative paths, heading paths/anchors, and deterministic chunk IDs. Index
replacement and migration application run in one database transaction. A
Markdown file larger than 5 MiB or one that is not valid UTF-8 causes indexing
to fail before database state is changed. `.git` metadata is excluded.

Vectors are stored in pgvector's variable-dimension `vector` type. Each row
records provider, resolved model, and dimension, and retrieval filters on all
three before comparing vectors. The initial corpus uses exact cosine search;
it does not add an approximate index before model dimensions and measured corpus
size justify one. The pgvector extension must be provisioned before the
application migration runs: Compose enables it during database initialization,
CI enables it for its PostgreSQL service, and external database operators must
enable the extension with an appropriately privileged role.

The CLI uses Trussium's stable HTTP embeddings contract rather than depending
on a Git-sourced SDK package. The runtime URL and model are operator
configuration. Requests use a bounded configurable timeout (30 seconds by
default, at most 120 seconds); no provider-specific endpoint is called by this
application. Chat uses the normalized `POST /v1/chat/completions` contract and
has a separately configured model from embeddings.

## Bounded Trussium workflow integration

The application exposes authenticated, fixed MCP tools for indexed-document
search, deterministic Markdown link auditing, and evidence-bounded review of a
supplied guidance excerpt. The audit checks links and anchors beneath one
operator-configured root and returns stable findings with relative paths and
line numbers. Guidance review compares the supplied excerpt with independent
indexed passages and validates every returned citation. It is advisory: opaque
revision identifiers do not establish freshness or authority, confidence is
qualitative and uncalibrated, and the supplied path is metadata only. Neither
tool fetches caller-provided URLs or modifies files.

A custom Trussium runtime application registers these remote tools explicitly
with `RemoteMCPTool` and the runtime's `ToolRegistry`. A caller then submits a
bounded, declared workflow through the Python SDK's `execute_workflow` method.
The runtime executes only registered tool names; it does not discover tools at
request time. See [`TRUSSIUM_WORKFLOW.md`](TRUSSIUM_WORKFLOW.md) for setup and
examples. This is deterministic orchestration, not an autonomous planner: it
does not infer new steps, make repository changes, or create GitHub issues or
pull requests.

Tools remain read-only. Creating an issue or pull request is not implemented;
it would require a separately designed human approval interaction and a
narrowly scoped GitHub credential. No tool may execute arbitrary code or
choose an unrestricted filesystem path, URL, or shell command.

## Trust and privacy

- Indexed content and retrieved passages are untrusted data, never system
  instructions or authorization.
- File access stays beneath a user-selected source root; symbolic links and
  unsupported file types are handled explicitly.
- Prompts, retrieved content, credentials, and vectors are excluded from logs.
- Retrieved passages are untrusted; answers must cite retrieved passage IDs,
  and model-generated source URLs are never authoritative.
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
