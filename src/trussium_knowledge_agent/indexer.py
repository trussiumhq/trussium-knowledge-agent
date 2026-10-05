"""Orchestrate local Markdown ingestion into PostgreSQL."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, replace
from pathlib import Path

from trussium_knowledge_agent.chunking import Chunk
from trussium_knowledge_agent.embeddings import EmbeddingsClient
from trussium_knowledge_agent.markdown import canonical_root, load_chunks
from trussium_knowledge_agent.store import persist_source, remove_source

EMBEDDING_BATCH_SIZE = 64


@dataclass(frozen=True, slots=True)
class IndexResult:
    """Counts and identity returned by a successful index operation."""

    source_id: str
    display_name: str
    revision: str | None
    file_count: int
    chunk_count: int
    embedding_provider: str | None = None
    embedding_model: str | None = None
    embedding_dimension: int | None = None


def source_id_for_root(source_root: str | Path, *, must_exist: bool = True) -> str:
    """Create a stable source key without storing its absolute local path."""
    root = canonical_root(source_root, must_exist=must_exist)
    normalized = os.path.normcase(str(root))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def index_source(
    source_root: str | Path,
    database_url: str,
    embeddings_client: EmbeddingsClient,
    model: str,
    *,
    max_chars: int = 1600,
    overlap_chars: int = 200,
    batch_size: int = EMBEDDING_BATCH_SIZE,
) -> IndexResult:
    """Embed a source before atomically replacing its database state."""
    if batch_size < 1 or batch_size > EMBEDDING_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {EMBEDDING_BATCH_SIZE}")
    root, revision, file_count, chunks = load_chunks(
        source_root, max_chars=max_chars, overlap_chars=overlap_chars
    )
    source_id = source_id_for_root(root)
    embedded_chunks: list[Chunk] = []
    identity: tuple[str, str, int] | None = None
    for start in range(0, len(chunks), batch_size):
        batch_chunks = chunks[start : start + batch_size]
        response = embeddings_client.embed(
            model=model,
            inputs=[_embedding_text(chunk) for chunk in batch_chunks],
        )
        batch_identity = (response.provider, response.model, response.dimension)
        if identity is None:
            identity = batch_identity
        elif batch_identity != identity:
            raise ValueError("Trussium returned inconsistent embedding identity across batches")
        if len(response.vectors) != len(batch_chunks):
            raise ValueError("Trussium returned a different number of vectors than inputs")
        embedded_chunks.extend(
            replace(
                chunk,
                embedding=vector,
                embedding_provider=response.provider,
                embedding_model=response.model,
            )
            for chunk, vector in zip(batch_chunks, response.vectors, strict=True)
        )
    persist_source(
        database_url,
        source_id=source_id,
        display_name=root.name or "Markdown source",
        revision=revision,
        chunks=embedded_chunks,
    )
    return IndexResult(
        source_id=source_id,
        display_name=root.name or "Markdown source",
        revision=revision,
        file_count=file_count,
        chunk_count=len(embedded_chunks),
        embedding_provider=identity[0] if identity else None,
        embedding_model=identity[1] if identity else None,
        embedding_dimension=identity[2] if identity else None,
    )


def remove_index(source_root: str | Path, database_url: str) -> bool:
    """Delete an explicitly named source index, even if its directory is gone."""
    source_id = source_id_for_root(source_root, must_exist=False)
    return remove_source(database_url, source_id)


def _embedding_text(chunk: Chunk) -> str:
    """Add source context to the body without modifying stored citation text."""
    relative_path = chunk.relative_path
    heading_path = chunk.heading_path
    content = chunk.content
    heading = " > ".join(heading_path) if heading_path else "(document preamble)"
    return f"Source: {relative_path}\nHeading: {heading}\n\n{content}"
