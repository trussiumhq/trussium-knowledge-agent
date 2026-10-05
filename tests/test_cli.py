from __future__ import annotations

from pathlib import Path

import pytest

from trussium_knowledge_agent import cli
from trussium_knowledge_agent.indexer import IndexResult


def test_cli_index_reports_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setattr(
        cli,
        "index_source",
        lambda source_root, database_url, **kwargs: IndexResult(
            source_id="a" * 64,
            display_name=Path(source_root).name,
            revision="b" * 40,
            file_count=2,
            chunk_count=3,
        ),
    )

    exit_code = cli.main(["index", "/tmp/manuals"])

    assert exit_code == 0
    assert "Indexed 2 Markdown files into 3 chunks" in capsys.readouterr().out


def test_cli_requires_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["index", "/tmp/manuals"])

    assert exc_info.value.code == 2
