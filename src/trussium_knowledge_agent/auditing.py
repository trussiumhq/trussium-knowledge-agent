"""Bounded, deterministic checks for local Markdown documentation."""

from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from trussium_knowledge_agent.chunking import _slugify
from trussium_knowledge_agent.markdown import MAX_MARKDOWN_BYTES, discover_markdown_files

MAX_AUDIT_FILES = 2_000
MAX_AUDIT_LINKS = 20_000
MAX_AUDIT_FINDINGS = 500
MAX_AUDIT_TOTAL_BYTES = 50 * 1024 * 1024
_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_HTML_ANCHOR = re.compile(r"\b(?:id|name)\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class LinkFinding:
    """One deterministic broken-link finding with a source location."""

    rule_id: str
    source_path: str
    line: int
    target: str
    message: str


@dataclass(frozen=True, slots=True)
class LinkAuditReport:
    """Bounded summary returned by the read-only Markdown link audit."""

    files_checked: int
    links_checked: int
    findings: tuple[LinkFinding, ...]
    truncated: bool

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serializable report without local absolute paths."""
        return {
            "files_checked": self.files_checked,
            "links_checked": self.links_checked,
            "findings": [asdict(finding) for finding in self.findings],
            "truncated": self.truncated,
        }


def audit_markdown_links(source_root: str | Path) -> LinkAuditReport:
    """Check relative Markdown links under one explicitly selected local root.

    The scan never follows symbolic links, fetches network destinations, or
    modifies source files. This first rule set recognizes inline Markdown
    links/images; reference-style links and raw HTML links are out of scope.
    """
    root, paths = discover_markdown_files(source_root)
    if len(paths) > MAX_AUDIT_FILES:
        raise ValueError(f"audit source exceeds the {MAX_AUDIT_FILES}-file limit")

    documents: dict[str, tuple[str, set[str]]] = {}
    total_bytes = 0
    for path in paths:
        relative = path.relative_to(root).as_posix()
        total_bytes += path.stat().st_size
        if total_bytes > MAX_AUDIT_TOTAL_BYTES:
            raise ValueError(f"audit source exceeds the {MAX_AUDIT_TOTAL_BYTES}-byte limit")
        source = _read_markdown(path, relative)
        documents[relative] = (source, _anchors(source))

    findings: list[LinkFinding] = []
    links_checked = 0
    truncated = False
    for relative, (source, _) in sorted(documents.items()):
        inside_fence = False
        fence_character = ""
        fence_length = 0
        for line_number, raw_line in enumerate(source.splitlines(), start=1):
            fence_match = _FENCE.match(raw_line)
            if inside_fence:
                if (
                    fence_match
                    and fence_match.group(1)[0] == fence_character
                    and len(fence_match.group(1)) >= fence_length
                ):
                    inside_fence = False
                continue
            if fence_match:
                inside_fence = True
                fence_character = fence_match.group(1)[0]
                fence_length = len(fence_match.group(1))
                continue

            for target in _inline_link_targets(_without_inline_code(raw_line)):
                links_checked += 1
                if links_checked > MAX_AUDIT_LINKS:
                    raise ValueError(f"audit source exceeds the {MAX_AUDIT_LINKS}-link limit")
                finding = _check_target(root, relative, line_number, target, documents)
                if finding is not None:
                    if len(findings) < MAX_AUDIT_FINDINGS:
                        findings.append(finding)
                    else:
                        truncated = True

    findings.sort(key=lambda item: (item.source_path, item.line, item.target, item.rule_id))
    return LinkAuditReport(len(paths), links_checked, tuple(findings), truncated)


def _read_markdown(path: Path, relative_path: str) -> str:
    size = path.stat().st_size
    if size > MAX_MARKDOWN_BYTES:
        raise ValueError(f"Markdown file exceeds {MAX_MARKDOWN_BYTES} bytes: {relative_path}")
    try:
        file_descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        raise ValueError(f"cannot safely open Markdown file: {relative_path}") from error
    with os.fdopen(file_descriptor, "rb") as source_file:
        raw = source_file.read(MAX_MARKDOWN_BYTES + 1)
    if len(raw) > MAX_MARKDOWN_BYTES:
        raise ValueError(f"Markdown file exceeds {MAX_MARKDOWN_BYTES} bytes: {relative_path}")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError(f"Markdown file is not valid UTF-8: {relative_path}") from error


def _check_target(
    root: Path,
    source_relative: str,
    line_number: int,
    raw_target: str,
    documents: dict[str, tuple[str, set[str]]],
) -> LinkFinding | None:
    target = raw_target.strip()
    if not target:
        return None
    try:
        parsed = urlsplit(target)
    except ValueError:
        return _finding("invalid-link-target", source_relative, line_number, target)
    if parsed.scheme or parsed.netloc:
        return None

    target_path = unquote(parsed.path)
    fragment = unquote(parsed.fragment)
    if "\\" in target_path:
        return _finding("invalid-link-target", source_relative, line_number, target)
    if target_path.startswith("/"):
        candidate = root / target_path.lstrip("/")
    elif target_path:
        candidate = root / source_relative
        candidate = candidate.parent / target_path
    else:
        candidate = root / source_relative

    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, ValueError):
        return _finding("link-target-outside-root", source_relative, line_number, target)
    if _contains_symlink(root, candidate):
        return _finding("link-target-symlink", source_relative, line_number, target)
    if not candidate.exists():
        return _finding("link-target-missing", source_relative, line_number, target)

    relative_target = resolved.relative_to(root).as_posix()
    target_document = documents.get(relative_target)
    if fragment and target_document is not None and fragment not in target_document[1]:
        return _finding("link-anchor-missing", source_relative, line_number, target)
    if fragment and target_path == "" and fragment not in documents[source_relative][1]:
        return _finding("link-anchor-missing", source_relative, line_number, target)
    return None


def _contains_symlink(root: Path, candidate: Path) -> bool:
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        if part in {"", "."}:
            continue
        current = current / part
        if current.is_symlink():
            return True
    return False


def _finding(rule_id: str, source_path: str, line: int, target: str) -> LinkFinding:
    explanations = {
        "invalid-link-target": "The local link target is malformed or unsupported.",
        "link-target-outside-root": "The link resolves outside the configured source root.",
        "link-target-symlink": "The link points through a symbolic link, which audits do not follow.",
        "link-target-missing": "The local link target does not exist beneath the source root.",
        "link-anchor-missing": "The Markdown target does not contain this heading anchor.",
    }
    return LinkFinding(rule_id, source_path, line, target[:512], explanations[rule_id])


def _anchors(source: str) -> set[str]:
    anchors: set[str] = set()
    occurrences: Counter[str] = Counter()
    inside_fence = False
    fence_character = ""
    fence_length = 0
    for line in source.splitlines():
        fence_match = _FENCE.match(line)
        if inside_fence:
            if (
                fence_match
                and fence_match.group(1)[0] == fence_character
                and len(fence_match.group(1)) >= fence_length
            ):
                inside_fence = False
            continue
        if fence_match:
            inside_fence = True
            fence_character = fence_match.group(1)[0]
            fence_length = len(fence_match.group(1))
            continue
        heading = _HEADING.match(line)
        if heading:
            slug = _slugify(heading.group(2))
            occurrence = occurrences[slug]
            occurrences[slug] += 1
            anchors.add(slug if occurrence == 0 else f"{slug}-{occurrence}")
        anchors.update(_HTML_ANCHOR.findall(line))
    return anchors


def _without_inline_code(line: str) -> str:
    characters = list(line)
    index = 0
    while index < len(line):
        if line[index] != "`":
            index += 1
            continue
        end_run = index
        while end_run < len(line) and line[end_run] == "`":
            end_run += 1
        delimiter = line[index:end_run]
        closing = line.find(delimiter, end_run)
        if closing < 0:
            index = end_run
            continue
        for position in range(index, closing + len(delimiter)):
            characters[position] = " "
        index = closing + len(delimiter)
    return "".join(characters)


def _inline_link_targets(line: str) -> list[str]:
    targets: list[str] = []
    search_from = 0
    while True:
        marker_index = line.find("](", search_from)
        if marker_index < 0:
            return targets
        search_from = marker_index + 2
        opening_bracket = line.rfind("[", 0, marker_index)
        if opening_bracket < 0:
            continue
        cursor = marker_index + 2
        while cursor < len(line) and line[cursor].isspace():
            cursor += 1
        if cursor >= len(line):
            continue
        if line[cursor] == "<":
            start = cursor + 1
            end = start
            while end < len(line) and (line[end] != ">" or _is_escaped(line, end)):
                end += 1
            if end >= len(line):
                continue
            target = line[start:end]
        else:
            start = cursor
            end = start
            depth = 0
            while end < len(line):
                character = line[end]
                if _is_escaped(line, end):
                    end += 1
                    continue
                if character == "(":
                    depth += 1
                elif character == ")":
                    if depth == 0:
                        break
                    depth -= 1
                elif character.isspace() and depth == 0:
                    break
                end += 1
            target = line[start:end]
        if target:
            targets.append(_unescape_target(target))


def _is_escaped(value: str, position: int) -> bool:
    backslashes = 0
    position -= 1
    while position >= 0 and value[position] == "\\":
        backslashes += 1
        position -= 1
    return backslashes % 2 == 1


def _unescape_target(value: str) -> str:
    return re.sub(r"\\([()\\ ])", r"\1", value)
