from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from trussium_knowledge_agent.answers import answer_from_passages
from trussium_knowledge_agent.chat import ChatCompletion
from trussium_knowledge_agent.chunking import Chunk, parse_markdown
from trussium_knowledge_agent.embeddings import EmbeddingsBatch
from trussium_knowledge_agent.evaluation import EvaluationQuery, ExpectedPassage, evaluate_queries
from trussium_knowledge_agent.github_snapshot import GitHubSnapshot, GitHubSnapshotError
from trussium_knowledge_agent.indexer import index_public_github_source, index_source, remove_index
from trussium_knowledge_agent.lifecycle_ledger import append_lifecycle_event
from trussium_knowledge_agent.private_github import process_github_webhook
from trussium_knowledge_agent.retrieval import search_index
from trussium_knowledge_agent.store import (
    SOURCE_QUARANTINE_DAYS,
    fetch_source_chunks,
    github_webhook_delivery_recorded,
    list_private_github_sources,
    mark_source_access_lost,
    persist_source,
    purge_expired_sources,
    reactivate_source,
    reconcile_source_lifecycle,
    remove_source,
    search_chunks,
    verify_private_source_authorization,
)

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL is not configured")


@pytest.fixture(autouse=True)
def lifecycle_ledger_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KNOWLEDGE_AGENT_LIFECYCLE_LEDGER_PATH", str(tmp_path / "lifecycle.jsonl"))


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


def _github_fixture_source() -> tuple[str, str, Chunk]:
    identity = f"github.com:repository:{uuid4().hex}"
    source_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    content = "The source lifecycle is tested with this passage."
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    chunk = Chunk(
        chunk_id=digest,
        relative_path="guide.md",
        heading_path=("Lifecycle",),
        heading_anchor="lifecycle",
        chunk_index=0,
        content_hash=digest,
        content=content,
        embedding=(1.0, 0.0),
        embedding_provider="test-provider",
        embedding_model="test-model",
    )
    return source_id, identity, chunk


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


def test_index_persists_each_chunks_own_embedding(tmp_path: Path) -> None:
    (tmp_path / "a-orchard.md").write_text("# Orchard\n\nGreen apple trees.", encoding="utf-8")
    (tmp_path / "z-ocean.md").write_text("# Ocean\n\nBlue ocean water.", encoding="utf-8")

    result = index_source(tmp_path, DATABASE_URL or "", _client(), "test-model")
    rows = fetch_source_chunks(DATABASE_URL or "", result.source_id)

    try:
        assert {row[1]: row[10] for row in rows} == {
            "a-orchard.md": "[1,0]",
            "z-ocean.md": "[0,1]",
        }
    finally:
        remove_index(tmp_path, DATABASE_URL or "")


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


def test_evaluation_runs_against_indexed_retrieval(tmp_path: Path) -> None:
    (tmp_path / "orchard.md").write_text(
        "# Orchard\n\nGreen apple trees grow here.", encoding="utf-8"
    )
    (tmp_path / "ocean.md").write_text("# Ocean\n\nBlue ocean water is deep.", encoding="utf-8")
    index_source(tmp_path, DATABASE_URL or "", _client(), "test-model")
    dataset = (
        EvaluationQuery(
            "green-fruit",
            "green apple",
            (ExpectedPassage("orchard.md", "orchard"),),
        ),
    )

    report = evaluate_queries(
        dataset,
        lambda query, limit: search_index(
            query, DATABASE_URL or "", _client(), "test-model", top_k=limit
        ),
        limit=2,
    )

    assert report.hit_rate == 1.0
    assert report.mean_recall == 1.0
    assert report.mean_reciprocal_rank == 1.0
    assert report.queries[0].retrieved[0] == "orchard.md#orchard"


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


def test_existing_local_sources_default_to_active_after_migration() -> None:
    source_id = hashlib.sha256(uuid4().hex.encode("utf-8")).hexdigest()
    persist_source(
        DATABASE_URL or "",
        source_id=source_id,
        display_name="local-manuals",
        revision=None,
        chunks=[],
    )
    try:
        with psycopg.connect(DATABASE_URL or "") as connection:
            lifecycle = connection.execute(
                """SELECT source_kind, source_identity, lifecycle_state,
                          access_lost_at, quarantine_expires_at
                   FROM knowledge_sources WHERE source_id = %s""",
                (source_id,),
            ).fetchone()
        assert lifecycle == ("local", None, "active", None, None)
        assert not mark_source_access_lost(DATABASE_URL or "", source_id)
    finally:
        remove_source(DATABASE_URL or "", source_id)


