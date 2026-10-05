from __future__ import annotations

import hashlib

import pytest

from trussium_knowledge_agent.chunking import parse_markdown, split_text


def test_parse_markdown_preserves_heading_context_and_duplicate_anchors() -> None:
    source = """# Guide

Intro text.

## Setup

First setup details.

## Setup

Second setup details.

```markdown
## Not a heading
```
Code sample.
"""
    content_hash = hashlib.sha256(source.encode()).hexdigest()

    chunks = parse_markdown(relative_path="guide.md", source=source, content_hash=content_hash)

    assert [chunk.heading_path for chunk in chunks] == [
        ("Guide",),
        ("Guide", "Setup"),
        ("Guide", "Setup"),
    ]
    assert [chunk.heading_anchor for chunk in chunks] == [
        "guide",
        "setup",
        "setup-1",
    ]
    assert "First setup details." in chunks[1].content
    assert "Second setup details." in chunks[2].content
    assert "## Not a heading" in chunks[2].content
    assert all(chunk.content_hash == content_hash for chunk in chunks)


def test_chunk_identifiers_are_stable_for_same_section_and_position() -> None:
    first = parse_markdown(
        relative_path="guide.md", source="## Setup\n\nOld text.", content_hash="a" * 64
    )
    changed = parse_markdown(
        relative_path="guide.md", source="## Setup\n\nNew text.", content_hash="b" * 64
    )

    assert first[0].chunk_id == changed[0].chunk_id
    assert first[0].content_hash != changed[0].content_hash


def test_split_text_observes_bounds_and_overlap() -> None:
    text = " ".join(f"word{index}" for index in range(120))

    chunks = split_text(text, max_chars=100, overlap_chars=20)

    assert len(chunks) > 1
    assert all(0 < len(chunk) <= 100 for chunk in chunks)
    assert all(chunks[index].split()[0] in chunks[index - 1] for index in range(1, len(chunks)))


@pytest.mark.parametrize(("max_chars", "overlap_chars"), [(0, 0), (10, -1), (10, 10)])
def test_split_text_rejects_invalid_bounds(max_chars: int, overlap_chars: int) -> None:
    with pytest.raises(ValueError):
        split_text("some text", max_chars=max_chars, overlap_chars=overlap_chars)
