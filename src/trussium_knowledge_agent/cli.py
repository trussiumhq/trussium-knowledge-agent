"""Command-line interface for local Markdown indexing."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence

from trussium_knowledge_agent.indexer import index_source, remove_index


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

    if arguments.command == "index":
        result = index_source(
            arguments.source_root,
            database_url,
            max_chars=arguments.max_chars,
            overlap_chars=arguments.overlap_chars,
        )
        print(
            f"Indexed {result.file_count} Markdown files into {result.chunk_count} chunks "
            f"(source {result.source_id[:12]}, revision {result.revision or 'unavailable'})."
        )
        return 0

    removed = remove_index(arguments.source_root, database_url)
    print("Removed indexed source." if removed else "No indexed source matched that path.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
