"""Command-line interface for local Markdown indexing."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

import psycopg

from trussium_knowledge_agent.embeddings import TrussiumEmbeddingsClient
from trussium_knowledge_agent.indexer import index_source, remove_index
from trussium_knowledge_agent.retrieval import search_index


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trussium-knowledge-agent",
        description="Index explicitly selected local Markdown repositories.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    index = commands.add_parser("index", help="replace the index for a Markdown source")
    index.add_argument("source_root", help="path to the local Markdown repository")
    index.add_argument("--max-chars", type=int, default=1600, help="maximum chunk size")
    index.add_argument("--overlap-chars", type=int, default=200, help="chunk overlap size")
    search = commands.add_parser("search", help="find semantically similar passages")
    search.add_argument("query", help="question or phrase to search for")
    search.add_argument("--limit", type=_bounded_limit, default=5, help="number of results (1-20)")
    remove = commands.add_parser("remove", help="remove one previously indexed source")
    remove.add_argument("source_root", help="path originally used to index the source")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the indexing command and return a process exit status."""
    parser = _parser()
    arguments = parser.parse_args(argv)
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL is required (set it in .env and use uv run --env-file .env)")

    try:
        if arguments.command == "remove":
            removed = remove_index(arguments.source_root, database_url)
            print("Removed indexed source." if removed else "No indexed source matched that path.")
            return 0

        model = os.environ.get("TRUSSIUM_EMBEDDING_MODEL", "").strip()
        if not model or model == "replace-with-runtime-embedding-model":
            parser.error("TRUSSIUM_EMBEDDING_MODEL must name a model enabled for embeddings")
        base_url = os.environ.get("TRUSSIUM_URL", "http://127.0.0.1:9000")
        api_key = os.environ.get("TRUSSIUM_API_KEY") or None
        try:
            timeout_seconds = float(os.environ.get("TRUSSIUM_TIMEOUT_SECONDS", "30"))
        except ValueError:
            parser.error("TRUSSIUM_TIMEOUT_SECONDS must be a number from 0 to 120")
        with TrussiumEmbeddingsClient(
            base_url, api_key=api_key, timeout_seconds=timeout_seconds
        ) as embeddings_client:
            if arguments.command == "index":
                indexed = index_source(
                    arguments.source_root,
                    database_url,
                    embeddings_client,
                    model,
                    max_chars=arguments.max_chars,
                    overlap_chars=arguments.overlap_chars,
                )
                identity = (
                    f"{indexed.embedding_provider}/{indexed.embedding_model}, "
                    f"{indexed.embedding_dimension} dimensions"
                    if indexed.embedding_provider and indexed.embedding_model
                    else "no chunks"
                )
                print(
                    f"Indexed {indexed.file_count} Markdown files into {indexed.chunk_count} "
                    f"chunks (source {indexed.source_id[:12]}, {identity})."
                )
                return 0

            results = search_index(
                arguments.query,
                database_url,
                embeddings_client,
                model,
                top_k=arguments.limit,
            )
        if not results:
            print("No matching passages found for this embedding model.")
            return 0
        for rank, result in enumerate(results, start=1):
            heading = " > ".join(result.heading_path) or "Document"
            print(
                f"{rank}. {result.score:.4f} — {result.display_name}/"
                f"{result.relative_path}#{result.heading_anchor} ({heading})"
            )
            if result.revision:
                print(f"   Revision: {result.revision}")
            print(f"   {result.content}\n")
        return 0
    except psycopg.Error:
        print("Error: PostgreSQL operation failed; the prior index was preserved.", file=sys.stderr)
        return 1
    except (RuntimeError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


def _bounded_limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("limit must be an integer from 1 to 20") from error
    if not 1 <= limit <= 20:
        raise argparse.ArgumentTypeError("limit must be between 1 and 20")
    return limit


if __name__ == "__main__":
    raise SystemExit(main())
