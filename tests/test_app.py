import hashlib
import hmac
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


def test_github_webhook_rejects_invalid_signature_before_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("KNOWLEDGE_AGENT_GITHUB_INSTALLATION_ID", "4567")
    monkeypatch.setenv("KNOWLEDGE_AGENT_GITHUB_WEBHOOK_SECRET", "s" * 40)
    monkeypatch.setattr(
        "trussium_knowledge_agent.github_webhook.process_github_webhook",
        lambda *_args, **_kwargs: pytest.fail("invalid signature must be rejected first"),
    )

    response = TestClient(app).post(
        "/webhooks/github",
        content=b'{"action":"deleted","installation":{"id":4567}}',
        headers={
            "x-hub-signature-256": "sha256=" + "0" * 64,
            "x-github-delivery": "delivery-1",
            "x-github-event": "installation",
        },
    )

    assert response.status_code == 401


def test_github_webhook_verifies_signature_and_passes_expected_installation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}
    secret = "configured-test-webhook-secret" * 2
    body = b'{"action":"suspend","installation":{"id":4567}}'
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("KNOWLEDGE_AGENT_GITHUB_INSTALLATION_ID", "4567")
    monkeypatch.setenv("KNOWLEDGE_AGENT_GITHUB_WEBHOOK_SECRET", secret)

    def process(
        database_url: str,
        delivery_id: str,
        event_name: str,
        payload: dict[str, object],
        *,
        expected_installation_id: int,
    ) -> bool:
        observed.update(
            database_url=database_url,
            delivery_id=delivery_id,
            event_name=event_name,
            payload=payload,
            expected_installation_id=expected_installation_id,
        )
        return True

    monkeypatch.setattr("trussium_knowledge_agent.github_webhook.process_github_webhook", process)
    response = TestClient(app).post(
        "/webhooks/github",
        content=body,
        headers={
            "x-hub-signature-256": signature,
            "x-github-delivery": "delivery-2",
            "x-github-event": "installation",
        },
    )

    assert response.status_code == 202
    assert observed["delivery_id"] == "delivery-2"
    assert observed["expected_installation_id"] == 4567
    assert observed["payload"] == {"action": "suspend", "installation": {"id": 4567}}
