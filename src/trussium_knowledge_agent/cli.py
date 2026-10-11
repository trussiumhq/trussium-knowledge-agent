"""Command-line interface for local Markdown indexing."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

import psycopg

from trussium_knowledge_agent.answers import answer_from_passages
from trussium_knowledge_agent.chat import TrussiumChatClient
from trussium_knowledge_agent.embeddings import TrussiumEmbeddingsClient
from trussium_knowledge_agent.evaluation import evaluate_queries, load_dataset
from trussium_knowledge_agent.guidance_review_evaluation import (
    evaluate_guidance_review_cases,
    load_guidance_review_cases,
)
from trussium_knowledge_agent.indexer import index_public_github_source, index_source, remove_index
from trussium_knowledge_agent.retrieval import search_index
from trussium_knowledge_agent.store import (
    purge_expired_sources,
    reconcile_source_lifecycle,
    remove_source,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trussium-knowledge-agent",
        description="Index and search explicitly selected local or public GitHub Markdown.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    index = commands.add_parser("index", help="replace the index for a Markdown source")
    index.add_argument("source_root", help="path to the local Markdown repository")
    index.add_argument("--max-chars", type=int, default=1600, help="maximum chunk size")
    index.add_argument("--overlap-chars", type=int, default=200, help="chunk overlap size")
    index_github = commands.add_parser(
        "index-github", help="snapshot and index an explicitly selected public GitHub repository"
    )
    index_github.add_argument("repository", help="public repository in owner/repository form")
    index_github.add_argument("--ref", required=True, help="branch, tag, or commit to snapshot")
    index_github.add_argument("--max-chars", type=int, default=1600, help="maximum chunk size")
    index_github.add_argument("--overlap-chars", type=int, default=200, help="chunk overlap size")
    search = commands.add_parser("search", help="find semantically similar passages")
    search.add_argument("query", help="question or phrase to search for")
    search.add_argument("--limit", type=_bounded_limit, default=5, help="number of results (1-20)")
    ask = commands.add_parser("ask", help="answer a question using retrieved source evidence")
    ask.add_argument("question", help="question to answer from indexed sources")
    ask.add_argument(
        "--limit", type=_bounded_answer_limit, default=5, help="evidence passages (1-10)"
    )
    evaluate = commands.add_parser(
        "evaluate", help="measure retrieval against expected source citations"
    )
    evaluate.add_argument("dataset", help="versioned JSON evaluation dataset")
    evaluate.add_argument(
        "--limit", type=_bounded_limit, default=5, help="retrieval results per query (1-20)"
    )
    review_evaluate = commands.add_parser(
        "evaluate-guidance", help="inspect guidance-review behavior on synthetic cases"
    )
    review_evaluate.add_argument("dataset", help="versioned guidance-review JSON dataset")
    remove = commands.add_parser("remove", help="remove one previously indexed source")
    remove.add_argument("source_root", help="path originally used to index the source")
    remove_by_id = commands.add_parser(
        "remove-source", help="remove a source by its full source ID"
    )
    remove_by_id.add_argument("source_id", help="full SHA-256 source ID printed by index commands")
    purge = commands.add_parser(
        "purge-expired-sources", help="purge sources past their access-loss retention deadline"
    )
    purge.add_argument(
        "--limit",
        type=_bounded_purge_limit,
        default=100,
        help="maximum expired sources to purge in one batch (1-1000)",
    )
    commands.add_parser(
        "reconcile-source-lifecycle",
        help="apply durable lifecycle restrictions to a restored database",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the indexing command and return a process exit status."""
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.command == "evaluate-guidance":
        return _run_guidance_review_evaluation(parser, arguments.dataset)
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL is required (set it in .env and use uv run --env-file .env)")

    try:
        if arguments.command == "remove":
            removed = remove_index(arguments.source_root, database_url)
            print("Removed indexed source." if removed else "No indexed source matched that path.")
            return 0
        if arguments.command == "remove-source":
            if len(arguments.source_id) != 64 or any(
                character not in "0123456789abcdef" for character in arguments.source_id
            ):
                parser.error("source_id must be a full lowercase SHA-256 ID")
            removed = remove_source(database_url, arguments.source_id)
            print("Removed indexed source." if removed else "No indexed source matched that ID.")
            return 0
        if arguments.command == "purge-expired-sources":
            purged = purge_expired_sources(database_url, limit=arguments.limit)
            print(f"Purged {purged} expired source(s).")
            return 0
        if arguments.command == "reconcile-source-lifecycle":
            ledger_path = os.environ.get("KNOWLEDGE_AGENT_LIFECYCLE_LEDGER_PATH")
            if not ledger_path:
                parser.error("KNOWLEDGE_AGENT_LIFECYCLE_LEDGER_PATH is required")
            deleted, quarantined, missing = reconcile_source_lifecycle(
                database_url, ledger_path=ledger_path
            )
            print(
                f"Reconciled source lifecycle: {deleted} deleted, {quarantined} quarantined, "
                f"{missing} already absent."
            )
            return 0

        model = os.environ.get("TRUSSIUM_EMBEDDING_MODEL", "").strip()
        if not model or model == "replace-with-runtime-embedding-model":
            parser.error("TRUSSIUM_EMBEDDING_MODEL must name a model enabled for embeddings")
        chat_model = os.environ.get("TRUSSIUM_CHAT_MODEL", "").strip()
        if arguments.command == "ask" and (
            not chat_model or chat_model == "replace-with-runtime-chat-model"
        ):
            parser.error("TRUSSIUM_CHAT_MODEL must name a model enabled for chat")
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

            if arguments.command == "index-github":
                indexed = index_public_github_source(
                    arguments.repository,
                    arguments.ref,
                    database_url,
                    embeddings_client,
                    model,
                    max_chars=arguments.max_chars,
                    overlap_chars=arguments.overlap_chars,
                )
                print(
                    f"Indexed public GitHub repository {indexed.display_name} at "
                    f"{indexed.revision}: {indexed.file_count} Markdown file(s), "
                    f"{indexed.chunk_count} chunk(s), source ID {indexed.source_id}."
                )
                return 0

            if arguments.command == "evaluate":
                dataset = load_dataset(arguments.dataset)
                report = evaluate_queries(
                    dataset,
                    lambda query, limit: search_index(
                        query, database_url, embeddings_client, model, top_k=limit
                    ),
                    limit=arguments.limit,
                )
                print(
                    f"Queries: {len(report.queries)} | Hit@{report.limit}: {report.hit_rate:.3f} | "
                    f"Recall@{report.limit}: {report.mean_recall:.3f} | "
                    f"MRR@{report.limit}: {report.mean_reciprocal_rank:.3f}"
                )
                for item in report.queries:
                    print(f"\n{item.id}: {item.query}")
                    print(f"  expected: {', '.join(item.expected)}")
                    print(f"  retrieved: {', '.join(item.retrieved) or '(none)'}")
                    print(
                        f"  hit={item.hit:.0f} recall={item.recall:.3f} "
                        f"reciprocal_rank={item.reciprocal_rank:.3f}"
                    )
                return 0

            query = arguments.question if arguments.command == "ask" else arguments.query
            results = search_index(
                query,
                database_url,
                embeddings_client,
                model,
                top_k=arguments.limit,
            )
        if arguments.command == "ask":
            with TrussiumChatClient(
                base_url, api_key=api_key, timeout_seconds=timeout_seconds
            ) as chat_client:
                answer = answer_from_passages(arguments.question, results, chat_client, chat_model)
            print(answer.answer)
            for citation in answer.citations:
                source = citation.source
                heading = " > ".join(source.heading_path) or "Document"
                print(
                    f"[{citation.reference_id}] {source.display_name}/{source.relative_path}"
                    f"#{source.heading_anchor} ({heading})"
                )
                if source.revision:
                    print(f"    Revision: {source.revision}")
            return 0
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
        print("Error: PostgreSQL operation failed.", file=sys.stderr)
        return 1
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