def test_github_access_loss_hides_source_and_only_matching_identity_reactivates() -> None:
    source_id, identity, chunk = _github_fixture_source()
    persist_source(
        DATABASE_URL or "",
        source_id=source_id,
        display_name="manuals",
        revision="a" * 40,
        chunks=[chunk],
        source_kind="github",
        source_identity=identity,
    )
    try:
        active_results = search_chunks(
            DATABASE_URL or "",
            query_vector=(1.0, 0.0),
            provider="test-provider",
            model="test-model",
        )
        assert any(result.display_name == "manuals" for result in active_results)
        assert mark_source_access_lost(DATABASE_URL or "", source_id)
        assert not mark_source_access_lost(DATABASE_URL or "", source_id)
        hidden_results = search_chunks(
            DATABASE_URL or "",
            query_vector=(1.0, 0.0),
            provider="test-provider",
            model="test-model",
        )
        assert not any(result.display_name == "manuals" for result in hidden_results)
        assert len(fetch_source_chunks(DATABASE_URL or "", source_id)) == 1
        with pytest.raises(ValueError, match="not active"):
            persist_source(
                DATABASE_URL or "",
                source_id=source_id,
                display_name="manuals",
                revision="b" * 40,
                chunks=[chunk],
                source_kind="github",
                source_identity=identity,
            )
        assert not reactivate_source(
            DATABASE_URL or "",
            source_id,
            verified_identity="github.com:repository:other",
        )
        assert reactivate_source(
            DATABASE_URL or "",
            source_id,
            verified_identity=identity,
        )
        active_results = search_chunks(
            DATABASE_URL or "",
            query_vector=(1.0, 0.0),
            provider="test-provider",
            model="test-model",
        )
        assert any(result.display_name == "manuals" for result in active_results)
    finally:
        remove_source(DATABASE_URL or "", source_id)


def test_private_source_lease_expiry_and_fresh_snapshot_reactivation() -> None:
    repository_id = 987654321
    installation_id = 4567
    identity = f"github.com:repository:{repository_id}"
    source_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    _, _, original = _github_fixture_source()
    verified_until = datetime.now(UTC) + timedelta(minutes=5)
    persist_source(
        DATABASE_URL or "",
        source_id=source_id,
        display_name="private-manuals",
        revision="a" * 40,
        chunks=[original],
        source_kind="github",
        source_identity=identity,
        github_installation_id=installation_id,
        authorization_verified_until=verified_until,
    )
    try:
        assert any(
            result.display_name == "private-manuals"
            for result in search_chunks(
                DATABASE_URL or "",
                query_vector=(1.0, 0.0),
                provider="test-provider",
                model="test-model",
            )
        )
        with psycopg.connect(DATABASE_URL or "") as connection:
            connection.execute(
                """UPDATE knowledge_sources SET authorization_verified_until = now() - interval '1 second'
                   WHERE source_id = %s""",
                (source_id,),
            )
        assert not any(
            result.display_name == "private-manuals"
            for result in search_chunks(
                DATABASE_URL or "",
                query_vector=(1.0, 0.0),
                provider="test-provider",
                model="test-model",
            )
        )
        assert verify_private_source_authorization(
            DATABASE_URL or "",
            source_id,
            installation_id,
            datetime.now(UTC) + timedelta(minutes=5),
        )
        assert mark_source_access_lost(DATABASE_URL or "", source_id)
        assert not reactivate_source(DATABASE_URL or "", source_id, verified_identity=identity)
        fresh = Chunk(
            chunk_id="c" * 64,
            relative_path="guide.md",
            heading_path=("Lifecycle",),
            heading_anchor="lifecycle",
            chunk_index=0,
            content_hash="d" * 64,
            content="A complete newly verified private snapshot.",
            embedding=(1.0, 0.0),
            embedding_provider="test-provider",
            embedding_model="test-model",
        )
        persist_source(
            DATABASE_URL or "",
            source_id=source_id,
            display_name="private-manuals",
            revision="b" * 40,
            chunks=[fresh],
            source_kind="github",
            source_identity=identity,
            github_installation_id=installation_id,
            authorization_verified_until=datetime.now(UTC) + timedelta(minutes=5),
        )
        rows = fetch_source_chunks(DATABASE_URL or "", source_id)
        assert rows[0][6] == "A complete newly verified private snapshot."
        assert rows[0][1] == "guide.md"
        assert any(
            result.display_name == "private-manuals"
            for result in search_chunks(
                DATABASE_URL or "",
                query_vector=(1.0, 0.0),
                provider="test-provider",
                model="test-model",
            )
        )
    finally:
        remove_source(DATABASE_URL or "", source_id)


