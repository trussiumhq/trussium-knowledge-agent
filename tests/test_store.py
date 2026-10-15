from __future__ import annotations

import math
from typing import Self

import psycopg
import pytest

from trussium_knowledge_agent import store
from trussium_knowledge_agent.store import search_chunks


@pytest.mark.parametrize("top_k", [0, 21])
def test_search_rejects_unbounded_result_limits(top_k: int) -> None:
    with pytest.raises(ValueError, match="top_k"):
        search_chunks(
            "not-used",
            query_vector=(1.0, 0.0),
            provider="provider",
            model="model",
            top_k=top_k,
        )


def test_search_rejects_nonfinite_query_vector() -> None:
    with pytest.raises(ValueError):
        search_chunks(
            "not-used",
            query_vector=(math.nan,),
            provider="provider",
            model="model",
        )


def test_search_excludes_the_reviewed_source_using_bound_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    class Cursor:
        def fetchall(self) -> list[tuple[object, ...]]:
            return []

    class Connection:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def execute(self, query: str, parameters: tuple[object, ...]) -> Cursor:
            observed["query"] = query
            observed["parameters"] = parameters
            return Cursor()

    monkeypatch.setattr(store, "_apply_migrations", lambda _: None)
    monkeypatch.setattr(psycopg, "connect", lambda _: Connection())

    results = search_chunks(
        "postgresql://example",
        query_vector=(1.0, 0.0),
        provider="provider",
        model="model",
        top_k=5,
        exclude_source=("manuals", "deploy.md"),
    )

    assert results == []
    query = observed["query"]
    assert isinstance(query, str)
    assert "NOT (s.display_name = %s AND c.relative_path = %s)" in query
    assert "s.lifecycle_state = 'active'" in query
    assert "s.authorization_verified_until > now()" in query
    assert observed["parameters"] == (
        "[1.0,0.0]",
        "provider",
        "model",
        2,
        "manuals",
        "deploy.md",
        "[1.0,0.0]",
        5,
    )


@pytest.mark.parametrize("limit", [0, 1001])
def test_purge_rejects_unbounded_batch_sizes(limit: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        store.purge_expired_sources("not-used", limit=limit)


def test_github_sources_require_bounded_identity_before_database_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        psycopg,
        "connect",
        lambda *_: pytest.fail("invalid source identity must fail before connecting"),
    )

    with pytest.raises(ValueError, match="immutable identity"):
        store.persist_source(
            "not-used",
            source_id="a" * 64,
            display_name="manuals",
            revision=None,
            chunks=[],
            source_kind="github",
            source_identity="",
        )


def test_github_sources_require_external_lifecycle_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KNOWLEDGE_AGENT_LIFECYCLE_LEDGER_PATH", raising=False)
    monkeypatch.setattr(
        psycopg,
        "connect",
        lambda *_: pytest.fail("GitHub sources require lifecycle storage before connecting"),
    )

    with pytest.raises(ValueError, match="KNOWLEDGE_AGENT_LIFECYCLE_LEDGER_PATH"):
        store.persist_source(
            "not-used",
            source_id="a" * 64,
            display_name="manuals",
            revision=None,
            chunks=[],
            source_kind="github",
            source_identity="github.com:repository:123",
        )


def test_private_github_source_requires_installation_and_expiring_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        psycopg,
        "connect",
        lambda *_: pytest.fail("invalid private source metadata must fail before connecting"),
    )

    with pytest.raises(ValueError, match="installation ID and authorization lease"):
        store.persist_source(
            "not-used",
            source_id="a" * 64,
            display_name="private-docs",
            revision="b" * 40,
            chunks=[],
            source_kind="github",
            source_identity="github.com:repository:123",
            github_installation_id=456,
        )
