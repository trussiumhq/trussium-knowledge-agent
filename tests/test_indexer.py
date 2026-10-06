from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from trussium_knowledge_agent.answers import answer_from_passages
from trussium_knowledge_agent.chat import ChatCompletion
from trussium_knowledge_agent.embeddings import EmbeddingsBatch
from trussium_knowledge_agent.indexer import index_source, remove_index
from trussium_knowledge_agent.retrieval import search_index
from trussium_knowledge_agent.store import fetch_source_chunks

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL is not configured")


class FakeEmbeddingsClient:
    def embed(self, *, model: str, inputs: Sequence[str]) -> EmbeddingsBatch:
        vectors = []
        for text in inputs:
            lowered = text.lower()
            if "green" in lowered or "apple" in lowered:
                vectors.append((1.0, 0.0))
            elif "blue" in lowered or "ocean" in lowered:
                vectors.append((0.0, 1.0))
            else:
                vectors.append((0.6, 0.8))
        return EmbeddingsBatch("test-provider", model, tuple(vectors))


def _client() -> FakeEmbeddingsClient:
    return FakeEmbeddingsClient()


def _index(path: Path) -> str:
    result = index_source(path, DATABASE_URL or "", _client(), "test-model")
    return result.source_id


def test_index_is_idempotent_and_removes_stale_files(tmp_path: Path) -> None:
    (tmp_path / "guide.md").write_text("# Guide\n\nOriginal passage.", encoding="utf-8")
    (tmp_path / "keep.md").write_text("# Keep\n\nKeep this.", encoding="utf-8")

    first = index_source(tmp_path, DATABASE_URL or "", _client(), "test-model")
    first_rows = fetch_source_chunks(DATABASE_URL or "", first.source_id)
    second = index_source(tmp_path, DATABASE_URL or "", _client(), "test-model")
    second_rows = fetch_source_chunks(DATABASE_URL or "", second.source_id)

    assert first.source_id == second.source_id
    assert first_rows == second_rows
    assert len(second_rows) == 2
    assert all(row[7:10] == ("test-provider", "test-model", 2) for row in second_rows)
    assert all(row[10] in {"[0.6,0.8]", "[1,0]"} for row in second_rows)

    (tmp_path / "guide.md").write_text("# Guide\n\nUpdated passage.", encoding="utf-8")
    (tmp_path / "keep.md").unlink()
    updated = index_source(tmp_path, DATABASE_URL or "", _client(), "test-model")
    updated_rows = fetch_source_chunks(DATABASE_URL or "", updated.source_id)

    assert len(updated_rows) == 1
    assert updated_rows[0][1] == "guide.md"
    assert updated_rows[0][5] != first_rows[0][5]
    assert remove_index(tmp_path, DATABASE_URL or "")
    assert fetch_source_chunks(DATABASE_URL or "", updated.source_id) == []
    assert not remove_index(tmp_path, DATABASE_URL or "")


def test_empty_repository_creates_removable_empty_index(tmp_path: Path) -> None:
    result = index_source(tmp_path, DATABASE_URL or "", _client(), "test-model")

    assert result.file_count == 0
    assert result.chunk_count == 0
    assert fetch_source_chunks(DATABASE_URL or "", result.source_id) == []
    assert remove_index(tmp_path, DATABASE_URL or "")


def test_database_failure_rolls_back_source_replacement(tmp_path: Path) -> None:
    source = tmp_path / "guide.md"
    source.write_text("# Guide\n\nPrevious durable text.", encoding="utf-8")
    source_id = _index(tmp_path)
    previous_rows = fetch_source_chunks(DATABASE_URL or "", source_id)

    suffix = uuid4().hex
    function_name = f"test_reject_marker_{suffix}"
    trigger_name = f"test_reject_marker_trigger_{suffix}"
    with psycopg.connect(DATABASE_URL or "", autocommit=True) as connection:
        connection.execute(
            f"""CREATE FUNCTION {function_name}() RETURNS trigger AS $$
                BEGIN
                  IF NEW.content LIKE '%force-database-failure%' THEN
                    RAISE EXCEPTION 'intentional test failure';
                  END IF;
                  RETURN NEW;
                END;
                $$ LANGUAGE plpgsql"""
        )
        connection.execute(
            f"CREATE TRIGGER {trigger_name} BEFORE INSERT ON document_chunks "
            f"FOR EACH ROW EXECUTE FUNCTION {function_name}()"
        )
    try:
        source.write_text("# Guide\n\nforce-database-failure", encoding="utf-8")
        with pytest.raises(psycopg.Error, match="intentional test failure"):
            index_source(tmp_path, DATABASE_URL or "", _client(), "test-model")
    finally:
        with psycopg.connect(DATABASE_URL or "", autocommit=True) as connection:
            connection.execute(f"DROP TRIGGER IF EXISTS {trigger_name} ON document_chunks")
            connection.execute(f"DROP FUNCTION IF EXISTS {function_name}()")

    assert fetch_source_chunks(DATABASE_URL or "", source_id) == previous_rows


