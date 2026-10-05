# Contributing

## Development setup

Use Python 3.12 or newer and `uv`:

```bash
cp .env.example .env
uv sync --all-groups
docker compose up -d postgres
```

## Before opening a pull request

```bash
uv run ruff format .
uv run ruff check src tests
uv run mypy src tests
uv run pytest
```

Open or confirm an issue, create a focused branch from updated `main`, and
include the issue reference on the first line of the pull request as
`Closes #<issue-number>`. Pull requests should explain the motivation, change,
validation, security impact, and documentation updates. Use Conventional
Commit messages for commits that affect the project.

Never commit secrets, private source documents, generated embeddings, or local
database volumes.
