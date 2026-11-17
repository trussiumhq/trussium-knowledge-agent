from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from trussium_knowledge_agent.github_app import (
    GitHubAppConfig,
    GitHubAppError,
    GitHubInstallationToken,
    create_app_jwt,
    create_installation_token,
    verify_repository_access,
)
from trussium_knowledge_agent.github_snapshot import (
    GITHUB_API,
    GitHubSnapshotError,
    fetch_private_github_snapshot,
)

REPOSITORY_ID = 4242
INSTALLATION_ID = 4567
APP_ID = 1234
COMMIT_SHA = "a" * 40
TREE_SHA = "b" * 40
CONTENT = b"# Private guide\n\nDo not send this token outside api.github.com.\n"
BLOB_SHA = hashlib.sha1(b"blob " + str(len(CONTENT)).encode() + b"\0" + CONTENT).hexdigest()


@pytest.fixture
def app_config(tmp_path: Path) -> GitHubAppConfig:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path = tmp_path / "github-app.pem"
    key_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    return GitHubAppConfig(APP_ID, INSTALLATION_ID, key_path)


def _token_response(repository_id: int = REPOSITORY_ID) -> dict[str, Any]:
    return {
        "token": "ghs-test-secret-token",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=55)).isoformat(),
        "permissions": {"contents": "read", "metadata": "read"},
        "repositories": [{"id": repository_id, "name": "docs", "private": True}],
    }


def test_app_jwt_is_short_lived_and_contains_app_id(app_config: GitHubAppConfig) -> None:
    token = create_app_jwt(app_config, now=1_800_000_000)
    claims = jwt.decode(token, options={"verify_signature": False})

    assert claims["iss"] == str(APP_ID)
    assert claims["iat"] == 1_799_999_970
    assert claims["exp"] - claims["iat"] == 570
    assert "BEGIN" not in token


def test_private_key_must_not_be_group_or_world_readable(app_config: GitHubAppConfig) -> None:
    app_config.private_key_path.chmod(0o640)

    with pytest.raises(GitHubAppError, match="regular, private, and bounded"):
        create_app_jwt(app_config)


def test_installation_token_is_scoped_to_one_repo_and_read_only(
    app_config: GitHubAppConfig,
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json=_token_response())

    token = create_installation_token(
        app_config,
        REPOSITORY_ID,
        transport=httpx.MockTransport(respond),
    )

    request = requests[0]
    assert request.url == (f"{GITHUB_API}/app/installations/{INSTALLATION_ID}/access_tokens")
    assert request.headers["authorization"].startswith("Bearer ")
    claims = jwt.decode(
        request.headers["authorization"].removeprefix("Bearer "),
        options={"verify_signature": False},
    )
    assert claims["iss"] == str(APP_ID)
    assert json.loads(request.content) == {
        "repository_ids": [REPOSITORY_ID],
        "permissions": {"contents": "read"},
    }
    assert token.value == "ghs-test-secret-token"
    assert "ghs-test-secret-token" not in repr(token)


@pytest.mark.parametrize(
    "response",
    [
        {**_token_response(), "repositories": [{"id": 9}]},
        {**_token_response(), "permissions": {"contents": "write"}},
        {**_token_response(), "permissions": {"contents": "read", "issues": "read"}},
        {**_token_response(), "repositories": [{"id": REPOSITORY_ID}, {"id": 9}]},
    ],
)
def test_installation_token_rejects_broader_than_requested_scope(
    app_config: GitHubAppConfig, response: dict[str, Any]
) -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(201, json=response))

    with pytest.raises(GitHubAppError, match="outside the requested scope"):
        create_installation_token(app_config, REPOSITORY_ID, transport=transport)


def test_installation_token_response_is_bounded(app_config: GitHubAppConfig) -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(201, content=b"x" * (64 * 1024 + 1))
    )

    with pytest.raises(GitHubAppError, match="response exceeds its size limit"):
        create_installation_token(app_config, REPOSITORY_ID, transport=transport)