def test_private_github_webhook_journals_loss_and_deduplicates_delivery() -> None:
    repository_id = 987654322
    installation_id = 4567
    identity = f"github.com:repository:{repository_id}"
    source_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    _, _, chunk = _github_fixture_source()
    persist_source(
        DATABASE_URL or "",
        source_id=source_id,
        display_name="private-webhook-docs",
        revision="a" * 40,
        chunks=[chunk],
        source_kind="github",
        source_identity=identity,
        github_installation_id=installation_id,
        authorization_verified_until=datetime.now(UTC) + timedelta(minutes=5),
    )
    try:
        assert list_private_github_sources(DATABASE_URL or "", installation_id) == [
            (source_id, repository_id, identity)
        ]
        payload = {
            "action": "removed",
            "installation": {"id": installation_id},
            "repositories_removed": [{"id": repository_id}],
        }
        assert process_github_webhook(
            DATABASE_URL or "",
            "integration-delivery-1",
            "installation_repositories",
            payload,
            expected_installation_id=installation_id,
        )
        assert not process_github_webhook(
            DATABASE_URL or "",
            "integration-delivery-1",
            "installation_repositories",
            payload,
            expected_installation_id=installation_id,
        )
        assert github_webhook_delivery_recorded(DATABASE_URL or "", "integration-delivery-1")
        with psycopg.connect(DATABASE_URL or "") as connection:
            lifecycle = connection.execute(
                """SELECT lifecycle_state, authorization_verified_until
                   FROM knowledge_sources WHERE source_id = %s""",
                (source_id,),
            ).fetchone()
        assert lifecycle == ("access_lost", None)
        assert not any(
            result.display_name == "private-webhook-docs"
            for result in search_chunks(
                DATABASE_URL or "",
                query_vector=(1.0, 0.0),
                provider="test-provider",
                model="test-model",
            )
        )
    finally:
        remove_source(DATABASE_URL or "", source_id)


def test_expired_github_quarantine_purges_source_and_chunks() -> None:
    source_id, identity, chunk = _github_fixture_source()
    persist_source(
        DATABASE_URL or "",
        source_id=source_id,
        display_name="manuals",
        revision="a" * 40,
        chunks=[chunk],
        source_kind="github",
        source_identity=identity,
    )
    try:
        assert mark_source_access_lost(DATABASE_URL or "", source_id)
        with psycopg.connect(DATABASE_URL or "") as connection:
            lifecycle = connection.execute(
                """SELECT quarantine_expires_at - access_lost_at
                   FROM knowledge_sources WHERE source_id = %s""",
                (source_id,),
            ).fetchone()
        assert lifecycle is not None
        assert lifecycle[0] == timedelta(days=SOURCE_QUARANTINE_DAYS)
        with psycopg.connect(DATABASE_URL or "", autocommit=True) as connection:
            connection.execute(
                """UPDATE knowledge_sources
                   SET quarantine_expires_at = now() + interval '1 hour'
                   WHERE source_id = %s""",
                (source_id,),
            )
        assert purge_expired_sources(DATABASE_URL or "") == 0
        assert len(fetch_source_chunks(DATABASE_URL or "", source_id)) == 1
        with psycopg.connect(DATABASE_URL or "", autocommit=True) as connection:
            connection.execute(
                """UPDATE knowledge_sources
                   SET quarantine_expires_at = now()
                   WHERE source_id = %s""",
                (source_id,),
            )
        assert not reactivate_source(
            DATABASE_URL or "",
            source_id,
            verified_identity=identity,
        )
        assert purge_expired_sources(DATABASE_URL or "") == 1
        assert purge_expired_sources(DATABASE_URL or "") == 0
        assert fetch_source_chunks(DATABASE_URL or "", source_id) == []
    finally:
        remove_source(DATABASE_URL or "", source_id)


def test_database_rejects_quarantine_beyond_thirty_days() -> None:
    source_id, identity, _chunk = _github_fixture_source()
    persist_source(
        DATABASE_URL or "",
        source_id=source_id,
        display_name="manuals",
        revision=None,
        chunks=[],
        source_kind="github",
        source_identity=identity,
    )
    try:
        assert mark_source_access_lost(DATABASE_URL or "", source_id)
        with (
            pytest.raises(psycopg.IntegrityError),
            psycopg.connect(DATABASE_URL or "") as connection,
        ):
            connection.execute(
                """UPDATE knowledge_sources
                   SET quarantine_expires_at = access_lost_at + interval '31 days'
                   WHERE source_id = %s""",
                (source_id,),
            )
    finally:
        remove_source(DATABASE_URL or "", source_id)


