"""Embedding-backed source retrieval."""

from __future__ import annotations

from trussium_knowledge_agent.embeddings import EmbeddingsClient
from trussium_knowledge_agent.store import SearchResult, search_chunks


def search_index(
    query: str,
    database_url: str,
    embeddings_client: EmbeddingsClient,
    model: str,
    *,
    top_k: int = 5,
) -> list[SearchResult]:
    """Embed one query and find nearest passages of the same model identity."""
    if not query.strip():
        raise ValueError("query must not be empty")
    query_embedding = embeddings_client.embed(model=model, inputs=[query])
    if len(query_embedding.vectors) != 1:
        raise ValueError("Trussium returned an unexpected number of query vectors")
    return search_chunks(
        database_url,
        query_vector=query_embedding.vectors[0],
        provider=query_embedding.provider,
        model=query_embedding.model,
        top_k=top_k,
    )
