"""Deterministic, bounded Markdown link audit tests."""

from pathlib import Path

import pytest

from trussium_knowledge_agent.auditing import audit_markdown_links


def test_audit_checks_local_files_anchors_and_ignores_external_links(tmp_path: Path) -> None:
    (tmp_path / "guide.md").write_text(
        """# Setup

## Repeated heading
## Repeated heading

[valid](other.md#known) [missing file](missing.md) [missing anchor](other.md#absent)
[same page](#setup) [duplicate](#repeated-heading-1)
[external](https://example.test/path) ![image](image.png)
`[inline code](ignored.md)`

```md
[fenced code](ignored.md)
```
""",
        encoding="utf-8",
    )
    (tmp_path / "other.md").write_text("# Known\n", encoding="utf-8")
    (tmp_path / "image.png").write_bytes(b"not parsed")

    report = audit_markdown_links(tmp_path)

    assert report.files_checked == 2
    assert [finding.rule_id for finding in report.findings] == [
        "link-target-missing",
        "link-anchor-missing",
    ]
    assert [finding.target for finding in report.findings] == ["missing.md", "other.md#absent"]
    assert all(
        finding.source_path == "guide.md" and finding.line == 6 for finding in report.findings
    )
    assert report.links_checked == 7
    assert report.truncated is False
    assert str(tmp_path) not in str(report.as_dict())


def test_audit_rejects_paths_outside_root_and_does_not_follow_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    (root / "index.md").write_text(
        "[outside](../outside.md) [traversal](%2e%2e/outside.md) [link](linked.md)\n",
        encoding="utf-8",
    )
    (root / "safe.md").write_text("# Safe\n", encoding="utf-8")
    try:
        (root / "linked.md").symlink_to(root / "safe.md")
    except OSError as error:
        pytest.skip(f"symlinks are unavailable: {error}")

    report = audit_markdown_links(root)

    assert {finding.rule_id for finding in report.findings} == {
        "link-target-outside-root",
        "link-target-symlink",
    }
    assert all(finding.source_path == "index.md" for finding in report.findings)


def test_audit_reports_invalid_local_targets(tmp_path: Path) -> None:
    (tmp_path / "index.md").write_text("[bad](bad%5ctarget.md)\n", encoding="utf-8")

    report = audit_markdown_links(tmp_path)

    assert len(report.findings) == 1
    assert report.findings[0].rule_id == "invalid-link-target"


def test_audit_rejects_oversized_markdown_file(tmp_path: Path) -> None:
    from trussium_knowledge_agent.markdown import MAX_MARKDOWN_BYTES

    (tmp_path / "large.md").write_bytes(b"x" * (MAX_MARKDOWN_BYTES + 1))

    with pytest.raises(ValueError, match="exceeds"):
        audit_markdown_links(tmp_path)


def test_audit_rejects_oversized_total_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trussium_knowledge_agent import auditing

    (tmp_path / "one.md").write_text("# One\n", encoding="utf-8")
    (tmp_path / "two.md").write_text("# Two\n", encoding="utf-8")
    monkeypatch.setattr(auditing, "MAX_AUDIT_TOTAL_BYTES", 10)

    with pytest.raises(ValueError, match="10-byte limit"):
        audit_markdown_links(tmp_path)
