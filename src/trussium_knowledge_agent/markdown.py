"""Safe discovery and parsing helpers for local Markdown repositories."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

from trussium_knowledge_agent.chunking import Chunk, parse_markdown

MAX_MARKDOWN_BYTES = 5 * 1024 * 1024
_SKIPPED_DIRECTORIES = {".git"}


def canonical_root(source_root: str | Path, *, must_exist: bool = True) -> Path:
    """Normalize a selected source path, rejecting a symlinked root."""
    supplied = Path(source_root).expanduser()
    if supplied.is_symlink():
        raise ValueError("the selected source root must not be a symbolic link")
    try:
        root = supplied.resolve(strict=must_exist)
    except OSError as exc:
        raise ValueError(f"cannot resolve source root: {supplied}") from exc
    if must_exist and not root.is_dir():
        raise ValueError(f"source root is not a directory: {root}")
    return root


def discover_markdown_files(source_root: str | Path) -> tuple[Path, list[Path]]:
    """Return the canonical root and sorted Markdown files beneath it.

    Symlinked files/directories and the repository's .git metadata are not
    followed. Any candidate resolving outside the chosen root is skipped.
    """
    root = canonical_root(source_root)
    found: list[Path] = []

    def fail_walk(error: OSError) -> None:
        raise error

    for current, directory_names, file_names in os.walk(
        root, topdown=True, followlinks=False, onerror=fail_walk
    ):
        current_path = Path(current)
        kept_directories: list[str] = []
        for name in sorted(directory_names):
            candidate = current_path / name
            if name in _SKIPPED_DIRECTORIES or candidate.is_symlink():
                continue
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(root)
            except (OSError, ValueError):
                continue
            if resolved.is_dir():
                kept_directories.append(name)
        directory_names[:] = kept_directories

        for name in sorted(file_names):
            candidate = current_path / name
            if candidate.suffix.lower() != ".md" or candidate.is_symlink():
                continue
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(root)
            except (OSError, ValueError):
                continue
            if resolved.is_file():
                found.append(resolved)

    return root, sorted(found, key=lambda path: path.relative_to(root).as_posix())


def git_revision(source_root: Path) -> str | None:
    """Return the checked-out Git revision without running repository hooks."""
    if not (source_root / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(source_root), "rev-parse", "--verify", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    revision = result.stdout.strip()
    return revision if result.returncode == 0 and revision else None


def load_chunks(
    source_root: str | Path,
    *,
    max_chars: int = 1600,
    overlap_chars: int = 200,
) -> tuple[Path, str | None, int, list[Chunk]]:
    """Read and chunk a source before any database transaction begins."""
    root, paths = discover_markdown_files(source_root)
    chunks: list[Chunk] = []
    for path in paths:
        size = path.stat().st_size
        if size > MAX_MARKDOWN_BYTES:
            relative = path.relative_to(root).as_posix()
            raise ValueError(f"Markdown file exceeds {MAX_MARKDOWN_BYTES} bytes: {relative}")
        try:
            file_descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            relative = path.relative_to(root).as_posix()
            raise ValueError(f"cannot safely open Markdown file: {relative}") from exc
        with os.fdopen(file_descriptor, "rb") as source_file:
            raw = source_file.read(MAX_MARKDOWN_BYTES + 1)
        if len(raw) > MAX_MARKDOWN_BYTES:
            relative = path.relative_to(root).as_posix()
            raise ValueError(f"Markdown file exceeds {MAX_MARKDOWN_BYTES} bytes: {relative}")
        relative_path = path.relative_to(root).as_posix()
        try:
            source = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Markdown file is not valid UTF-8: {relative_path}") from exc
        content_hash = hashlib.sha256(raw).hexdigest()
        chunks.extend(
            parse_markdown(
                relative_path=relative_path,
                source=source,
                content_hash=content_hash,
                max_chars=max_chars,
                overlap_chars=overlap_chars,
            )
        )
    return root, git_revision(root), len(paths), chunks