def test_restore_reconciliation_removes_deleted_hides_revoked_and_preserves_unrelated(
    tmp_path: Path,
) -> None:
    deleted_id, deleted_identity, deleted_chunk = _github_fixture_source()
    _, _, revoked_chunk = _github_fixture_source()
    revoked_identity = "github.com:repository:987654321"
    revoked_id = hashlib.sha256(revoked_identity.encode("utf-8")).hexdigest()
    active_id = hashlib.sha256(uuid4().hex.encode("utf-8")).hexdigest()
    persist_source(
        DATABASE_URL or "",
        source_id=deleted_id,
        display_name="deleted-repo",
        revision="a" * 40,
        chunks=[deleted_chunk],
        source_kind="github",
        source_identity=deleted_identity,
    )
    persist_source(
        DATABASE_URL or "",
        source_id=revoked_id,
        display_name="revoked-repo",
        revision="b" * 40,
        chunks=[revoked_chunk],
        source_kind="github",
        source_identity=revoked_identity,
        github_installation_id=4567,
        authorization_verified_until=datetime.now(UTC) + timedelta(minutes=10),
    )
    persist_source(
        DATABASE_URL or "",
        source_id=active_id,
        display_name="unrelated-local",
        revision=None,
        chunks=[],
    )
    ledger_path = tmp_path / "restore-ledger.jsonl"
    append_lifecycle_event(
        ledger_path,
        source_id=deleted_id,
        source_kind="github",
        source_identity=deleted_identity,
        event_type="deleted",
    )
    append_lifecycle_event(
        ledger_path,
        source_id=revoked_id,
        source_kind="github",
        source_identity=revoked_identity,
        event_type="access_lost",
    )

    # Simulate restoring an older backup with both sources active.
    with psycopg.connect(DATABASE_URL or "") as connection:
        connection.execute(
            """UPDATE knowledge_sources SET lifecycle_state = 'active', access_lost_at = NULL,
                       quarantine_expires_at = NULL, deleted_at = NULL,
                       authorization_verified_until = now() + interval '10 minutes'
               WHERE source_id IN (%s, %s)""",
            (deleted_id, revoked_id),
        )

    assert reconcile_source_lifecycle(DATABASE_URL or "", ledger_path=ledger_path) == (1, 1, 0)
    assert fetch_source_chunks(DATABASE_URL or "", deleted_id) == []
    assert len(fetch_source_chunks(DATABASE_URL or "", revoked_id)) == 1
    assert fetch_source_chunks(DATABASE_URL or "", active_id) == []
    with psycopg.connect(DATABASE_URL or "") as connection:
        states = connection.execute(
            "SELECT source_id, lifecycle_state FROM knowledge_sources WHERE source_id IN (%s, %s)",
            (revoked_id, active_id),
        ).fetchall()
    assert dict(states) == {revoked_id: "access_lost", active_id: "active"}
    assert reconcile_source_lifecycle(DATABASE_URL or "", ledger_path=ledger_path) == (0, 0, 1)
    remove_source(DATABASE_URL or "", revoked_id)
    remove_source(DATABASE_URL or "", active_id)


def test_removal_journal_failure_keeps_source_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id, identity, chunk = _github_fixture_source()
    persist_source(
        DATABASE_URL or "",
        source_id=source_id,
        display_name="manuals",
        revision="a" * 40,
        chunks=[chunk],
        source_kind="github",
        source_identity=identity,
    )
    with monkeypatch.context() as patcher:
        patcher.setattr(
            "trussium_knowledge_agent.store.append_lifecycle_event",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("journal unavailable")),
        )
        with pytest.raises(OSError, match="journal unavailable"):
            remove_source(DATABASE_URL or "", source_id)

    with psycopg.connect(DATABASE_URL or "") as connection:
        lifecycle = connection.execute(
            "SELECT lifecycle_state FROM knowledge_sources WHERE source_id = %s", (source_id,)
        ).fetchone()
    assert lifecycle == ("deleted",)
    results = search_chunks(
        DATABASE_URL or "",
        query_vector=(1.0, 0.0),
        provider="test-provider",
        model="test-model",
    )
    assert not any(result.display_name == "manuals" for result in results)
    assert remove_source(DATABASE_URL or "", source_id)