def test_search_ranks_cosine_matches_and_filters_embedding_identity(tmp_path: Path) -> None:
    (tmp_path / "orchard.md").write_text("# Orchard\n\nGreen apple trees.", encoding="utf-8")
    (tmp_path / "ocean.md").write_text("# Ocean\n\nBlue ocean water.", encoding="utf-8")
    index_source(tmp_path, DATABASE_URL or "", _client(), "test-model")

    results = search_index("green apple", DATABASE_URL or "", _client(), "test-model", top_k=1)
    other_model_results = search_index(
        "green apple", DATABASE_URL or "", _client(), "different-model"
    )

    assert len(results) == 1
    assert results[0].relative_path == "orchard.md"
    assert results[0].heading_path == ("Orchard",)
    assert results[0].heading_anchor == "orchard"
    assert results[0].score == pytest.approx(1.0)
    assert other_model_results == []


def test_search_does_not_compare_different_dimensions(tmp_path: Path) -> None:
    (tmp_path / "two-dimensional.md").write_text(
        "# Two dimensions\n\nMatching source.", encoding="utf-8"
    )
    index_source(tmp_path, DATABASE_URL or "", _client(), "same-model")

    other_source = tmp_path / "other"
    other_source.mkdir()
    (other_source / "three-dimensional.md").write_text(
        "# Three dimensions\n\nDifferent vector space.", encoding="utf-8"
    )

    class ThreeDimensionalEmbeddingsClient:
        def embed(self, *, model: str, inputs: Sequence[str]) -> EmbeddingsBatch:
            return EmbeddingsBatch("test-provider", model, tuple((1.0, 0.0, 0.0) for _ in inputs))

    index_source(
        other_source,
        DATABASE_URL or "",
        ThreeDimensionalEmbeddingsClient(),
        "same-model",
    )
    results = search_index("matching", DATABASE_URL or "", _client(), "same-model")

    assert results
    assert {result.display_name for result in results} == {tmp_path.name}


def test_grounded_answer_uses_postgres_retrieval_evidence(tmp_path: Path) -> None:
    (tmp_path / "guide.md").write_text(
        "# Backups\n\nRun a daily database backup and test restoration monthly.",
        encoding="utf-8",
    )
    index_source(tmp_path, DATABASE_URL or "", _client(), "test-model")
    passages = search_index("How often restore?", DATABASE_URL or "", _client(), "test-model")

    class EvidenceChatClient:
        def complete(self, *, model: str, messages: list[dict[str, str]]) -> ChatCompletion:
            assert model == "chat-model"
            assert "test restoration monthly" in messages[1]["content"]
            return ChatCompletion(
                "test-provider",
                "resolved-chat-model",
                '{"status":"answered","answer":"Test restoration monthly [C1].",'
                '"citations":["C1"]}',
                "stop",
            )

    answer = answer_from_passages(
        "How often restore?", passages, EvidenceChatClient(), "chat-model"
    )

    assert answer.status == "answered"
    assert answer.citations[0].source.relative_path == "guide.md"


def test_embedding_failure_preserves_previous_index(tmp_path: Path) -> None:
    source = tmp_path / "guide.md"
    source.write_text("# Guide\n\nOriginal passage.", encoding="utf-8")
    source_id = _index(tmp_path)
    before = fetch_source_chunks(DATABASE_URL or "", source_id)

    class FailedEmbeddingsClient:
        def embed(self, *, model: str, inputs: Sequence[str]) -> EmbeddingsBatch:
            raise RuntimeError("runtime unavailable")

    source.write_text("# Guide\n\nChanged passage.", encoding="utf-8")
    with pytest.raises(RuntimeError, match="runtime unavailable"):
        index_source(tmp_path, DATABASE_URL or "", FailedEmbeddingsClient(), "test-model")

    assert fetch_source_chunks(DATABASE_URL or "", source_id) == before