def _run_guidance_review_evaluation(parser: argparse.ArgumentParser, dataset_path: str) -> int:
    model = os.environ.get("TRUSSIUM_CHAT_MODEL", "").strip()
    if not model or model == "replace-with-runtime-chat-model":
        parser.error("TRUSSIUM_CHAT_MODEL must name a model enabled for chat")
    base_url = os.environ.get("TRUSSIUM_URL", "http://127.0.0.1:9000")
    api_key = os.environ.get("TRUSSIUM_API_KEY") or None
    try:
        timeout_seconds = float(os.environ.get("TRUSSIUM_TIMEOUT_SECONDS", "30"))
        cases = load_guidance_review_cases(dataset_path)
        with TrussiumChatClient(
            base_url, api_key=api_key, timeout_seconds=timeout_seconds
        ) as chat_client:
            results = evaluate_guidance_review_cases(cases, chat_client, model)
    except (RuntimeError, TypeError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    print(f"Guidance-review cases: {len(results)} | chat model: {model}")
    print("Manual inspection only; status/citation agreement is not calibrated accuracy.")
    for item in results:
        review = item.review
        print(f"\n{item.case.id}")
        print(f"  expected status: {item.case.expected_status}")
        print(f"  observed status: {review.status} (agreement: {item.status_agrees})")
        print(f"  confidence: {review.confidence} (qualitative, uncalibrated)")
        print(f"  expected citations: {', '.join(item.case.expected_citations) or '(none)'}")
        print(f"  observed citations: {', '.join(item.cited_locations) or '(none)'}")
        print(f"  citation agreement: {item.citations_agree}")
        print(f"  expected rationale: {item.case.rationale}")
        print(f"  assessment: {review.assessment}")
    return 0


def _bounded_limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("limit must be an integer from 1 to 20") from error
    if not 1 <= limit <= 20:
        raise argparse.ArgumentTypeError("limit must be between 1 and 20")
    return limit


def _bounded_answer_limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("limit must be an integer from 1 to 10") from error
    if not 1 <= limit <= 10:
        raise argparse.ArgumentTypeError("limit must be between 1 and 10")
    return limit


def _bounded_purge_limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("limit must be an integer from 1 to 1000") from error
    if not 1 <= limit <= 1000:
        raise argparse.ArgumentTypeError("limit must be between 1 and 1000")
    return limit


if __name__ == "__main__":
    raise SystemExit(main())
