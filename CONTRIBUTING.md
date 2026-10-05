# Contributing

## Development setup

Use Python 3.12 or newer and `uv`:

```bash
cp .env.example .env
uv sync --all-groups
docker compose up -d postgres
uv run --env-file .env pytest
```

## Before opening a pull request

```bash
uv run ruff format .
uv run ruff check src tests
uv run mypy src tests
uv run --env-file .env pytest
```

The database-backed ingestion tests run when `DATABASE_URL` points to a
PostgreSQL instance. CI starts PostgreSQL with pgvector and runs those tests;
without a local database, the PostgreSQL integration tests are skipped.

Open or confirm an issue, create a focused branch from updated `main`, and
include the issue reference on the first line of the pull request as
`Closes #<issue-number>`. Pull requests should explain the motivation, change,
validation, security impact, and documentation updates. Use Conventional
Commit messages for commits that affect the project.

Never commit secrets, private source documents, generated embeddings, or local
database volumes.
