# Local setup

## Database

Start PostgreSQL with pgvector by running `docker compose up -d postgres`. The
default local database listens on port 5433.

## Runtime

Set `TRUSSIUM_URL` to a reachable Trussium runtime and choose an embeddings-
enabled model with `TRUSSIUM_EMBEDDING_MODEL`. Set `TRUSSIUM_CHAT_MODEL` for
grounded answer generation.