def test_restore_reconciliation_identity_mismatch_rolls_back_all_changes(
    tmp_path: Path,
) -> None:
    first_id, first_identity, first_chunk = _github_fixture_source()
    second_id, second_identity, second_chunk = _github_fixture_source()
    persist_source(
        DATABASE_URL or "",
        source_id=first_id,
        display_name="first-repo",
        revision="a" * 40,
        chunks=[first_chunk],
        source_kind="github",
        source_identity=first_identity,
    )
    persist_source(
        DATABASE_URL or "",
        source_id=second_id,
        display_name="second-repo",
        revision="b" * 40,
        chunks=[second_chunk],
        source_kind="github",
        source_identity=second_identity,
    )
    ledger_path = tmp_path / "mismatched-identity.jsonl"
    append_lifecycle_event(
        ledger_path,
        source_id=first_id,
        source_kind="github",
        source_identity=first_identity,
        event_type="access_lost",
    )
    append_lifecycle_event(
        ledger_path,
        source_id=second_id,
        source_kind="github",
        source_identity="github.com:repository:unexpected",
        event_type="access_lost",
    )

    with pytest.raises(ValueError, match="identity does not match"):
        reconcile_source_lifecycle(DATABASE_URL or "", ledger_path=ledger_path)
    with psycopg.connect(DATABASE_URL or "") as connection:
        states = connection.execute(
            "SELECT source_id, lifecycle_state FROM knowledge_sources WHERE source_id IN (%s, %s)",
            (first_id, second_id),
        ).fetchall()
    assert dict(states) == {first_id: "active", second_id: "active"}
    remove_source(DATABASE_URL or "", first_id)
    remove_source(DATABASE_URL or "", second_id)


def test_public_github_refresh_is_pinned_transactional_searchable_and_removable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = "github.com:repository:424242"
    source_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def make_snapshot(revision: str, content: str) -> GitHubSnapshot:
        raw = content.encode("utf-8")
        chunks = parse_markdown(
            relative_path="guide.md",
            source=content,
            content_hash=hashlib.sha256(raw).hexdigest(),
            max_chars=1600,
            overlap_chars=200,
        )
        return GitHubSnapshot(424242, "acme/docs", revision, 1, tuple(chunks))

    current = make_snapshot("a" * 40, "# Guide\n\nGreen apple snapshot.")

    class SnapshotEmbeddingsClient:
        def embed(self, *, model: str, inputs: Sequence[str]) -> EmbeddingsBatch:
            vectors = tuple(
                (0.0, 0.0, 1.0) if "ocean" in text.lower() else (0.0, 1.0, 0.0) for text in inputs
            )
            return EmbeddingsBatch("snapshot-provider", model, vectors)

    embeddings_client = SnapshotEmbeddingsClient()
    monkeypatch.setattr(
        "trussium_knowledge_agent.indexer.fetch_public_github_snapshot",
        lambda *_args, **_kwargs: current,
    )
    first = index_public_github_source(
        "acme/docs", "main", DATABASE_URL or "", embeddings_client, "test-model"
    )
    assert first.source_id == source_id
    assert first.revision == "a" * 40
    previous_rows = fetch_source_chunks(DATABASE_URL or "", source_id)

    monkeypatch.setattr(
        "trussium_knowledge_agent.indexer.fetch_public_github_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            GitHubSnapshotError("transient GitHub failure")
        ),
    )
    with pytest.raises(GitHubSnapshotError, match="transient"):
        index_public_github_source(
            "acme/docs", "main", DATABASE_URL or "", embeddings_client, "test-model"
        )
    assert fetch_source_chunks(DATABASE_URL or "", source_id) == previous_rows

    current = make_snapshot("b" * 40, "# Guide\n\nBlue ocean replacement.")
    monkeypatch.setattr(
        "trussium_knowledge_agent.indexer.fetch_public_github_snapshot",
        lambda *_args, **_kwargs: current,
    )
    refreshed = index_public_github_source(
        "acme/docs", "main", DATABASE_URL or "", embeddings_client, "test-model"
    )
    rows = fetch_source_chunks(DATABASE_URL or "", source_id)
    assert refreshed.revision == "b" * 40
    assert len(rows) == 1
    assert rows[0][1] == "guide.md"
    assert "Blue ocean replacement." in rows[0][6]
    assert "Green apple snapshot." not in rows[0][6]
    results = search_chunks(
        DATABASE_URL or "",
        query_vector=(0.0, 0.0, 1.0),
        provider="snapshot-provider",
        model="test-model",
    )
    assert any(result.display_name == "acme/docs" for result in results)
    assert remove_source(DATABASE_URL or "", source_id)
    assert fetch_source_chunks(DATABASE_URL or "", source_id) == []
