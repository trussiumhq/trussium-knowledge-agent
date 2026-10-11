"""Durable, append-only source lifecycle records kept outside PostgreSQL backups."""

from __future__ import annotations

import fcntl
import json
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

MAX_LEDGER_BYTES = 16 * 1024 * 1024
LifecycleEventType = Literal["access_lost", "deleted"]


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    """One validated source restriction from the external lifecycle journal."""

    source_id: str
    source_kind: Literal["local", "github"]
    source_identity: str | None
    event_type: LifecycleEventType
    recorded_at: datetime


def append_lifecycle_event(
    path: str | Path,
    *,
    source_id: str,
    source_kind: Literal["local", "github"],
    source_identity: str | None,
    event_type: LifecycleEventType,
) -> bool:
    """Durably append one restriction, deduplicating a repeated terminal state."""
    event = _make_event(source_id, source_kind, source_identity, event_type)
    ledger_path = Path(path)
    try:
        ledger_path.stat(follow_symlinks=False)
        created = False
    except FileNotFoundError:
        created = True
    descriptor = _open_ledger(ledger_path, os.O_RDWR | os.O_CREAT | os.O_APPEND)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        existing = _read_descriptor(descriptor)
        previous = next(
            (item for item in reversed(existing) if item.source_id == event.source_id), None
        )
        if previous is not None:
            if previous.source_identity != event.source_identity:
                raise ValueError("lifecycle ledger source identity mismatch")
            if previous.event_type == "deleted":
                return False
            if previous.event_type == event.event_type:
                return False
        payload = {
            "version": 1,
            "source_id": event.source_id,
            "source_kind": event.source_kind,
            "source_identity": event.source_identity,
            "event_type": event.event_type,
            "recorded_at": event.recorded_at.isoformat(),
        }
        encoded = (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode()
        if os.fstat(descriptor).st_size + len(encoded) > MAX_LEDGER_BYTES:
            raise ValueError("lifecycle ledger reached its size limit")
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        if created:
            directory = os.open(ledger_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return True
    finally:
        os.close(descriptor)


def read_lifecycle_events(path: str | Path) -> tuple[LifecycleEvent, ...]:
    """Read and validate the complete bounded journal; partial/corrupt data fails closed."""
    descriptor = _open_ledger(path, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        return tuple(_read_descriptor(descriptor))
    finally:
        os.close(descriptor)


def _open_ledger(path: str | Path, flags: int) -> int:
    ledger_path = Path(path)
    if not ledger_path.is_absolute() or not ledger_path.parent.is_dir():
        raise ValueError("lifecycle ledger path must be absolute and its parent must exist")
    if ledger_path.parent.is_symlink():
        raise ValueError("lifecycle ledger directory cannot be a symbolic link")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(ledger_path, flags | nofollow, 0o600)
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise ValueError("lifecycle ledger must be a regular file")
    if metadata.st_mode & 0o077:
        os.close(descriptor)
        raise ValueError("lifecycle ledger permissions must not allow group or other access")
    if metadata.st_size > MAX_LEDGER_BYTES:
        os.close(descriptor)
        raise ValueError("lifecycle ledger exceeds its size limit")
    return descriptor


def _read_descriptor(descriptor: int) -> list[LifecycleEvent]:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = MAX_LEDGER_BYTES + 1
    while remaining > 0:
        block = os.read(descriptor, min(65536, remaining))
        if not block:
            break
        chunks.append(block)
        remaining -= len(block)
    raw = b"".join(chunks)
    if len(raw) > MAX_LEDGER_BYTES:
        raise ValueError("lifecycle ledger exceeds its size limit")
    if raw and not raw.endswith(b"\n"):
        raise ValueError("lifecycle ledger ends with an incomplete record")
    events: list[LifecycleEvent] = []
    last_by_source: dict[str, LifecycleEvent] = {}
    for line in raw.splitlines():
        try:
            decoded = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("lifecycle ledger contains an invalid record") from error
        if not isinstance(decoded, dict) or set(decoded) != {
            "version",
            "source_id",
            "source_kind",
            "source_identity",
            "event_type",
            "recorded_at",
        }:
            raise ValueError("lifecycle ledger record has an invalid shape")
        event = _make_event(
            decoded["source_id"],
            decoded["source_kind"],
            decoded["source_identity"],
            decoded["event_type"],
            recorded_at=decoded["recorded_at"],
        )
        if decoded["version"] != 1:
            raise ValueError("lifecycle ledger version is unsupported")
        previous = last_by_source.get(event.source_id)
        if previous is not None:
            if previous.source_identity != event.source_identity:
                raise ValueError("lifecycle ledger source identity mismatch")
            if previous.event_type == "deleted":
                raise ValueError("deleted source has a later lifecycle event")
        last_by_source[event.source_id] = event
        events.append(event)
    return events


def _make_event(
    source_id: object,
    source_kind: object,
    source_identity: object,
    event_type: object,
    *,
    recorded_at: object | None = None,
) -> LifecycleEvent:
    if not isinstance(source_id, str) or len(source_id) != 64:
        raise ValueError("lifecycle source ID must be a SHA-256 hex digest")
    try:
        int(source_id, 16)
    except ValueError as error:
        raise ValueError("lifecycle source ID must be a SHA-256 hex digest") from error
    if source_kind not in ("local", "github"):
        raise ValueError("lifecycle source kind is invalid")
    if source_kind == "local":
        if source_identity is not None:
            raise ValueError("local lifecycle events cannot have an external identity")
        parsed_identity = None
    else:
        if not isinstance(source_identity, str) or not 1 <= len(source_identity) <= 256:
            raise ValueError("GitHub lifecycle events require a bounded source identity")
        parsed_identity = source_identity
    if event_type not in ("access_lost", "deleted"):
        raise ValueError("lifecycle event type is invalid")
    parsed_time = datetime.now(UTC) if recorded_at is None else _parse_time(recorded_at)
    return LifecycleEvent(
        source_id=source_id,
        source_kind=source_kind,
        source_identity=parsed_identity,
        event_type=event_type,
        recorded_at=parsed_time,
    )


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("lifecycle event timestamp must be a string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("lifecycle event timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise ValueError("lifecycle event timestamp must include a timezone")
    return parsed
