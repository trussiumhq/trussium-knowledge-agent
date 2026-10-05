"""Versioned PostgreSQL schema and transactional source persistence."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

from trussium_knowledge_agent.chunking import Chunk
from trussium_knowledge_agent.embeddings import MAX_VECTOR_DIMENSIONS

_MIGRATIONS = Path(__file__).parent / "migrations"


def _apply_migrations(connection: psycopg.Connection[Any]) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
    )
    connection.execute("SELECT pg_advisory_xact_lock(%s, %s)", (741205, 1))
    for migration in sorted(_MIGRATIONS.glob("*.sql")):
        version = migration.stem
        applied = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = %s", (version,)
        ).fetchone()
        if applied is not None:
            continue
        connection.execute(migration.read_text(encoding="utf-8"))
        connection.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))


def persist_source(
    database_url: str,
    *,
    source_id: str,
    display_name: str,
    revision: str | None,
    chunks: list[Chunk],
) -> None:
    """Apply schema and atomically replace one source and its chunk records."""
    with psycopg.connect(database_url) as connection:
        _apply_migrations(connection)
        connection.execute(
            """INSERT INTO knowledge_sources (source_id, display_name, revision, indexed_at)
               VALUES (%s, %s, %s, now())
               ON CONFLICT (source_id) DO UPDATE SET
                   display_name = EXCLUDED.display_name,
                   revision = EXCLUDED.revision,
                   indexed_at = now()""",
            (source_id, display_name, revision),
        )
        connection.execute("DELETE FROM document_chunks WHERE source_id = %s", (source_id,))
        if chunks:
            expected_identity: tuple[str, str, int] | None = None
            for chunk in chunks:
                if (
                    chunk.embedding is None
                    or chunk.embedding_provider is None
                    or chunk.embedding_model is None
                ):
                    raise ValueError("every persisted chunk must have an embedding identity")
                vector = chunk.embedding
                if not 0 < len(vector) <= MAX_VECTOR_DIMENSIONS:
                    raise ValueError("embedding dimension is not supported")
                if not chunk.embedding_provider.strip() or not chunk.embedding_model.strip():
                    raise ValueError("embedding provider and model must be non-empty")
                if not all(math.isfinite(value) for value in vector):
                    raise ValueError("embedding values must be finite")
                if not any(value != 0.0 for value in vector):
                    raise ValueError("zero vectors cannot be used for cosine search")
                identity = (
                    chunk.embedding_provider,
                    chunk.embedding_model,
                    len(vector),
                )
                if expected_identity is None:
                    expected_identity = identity
                elif identity != expected_identity:
                    raise ValueError(
                        "all chunks in one source must use the same embedding identity"
                    )
            connection.cursor().executemany(
                """INSERT INTO document_chunks
                   (source_id, chunk_id, relative_path, heading_path, heading_anchor,
                    chunk_index, content_hash, content, embedding, embedding_provider,
                    embedding_model, embedding_dimension)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s, %s, %s)""",
                [
                    (
                        source_id,
                        chunk.chunk_id,
                        chunk.relative_path,
                        list(chunk.heading_path),
                        chunk.heading_anchor,
                        chunk.chunk_index,
                        chunk.content_hash,
                        chunk.content,
                        json.dumps(vector, separators=(",", ":"), allow_nan=False),
                        chunk.embedding_provider,
                        chunk.embedding_model,
                        len(vector),
                    )
                    for chunk in chunks
                ],
            )


def remove_source(database_url: str, source_id: str) -> bool:
    """Delete a source and its chunks with foreign-key cascade semantics."""
    with psycopg.connect(database_url) as connection:
        _apply_migrations(connection)
        cursor = connection.execute(
            "DELETE FROM knowledge_sources WHERE source_id = %s", (source_id,)
        )
        return cursor.rowcount > 0


def fetch_source_chunks(database_url: str, source_id: str) -> list[tuple[Any, ...]]:
    """Return ordered chunk metadata; primarily useful for diagnostics/tests."""
    with psycopg.connect(database_url) as connection:
        _apply_migrations(connection)
        rows = connection.execute(
            """SELECT chunk_id, relative_path, heading_path, heading_anchor, chunk_index,
                      content_hash, content, embedding_provider, embedding_model,
                      embedding_dimension, embedding::text
               FROM document_chunks WHERE source_id = %s
               ORDER BY relative_path, heading_anchor, chunk_index""",
            (source_id,),
        ).fetchall()
        return list(rows)


@dataclass(frozen=True, slots=True)
class SearchResult:
    """A ranked source passage returned by exact cosine search."""

    display_name: str
    revision: str | None
    relative_path: str
    heading_path: tuple[str, ...]
    heading_anchor: str
    content_hash: str
    content: str
    score: float


def search_chunks(
    database_url: str,
    *,
    query_vector: tuple[float, ...],
    provider: str,
    model: str,
    top_k: int = 5,
) -> list[SearchResult]:
    """Return bounded exact cosine matches for one embedding identity."""
    if not 1 <= top_k <= 20:
        raise ValueError("top_k must be between 1 and 20")
    if not 0 < len(query_vector) <= MAX_VECTOR_DIMENSIONS:
        raise ValueError("query vector dimension is not supported")
    if not provider.strip() or not model.strip():
        raise ValueError("provider and model must be non-empty")
    if not all(math.isfinite(value) for value in query_vector):
        raise ValueError("query vector values must be finite")
    if not any(value != 0.0 for value in query_vector):
        raise ValueError("zero vectors cannot be used for cosine search")
    vector_literal = json.dumps(query_vector, separators=(",", ":"), allow_nan=False)
    with psycopg.connect(database_url) as connection:
        _apply_migrations(connection)
        rows = connection.execute(
            """SELECT s.display_name, s.revision, c.relative_path, c.heading_path,
                      c.heading_anchor, c.content_hash, c.content,
                      1 - (c.embedding <=> %s::vector) AS score
               FROM document_chunks AS c
               JOIN knowledge_sources AS s USING (source_id)
               WHERE c.embedding IS NOT NULL
                 AND c.embedding_provider = %s
                 AND c.embedding_model = %s
                 AND c.embedding_dimension = %s
               ORDER BY c.embedding <=> %s::vector ASC, c.chunk_id ASC
               LIMIT %s""",
            (
                vector_literal,
                provider,
                model,
                len(query_vector),
                vector_literal,
                top_k,
            ),
        ).fetchall()
        return [
            SearchResult(
                display_name=row[0],
                revision=row[1],
                relative_path=row[2],
                heading_path=tuple(row[3]),
                heading_anchor=row[4],
                content_hash=row[5],
                content=row[6],
                score=float(row[7]),
            )
            for row in rows
        ]
