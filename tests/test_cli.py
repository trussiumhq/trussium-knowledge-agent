from __future__ import annotations

from pathlib import Path
from typing import Self

import pytest

from trussium_knowledge_agent import cli
from trussium_knowledge_agent.answers import AnswerCitation, AnswerResult
from trussium_knowledge_agent.indexer import IndexResult
from trussium_knowledge_agent.store import SearchResult


class FakeRuntime:
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def test_cli_index_reports_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("TRUSSIUM_EMBEDDING_MODEL", "test-model")
    monkeypatch.setattr(cli, "TrussiumEmbeddingsClient", lambda *args, **kwargs: FakeRuntime())
    monkeypatch.setattr(
        cli,
        "index_source",
        lambda source_root, database_url, *args, **kwargs: IndexResult(
            source_id="a" * 64,
            display_name=Path(source_root).name,
            revision="b" * 40,
            file_count=2,
            chunk_count=3,
            embedding_provider="test-provider",
            embedding_model="test-model",
            embedding_dimension=2,
        ),
    )

    exit_code = cli.main(["index", "/tmp/manuals"])

    assert exit_code == 0
    assert "Indexed 2 Markdown files into 3 chunks" in capsys.readouterr().out


def test_cli_search_prints_citation_and_score(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("TRUSSIUM_EMBEDDING_MODEL", "test-model")
    monkeypatch.setattr(cli, "TrussiumEmbeddingsClient", lambda *args, **kwargs: FakeRuntime())
    monkeypatch.setattr(
        cli,
        "search_index",
        lambda query, database_url, client, model, *, top_k: [
            SearchResult(
                display_name="manuals",
                revision="a" * 40,
                relative_path="guide.md",
                heading_path=("Setup",),
                heading_anchor="setup",
                content_hash="b" * 64,
                content="Install the runtime.",
                score=0.875,
            )
        ],
    )

    exit_code = cli.main(["search", "install runtime", "--limit", "1"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "manuals/guide.md#setup" in output
    assert "0.8750" in output
    assert "Install the runtime." in output


def test_cli_ask_prints_answer_and_validated_source(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("TRUSSIUM_EMBEDDING_MODEL", "embed-model")
    monkeypatch.setenv("TRUSSIUM_CHAT_MODEL", "chat-model")
    monkeypatch.setattr(cli, "TrussiumEmbeddingsClient", lambda *args, **kwargs: FakeRuntime())
    monkeypatch.setattr(cli, "TrussiumChatClient", lambda *args, **kwargs: FakeRuntime())
    passage = SearchResult(
        display_name="manuals",
        revision="a" * 40,
        relative_path="guide.md",
        heading_path=("Setup",),
        heading_anchor="setup",
        content_hash="b" * 64,
        content="Install the runtime.",
        score=0.9,
    )
    monkeypatch.setattr(cli, "search_index", lambda *args, **kwargs: [passage])
    monkeypatch.setattr(
        cli,
        "answer_from_passages",
        lambda *args: AnswerResult(
            "answered",
            "Install the runtime first [C1].",
            (AnswerCitation("C1", passage),),
        ),
    )

    exit_code = cli.main(["ask", "How do I set up?", "--limit", "1"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Install the runtime first [C1]." in output
    assert "[C1] manuals/guide.md#setup (Setup)" in output


def test_cli_ask_requires_a_chat_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("TRUSSIUM_EMBEDDING_MODEL", "embed-model")
    monkeypatch.delenv("TRUSSIUM_CHAT_MODEL", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["ask", "question"])

    assert exc_info.value.code == 2


def test_cli_requires_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["index", "/tmp/manuals"])

    assert exc_info.value.code == 2
