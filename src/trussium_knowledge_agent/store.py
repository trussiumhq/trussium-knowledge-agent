"""Versioned PostgreSQL schema and transactional source persistence."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg

from trussium_knowledge_agent.chunking import Chunk

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
            connection.cursor().executemany(
                """INSERT INTO document_chunks
                   (source_id, chunk_id, relative_path, heading_path, heading_anchor,
                    chunk_index, content_hash, content)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
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
                      content_hash, content
               FROM document_chunks WHERE source_id = %s
               ORDER BY relative_path, heading_anchor, chunk_index""",
            (source_id,),
        ).fetchall()
        return list(rows)
