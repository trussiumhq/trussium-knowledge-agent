# Browser question interface

The local browser interface sends questions to the FastAPI application, which
uses the same retrieval and citation-validation path as the CLI `ask` command.
It does not index documents; index a source first using the documented CLI
command.

## Start and configure

Set `DATABASE_URL`, `TRUSSIUM_URL`, `TRUSSIUM_EMBEDDING_MODEL`, and
`TRUSSIUM_CHAT_MODEL` in the ignored local `.env` file. Configure
`TRUSSIUM_API_KEY` only when the runtime requires bearer authentication. Start
PostgreSQL and the app:

```bash
docker compose --env-file .env up -d postgres
uv run --env-file .env uvicorn trussium_knowledge_agent.app:app --reload --port 8000
```

Open <http://127.0.0.1:8000/>. The application must be able to reach the
configured runtime and database. The CLI remains available for indexing,
search, asking, and evaluation.

## Question API

The browser posts JSON to `POST /api/ask`:

```json
{
  "question": "How do I configure the embedding model?",
  "limit": 5
}
```

`question` is trimmed, required, and limited to 4,000 characters. `limit` is
optional and defaults to 5; valid values are 1 through 10. Extra request fields
are rejected. The success response contains `status`, `answer`, and `citations`.
Each citation includes its validated reference ID, repository-relative path,
heading path, heading anchor, and optional indexed Git revision. Citation paths
are display-only: the interface does not construct links to local files or
serve arbitrary source paths.

The service returns HTTP 422 for invalid request data and HTTP 503 with a
generic message when runtime, database, model, or local configuration is
unavailable. Internal exception details are not returned to the browser. Empty
retrieval is a successful `insufficient_evidence` response with no citations;
the interface explains that more relevant material may need to be indexed or
the question rephrased. Invalid model answers continue to fail closed through
the existing citation validator.

## Security and deployment boundary

The browser UI is a minimal local reference interface, not an authenticated
multi-user application. The development command binds Uvicorn to loopback by
default. Do not expose the app directly to an untrusted network. A deployment
that is reachable by other users must add authentication, authorization,
request controls, and TLS at an appropriate trusted boundary before enabling
access to private indexed content. Keep runtime credentials in a secret store
or ignored local environment file; never put them in browser code.

The page uses same-origin requests and renders answer/citation values using DOM
text nodes. It does not interpret model output as HTML or allow the model to
choose source links.
