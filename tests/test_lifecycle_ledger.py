from __future__ import annotations

import json
from pathlib import Path

import pytest

from trussium_knowledge_agent.lifecycle_ledger import (
    MAX_LEDGER_BYTES,
    append_lifecycle_event,
    read_lifecycle_events,
)


def test_lifecycle_ledger_appends_and_deduplicates_events(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle.jsonl"
    source_id = "a" * 64

    assert append_lifecycle_event(
        path,
        source_id=source_id,
        source_kind="github",
        source_identity="github.com:repository:123",
        event_type="access_lost",
    )
    assert not append_lifecycle_event(
        path,
        source_id=source_id,
        source_kind="github",
        source_identity="github.com:repository:123",
        event_type="access_lost",
    )
    assert append_lifecycle_event(
        path,
        source_id=source_id,
        source_kind="github",
        source_identity="github.com:repository:123",
        event_type="deleted",
    )
    assert not append_lifecycle_event(
        path,
        source_id=source_id,
        source_kind="github",
        source_identity="github.com:repository:123",
        event_type="deleted",
    )
    events = read_lifecycle_events(path)

    assert [event.event_type for event in events] == ["access_lost", "deleted"]
    assert events[-1].source_identity == "github.com:repository:123"
    assert path.stat().st_mode & 0o777 == 0o600


def test_lifecycle_ledger_rejects_identity_change_after_first_record(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle.jsonl"
    append_lifecycle_event(
        path,
        source_id="b" * 64,
        source_kind="github",
        source_identity="github.com:repository:123",
        event_type="access_lost",
    )

    with pytest.raises(ValueError, match="identity mismatch"):
        append_lifecycle_event(
            path,
            source_id="b" * 64,
            source_kind="github",
            source_identity="github.com:repository:456",
            event_type="deleted",
        )


@pytest.mark.parametrize("contents", [b'{"partial":true}', b"not-json\n"])
def test_lifecycle_ledger_rejects_incomplete_or_invalid_records(
    tmp_path: Path, contents: bytes
) -> None:
    path = tmp_path / "lifecycle.jsonl"
    path.write_bytes(contents)
    path.chmod(0o600)

    with pytest.raises(ValueError):
        read_lifecycle_events(path)


def test_lifecycle_ledger_rejects_insecure_permissions_and_oversize(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle.jsonl"
    path.write_text("", encoding="utf-8")
    path.chmod(0o640)
    with pytest.raises(ValueError, match="permissions"):
        read_lifecycle_events(path)

    path.chmod(0o600)
    path.write_bytes(b" " * (MAX_LEDGER_BYTES + 1))
    with pytest.raises(ValueError, match="size limit"):
        read_lifecycle_events(path)


def test_lifecycle_ledger_rejects_symlinks(tmp_path: Path) -> None:
    target = tmp_path / "real-ledger.jsonl"
    target.write_text("", encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "linked-ledger.jsonl"
    link.symlink_to(target)

    with pytest.raises(OSError):
        read_lifecycle_events(link)


def test_lifecycle_ledger_records_no_repository_content(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle.jsonl"
    append_lifecycle_event(
        path,
        source_id="c" * 64,
        source_kind="github",
        source_identity="github.com:repository:789",
        event_type="deleted",
    )

    record = json.loads(path.read_text(encoding="utf-8"))
    assert set(record) == {
        "version",
        "source_id",
        "source_kind",
        "source_identity",
        "event_type",
        "recorded_at",
    }
    assert "content" not in record and "token" not in record
