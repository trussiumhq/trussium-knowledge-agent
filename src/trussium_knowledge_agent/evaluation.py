"""Deterministic retrieval evaluation against expected source citations."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from trussium_knowledge_agent.store import SearchResult

MAX_DATASET_BYTES = 1024 * 1024
MAX_EVALUATION_QUERIES = 100
MAX_EXPECTED_PASSAGES = 10


@dataclass(frozen=True, slots=True)
class ExpectedPassage:
    """A stable source citation expected in a retrieval result."""

    path: str
    anchor: str

    @property
    def citation(self) -> str:
        return f"{self.path}#{self.anchor}"


@dataclass(frozen=True, slots=True)
class EvaluationQuery:
    """One bounded query and its expected evidence citations."""

    id: str
    query: str
    expected: tuple[ExpectedPassage, ...]


@dataclass(frozen=True, slots=True)
class QueryEvaluation:
    """Retrieval results and relevance metrics for one evaluation query."""

    id: str
    query: str
    expected: tuple[str, ...]
    retrieved: tuple[str, ...]
    hit: float
    recall: float
    reciprocal_rank: float


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    """Aggregate and per-query retrieval metrics."""

    limit: int
    queries: tuple[QueryEvaluation, ...]
    hit_rate: float
    mean_recall: float
    mean_reciprocal_rank: float


def load_dataset(path: str | Path) -> tuple[EvaluationQuery, ...]:
    """Load and validate a small versioned JSON retrieval dataset."""
    dataset_path = Path(path)
    try:
        if dataset_path.stat().st_size > MAX_DATASET_BYTES:
            raise ValueError("evaluation dataset exceeds the 1 MiB limit")
        raw = dataset_path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read evaluation dataset: {error}") from error
    if not isinstance(payload, dict) or set(payload) != {"version", "queries"}:
        raise ValueError("dataset must contain only 'version' and 'queries'")
    if payload["version"] != 1:
        raise ValueError("dataset version must be 1")
    rows = payload["queries"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_EVALUATION_QUERIES:
        raise ValueError("dataset must contain between 1 and 100 queries")

    queries: list[EvaluationQuery] = []
    seen_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"id", "query", "expected"}:
            raise ValueError("each query must contain only 'id', 'query', and 'expected'")
        identifier = _bounded_text(row["id"], "query id", 80)
        query = _bounded_text(row["query"], "query", 4000)
        if identifier in seen_ids:
            raise ValueError(f"duplicate query id: {identifier}")
        seen_ids.add(identifier)
        expected_rows = row["expected"]
        if (
            not isinstance(expected_rows, list)
            or not 1 <= len(expected_rows) <= MAX_EXPECTED_PASSAGES
        ):
            raise ValueError(f"query {identifier} must expect between 1 and 10 passages")
        expected: list[ExpectedPassage] = []
        for item in expected_rows:
            if not isinstance(item, dict) or set(item) != {"path", "anchor"}:
                raise ValueError("each expected passage must contain only 'path' and 'anchor'")
            source_path = _bounded_text(item["path"], "expected path", 512)
            pure_path = PurePosixPath(source_path)
            if pure_path.is_absolute() or ".." in pure_path.parts or "\\" in source_path:
                raise ValueError("expected path must be a safe repository-relative POSIX path")
            expected.append(
                ExpectedPassage(source_path, _bounded_text(item["anchor"], "anchor", 200))
            )
        if len({item.citation for item in expected}) != len(expected):
            raise ValueError(f"query {identifier} contains duplicate expected passages")
        queries.append(EvaluationQuery(identifier, query, tuple(expected)))
    return tuple(queries)


def evaluate_queries(
    queries: Sequence[EvaluationQuery],
    search: Callable[[str, int], Sequence[SearchResult]],
    *,
    limit: int = 5,
) -> EvaluationReport:
    """Run queries and calculate Hit@k, Recall@k, and reciprocal rank."""
    if not 1 <= limit <= 20:
        raise ValueError("limit must be between 1 and 20")
    if not queries:
        raise ValueError("at least one evaluation query is required")
    details: list[QueryEvaluation] = []
    for item in queries:
        expected = tuple(passage.citation for passage in item.expected)
        results = search(item.query, limit)
        retrieved = tuple(
            f"{result.relative_path}#{result.heading_anchor}" for result in results[:limit]
        )
        ranks = [index for index, citation in enumerate(retrieved, start=1) if citation in expected]
        details.append(
            QueryEvaluation(
                id=item.id,
                query=item.query,
                expected=expected,
                retrieved=retrieved,
                hit=float(bool(ranks)),
                recall=len(set(retrieved).intersection(expected)) / len(expected),
                reciprocal_rank=1.0 / min(ranks) if ranks else 0.0,
            )
        )
    count = len(details)
    return EvaluationReport(
        limit,
        tuple(details),
        sum(item.hit for item in details) / count,
        sum(item.recall for item in details) / count,
        sum(item.reciprocal_rank for item in details) / count,
    )


def _bounded_text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} must be a non-empty string of at most {maximum} characters")
    return value.strip()
