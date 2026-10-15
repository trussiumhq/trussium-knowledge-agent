"""Bounded, explicit snapshots of public GitHub Markdown repositories."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import time
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from trussium_knowledge_agent.chunking import Chunk, parse_markdown
from trussium_knowledge_agent.markdown import MAX_MARKDOWN_BYTES

GITHUB_API = "https://api.github.com"
GITHUB_RAW = "https://raw.githubusercontent.com"
MAX_TREE_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_TREE_ENTRIES = 20_000
MAX_MARKDOWN_FILES = 100
MAX_MARKDOWN_PATH_BYTES = 4096
MAX_SNAPSHOT_BYTES = 20 * 1024 * 1024
MAX_SNAPSHOT_CHUNKS = 10_000
SNAPSHOT_TIMEOUT_SECONDS = 10.0
SNAPSHOT_DEADLINE_SECONDS = 120.0
_REPOSITORY_PART = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_SHA = re.compile(r"^[0-9a-fA-F]{40}$")


class GitHubSnapshotError(RuntimeError):
    """A public GitHub snapshot failed validation or bounded retrieval."""


@dataclass(frozen=True, slots=True)
class GitHubSnapshot:
    """Complete validated Markdown snapshot pinned to one commit."""

    repository_id: int
    repository: str
    revision: str
    file_count: int
    chunks: tuple[Chunk, ...]

    @property
    def source_identity(self) -> str:
        """Immutable identity independent of repository owner/name changes."""
        return f"github.com:repository:{self.repository_id}"


def fetch_public_github_snapshot(
    repository: str,
    ref: str,
    *,
    max_chars: int = 1600,
    overlap_chars: int = 200,
    transport: httpx.BaseTransport | None = None,
) -> GitHubSnapshot:
    """Fetch and validate a bounded Markdown snapshot from GitHub's fixed hosts.

    The transport argument exists for deterministic tests. Production always
    creates a client with redirects disabled and fixed GitHub endpoints.
    """
    owner, name = _parse_repository(repository)
    if not isinstance(ref, str) or not ref.strip() or len(ref) > 255:
        raise ValueError("GitHub ref must be a non-empty string of at most 255 characters")
    if (
        "\x00" in ref
        or "\r" in ref
        or "\n" in ref
        or "\\" in ref
        or any(part in ("", ".", "..") for part in ref.split("/"))
    ):
        raise ValueError("GitHub ref contains an invalid character")
    if max_chars < 100 or not 0 <= overlap_chars < max_chars:
        raise ValueError("chunk sizes are invalid")

    started = time.monotonic()
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "trussium-knowledge-agent",
    }
    with httpx.Client(
        transport=transport,
        timeout=SNAPSHOT_TIMEOUT_SECONDS,
        follow_redirects=False,
        headers=headers,
        trust_env=False,
    ) as client:
        encoded_owner = quote(owner, safe="")
        encoded_name = quote(name, safe="")
        base = f"{GITHUB_API}/repos/{encoded_owner}/{encoded_name}"
        repository_data = _get_json(
            client, f"{base}", max_bytes=64 * 1024, deadline=started + SNAPSHOT_DEADLINE_SECONDS
        )
        repository_id = repository_data.get("id")
        full_name = repository_data.get("full_name")
        if (
            isinstance(repository_id, bool)
            or not isinstance(repository_id, int)
            or repository_id <= 0
            or not isinstance(full_name, str)
        ):
            raise GitHubSnapshotError("GitHub returned invalid repository metadata")
        if repository_data.get("private") is not False:
            raise GitHubSnapshotError("only public GitHub repositories are supported")
        canonical_parts = _parse_repository(full_name)
        canonical_owner, canonical_name = canonical_parts
        canonical_base = (
            f"{GITHUB_API}/repos/{quote(canonical_owner, safe='')}/{quote(canonical_name, safe='')}"
        )
        commit = _get_json(
            client,
            f"{canonical_base}/commits/{quote(ref, safe='')}",
            max_bytes=64 * 1024,
            deadline=started + SNAPSHOT_DEADLINE_SECONDS,
        )
        revision = commit.get("sha")
        if not isinstance(revision, str) or not _SHA.fullmatch(revision):
            raise GitHubSnapshotError("GitHub returned an invalid commit SHA")
        revision = revision.lower()
        commit_details = commit.get("commit")
        tree_details = commit_details.get("tree") if isinstance(commit_details, dict) else None
        tree_sha = tree_details.get("sha") if isinstance(tree_details, dict) else None
        if not isinstance(tree_sha, str) or not _SHA.fullmatch(tree_sha):
            raise GitHubSnapshotError("GitHub returned an invalid tree SHA")
        tree = _get_json(
            client,
            f"{canonical_base}/git/trees/{tree_sha.lower()}?recursive=1",
            max_bytes=MAX_TREE_RESPONSE_BYTES,
            deadline=started + SNAPSHOT_DEADLINE_SECONDS,
        )
        entries = tree.get("tree")
        if tree.get("truncated") is not False or not isinstance(entries, list):
            raise GitHubSnapshotError("GitHub returned an incomplete repository tree")
        if len(entries) > MAX_TREE_ENTRIES:
            raise GitHubSnapshotError("GitHub repository tree exceeds the entry limit")

        candidates: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise GitHubSnapshotError("GitHub returned an invalid repository tree entry")
            path = entry.get("path")
            if not isinstance(path, str):
                raise GitHubSnapshotError("GitHub returned an invalid repository tree path")
            if not path.lower().endswith(".md"):
                continue
            _validate_markdown_path(path)
            if entry.get("type") == "commit" or entry.get("mode") == "160000":
                continue
            if entry.get("type") != "blob" or entry.get("mode") not in ("100644", "100755"):
                raise GitHubSnapshotError("GitHub Markdown tree entry uses an unsafe file mode")
            candidates.append(path)
        if len(candidates) > MAX_MARKDOWN_FILES:
            raise GitHubSnapshotError("repository contains more Markdown files than the limit")

        chunks: list[Chunk] = []
        total_bytes = 0
        raw_base = (
            f"{GITHUB_RAW}/{quote(canonical_owner, safe='')}/"
            f"{quote(canonical_name, safe='')}/{revision}"
        )
        for path in sorted(candidates):
            raw = _get_bytes(
                client,
                f"{raw_base}/{quote(path, safe='/')}",
                max_bytes=MAX_MARKDOWN_BYTES,
                deadline=started + SNAPSHOT_DEADLINE_SECONDS,
            )
            total_bytes += len(raw)
            if total_bytes > MAX_SNAPSHOT_BYTES:
                raise GitHubSnapshotError("GitHub Markdown snapshot exceeds the total byte limit")
            try:
                content = raw.decode("utf-8-sig")
            except UnicodeDecodeError as error:
                raise GitHubSnapshotError("GitHub Markdown file is not valid UTF-8") from error
            chunks.extend(
                parse_markdown(
                    relative_path=path,
                    source=content,
                    content_hash=hashlib.sha256(raw).hexdigest(),
                    max_chars=max_chars,
                    overlap_chars=overlap_chars,
                )
            )
            if len(chunks) > MAX_SNAPSHOT_CHUNKS:
                raise GitHubSnapshotError("GitHub Markdown snapshot exceeds the chunk limit")
    return GitHubSnapshot(
        repository_id=repository_id,
        repository=f"{canonical_owner}/{canonical_name}",
        revision=revision,
        file_count=len(candidates),
        chunks=tuple(chunks),
    )


def fetch_private_github_snapshot(
    repository_id: int,
    ref: str,
    token: str,
    *,
    max_chars: int = 1600,
    overlap_chars: int = 200,
    transport: httpx.BaseTransport | None = None,
) -> GitHubSnapshot:
    """Fetch a bounded private snapshot without sending its token off api.github.com."""
    if isinstance(repository_id, bool) or not 0 < repository_id <= 2**63 - 1:
        raise ValueError("repository ID must be a positive integer")
    if not token or any(character in token for character in "\r\n"):
        raise ValueError("GitHub installation token is invalid")
    if not isinstance(ref, str) or not ref.strip() or len(ref) > 255:
        raise ValueError("GitHub ref must be a non-empty string of at most 255 characters")
    if (
        "\x00" in ref
        or "\r" in ref
        or "\n" in ref
        or "\\" in ref
        or any(part in ("", ".", "..") for part in ref.split("/"))
    ):
        raise ValueError("GitHub ref contains an invalid character")
    if max_chars < 100 or not 0 <= overlap_chars < max_chars:
        raise ValueError("chunk sizes are invalid")

    started = time.monotonic()
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "trussium-knowledge-agent",
    }
    with httpx.Client(
        transport=transport,
        timeout=SNAPSHOT_TIMEOUT_SECONDS,
        follow_redirects=False,
        headers=headers,
        trust_env=False,
    ) as client:
        base = f"{GITHUB_API}/repositories/{repository_id}"
        metadata = _get_json(
            client, base, max_bytes=64 * 1024, deadline=started + SNAPSHOT_DEADLINE_SECONDS
        )
        if (
            isinstance(metadata.get("id"), bool)
            or metadata.get("id") != repository_id
            or metadata.get("private") is not True
        ):
            raise GitHubSnapshotError("authorized GitHub repository identity did not match")
        full_name = metadata.get("full_name")
        if not isinstance(full_name, str):
            raise GitHubSnapshotError("GitHub returned invalid repository metadata")
        owner, name = _parse_repository(full_name)
        repository_path = f"{GITHUB_API}/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
        commit = _get_json(
            client,
            f"{repository_path}/commits/{quote(ref, safe='')}",
            max_bytes=64 * 1024,
            deadline=started + SNAPSHOT_DEADLINE_SECONDS,
        )
        revision = commit.get("sha")
        details = commit.get("commit")
        tree_details = details.get("tree") if isinstance(details, dict) else None
        tree_sha = tree_details.get("sha") if isinstance(tree_details, dict) else None
        if not isinstance(revision, str) or not _SHA.fullmatch(revision):
            raise GitHubSnapshotError("GitHub returned an invalid commit SHA")
        if not isinstance(tree_sha, str) or not _SHA.fullmatch(tree_sha):
            raise GitHubSnapshotError("GitHub returned an invalid tree SHA")
        revision = revision.lower()
        tree = _get_json(
            client,
            f"{repository_path}/git/trees/{tree_sha.lower()}?recursive=1",
            max_bytes=MAX_TREE_RESPONSE_BYTES,
            deadline=started + SNAPSHOT_DEADLINE_SECONDS,
        )
        entries = tree.get("tree")
        if tree.get("truncated") is not False or not isinstance(entries, list):
            raise GitHubSnapshotError("GitHub returned an incomplete repository tree")
        if len(entries) > MAX_TREE_ENTRIES:
            raise GitHubSnapshotError("GitHub repository tree exceeds the entry limit")

        candidates: list[tuple[str, str]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise GitHubSnapshotError("GitHub returned an invalid repository tree entry")
            path = entry.get("path")
            if not isinstance(path, str):
                raise GitHubSnapshotError("GitHub returned an invalid repository tree path")
            if not path.lower().endswith(".md"):
                continue
            _validate_markdown_path(path)
            if entry.get("type") == "commit" or entry.get("mode") == "160000":
                continue
            if entry.get("type") != "blob" or entry.get("mode") not in ("100644", "100755"):
                raise GitHubSnapshotError("GitHub Markdown tree entry uses an unsafe file mode")
            blob_sha = entry.get("sha")
            if not isinstance(blob_sha, str) or not _SHA.fullmatch(blob_sha):
                raise GitHubSnapshotError("GitHub Markdown tree entry has an invalid blob SHA")
            candidates.append((path, blob_sha.lower()))
        if len(candidates) > MAX_MARKDOWN_FILES:
            raise GitHubSnapshotError("repository contains more Markdown files than the limit")

        chunks: list[Chunk] = []
        total_bytes = 0
        for path, expected_sha in sorted(candidates):
            blob = _get_json(
                client,
                f"{repository_path}/git/blobs/{expected_sha}",
                max_bytes=((MAX_MARKDOWN_BYTES + 2) // 3) * 4 + 4096,
                deadline=started + SNAPSHOT_DEADLINE_SECONDS,
            )
            if blob.get("sha") != expected_sha or blob.get("encoding") != "base64":
                raise GitHubSnapshotError("GitHub returned an invalid Markdown blob")
            declared_size = blob.get("size")
            encoded_content = blob.get("content")
            if (
                isinstance(declared_size, bool)
                or not isinstance(declared_size, int)
                or not 0 <= declared_size <= MAX_MARKDOWN_BYTES
                or not isinstance(encoded_content, str)
            ):
                raise GitHubSnapshotError("GitHub Markdown blob exceeds its byte limit")
            try:
                raw = base64.b64decode("".join(encoded_content.split()), validate=True)
            except (ValueError, binascii.Error) as error:
                raise GitHubSnapshotError("GitHub returned invalid base64 Markdown") from error
            if len(raw) != declared_size:
                raise GitHubSnapshotError("GitHub Markdown blob size did not match its metadata")
            git_sha = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
            if git_sha != expected_sha:
                raise GitHubSnapshotError("GitHub Markdown blob did not match its tree identity")
            total_bytes += len(raw)
            if total_bytes > MAX_SNAPSHOT_BYTES:
                raise GitHubSnapshotError("GitHub Markdown snapshot exceeds the total byte limit")
            try:
                content = raw.decode("utf-8-sig")
            except UnicodeDecodeError as error:
                raise GitHubSnapshotError("GitHub Markdown file is not valid UTF-8") from error
            chunks.extend(
                parse_markdown(
                    relative_path=path,
                    source=content,
                    content_hash=hashlib.sha256(raw).hexdigest(),
                    max_chars=max_chars,
                    overlap_chars=overlap_chars,
                )
            )
            if len(chunks) > MAX_SNAPSHOT_CHUNKS:
                raise GitHubSnapshotError("GitHub Markdown snapshot exceeds the chunk limit")
    return GitHubSnapshot(
        repository_id=repository_id,
        repository=f"{owner}/{name}",
        revision=revision,
        file_count=len(candidates),
        chunks=tuple(chunks),
    )


def _get_json(
    client: httpx.Client, url: str, *, max_bytes: int, deadline: float
) -> dict[str, object]:
    raw = _get_bytes(client, url, max_bytes=max_bytes, deadline=deadline)
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GitHubSnapshotError("GitHub returned an invalid JSON response") from error
    if not isinstance(payload, dict):
        raise GitHubSnapshotError("GitHub returned an invalid JSON response")
    return payload


def _get_bytes(client: httpx.Client, url: str, *, max_bytes: int, deadline: float) -> bytes:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise GitHubSnapshotError("GitHub snapshot exceeded its total time limit")
    try:
        with client.stream(
            "GET", url, timeout=min(SNAPSHOT_TIMEOUT_SECONDS, remaining)
        ) as response:
            if response.is_redirect:
                raise GitHubSnapshotError("GitHub redirects are not followed")
            if response.status_code != 200:
                if response.status_code == 403:
                    raise GitHubSnapshotError(
                        "GitHub denied the public read or rate limit was reached"
                    )
                if response.status_code == 404:
                    raise GitHubSnapshotError(
                        "GitHub repository or ref was not found or is not public"
                    )
                raise GitHubSnapshotError(
                    f"GitHub returned an unsuccessful response (HTTP {response.status_code})"
                )
            length = response.headers.get("content-length")
            if length is not None:
                try:
                    if int(length) > max_bytes:
                        raise GitHubSnapshotError("GitHub response exceeds its byte limit")
                except ValueError as error:
                    raise GitHubSnapshotError(
                        "GitHub returned an invalid content length"
                    ) from error
            body = bytearray()
            for block in response.iter_bytes():
                if time.monotonic() >= deadline:
                    raise GitHubSnapshotError("GitHub snapshot exceeded its total time limit")
                body.extend(block)
                if len(body) > max_bytes:
                    raise GitHubSnapshotError("GitHub response exceeds its byte limit")
            return bytes(body)
    except httpx.HTTPError as error:
        raise GitHubSnapshotError("GitHub snapshot request failed") from error


def _parse_repository(repository: str) -> tuple[str, str]:
    if not isinstance(repository, str) or repository.count("/") != 1:
        raise ValueError("repository must use the owner/repository form")
    owner, name = repository.split("/")
    if (
        owner in (".", "..")
        or name in (".", "..")
        or not _REPOSITORY_PART.fullmatch(owner)
        or not _REPOSITORY_PART.fullmatch(name)
    ):
        raise ValueError("repository must use the owner/repository form")
    return owner, name


def _validate_markdown_path(path: str) -> None:
    parts = path.split("/")
    try:
        path_size = len(path.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise GitHubSnapshotError("GitHub Markdown path is not valid UTF-8") from error
    if (
        path.startswith("/")
        or "\\" in path
        or "\x00" in path
        or path_size > MAX_MARKDOWN_PATH_BYTES
        or any(part in ("", ".", "..") for part in parts)
        or any(ord(character) < 32 for character in path)
    ):
        raise GitHubSnapshotError("GitHub Markdown path is unsafe")
