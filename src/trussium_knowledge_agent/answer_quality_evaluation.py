"""Fixed, synthetic answer-quality cases for manual inspection."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from trussium_knowledge_agent.answers import AnswerResult, answer_from_passages
from trussium_knowledge_agent.chat import ChatClient
from trussium_knowledge_agent.guidance_review import validate_source_relative_path
from trussium_knowledge_agent.store import SearchResult

MAX_DATASET_BYTES = 1024 * 1024
MAX_CASES = 12
MAX_PASSAGES_PER_CASE = 10
MAX_REVIEW_POINTS = 10
AnswerStatus = Literal["answered", "insufficient_evidence"]


@dataclass(frozen=True, slots=True)
class AnswerQualityCase:
    """Synthetic question, fixed evidence, and human-authored review guide."""

    id: str
    question: str
    expected_status: AnswerStatus
    reference_answer: str
    required_points: tuple[str, ...]
    expected_citations: tuple[str, ...]
    rationale: str
    passages: tuple[SearchResult, ...]


@dataclass(frozen=True, slots=True)
class AnswerQualityCaseResult:
    """One model observation; no automated quality score is inferred."""

    case: AnswerQualityCase
    answer: AnswerResult

    @property
    def cited_locations(self) -> tuple[str, ...]:
        return tuple(
            f"{citation.source.relative_path}#{citation.source.heading_anchor}"
            for citation in self.answer.citations
        )


def load_answer_quality_cases(path: str | Path) -> tuple[AnswerQualityCase, ...]:
    """Load a bounded version-1 answer-quality dataset with fixed synthetic evidence."""
    dataset_path = Path(path)
    try:
        if dataset_path.stat().st_size > MAX_DATASET_BYTES:
            raise ValueError("answer-quality dataset exceeds the 1 MiB limit")
        payload = json.loads(dataset_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read answer-quality dataset: {error}") from error
    if not isinstance(payload, dict) or set(payload) != {"version", "cases"}:
        raise ValueError("dataset must contain only 'version' and 'cases'")
    if payload["version"] != 1:
        raise ValueError("answer-quality dataset version must be 1")
    rows = payload["cases"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_CASES:
        raise ValueError(f"dataset must contain between 1 and {MAX_CASES} cases")

    cases: list[AnswerQualityCase] = []
    seen_ids: set[str] = set()
    expected_keys = {
        "id",
        "question",
        "expected_status",
        "reference_answer",
        "required_points",
        "expected_citations",
        "rationale",
        "evidence",
    }
    for row in rows:
        if not isinstance(row, dict) or set(row) != expected_keys:
            raise ValueError("each answer-quality case has missing or unknown fields")
        identifier = _text(row["id"], "case id", 80)
        if identifier in seen_ids:
            raise ValueError(f"duplicate case id: {identifier}")
        seen_ids.add(identifier)
        question = _text(row["question"], "question", 4000)
        reference_answer = _text(row["reference_answer"], "reference answer", 4000)
        rationale = _text(row["rationale"], "rationale", 1200)
        status = row["expected_status"]
        if not isinstance(status, str) or status not in {"answered", "insufficient_evidence"}:
            raise ValueError(f"case {identifier} has an invalid expected status")
        points = _text_list(row["required_points"], "required points", MAX_REVIEW_POINTS, 500)
        citations = _citation_list(row["expected_citations"], identifier)
        evidence_rows = row["evidence"]
        if (
            not isinstance(evidence_rows, list)
            or len(evidence_rows) > MAX_PASSAGES_PER_CASE
            or not evidence_rows
        ):
            raise ValueError(f"case {identifier} must contain 1 to 10 evidence passages")
        passages = tuple(_passage(value, identifier) for value in evidence_rows)
        available = {f"{passage.relative_path}#{passage.heading_anchor}" for passage in passages}
        if not set(citations) <= available:
            raise ValueError(f"case {identifier} expects a citation outside its evidence")
        if status == "answered" and (not points or not citations):
            raise ValueError(f"answered case {identifier} needs review points and citations")
        if status == "insufficient_evidence" and (points or citations):
            raise ValueError(
                f"insufficient-evidence case {identifier} must not expect facts or citations"
            )
        cases.append(
            AnswerQualityCase(
                identifier,
                question,
                cast(AnswerStatus, status),
                reference_answer,
                points,
                citations,
                rationale,
                passages,
            )
        )
    return tuple(cases)


def evaluate_answer_quality_cases(
    cases: Sequence[AnswerQualityCase], chat_client: ChatClient, model: str
) -> tuple[AnswerQualityCaseResult, ...]:
    """Generate one answer per fixed case for human review; do not calculate a score."""
    if not cases:
        raise ValueError("at least one answer-quality case is required")
    if not model.strip():
        raise ValueError("chat model must be non-empty")
    return tuple(
        AnswerQualityCaseResult(
            case,
            answer_from_passages(case.question, list(case.passages), chat_client, model),
        )
        for case in cases
    )


def _passage(value: Any, case_id: str) -> SearchResult:
    keys = {"source_name", "revision", "path", "heading_path", "anchor", "content"}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"case {case_id} contains malformed evidence")
    source_name = _text(value["source_name"], "evidence source name", 128)
    revision_value = value["revision"]
    revision = None if revision_value is None else _text(revision_value, "revision", 128)
    path = _safe_path(value["path"], "evidence path")
    anchor = _text(value["anchor"], "evidence anchor", 200)
    content = _text(value["content"], "evidence content", 4000)
    headings = value["heading_path"]
    if (
        not isinstance(headings, list)
        or len(headings) > 12
        or any(
            not isinstance(item, str) or not item.strip() or len(item) > 200 for item in headings
        )
    ):
        raise ValueError(f"case {case_id} has invalid evidence headings")
    digest = hashlib.sha256(f"{path}\0{anchor}\0{content}".encode()).hexdigest()
    return SearchResult(
        source_name,
        revision,
        path,
        tuple(headings),
        anchor,
        digest,
        content,
        1.0,
    )


def _safe_path(value: Any, label: str) -> str:
    path = _text(value, label, 512)
    try:
        validate_source_relative_path(path)
    except ValueError:
        raise ValueError(f"{label} must be a safe relative POSIX path")
    return path


def _citation_list(value: Any, case_id: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > MAX_PASSAGES_PER_CASE:
        raise ValueError(f"case {case_id} has invalid expected citations")
    citations: list[str] = []
    for item in value:
        citation = _text(item, "expected citation", 713)
        if "#" not in citation:
            raise ValueError(f"case {case_id} citations must include a path and anchor")
        path, anchor = citation.rsplit("#", maxsplit=1)
        citations.append(
            f"{_safe_path(path, 'expected citation path')}#{_text(anchor, 'anchor', 200)}"
        )
    if len(set(citations)) != len(citations):
        raise ValueError(f"case {case_id} has duplicate expected citations")
    return tuple(citations)


def _text_list(value: Any, label: str, maximum_items: int, maximum_text: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum_items:
        raise ValueError(f"{label} must contain at most {maximum_items} items")
    items = tuple(_text(item, label, maximum_text) for item in value)
    if len(set(items)) != len(items):
        raise ValueError(f"{label} must not contain duplicates")
    return items


def _text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} must be a non-empty string of at most {maximum} characters")
    return value.strip()