def test_repository_authorization_check_uses_only_github_api(
    app_config: GitHubAppConfig,
) -> None:
    del app_config
    observed: list[httpx.Request] = []
    token = GitHubInstallationToken("ghs-scoped-secret", datetime.now(UTC) + timedelta(minutes=50))

    def respond(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(200, json={"id": REPOSITORY_ID, "private": True})

    assert verify_repository_access(REPOSITORY_ID, token, transport=httpx.MockTransport(respond))
    assert len(observed) == 1
    assert observed[0].url.host == "api.github.com"
    assert observed[0].headers["authorization"] == f"Bearer {token.value}"


def test_repository_authorization_rejects_boolean_repository_id() -> None:
    token = GitHubInstallationToken("ghs-scoped-secret", datetime.now(UTC) + timedelta(minutes=50))
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"id": True, "private": True})
    )

    with pytest.raises(GitHubAppError, match="identity did not match"):
        verify_repository_access(1, token, transport=transport)


def test_private_snapshot_fetches_blobs_only_from_authenticated_api() -> None:
    requests: list[httpx.Request] = []
    tree = {
        "truncated": False,
        "tree": [
            {"path": "guide.md", "mode": "100644", "type": "blob", "sha": BLOB_SHA},
        ],
    }

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "api.github.com"
        assert request.headers["authorization"] == "Bearer ghs-private-test"
        if request.url.path == f"/repositories/{REPOSITORY_ID}":
            return httpx.Response(
                200,
                json={"id": REPOSITORY_ID, "full_name": "acme/docs", "private": True},
            )
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(
                200, json={"sha": COMMIT_SHA, "commit": {"tree": {"sha": TREE_SHA}}}
            )
        if request.url.path.endswith(f"/git/trees/{TREE_SHA}"):
            return httpx.Response(200, json=tree)
        if request.url.path.endswith(f"/git/blobs/{BLOB_SHA}"):
            return httpx.Response(
                200,
                json={
                    "sha": BLOB_SHA,
                    "size": len(CONTENT),
                    "encoding": "base64",
                    "content": base64.b64encode(CONTENT).decode("ascii"),
                },
            )
        raise AssertionError(f"unexpected request path: {request.url.path}")

    result = fetch_private_github_snapshot(
        REPOSITORY_ID,
        "main",
        "ghs-private-test",
        transport=httpx.MockTransport(respond),
    )

    assert result.repository_id == REPOSITORY_ID
    assert result.repository == "acme/docs"
    assert result.revision == COMMIT_SHA
    assert result.source_identity == f"github.com:repository:{REPOSITORY_ID}"
    assert result.file_count == 1
    assert "Do not send this token" in result.chunks[0].content
    assert {request.url.host for request in requests} == {"api.github.com"}


def test_private_snapshot_rejects_content_that_does_not_match_git_blob_sha() -> None:
    tree = {
        "truncated": False,
        "tree": [
            {"path": "guide.md", "mode": "100644", "type": "blob", "sha": BLOB_SHA},
        ],
    }

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/repositories/{REPOSITORY_ID}":
            return httpx.Response(
                200,
                json={"id": REPOSITORY_ID, "full_name": "acme/docs", "private": True},
            )
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(
                200, json={"sha": COMMIT_SHA, "commit": {"tree": {"sha": TREE_SHA}}}
            )
        if request.url.path.endswith(f"/git/trees/{TREE_SHA}"):
            return httpx.Response(200, json=tree)
        if request.url.path.endswith(f"/git/blobs/{BLOB_SHA}"):
            return httpx.Response(
                200,
                json={
                    "sha": BLOB_SHA,
                    "size": len(CONTENT),
                    "encoding": "base64",
                    "content": base64.b64encode(b"x" * len(CONTENT)).decode("ascii"),
                },
            )
        raise AssertionError(f"unexpected request path: {request.url.path}")

    with pytest.raises(GitHubSnapshotError, match="tree identity"):
        fetch_private_github_snapshot(
            REPOSITORY_ID,
            "main",
            "ghs-private-test",
            transport=httpx.MockTransport(respond),
        )
