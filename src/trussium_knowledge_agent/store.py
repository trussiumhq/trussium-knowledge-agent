"""Versioned PostgreSQL schema and transactional source persistence."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import psycopg

from trussium_knowledge_agent.chunking import Chunk
from trussium_knowledge_agent.embeddings import MAX_VECTOR_DIMENSIONS

_MIGRATIONS = Path(__file__).parent / "migrations"
SourceKind = Literal["local", "github"]
SOURCE_QUARANTINE_DAYS = 30
MAX_PURGE_BATCH_SIZE = 1000


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
    source_kind: SourceKind = "local",
    source_identity: str | None = None,
) -> None:
    """Apply schema and atomically replace one source and its chunk records."""
    _validate_source_identity(source_kind, source_identity)
    with psycopg.connect(database_url) as connection:
        _apply_migrations(connection)
        cursor = connection.execute(
            """INSERT INTO knowledge_sources
                   (source_id, display_name, revision, indexed_at, source_kind, source_identity)
               VALUES (%s, %s, %s, now(), %s, %s)
               ON CONFLICT (source_id) DO UPDATE SET
                   display_name = EXCLUDED.display_name,
                   revision = EXCLUDED.revision,
                   indexed_at = now()
               WHERE knowledge_sources.source_kind = EXCLUDED.source_kind
                 AND knowledge_sources.source_identity IS NOT DISTINCT FROM
                     EXCLUDED.source_identity
                 AND knowledge_sources.lifecycle_state = 'active'""",
            (source_id, display_name, revision, source_kind, source_identity),
        )
        if cursor.rowcount != 1:
            raise ValueError("source identity mismatch or source is not active")
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
                        json.dumps(chunk.embedding, separators=(",", ":"), allow_nan=False),
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


def mark_source_access_lost(
    database_url: str,
    source_id: str,
) -> bool:
    """Hide a GitHub source from retrieval and start its fixed quarantine once."""
    with psycopg.connect(database_url) as connection:
        _apply_migrations(connection)
        cursor = connection.execute(
            """UPDATE knowledge_sources
               SET lifecycle_state = 'access_lost', access_lost_at = now(),
                   quarantine_expires_at = now() + (%s * interval '1 day')
               WHERE source_id = %s AND source_kind = 'github'
                 AND lifecycle_state = 'active'""",
            (SOURCE_QUARANTINE_DAYS, source_id),
        )
        return cursor.rowcount == 1


def reactivate_source(
    database_url: str,
    source_id: str,
    *,
    verified_identity: str,
) -> bool:
    """Reactivate only an unexpired GitHub source whose immutable identity matches."""
    _validate_source_identity("github", verified_identity)
    with psycopg.connect(database_url) as connection:
        _apply_migrations(connection)
        cursor = connection.execute(
            """UPDATE knowledge_sources
               SET lifecycle_state = 'active', access_lost_at = NULL,
                   quarantine_expires_at = NULL
               WHERE source_id = %s AND source_kind = 'github'
                 AND source_identity = %s AND lifecycle_state = 'access_lost'
                 AND quarantine_expires_at > now()""",
            (source_id, verified_identity),
        )
        return cursor.rowcount == 1


def purge_expired_sources(
    database_url: str,
    *,
    limit: int = MAX_PURGE_BATCH_SIZE,
) -> int:
    """Purge an ordered, bounded batch of sources whose quarantine has expired."""
    if not 1 <= limit <= MAX_PURGE_BATCH_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_PURGE_BATCH_SIZE}")
    with psycopg.connect(database_url) as connection:
        _apply_migrations(connection)
        cursor = connection.execute(
            """WITH expired AS (
                   SELECT source_id FROM knowledge_sources
                   WHERE lifecycle_state = 'access_lost'
                     AND quarantine_expires_at <= now()
                   ORDER BY quarantine_expires_at, source_id
                   LIMIT %s
                   FOR UPDATE SKIP LOCKED
               )
               DELETE FROM knowledge_sources AS source
               USING expired
               WHERE source.source_id = expired.source_id""",
            (limit,),
        )
        return cursor.rowcount


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
    exclude_source: tuple[str, str] | None = None,
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
    if exclude_source is not None and (
        not exclude_source[0].strip() or not exclude_source[1].strip()
    ):
        raise ValueError("excluded source name and path must be non-empty")
    vector_literal = json.dumps(query_vector, separators=(",", ":"), allow_nan=False)
    exclusion = ""
    parameters: list[Any] = [
        vector_literal,
        provider,
        model,
        len(query_vector),
    ]
    if exclude_source is not None:
        exclusion = "AND NOT (s.display_name = %s AND c.relative_path = %s)"
        parameters.extend(exclude_source)
    parameters.append(vector_literal)
    parameters.append(top_k)
    with psycopg.connect(database_url) as connection:
        _apply_migrations(connection)
        rows = connection.execute(
            f"""SELECT s.display_name, s.revision, c.relative_path, c.heading_path,
                      c.heading_anchor, c.content_hash, c.content,
                      1 - (c.embedding <=> %s::vector) AS score
               FROM document_chunks AS c
               JOIN knowledge_sources AS s USING (source_id)
               WHERE c.embedding IS NOT NULL
                 AND s.lifecycle_state = 'active'
                 AND c.embedding_provider = %s
                 AND c.embedding_model = %s
                 AND c.embedding_dimension = %s
                 {exclusion}
               ORDER BY c.embedding <=> %s::vector ASC, c.chunk_id ASC
               LIMIT %s""",
            tuple(parameters),
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


def _validate_source_identity(source_kind: SourceKind, source_identity: str | None) -> None:
    if source_kind not in ("local", "github"):
        raise ValueError("source_kind must be local or github")
    if source_kind == "local" and source_identity is not None:
        raise ValueError("local sources cannot have an external identity")
    if source_kind == "github" and (
        not isinstance(source_identity, str) or not 1 <= len(source_identity) <= 256
    ):
        raise ValueError("GitHub sources require a bounded immutable identity")
