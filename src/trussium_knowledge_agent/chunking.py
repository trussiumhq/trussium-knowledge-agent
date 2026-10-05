"""Deterministic heading-aware Markdown chunking."""

from __future__ import annotations

import hashlib
import re
import string
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Chunk:
    """A persisted-ready Markdown passage with stable source metadata."""

    chunk_id: str
    relative_path: str
    heading_path: tuple[str, ...]
    heading_anchor: str
    chunk_index: int
    content_hash: str
    content: str


def _slugify(heading: str) -> str:
    value = heading.strip().lower()
    value = re.sub(r"[`*_~]", "", value)
    value = re.sub(r"<[^>]+>", "", value)
    value = "".join(char for char in value if char not in string.punctuation or char == "-")
    return re.sub(r"\s+", "-", value).strip("-") or "section"


def split_text(text: str, *, max_chars: int = 1600, overlap_chars: int = 200) -> list[str]:
    """Split text into bounded overlapping pieces without dropping characters."""
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    if overlap_chars < 0 or overlap_chars >= max_chars:
        raise ValueError("overlap_chars must be non-negative and less than max_chars")

    normalized = text.strip()
    if not normalized:
        return []
    if len(normalized) <= max_chars:
        return [normalized]

    chunks: list[str] = []
    start = 0
    while start < len(normalized):
        limit = min(start + max_chars, len(normalized))
        end = limit
        if limit < len(normalized):
            boundary = max(
                normalized.rfind("\n", start + max_chars // 2, limit),
                normalized.rfind(" ", start + max_chars // 2, limit),
            )
            if boundary > start:
                end = boundary
        piece = normalized[start:end].strip()
        if piece:
            chunks.append(piece)
        if end == len(normalized):
            break
        next_start = max(start + 1, end - overlap_chars)
        while next_start < len(normalized) and normalized[next_start].isspace():
            next_start += 1
        start = next_start
    return chunks


def parse_markdown(
    *,
    relative_path: str,
    source: str,
    content_hash: str,
    max_chars: int = 1600,
    overlap_chars: int = 200,
) -> list[Chunk]:
    """Parse Markdown sections and create stable, bounded chunks."""
    heading_pattern = re.compile(r"^(#{1,6})[ \t]+(.+?)\s*#*\s*$")
    sections: list[tuple[tuple[str, ...], str, str]] = []
    heading_stack: list[tuple[int, str, str]] = []
    seen_anchors: dict[str, int] = {}
    current_lines: list[str] = []
    fence_char: str | None = None
    fence_length = 0

    def flush() -> None:
        body = "\n".join(current_lines).strip()
        if body:
            if heading_stack:
                path = tuple(item[1] for item in heading_stack)
                anchor = heading_stack[-1][2]
            else:
                path = ()
                anchor = "document"
            sections.append((path, anchor, body))
        current_lines.clear()

    for line in source.splitlines():
        stripped = line.lstrip()
        fence_match = re.match(r"(`{3,}|~{3,})", stripped)
        if fence_char is not None:
            if (
                fence_match
                and fence_match.group(0)[0] == fence_char
                and len(fence_match.group(0)) >= fence_length
            ):
                fence_char = None
                fence_length = 0
            current_lines.append(line)
            continue
        if fence_match:
            fence_char = fence_match.group(0)[0]
            fence_length = len(fence_match.group(0))
            current_lines.append(line)
            continue

        match = heading_pattern.match(line)
        if match:
            flush()
            level = len(match.group(1))
            title = match.group(2).strip()
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            base_anchor = _slugify(title)
            occurrence = seen_anchors.get(base_anchor, 0)
            seen_anchors[base_anchor] = occurrence + 1
            anchor = base_anchor if occurrence == 0 else f"{base_anchor}-{occurrence}"
            heading_stack.append((level, title, anchor))
        else:
            current_lines.append(line)
    flush()

    chunks: list[Chunk] = []
    for heading_path, anchor, body in sections:
        for chunk_index, content in enumerate(
            split_text(body, max_chars=max_chars, overlap_chars=overlap_chars)
        ):
            identity = f"{relative_path}\0{anchor}\0{chunk_index}"
            chunk_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
            chunks.append(
                Chunk(
                    chunk_id=chunk_id,
                    relative_path=relative_path,
                    heading_path=heading_path,
                    heading_anchor=anchor,
                    chunk_index=chunk_index,
                    content_hash=content_hash,
                    content=content,
                )
            )
    return chunks
