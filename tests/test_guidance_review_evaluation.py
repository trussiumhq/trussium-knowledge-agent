from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from trussium_knowledge_agent.chat import ChatCompletion
from trussium_knowledge_agent.guidance_review_evaluation import (
    evaluate_guidance_review_cases,
    load_guidance_review_cases,
)

_DATASET = Path("fixtures/guidance-review-cases.json")


class FakeChatClient:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[tuple[str, list[dict[str, str]]]] = []

    def complete(self, *, model: str, messages: list[dict[str, str]]) -> ChatCompletion:
        self.calls.append((model, messages))
        return ChatCompletion("fixture-provider", model, self.content, "stop")


def _response(
    status: str,
    assessment: str,
    citations: Sequence[str],
    confidence: str = "medium",
) -> str:
    return json.dumps(
        {
            "status": status,
            "assessment": assessment,
            "citations": list(citations),
            "confidence": confidence,
        }
    )


def test_guidance_review_fixture_covers_expected_synthetic_categories() -> None:
    cases = load_guidance_review_cases(_DATASET)

    assert len(cases) == 6
    assert {case.expected_status for case in cases} == {
        "potential_conflict",
        "no_conflict_found",
        "insufficient_evidence",
    }
    assert all(
        case.source_name != passage.display_name for case in cases for passage in case.passages
    )


def test_evaluator_keeps_observations_separate_from_expected_labels() -> None:
    cases = load_guidance_review_cases(_DATASET)
    compatible_case = next(case for case in cases if case.id == "https-compatible-tls-detail")
    client = FakeChatClient(
        _response(
            "no_conflict_found",
            "The TLS baseline is compatible with requiring HTTPS [C1].",
            ("C1",),
        )
    )

    results = evaluate_guidance_review_cases([compatible_case], client, "chat-model")

    assert len(results) == 1
    assert results[0].case is compatible_case
    assert results[0].review.status == "no_conflict_found"
    assert results[0].status_agrees
    assert results[0].citations_agree
    assert results[0].cited_locations == ("tls-baseline.md#transport-security",)
    assert client.calls[0][0] == "chat-model"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(extra="unknown"),
        lambda payload: payload.update(version=2),
        lambda payload: payload["cases"][0].update(expected_status=[]),
        lambda payload: payload["cases"][0].update(source_relative_path="../private.md"),
        lambda payload: payload["cases"][0].update(
            expected_citations=["not-in-evidence.md#missing"]
        ),
    ],
)
def test_loader_rejects_malformed_or_unbounded_dataset(
    tmp_path: Path,
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    payload = json.loads(_DATASET.read_text(encoding="utf-8"))
    mutate(payload)
    dataset_path = tmp_path / "cases.json"
    dataset_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError):
        load_guidance_review_cases(dataset_path)


def test_evaluator_requires_cases_and_model() -> None:
    client = FakeChatClient(_response("insufficient_evidence", "", (), "low"))

    with pytest.raises(ValueError, match="at least one"):
        evaluate_guidance_review_cases([], client, "chat-model")
    with pytest.raises(ValueError, match="model"):
        evaluate_guidance_review_cases(load_guidance_review_cases(_DATASET), client, " ")
