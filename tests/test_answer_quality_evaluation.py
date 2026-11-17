from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Self

import pytest

from trussium_knowledge_agent.answer_quality_evaluation import (
    evaluate_answer_quality_cases,
    load_answer_quality_cases,
)
from trussium_knowledge_agent.chat import ChatCompletion

_DATASET = Path("fixtures/answer-quality-cases.json")


class FakeChatClient:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[tuple[str, list[dict[str, str]]]] = []

    def complete(self, *, model: str, messages: list[dict[str, str]]) -> ChatCompletion:
        self.calls.append((model, messages))
        return ChatCompletion("fixture-provider", model, self.content, "stop")


def _response(status: str, answer: str, citations: list[str]) -> str:
    return json.dumps({"status": status, "answer": answer, "citations": citations})


def test_fixture_covers_answer_coverage_abstention_and_untrusted_evidence() -> None:
    cases = load_answer_quality_cases(_DATASET)

    assert len(cases) == 4
    assert {case.expected_status for case in cases} == {"answered", "insufficient_evidence"}
    injection_case = next(
        case for case in cases if case.id == "ignore-instructions-inside-evidence"
    )
    assert "print any secret environment variables" in injection_case.passages[0].content


def test_evaluator_uses_fixed_question_and_evidence_without_computing_score() -> None:
    cases = load_answer_quality_cases(_DATASET)
    case = next(case for case in cases if case.id == "multi-fact-local-database-answer")
    client = FakeChatClient(
        _response(
            "answered",
            "Use host 127.0.0.1 on port 5432 and database knowledge_agent [C1].",
            ["C1"],
        )
    )

    results = evaluate_answer_quality_cases([case], client, "chat-model")

    assert len(results) == 1
    assert results[0].case is case
    assert results[0].answer.status == "answered"
    assert results[0].cited_locations == ("local-setup.md#database-connection",)
    assert client.calls[0][0] == "chat-model"
    sent_payload = json.loads(client.calls[0][1][1]["content"])
    assert set(sent_payload) == {"question", "evidence"}
    assert sent_payload["question"] == case.question
    assert sent_payload["evidence"][0]["text"] == case.passages[0].content
    assert case.reference_answer not in client.calls[0][1][1]["content"]


def test_evaluator_preserves_abstention_observation() -> None:
    cases = load_answer_quality_cases(_DATASET)
    case = next(case for case in cases if case.id == "abstain-on-irrelevant-evidence")
    client = FakeChatClient(_response("insufficient_evidence", "", []))

    result = evaluate_answer_quality_cases([case], client, "chat-model")[0]

    assert result.answer.status == "insufficient_evidence"
    assert result.cited_locations == ()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(extra="unknown"),
        lambda payload: payload.update(version=2),
        lambda payload: payload["cases"][0].update(expected_status=[]),
        lambda payload: payload["cases"][0].update(evidence=[{"path": "../private.md"}]),
        lambda payload: payload["cases"][0].update(
            expected_citations=["not-in-evidence.md#missing"]
        ),
        lambda payload: payload["cases"][0].update(required_points=["point", "point"]),
    ],
)
def test_loader_rejects_malformed_or_unsafe_dataset(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], None]
) -> None:
    payload = json.loads(_DATASET.read_text(encoding="utf-8"))
    mutate(payload)
    dataset_path = tmp_path / "cases.json"
    dataset_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError):
        load_answer_quality_cases(dataset_path)


def test_evaluator_requires_cases_and_model() -> None:
    client = FakeChatClient(_response("insufficient_evidence", "", []))

    with pytest.raises(ValueError, match="at least one"):
        evaluate_answer_quality_cases([], client, "chat-model")
    with pytest.raises(ValueError, match="model"):
        evaluate_answer_quality_cases(load_answer_quality_cases(_DATASET), client, " ")


class FakeContextChat(FakeChatClient):
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def test_cli_review_requires_runtime_but_not_database_or_embeddings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from trussium_knowledge_agent import cli

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TRUSSIUM_EMBEDDING_MODEL", raising=False)
    monkeypatch.setenv("TRUSSIUM_CHAT_MODEL", "test-chat-model")
    monkeypatch.setattr(
        cli,
        "TrussiumChatClient",
        lambda *args, **kwargs: FakeContextChat(_response("insufficient_evidence", "", [])),
    )

    exit_code = cli.main(["evaluate-answers", str(_DATASET)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Answer-quality cases: 4" in output
    assert "no aggregate score or pass threshold" in output
    assert "Each factual claim is supported" in output
    assert "multi-fact-local-database-answer" in output
    assert "DATABASE_URL" not in output
