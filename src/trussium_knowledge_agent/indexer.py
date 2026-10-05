"""Orchestrate local Markdown ingestion into PostgreSQL."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from trussium_knowledge_agent.markdown import canonical_root, load_chunks
from trussium_knowledge_agent.store import persist_source, remove_source


@dataclass(frozen=True, slots=True)
class IndexResult:
    """Counts and identity returned by a successful index operation."""

    source_id: str
    display_name: str
    revision: str | None
    file_count: int
    chunk_count: int


def source_id_for_root(source_root: str | Path, *, must_exist: bool = True) -> str:
    """Create a stable source key without storing its absolute local path."""
    root = canonical_root(source_root, must_exist=must_exist)
    normalized = os.path.normcase(str(root))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def index_source(
    source_root: str | Path,
    database_url: str,
    *,
    max_chars: int = 1600,
    overlap_chars: int = 200,
) -> IndexResult:
    """Read and chunk a source, then replace its database state atomically."""
    root, revision, file_count, chunks = load_chunks(
        source_root, max_chars=max_chars, overlap_chars=overlap_chars
    )
    source_id = source_id_for_root(root)
    persist_source(
        database_url,
        source_id=source_id,
        display_name=root.name or "Markdown source",
        revision=revision,
        chunks=chunks,
    )
    return IndexResult(
        source_id=source_id,
        display_name=root.name or "Markdown source",
        revision=revision,
        file_count=file_count,
        chunk_count=len(chunks),
    )


def remove_index(source_root: str | Path, database_url: str) -> bool:
    """Delete an explicitly named source index, even if its directory is gone."""
    source_id = source_id_for_root(source_root, must_exist=False)
    return remove_source(database_url, source_id)
