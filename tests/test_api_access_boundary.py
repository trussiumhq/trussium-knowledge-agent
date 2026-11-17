"""PostgreSQL-backed HTTP tests for private-source retrieval boundaries."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Self
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from trussium_knowledge_agent import web
from trussium_knowledge_agent.app import app
from trussium_knowledge_agent.chunking import Chunk
from trussium_knowledge_agent.embeddings import EmbeddingsBatch
from trussium_knowledge_agent.store import persist_source, remove_source

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL is not configured")


class FixedEmbeddingsClient:
    """Return one deterministic vector matching the isolated test source."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def embed(self, *, model: str, inputs: Sequence[str]) -> EmbeddingsBatch:
        return EmbeddingsBatch("expired-lease-test", model, tuple((1.0, 0.0) for _ in inputs))


@pytest.fixture(autouse=True)
def lifecycle_ledger_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KNOWLEDGE_AGENT_LIFECYCLE_LEDGER_PATH", str(tmp_path / "lifecycle.jsonl"))


def test_question_api_never_sends_expired_private_source_to_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expired private evidence must be excluded at the public question boundary."""
    identity = f"github.com:repository:{uuid4().int % 9_000_000_000 + 1_000_000_000}"
    source_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    content = "Secret customer deployment instructions must stay private."
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    model = f"expired-lease-{uuid4().hex}"
    chunk = Chunk(
        chunk_id=content_hash,
        relative_path="private.md",
        heading_path=("Private", "Deployment"),
        heading_anchor="deployment",
        chunk_index=0,
        content_hash=content_hash,
        content=content,
        embedding=(1.0, 0.0),
        embedding_provider="expired-lease-test",
        embedding_model=model,
    )
    persist_source(
        DATABASE_URL or "",
        source_id=source_id,
        display_name="private-expired-lease-test",
        revision="a" * 40,
        chunks=[chunk],
        source_kind="github",
        source_identity=identity,
        github_installation_id=uuid4().int % 9_000_000_000 + 1_000_000_000,
        authorization_verified_until=datetime.now(UTC) - timedelta(seconds=1),
    )

    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setenv("TRUSSIUM_EMBEDDING_MODEL", model)
    monkeypatch.setenv("TRUSSIUM_CHAT_MODEL", "must-not-be-called")
    monkeypatch.setattr(web, "TrussiumEmbeddingsClient", FixedEmbeddingsClient)
    monkeypatch.setattr(
        web,
        "TrussiumChatClient",
        lambda *_args, **_kwargs: pytest.fail("chat must not receive expired private evidence"),
    )

    try:
        response = TestClient(app).post(
            "/api/ask", json={"question": "What are the deployment instructions?"}
        )

        assert response.status_code == 200
        assert response.json() == {
            "status": "insufficient_evidence",
            "answer": "I couldn't find enough evidence in the indexed sources to answer that.",
            "citations": [],
        }
        assert content not in response.text
    finally:
        remove_source(DATABASE_URL or "", source_id)
