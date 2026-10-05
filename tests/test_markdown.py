from __future__ import annotations

from pathlib import Path

import pytest

from trussium_knowledge_agent.markdown import (
    MAX_MARKDOWN_BYTES,
    canonical_root,
    discover_markdown_files,
    load_chunks,
)


def test_discovery_is_sorted_and_ignores_non_markdown_git_and_symlinks(
    tmp_path: Path,
) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    (root / "z.md").write_text("Z", encoding="utf-8")
    nested = root / "guide"
    nested.mkdir()
    (nested / "a.MD").write_text("A", encoding="utf-8")
    (root / "notes.txt").write_text("ignored", encoding="utf-8")
    git_dir = root / ".git"
    git_dir.mkdir()
    (git_dir / "internal.md").write_text("ignored", encoding="utf-8")

    outside_file = tmp_path / "outside.md"
    outside_file.write_text("private", encoding="utf-8")
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "external.md").write_text("private", encoding="utf-8")
    try:
        (root / "linked.md").symlink_to(outside_file)
        (root / "linked-dir").symlink_to(outside_dir, target_is_directory=True)
    except OSError:
        pytest.skip("this platform does not allow creating symlinks")

    canonical, files = discover_markdown_files(root)

    assert canonical == root.resolve()
    assert [path.relative_to(canonical).as_posix() for path in files] == [
        "guide/a.MD",
        "z.md",
    ]


def test_canonical_root_rejects_symlink(tmp_path: Path) -> None:
    root = tmp_path / "real"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic link"):
        canonical_root(alias)


def test_load_chunks_tracks_file_hash_and_relative_path(tmp_path: Path) -> None:
    root = tmp_path / "docs"
    (root / "nested").mkdir(parents=True)
    (root / "nested" / "guide.md").write_text("# Guide\n\nUseful text.", encoding="utf-8")

    actual_root, revision, file_count, chunks = load_chunks(root)

    assert actual_root == root.resolve()
    assert revision is None
    assert file_count == 1
    assert chunks[0].relative_path == "nested/guide.md"
    assert chunks[0].heading_path == ("Guide",)
    assert len(chunks[0].content_hash) == 64


def test_load_chunks_rejects_oversized_and_invalid_utf8_files(tmp_path: Path) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    oversized = root / "large.md"
    oversized.write_bytes(b"x" * (MAX_MARKDOWN_BYTES + 1))

    with pytest.raises(ValueError, match="exceeds"):
        load_chunks(root)

    oversized.unlink()
    (root / "invalid.md").write_bytes(b"\xff")
    with pytest.raises(ValueError, match="UTF-8"):
        load_chunks(root)
