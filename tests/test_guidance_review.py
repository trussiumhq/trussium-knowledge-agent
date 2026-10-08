from __future__ import annotations

import json
from collections.abc import Sequence

import pytest

from trussium_knowledge_agent.chat import ChatCompletion
from trussium_knowledge_agent.guidance_review import (
    INSUFFICIENT_REVIEW,
    GuidanceReview,
    review_guidance,
)
from trussium_knowledge_agent.store import SearchResult


def _passage(
    *,
    source: str = "current-manuals",
    path: str = "deploy.md",
    content: str = "The deployment process uses rolling updates.",
) -> SearchResult:
    return SearchResult(
        display_name=source,
        revision="opaque-revision",
        relative_path=path,
        heading_path=("Deployment", "Rollout"),
        heading_anchor="rollout",
        content_hash="a" * 64,
        content=content,
        score=0.88,
    )


def _response(
    *,
    status: str = "potential_conflict",
    assessment: str = "The indexed runbook requires a rolling update [C1].",
    citations: Sequence[str] = ("C1",),
    confidence: object = "medium",
) -> str:
    return json.dumps(
        {
            "status": status,
            "assessment": assessment,
            "citations": list(citations),
            "confidence": confidence,
        }
    )


class FakeChatClient:
    def __init__(self, content: str, *, finish_reason: str = "stop") -> None:
        self.content = content
        self.finish_reason = finish_reason
        self.calls: list[tuple[str, list[dict[str, str]]]] = []

    def complete(self, *, model: str, messages: list[dict[str, str]]) -> ChatCompletion:
        self.calls.append((model, messages))
        return ChatCompletion("test-provider", "resolved-model", self.content, self.finish_reason)


def _review(
    client: FakeChatClient,
    passages: list[SearchResult] | None = None,
) -> GuidanceReview:
    return review_guidance(
        "Use blue-green deployment.",
        source_name="operator-manuals",
        source_relative_path="deploy.md",
        passages=[_passage()] if passages is None else passages,
        chat_client=client,
        model="chat-model",
    )


def test_review_returns_only_citations_from_independent_retrieved_passages() -> None:
    client = FakeChatClient(_response())
    target_source = _passage(source="operator-manuals", content="Use blue-green deployment.")
    independent_source = _passage()

    result = _review(client, [target_source, independent_source])

    assert result.status == "potential_conflict"
    assert result.confidence == "medium"
    assert result.citations[0].reference_id == "C1"
    assert result.citations[0].source is independent_source
    payload = json.loads(client.calls[0][1][1]["content"])
    assert payload["target"]["guidance"] == "Use blue-green deployment."
    assert [item["source"] for item in payload["evidence"]] == ["current-manuals"]
    system_message = client.calls[0][1][0]["content"]
    assert "untrusted data" in system_message
    assert "opaque identifier" in system_message
    assert "uncalibrated" in system_message


def test_review_without_independent_evidence_does_not_call_chat() -> None:
    client = FakeChatClient(_response())
    target_source = _passage(source="operator-manuals")

    result = _review(client, [target_source])

    assert result.status == "insufficient_evidence"
    assert result.confidence == "low"
    assert result.assessment == INSUFFICIENT_REVIEW
    assert result.citations == ()
    assert client.calls == []


@pytest.mark.parametrize(
    "content",
    [
        "not JSON",
        _response(assessment="Unsupported finding [C9].", citations=("C9",)),
        _response(assessment="Unsupported finding without a citation."),
        _response(assessment="Open https://untrusted.example [C1]."),
        _response(confidence=[]),
        json.dumps(
            {
                "status": "no_conflict_found",
                "assessment": "No conflict found [C1].",
                "citations": ["C1"],
                "confidence": "medium",
                "extra": "not allowed",
            }
        ),
    ],
)
def test_malformed_or_unsupported_review_fails_closed(content: str) -> None:
    result = _review(FakeChatClient(content))

    assert result.status == "insufficient_evidence"
    assert result.citations == ()


def test_non_stop_completion_fails_closed() -> None:
    result = _review(FakeChatClient(_response(), finish_reason="length"))

    assert result.status == "insufficient_evidence"


@pytest.mark.parametrize(
    ("guidance", "source_path"),
    [
        ("", "deploy.md"),
        ("x" * 4001, "deploy.md"),
        ("review text", "../secrets.md"),
        ("review text", "/private/secrets.md"),
        ("review text", "https://example.test/doc.md"),
        ("review text", "nested\\secrets.md"),
    ],
)
def test_review_rejects_empty_or_unbounded_target_metadata(
    guidance: str,
    source_path: str,
) -> None:
    with pytest.raises(ValueError):
        review_guidance(
            guidance,
            source_name="operator-manuals",
            source_relative_path=source_path,
            passages=[_passage()],
            chat_client=FakeChatClient(_response()),
            model="chat-model",
        )
