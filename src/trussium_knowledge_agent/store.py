"""Versioned PostgreSQL schema and transactional source persistence."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import psycopg

from trussium_knowledge_agent.chunking import Chunk
from trussium_knowledge_agent.embeddings import MAX_VECTOR_DIMENSIONS
from trussium_knowledge_agent.lifecycle_ledger import (
    LifecycleEvent,
    append_lifecycle_event,
    read_lifecycle_events,
)

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


def validate_public_demo_sources(database_url: str, allowed_source_ids: tuple[str, ...]) -> None:
    """Fail closed unless every explicitly allowed demo source is public and active."""
    if not allowed_source_ids or len(set(allowed_source_ids)) != len(allowed_source_ids):
        raise ValueError("public demo source allowlist must be non-empty and unique")
    with psycopg.connect(database_url) as connection:
        _apply_migrations(connection)
        rows = connection.execute(
            """SELECT source_id, github_installation_id, lifecycle_state
               FROM knowledge_sources WHERE source_id = ANY(%s)""",
            (list(allowed_source_ids),),
        ).fetchall()
    if len(rows) != len(allowed_source_ids):
        raise ValueError("public demo source allowlist contains a missing source")
    if any(installation_id is not None or state != "active" for _, installation_id, state in rows):
        raise ValueError(
            "public demo sources must be active and must not use private GitHub access"
        )


def reserve_public_demo_model_call(database_url: str, daily_limit: int) -> bool:
    """Atomically reserve one persistent UTC-day model-call unit before provider use."""
    if not 1 <= daily_limit <= 10_000:
        raise ValueError("daily model-call limit is outside its supported range")
    with psycopg.connect(database_url) as connection:
        _apply_migrations(connection)
        row = connection.execute(
            """INSERT INTO public_demo_model_usage (usage_day, model_calls)
               VALUES ((now() AT TIME ZONE 'UTC')::date, 1)
               ON CONFLICT (usage_day) DO UPDATE
                 SET model_calls = public_demo_model_usage.model_calls + 1,
                     updated_at = now()
                 WHERE public_demo_model_usage.model_calls < %s
               RETURNING model_calls""",
            (daily_limit,),
        ).fetchone()
    return row is not None


def persist_source(
    database_url: str,
    *,
    source_id: str,
    display_name: str,
    revision: str | None,
    chunks: list[Chunk],
    source_kind: SourceKind = "local",
    source_identity: str | None = None,
    github_installation_id: int | None = None,
    authorization_verified_until: datetime | None = None,
) -> None:
    """Apply schema and atomically replace one source and its chunk records."""
    _validate_source_identity(source_kind, source_identity)
    if github_installation_id is not None and (
        source_kind != "github"
        or isinstance(github_installation_id, bool)
        or github_installation_id <= 0
        or authorization_verified_until is None
        or authorization_verified_until.tzinfo is None
    ):
        raise ValueError(
            "private GitHub sources require an installation ID and authorization lease"
        )
    if github_installation_id is None and authorization_verified_until is not None:
        raise ValueError("authorization leases are only valid for private GitHub sources")
    if source_kind == "github":
        _configured_ledger_path(None)
    with psycopg.connect(database_url) as connection:
        _apply_migrations(connection)
        cursor = connection.execute(
            """INSERT INTO knowledge_sources
                   (source_id, display_name, revision, indexed_at, source_kind, source_identity,
                    github_installation_id, authorization_verified_until)
               VALUES (%s, %s, %s, now(), %s, %s, %s, %s)
               ON CONFLICT (source_id) DO UPDATE SET
                   display_name = EXCLUDED.display_name,
                   revision = EXCLUDED.revision,
                   indexed_at = now(),
                   github_installation_id = EXCLUDED.github_installation_id,
                   authorization_verified_until = EXCLUDED.authorization_verified_until,
                   lifecycle_state = 'active', access_lost_at = NULL,
                   quarantine_expires_at = NULL, deleted_at = NULL
               WHERE knowledge_sources.source_kind = EXCLUDED.source_kind
                 AND knowledge_sources.source_identity IS NOT DISTINCT FROM
                     EXCLUDED.source_identity
                 AND knowledge_sources.github_installation_id IS NOT DISTINCT FROM
                     EXCLUDED.github_installation_id
                 AND (knowledge_sources.lifecycle_state = 'active'
                      OR (knowledge_sources.lifecycle_state = 'access_lost'
                          AND EXCLUDED.github_installation_id IS NOT NULL
                          AND knowledge_sources.quarantine_expires_at > now()))""",
            (
                source_id,
                display_name,
                revision,
                source_kind,
                source_identity,
                github_installation_id,
                authorization_verified_until,
            ),
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
    ledger_path = _configured_ledger_path(None)
    with psycopg.connect(database_url) as connection:
        _apply_migrations(connection)
        row = connection.execute(
            """SELECT source_kind, source_identity, lifecycle_state
               FROM knowledge_sources WHERE source_id = %s FOR UPDATE""",
            (source_id,),
        ).fetchone()
        if row is None:
            return False
        source_kind, source_identity, lifecycle_state = row
        if lifecycle_state != "deleted":
            connection.execute(
                """UPDATE knowledge_sources
                   SET lifecycle_state = 'deleted', access_lost_at = NULL,
                       quarantine_expires_at = NULL, deleted_at = now()
                   WHERE source_id = %s""",
                (source_id,),
            )
    append_lifecycle_event(
        ledger_path,
        source_id=source_id,
        source_kind=source_kind,
        source_identity=source_identity,
        event_type="deleted",
    )
    with psycopg.connect(database_url) as connection:
        _apply_migrations(connection)
        connection.execute("DELETE FROM knowledge_sources WHERE source_id = %s", (source_id,))
    return True


def mark_source_access_lost(
    database_url: str,
    source_id: str,
) -> bool:
    """Hide a GitHub source from retrieval and start its fixed quarantine once."""
    ledger_path = _configured_ledger_path(None)
    with psycopg.connect(database_url) as connection:
        _apply_migrations(connection)
        cursor = connection.execute(
            """UPDATE knowledge_sources
               SET lifecycle_state = 'access_lost', access_lost_at = now(),
                   quarantine_expires_at = now() + (%s * interval '1 day'),
                   authorization_verified_until = NULL
               WHERE source_id = %s AND source_kind = 'github'
                 AND lifecycle_state = 'active'""",
            (SOURCE_QUARANTINE_DAYS, source_id),
        )
        changed = cursor.rowcount == 1
        row = connection.execute(
            """SELECT source_kind, source_identity, lifecycle_state
               FROM knowledge_sources WHERE source_id = %s""",
            (source_id,),
        ).fetchone()
        if row is None or row[0] != "github" or row[2] not in ("active", "access_lost"):
            return False
        source_kind, source_identity = row[:2]
    append_lifecycle_event(
        ledger_path,
        source_id=source_id,
        source_kind=source_kind,
        source_identity=source_identity,
        event_type="access_lost",
    )
    return changed


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
                 AND github_installation_id IS NULL
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
    ledger_path = _configured_ledger_path(None)
    with psycopg.connect(database_url) as connection:
        _apply_migrations(connection)
        rows = connection.execute(
            """WITH expired AS (
                   SELECT source_id FROM knowledge_sources
                   WHERE lifecycle_state = 'deleted'
                      OR (lifecycle_state = 'access_lost' AND quarantine_expires_at <= now())
                   ORDER BY COALESCE(quarantine_expires_at, deleted_at), source_id
                   LIMIT %s
                   FOR UPDATE SKIP LOCKED
               )
               UPDATE knowledge_sources AS source
               SET lifecycle_state = 'deleted', access_lost_at = NULL,
                   quarantine_expires_at = NULL,
                   deleted_at = COALESCE(source.deleted_at, now())
               FROM expired
               WHERE source.source_id = expired.source_id
               RETURNING source.source_id, source.source_kind, source.source_identity""",
            (limit,),
        ).fetchall()
    for source_id, source_kind, source_identity in rows:
        append_lifecycle_event(
            ledger_path,
            source_id=source_id,
            source_kind=source_kind,
            source_identity=source_identity,
            event_type="deleted",
        )
    if rows:
        with psycopg.connect(database_url) as connection:
            _apply_migrations(connection)
            connection.cursor().executemany(
                "DELETE FROM knowledge_sources WHERE source_id = %s",
                [(row[0],) for row in rows],
            )
    return len(rows)


def list_private_github_sources(
    database_url: str, installation_id: int
) -> list[tuple[str, int, str]]:
    """List source ID, immutable repository ID, and identity for one installation."""
    if isinstance(installation_id, bool) or installation_id <= 0:
        raise ValueError("installation ID must be a positive integer")
    with psycopg.connect(database_url) as connection:
        _apply_migrations(connection)
        rows = connection.execute(
            """SELECT source_id, source_identity
               FROM knowledge_sources
               WHERE source_kind = 'github' AND github_installation_id = %s
                 AND lifecycle_state IN ('active', 'access_lost')
               ORDER BY source_id""",
            (installation_id,),
        ).fetchall()
    results: list[tuple[str, int, str]] = []
    for source_id, identity in rows:
        prefix = "github.com:repository:"
        if not isinstance(identity, str) or not identity.startswith(prefix):
            raise ValueError("private GitHub source has an invalid repository identity")
        repository_id_text = identity.removeprefix(prefix)
        if not repository_id_text.isdecimal() or int(repository_id_text) <= 0:
            raise ValueError("private GitHub source has an invalid repository identity")
        results.append((source_id, int(repository_id_text), identity))
    return results


def verify_private_source_authorization(
    database_url: str,
    source_id: str,
    installation_id: int,
    verified_until: datetime,
) -> bool:
    """Extend a lease only for an already active source in the same installation."""
    if verified_until.tzinfo is None:
        raise ValueError("authorization lease must be timezone-aware")
    with psycopg.connect(database_url) as connection:
        _apply_migrations(connection)
        cursor = connection.execute(
            """UPDATE knowledge_sources
               SET authorization_verified_until = %s
               WHERE source_id = %s AND source_kind = 'github'
                 AND github_installation_id = %s AND lifecycle_state = 'active'""",
            (verified_until, source_id, installation_id),
        )
        return cursor.rowcount == 1


def record_github_webhook_delivery(database_url: str, delivery_id: str) -> bool:
    """Record a completed webhook delivery exactly once."""
    if not delivery_id or len(delivery_id) > 100 or any(ord(char) < 33 for char in delivery_id):
        raise ValueError("GitHub webhook delivery ID is invalid")
    with psycopg.connect(database_url) as connection:
        _apply_migrations(connection)
        connection.execute(
            "DELETE FROM github_webhook_deliveries WHERE received_at < now() - interval '30 days'"
        )
        cursor = connection.execute(
            """INSERT INTO github_webhook_deliveries (delivery_id)
               VALUES (%s) ON CONFLICT (delivery_id) DO NOTHING""",
            (delivery_id,),
        )
        return cursor.rowcount == 1


def github_webhook_delivery_recorded(database_url: str, delivery_id: str) -> bool:
    """Check whether a webhook delivery has completed successfully before."""
    with psycopg.connect(database_url) as connection:
        _apply_migrations(connection)
        return (
            connection.execute(
                "SELECT 1 FROM github_webhook_deliveries WHERE delivery_id = %s",
                (delivery_id,),
            ).fetchone()
            is not None
        )


def reconcile_source_lifecycle(
    database_url: str, *, ledger_path: str | Path | None = None
) -> tuple[int, int, int]:
    """Apply external deletion/access-loss records before a restored DB serves traffic.

    Returns (deleted, quarantined, missing) counts. Reconciliation is one
    transaction and deliberately never reactivates a source.
    """
    events = read_lifecycle_events(_configured_ledger_path(ledger_path))
    latest: dict[str, LifecycleEvent] = {}
    for event in events:
        latest[event.source_id] = event
    deleted = quarantined = missing = 0
    with psycopg.connect(database_url) as connection:
        _apply_migrations(connection)
        for source_id, event in latest.items():
            row = connection.execute(
                """SELECT source_kind, source_identity, lifecycle_state
                   FROM knowledge_sources WHERE source_id = %s FOR UPDATE""",
                (source_id,),
            ).fetchone()
            if row is None:
                missing += 1
                continue
            if row[0] != event.source_kind or row[1] != event.source_identity:
                raise ValueError(
                    "lifecycle ledger source identity does not match restored database"
                )
            if event.event_type == "deleted":
                deleted += connection.execute(
                    "DELETE FROM knowledge_sources WHERE source_id = %s", (source_id,)
                ).rowcount
            elif row[2] == "active":
                if row[0] != "github":
                    raise ValueError("only GitHub sources can be quarantined for access loss")
                connection.execute(
                    """UPDATE knowledge_sources
                       SET lifecycle_state = 'access_lost', access_lost_at = now(),
                           quarantine_expires_at = now() + (%s * interval '1 day'),
                           deleted_at = NULL, authorization_verified_until = NULL
                       WHERE source_id = %s""",
                    (SOURCE_QUARANTINE_DAYS, source_id),
                )
                quarantined += 1
    return deleted, quarantined, missing


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
    allowed_source_ids: tuple[str, ...] | None = None,
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
    if allowed_source_ids is not None and (
        not allowed_source_ids or len(set(allowed_source_ids)) != len(allowed_source_ids)
    ):
        raise ValueError("allowed source IDs must be non-empty and unique")
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
    source_filter = ""
    if allowed_source_ids is not None:
        source_filter = "AND s.source_id = ANY(%s)"
        parameters.append(list(allowed_source_ids))
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
                 AND (s.github_installation_id IS NULL
                      OR s.authorization_verified_until > now())
                 AND c.embedding_provider = %s
                 AND c.embedding_model = %s
                 AND c.embedding_dimension = %s
                 {exclusion}
                 {source_filter}
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


def _configured_ledger_path(path: str | Path | None) -> Path:
    configured = (
        path if path is not None else os.environ.get("KNOWLEDGE_AGENT_LIFECYCLE_LEDGER_PATH")
    )
    if not configured:
        raise ValueError(
            "KNOWLEDGE_AGENT_LIFECYCLE_LEDGER_PATH must point to durable storage "
            "outside PostgreSQL backups"
        )
    return Path(configured)
