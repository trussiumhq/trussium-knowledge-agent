from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from trussium_knowledge_agent.indexer import index_source, remove_index
from trussium_knowledge_agent.store import fetch_source_chunks

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL is not configured")


def _source_id(path: Path) -> str:
    result = index_source(path, DATABASE_URL or "")
    return result.source_id


def test_index_is_idempotent_and_removes_stale_files(tmp_path: Path) -> None:
    (tmp_path / "guide.md").write_text("# Guide\n\nOriginal passage.", encoding="utf-8")
    (tmp_path / "keep.md").write_text("# Keep\n\nKeep this.", encoding="utf-8")

    first = index_source(tmp_path, DATABASE_URL or "")
    first_rows = fetch_source_chunks(DATABASE_URL or "", first.source_id)
    second = index_source(tmp_path, DATABASE_URL or "")
    second_rows = fetch_source_chunks(DATABASE_URL or "", second.source_id)

    assert first.source_id == second.source_id
    assert first_rows == second_rows
    assert len(second_rows) == 2

    (tmp_path / "guide.md").write_text("# Guide\n\nUpdated passage.", encoding="utf-8")
    (tmp_path / "keep.md").unlink()
    updated = index_source(tmp_path, DATABASE_URL or "")
    updated_rows = fetch_source_chunks(DATABASE_URL or "", updated.source_id)

    assert len(updated_rows) == 1
    assert updated_rows[0][1] == "guide.md"
    assert updated_rows[0][5] != first_rows[0][5]
    assert remove_index(tmp_path, DATABASE_URL or "")
    assert fetch_source_chunks(DATABASE_URL or "", updated.source_id) == []
    assert not remove_index(tmp_path, DATABASE_URL or "")


def test_empty_repository_creates_removable_empty_index(tmp_path: Path) -> None:
    result = index_source(tmp_path, DATABASE_URL or "")

    assert result.file_count == 0
    assert result.chunk_count == 0
    assert fetch_source_chunks(DATABASE_URL or "", result.source_id) == []
    assert remove_index(tmp_path, DATABASE_URL or "")


def test_database_failure_rolls_back_source_replacement(tmp_path: Path) -> None:
    source = tmp_path / "guide.md"
    source.write_text("# Guide\n\nPrevious durable text.", encoding="utf-8")
    source_id = _source_id(tmp_path)
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
            index_source(tmp_path, DATABASE_URL or "")
    finally:
        with psycopg.connect(DATABASE_URL or "", autocommit=True) as connection:
            connection.execute(f"DROP TRIGGER IF EXISTS {trigger_name} ON document_chunks")
            connection.execute(f"DROP FUNCTION IF EXISTS {function_name}()")

    assert fetch_source_chunks(DATABASE_URL or "", source_id) == previous_rows
