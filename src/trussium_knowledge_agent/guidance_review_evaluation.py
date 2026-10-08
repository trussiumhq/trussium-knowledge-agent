"""Small, human-reviewed fixtures for inspecting guidance-review behavior."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from trussium_knowledge_agent.chat import ChatClient
from trussium_knowledge_agent.guidance_review import (
    GuidanceReview,
    review_guidance,
    validate_source_relative_path,
)
from trussium_knowledge_agent.store import SearchResult

MAX_DATASET_BYTES = 1024 * 1024
MAX_CASES = 20
MAX_PASSAGES_PER_CASE = 10
ExpectedStatus = Literal["potential_conflict", "no_conflict_found", "insufficient_evidence"]


@dataclass(frozen=True, slots=True)
class GuidanceReviewCase:
    """One synthetic input with a human-authored expected label and rationale."""

    id: str
    source_name: str
    source_relative_path: str
    guidance: str
    expected_status: ExpectedStatus
    expected_citations: tuple[str, ...]
    rationale: str
    passages: tuple[SearchResult, ...]


@dataclass(frozen=True, slots=True)
class GuidanceReviewCaseResult:
    """Observed review result, kept separate from human expectations."""

    case: GuidanceReviewCase
    review: GuidanceReview

    @property
    def cited_locations(self) -> tuple[str, ...]:
        return tuple(
            f"{item.source.relative_path}#{item.source.heading_anchor}"
            for item in self.review.citations
        )

    @property
    def status_agrees(self) -> bool:
        return self.review.status == self.case.expected_status

    @property
    def citations_agree(self) -> bool:
        return set(self.cited_locations) == set(self.case.expected_citations)


def load_guidance_review_cases(path: str | Path) -> tuple[GuidanceReviewCase, ...]:
    """Load a bounded version-1 synthetic guidance-review dataset."""
    dataset_path = Path(path)
    try:
        if dataset_path.stat().st_size > MAX_DATASET_BYTES:
            raise ValueError("guidance-review dataset exceeds the 1 MiB limit")
        payload = json.loads(dataset_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read guidance-review dataset: {error}") from error
    if not isinstance(payload, dict) or set(payload) != {"version", "cases"}:
        raise ValueError("dataset must contain only 'version' and 'cases'")
    if payload["version"] != 1:
        raise ValueError("guidance-review dataset version must be 1")
    rows = payload["cases"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_CASES:
        raise ValueError(f"dataset must contain between 1 and {MAX_CASES} cases")

    cases: list[GuidanceReviewCase] = []
    seen_ids: set[str] = set()
    for row in rows:
        expected_keys = {
            "id",
            "source_name",
            "source_relative_path",
            "guidance",
            "expected_status",
            "expected_citations",
            "rationale",
            "evidence",
        }
        if not isinstance(row, dict) or set(row) != expected_keys:
            raise ValueError("each case has missing or unknown fields")
        identifier = _text(row["id"], "case id", 80)
        if identifier in seen_ids:
            raise ValueError(f"duplicate case id: {identifier}")
        seen_ids.add(identifier)
        source_name = _text(row["source_name"], "source name", 128)
        source_path = _safe_relative_path(row["source_relative_path"], "source path")
        guidance = _text(row["guidance"], "guidance", 4000)
        rationale = _text(row["rationale"], "rationale", 1000)
        expected_status = row["expected_status"]
        if not isinstance(expected_status, str) or expected_status not in {
            "potential_conflict",
            "no_conflict_found",
            "insufficient_evidence",
        }:
            raise ValueError(f"case {identifier} has an invalid expected status")
        citation_rows = row["expected_citations"]
        if not isinstance(citation_rows, list) or len(citation_rows) > MAX_PASSAGES_PER_CASE:
            raise ValueError(f"case {identifier} has invalid expected citations")
        expected_citations = tuple(
            _citation(value, f"case {identifier} expected citation") for value in citation_rows
        )
        if len(set(expected_citations)) != len(expected_citations):
            raise ValueError(f"case {identifier} has duplicate expected citations")
        evidence_rows = row["evidence"]
        if (
            not isinstance(evidence_rows, list)
            or len(evidence_rows) > MAX_PASSAGES_PER_CASE
            or (expected_status != "insufficient_evidence" and not evidence_rows)
        ):
            raise ValueError(f"case {identifier} has invalid evidence")
        passages = tuple(_passage(item, identifier) for item in evidence_rows)
        if any(
            passage.display_name == source_name and passage.relative_path == source_path
            for passage in passages
        ):
            raise ValueError(f"case {identifier} includes evidence from its reviewed source")
        available_citations = {
            f"{passage.relative_path}#{passage.heading_anchor}" for passage in passages
        }
        if not set(expected_citations) <= available_citations:
            raise ValueError(f"case {identifier} expects a citation outside its evidence")
        if expected_status == "insufficient_evidence" and expected_citations:
            raise ValueError(
                f"case {identifier} must not expect citations for insufficient evidence"
            )
        if expected_status != "insufficient_evidence" and not expected_citations:
            raise ValueError(f"case {identifier} must expect cited evidence")
        cases.append(
            GuidanceReviewCase(
                identifier,
                source_name,
                source_path,
                guidance,
                cast(ExpectedStatus, expected_status),
                expected_citations,
                rationale,
                passages,
            )
        )
    return tuple(cases)


def evaluate_guidance_review_cases(
    cases: Sequence[GuidanceReviewCase], chat_client: ChatClient, model: str
) -> tuple[GuidanceReviewCaseResult, ...]:
    """Run each fixed case once; results are for human inspection, not scoring."""
    if not cases:
        raise ValueError("at least one guidance-review case is required")
    if not model.strip():
        raise ValueError("chat model must be non-empty")
    return tuple(
        GuidanceReviewCaseResult(
            case,
            review_guidance(
                case.guidance,
                source_name=case.source_name,
                source_relative_path=case.source_relative_path,
                passages=list(case.passages),
                chat_client=chat_client,
                model=model,
            ),
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
    path = _safe_relative_path(value["path"], "evidence path")
    anchor = _text(value["anchor"], "evidence anchor", 200)
    content = _text(value["content"], "evidence content", 4000)
    heading_rows = value["heading_path"]
    if (
        not isinstance(heading_rows, list)
        or len(heading_rows) > 12
        or any(
            not isinstance(item, str) or not item.strip() or len(item) > 200
            for item in heading_rows
        )
    ):
        raise ValueError(f"case {case_id} has invalid evidence heading")
    digest = hashlib.sha256(f"{path}\0{anchor}\0{content}".encode()).hexdigest()
    return SearchResult(
        source_name,
        revision,
        path,
        tuple(heading_rows),
        anchor,
        digest,
        content,
        1.0,
    )


def _safe_relative_path(value: Any, label: str) -> str:
    text = _text(value, label, 512)
    try:
        validate_source_relative_path(text)
    except ValueError:
        raise ValueError(f"{label} must be a safe relative POSIX path")
    return text


def _citation(value: Any, label: str) -> str:
    text = _text(value, label, 713)
    if "#" not in text:
        raise ValueError(f"{label} must include a path and heading anchor")
    path, anchor = text.rsplit("#", maxsplit=1)
    return f"{_safe_relative_path(path, label)}#{_text(anchor, label, 200)}"


def _text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} must be a non-empty string of at most {maximum} characters")
    return value.strip()
