from typing import Self

import pytest
from fastapi.testclient import TestClient

from trussium_knowledge_agent import web
from trussium_knowledge_agent.answers import AnswerCitation, AnswerResult
from trussium_knowledge_agent.app import app
from trussium_knowledge_agent.store import SearchResult


def test_liveness() -> None:
    response = TestClient(app).get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_browser_interface_and_assets_are_served() -> None:
    client = TestClient(app)

    page = client.get("/")
    script = client.get("/static/app.js")

    assert page.status_code == 200
    assert "Ask your documentation" in page.text
    assert script.status_code == 200
    assert "textContent" in script.text
    assert "innerHTML" not in script.text


class FakeRuntime:
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def _passage() -> SearchResult:
    return SearchResult(
        "manuals", "abc123", "setup.md", ("Runtime", "Setup"), "setup", "hash", "text", 0.9
    )


def _configure_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("TRUSSIUM_EMBEDDING_MODEL", "embed-model")
    monkeypatch.setenv("TRUSSIUM_CHAT_MODEL", "chat-model")
    monkeypatch.setattr(web, "TrussiumEmbeddingsClient", lambda *args, **kwargs: FakeRuntime())


def test_question_endpoint_returns_validated_citation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_runtime(monkeypatch)
    passage = _passage()
    monkeypatch.setattr(web, "search_index", lambda *args, **kwargs: [passage])
    monkeypatch.setattr(web, "TrussiumChatClient", lambda *args, **kwargs: FakeRuntime())
    monkeypatch.setattr(
        web,
        "answer_from_passages",
        lambda *args: AnswerResult(
            "answered",
            "Configure the runtime URL [C1].",
            (AnswerCitation("C1", passage),),
        ),
    )

    response = TestClient(app).post(
        "/api/ask", json={"question": "How do I configure the runtime?", "limit": 3}
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "answered",
        "answer": "Configure the runtime URL [C1].",
        "citations": [
            {
                "reference_id": "C1",
                "relative_path": "setup.md",
                "heading_path": ["Runtime", "Setup"],
                "heading_anchor": "setup",
                "revision": "abc123",
            }
        ],
    }


def test_question_endpoint_returns_insufficient_evidence_without_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("TRUSSIUM_EMBEDDING_MODEL", "embed-model")
    monkeypatch.delenv("TRUSSIUM_CHAT_MODEL", raising=False)
    monkeypatch.setattr(web, "TrussiumEmbeddingsClient", lambda *args, **kwargs: FakeRuntime())
    monkeypatch.setattr(web, "search_index", lambda *args, **kwargs: [])

    response = TestClient(app).post("/api/ask", json={"question": "An unsupported topic"})

    assert response.status_code == 200
    assert response.json()["status"] == "insufficient_evidence"
    assert response.json()["citations"] == []


def test_question_endpoint_hides_service_error_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_runtime(monkeypatch)
    monkeypatch.setattr(
        web, "search_index", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("secret"))
    )

    response = TestClient(app).post("/api/ask", json={"question": "question"})

    assert response.status_code == 503
    assert response.json() == {"detail": "The question service is unavailable."}
    assert "secret" not in response.text


@pytest.mark.parametrize(
    "payload",
    [
        {"question": "   "},
        {"question": "valid", "limit": 0},
        {"question": "valid", "limit": 11},
        {"question": "valid", "unexpected": True},
    ],
)
def test_question_endpoint_validates_request_bounds(payload: dict[str, object]) -> None:
    response = TestClient(app).post("/api/ask", json=payload)

    assert response.status_code == 422
