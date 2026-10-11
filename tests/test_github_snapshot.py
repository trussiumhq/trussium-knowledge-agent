from __future__ import annotations

import httpx
import pytest

from trussium_knowledge_agent import github_snapshot
from trussium_knowledge_agent.github_snapshot import (
    GITHUB_API,
    GITHUB_RAW,
    MAX_MARKDOWN_FILES,
    MAX_TREE_RESPONSE_BYTES,
    GitHubSnapshotError,
    fetch_public_github_snapshot,
)
from trussium_knowledge_agent.markdown import MAX_MARKDOWN_BYTES

COMMIT = "a" * 40
TREE_SHA = "c" * 40


def _transport(
    *,
    tree: dict[str, object] | None = None,
    raw: bytes = b"# Guide\n\nPublic setup instructions.\n",
    raw_status: int = 200,
    raw_headers: dict[str, str] | None = None,
    api_status: int = 200,
    private: bool = False,
    requests: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    entries = tree or {
        "truncated": False,
        "tree": [
            {"path": "guide.md", "mode": "100644", "type": "blob"},
            {"path": "assets/logo.png", "mode": "100644", "type": "blob"},
        ],
    }

    def respond(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        if request.url.host == "raw.githubusercontent.com":
            return httpx.Response(raw_status, headers=raw_headers, content=raw)
        if request.url.host != "api.github.com":
            raise AssertionError(f"unexpected host: {request.url.host}")
        if request.url.path == "/repos/acme/docs":
            payload: object = {
                "id": 4242,
                "full_name": "acme/docs",
                "private": private,
            }
        elif request.url.path == "/repos/acme/docs/commits/main":
            payload = {"sha": COMMIT, "commit": {"tree": {"sha": TREE_SHA}}}
        elif request.url.path == f"/repos/acme/docs/git/trees/{TREE_SHA}":
            payload = entries
        else:
            raise AssertionError(f"unexpected URL path: {request.url.path}")
        return httpx.Response(api_status, json=payload)

    return httpx.MockTransport(respond)


def test_fetches_public_markdown_from_commit_pinned_snapshot() -> None:
    requests: list[httpx.Request] = []
    result = fetch_public_github_snapshot(
        "acme/docs", "main", transport=_transport(requests=requests)
    )

    assert result.repository_id == 4242
    assert result.repository == "acme/docs"
    assert result.revision == COMMIT
    assert result.source_identity == "github.com:repository:4242"
    assert result.file_count == 1
    assert result.chunks[0].relative_path == "guide.md"
    assert "Public setup instructions." in result.chunks[0].content
    assert {request.url.host for request in requests} == {
        "api.github.com",
        "raw.githubusercontent.com",
    }
    assert requests[0].url == f"{GITHUB_API}/repos/acme/docs"
    assert str(requests[-1].url).startswith(f"{GITHUB_RAW}/acme/docs/{COMMIT}/")
    assert all(request.headers["user-agent"] == "trussium-knowledge-agent" for request in requests)


@pytest.mark.parametrize(
    "repository", ["https://attacker.invalid/acme/docs", "acme/docs/extra", "../docs"]
)
def test_rejects_urls_and_unsafe_repository_names_before_network(repository: str) -> None:
    transport = _transport()

    with pytest.raises(ValueError, match="owner/repository"):
        fetch_public_github_snapshot(repository, "main", transport=transport)


@pytest.mark.parametrize("ref", ["..", "feature/../main", "a\\b", "\nmain"])
def test_rejects_unsafe_ref_before_network(ref: str) -> None:
    with pytest.raises(ValueError, match="ref"):
        fetch_public_github_snapshot("acme/docs", ref, transport=_transport())


def test_rejects_private_repository_without_fetching_content() -> None:
    requests: list[httpx.Request] = []

    with pytest.raises(GitHubSnapshotError, match="only public"):
        fetch_public_github_snapshot(
            "acme/docs", "main", transport=_transport(private=True, requests=requests)
        )
    assert len(requests) == 1


def test_rejects_redirect_without_following_external_location() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://attacker.invalid/steal"})

    with pytest.raises(GitHubSnapshotError, match="redirects"):
        fetch_public_github_snapshot("acme/docs", "main", transport=httpx.MockTransport(respond))
    assert len(requests) == 1
    assert requests[0].url.host == "api.github.com"


@pytest.mark.parametrize(
    ("tree", "message"),
    [
        ({"truncated": True, "tree": []}, "incomplete"),
        (
            {
                "truncated": False,
                "tree": [{"path": "../guide.md", "mode": "100644", "type": "blob"}],
            },
            "unsafe",
        ),
        (
            {"truncated": False, "tree": [{"path": "guide.md", "mode": "120000", "type": "blob"}]},
            "unsafe file mode",
        ),
        (
            {
                "truncated": False,
                "tree": [{"path": "a" * 4094 + ".md", "mode": "100644", "type": "blob"}],
            },
            "unsafe",
        ),
    ],
)
def test_rejects_incomplete_or_unsafe_trees(tree: dict[str, object], message: str) -> None:
    with pytest.raises(GitHubSnapshotError, match=message):
        fetch_public_github_snapshot("acme/docs", "main", transport=_transport(tree=tree))


def test_excludes_submodules_and_skips_non_markdown_files() -> None:
    tree = {
        "truncated": False,
        "tree": [
            {"path": "guide.md", "mode": "100644", "type": "blob"},
            {"path": "vendor/docs.md", "mode": "160000", "type": "commit"},
            {"path": "script.py", "mode": "100644", "type": "blob"},
        ],
    }
    result = fetch_public_github_snapshot("acme/docs", "main", transport=_transport(tree=tree))

    assert result.file_count == 1


def test_rejects_too_many_markdown_files_before_fetching_contents() -> None:
    requests: list[httpx.Request] = []
    tree = {
        "truncated": False,
        "tree": [
            {"path": f"{index}.md", "mode": "100644", "type": "blob"}
            for index in range(MAX_MARKDOWN_FILES + 1)
        ],
    }

    with pytest.raises(GitHubSnapshotError, match="more Markdown files"):
        fetch_public_github_snapshot(
            "acme/docs", "main", transport=_transport(tree=tree, requests=requests)
        )
    assert len(requests) == 3


def test_rejects_too_many_tree_entries_before_fetching_contents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    monkeypatch.setattr(github_snapshot, "MAX_TREE_ENTRIES", 1)
    tree = {
        "truncated": False,
        "tree": [
            {"path": "one.md", "mode": "100644", "type": "blob"},
            {"path": "two.md", "mode": "100644", "type": "blob"},
        ],
    }

    with pytest.raises(GitHubSnapshotError, match="entry limit"):
        fetch_public_github_snapshot(
            "acme/docs", "main", transport=_transport(tree=tree, requests=requests)
        )
    assert len(requests) == 3


@pytest.mark.parametrize(
    ("raw", "raw_status", "raw_headers", "message"),
    [
        (b"\xff", 200, None, "not valid UTF-8"),
        (b"too large", 200, {"content-length": str(MAX_MARKDOWN_BYTES + 1)}, "byte limit"),
        (b"", 403, None, "denied the public read or rate limit"),
        (b"", 500, None, "HTTP 500"),
        (b"", 302, {"location": "https://attacker.invalid"}, "redirects"),
    ],
)
def test_rejects_failed_oversized_or_invalid_markdown(
    raw: bytes, raw_status: int, raw_headers: dict[str, str] | None, message: str
) -> None:
    with pytest.raises(GitHubSnapshotError, match=message):
        fetch_public_github_snapshot(
            "acme/docs",
            "main",
            transport=_transport(raw=raw, raw_status=raw_status, raw_headers=raw_headers),
        )


def test_rejects_oversized_tree_response_before_json_parse() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/docs":
            return httpx.Response(200, json={"id": 42, "full_name": "acme/docs", "private": False})
        if request.url.path == "/repos/acme/docs/commits/main":
            return httpx.Response(200, json={"sha": COMMIT, "commit": {"tree": {"sha": TREE_SHA}}})
        return httpx.Response(200, content=b" " * (MAX_TREE_RESPONSE_BYTES + 1))

    with pytest.raises(GitHubSnapshotError, match="byte limit"):
        fetch_public_github_snapshot("acme/docs", "main", transport=httpx.MockTransport(respond))


def test_rejects_aggregate_markdown_over_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github_snapshot, "MAX_MARKDOWN_BYTES", 4)
    monkeypatch.setattr(github_snapshot, "MAX_SNAPSHOT_BYTES", 6)
    tree = {
        "truncated": False,
        "tree": [
            {"path": "one.md", "mode": "100644", "type": "blob"},
            {"path": "two.md", "mode": "100644", "type": "blob"},
        ],
    }

    with pytest.raises(GitHubSnapshotError, match="total byte limit"):
        fetch_public_github_snapshot(
            "acme/docs", "main", transport=_transport(tree=tree, raw=b"abcd")
        )


def test_rejects_chunk_expansion_over_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github_snapshot, "MAX_SNAPSHOT_CHUNKS", 0)

    with pytest.raises(GitHubSnapshotError, match="chunk limit"):
        fetch_public_github_snapshot("acme/docs", "main", transport=_transport())
