"""Least-privilege GitHub App authentication for private repository reads."""

from __future__ import annotations

import json
import os
import stat
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import jwt

from trussium_knowledge_agent.github_snapshot import GITHUB_API, SNAPSHOT_TIMEOUT_SECONDS

MAX_APP_KEY_BYTES = 64 * 1024
MAX_APP_RESPONSE_BYTES = 64 * 1024
MAX_INSTALLATION_TOKEN_SECONDS = 3600


def _read_bounded_response(response: httpx.Response) -> bytes:
    """Read a GitHub API response without allowing an unbounded body allocation."""
    body = bytearray()
    for block in response.iter_bytes():
        body.extend(block)
        if len(body) > MAX_APP_RESPONSE_BYTES:
            raise GitHubAppError("GitHub App response exceeds its size limit")
    return bytes(body)


class GitHubAppError(RuntimeError):
    """GitHub App configuration or authorization failed safely."""


@dataclass(frozen=True, slots=True)
class GitHubInstallationToken:
    """Short-lived token and validated server-provided expiry."""

    value: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class GitHubAppConfig:
    """Operator-provided identifiers and local key path; no secret is persisted."""

    app_id: int
    installation_id: int
    private_key_path: Path
    authorization_lease_seconds: int = 900

    @classmethod
    def from_environment(cls) -> GitHubAppConfig:
        """Load and validate explicit GitHub App settings from the environment."""
        try:
            app_id = int(os.environ["KNOWLEDGE_AGENT_GITHUB_APP_ID"])
            installation_id = int(os.environ["KNOWLEDGE_AGENT_GITHUB_INSTALLATION_ID"])
        except (KeyError, ValueError) as error:
            raise GitHubAppError("GitHub App ID and installation ID must be configured") from error
        if not 0 < app_id <= 2**63 - 1 or not 0 < installation_id <= 2**63 - 1:
            raise GitHubAppError("GitHub App ID and installation ID must be positive integers")
        key_value = os.environ.get("KNOWLEDGE_AGENT_GITHUB_PRIVATE_KEY_PATH", "")
        key_path = Path(key_value)
        if not key_value or not key_path.is_absolute():
            raise GitHubAppError("GitHub App private key path must be absolute")
        try:
            lease_seconds = int(
                os.environ.get("KNOWLEDGE_AGENT_GITHUB_AUTHORIZATION_LEASE_SECONDS", "900")
            )
        except ValueError as error:
            raise GitHubAppError("authorization lease must be an integer") from error
        if not 60 <= lease_seconds <= MAX_INSTALLATION_TOKEN_SECONDS:
            raise GitHubAppError("authorization lease must be between 60 and 3600 seconds")
        return cls(app_id, installation_id, key_path, lease_seconds)

    def read_private_key(self) -> bytes:
        """Read a small, regular, owner-only, non-symlinked PEM key file."""
        descriptor = -1
        try:
            descriptor = os.open(
                self.private_key_path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_mode & 0o077
                or metadata.st_size > MAX_APP_KEY_BYTES
            ):
                raise GitHubAppError("GitHub App key file must be regular, private, and bounded")
            if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
                raise GitHubAppError("GitHub App key file must be owned by the service account")
            key = os.read(descriptor, MAX_APP_KEY_BYTES + 1)
        except OSError as error:
            raise GitHubAppError("GitHub App key file is unavailable") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if len(key) > MAX_APP_KEY_BYTES or not key.startswith(b"-----BEGIN "):
            raise GitHubAppError("GitHub App key file is not a PEM key")
        return key


def create_app_jwt(config: GitHubAppConfig, *, now: int | None = None) -> str:
    """Sign a short-lived RS256 GitHub App JWT without exposing key material."""
    issued_at = int(time.time()) if now is None else now
    try:
        return jwt.encode(
            {"iat": issued_at - 30, "exp": issued_at + 540, "iss": str(config.app_id)},
            config.read_private_key(),
            algorithm="RS256",
        )
    except (ValueError, jwt.PyJWTError) as error:
        raise GitHubAppError("GitHub App key could not sign an authentication token") from error


def create_installation_token(
    config: GitHubAppConfig,
    repository_id: int,
    *,
    transport: httpx.BaseTransport | None = None,
    now: datetime | None = None,
) -> GitHubInstallationToken:
    """Mint a token scoped to one immutable repository and Contents: read."""
    if isinstance(repository_id, bool) or not 0 < repository_id <= 2**63 - 1:
        raise ValueError("repository ID must be a positive integer")
    request_time = now or datetime.now(UTC)
    url = f"{GITHUB_API}/app/installations/{config.installation_id}/access_tokens"
    try:
        with (
            httpx.Client(
                transport=transport,
                timeout=SNAPSHOT_TIMEOUT_SECONDS,
                follow_redirects=False,
                trust_env=False,
            ) as client,
            client.stream(
                "POST",
                url,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {create_app_jwt(config)}",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "trussium-knowledge-agent",
                },
                json={
                    "repository_ids": [repository_id],
                    "permissions": {"contents": "read"},
                },
            ) as response,
        ):
            if response.is_redirect:
                raise GitHubAppError("GitHub App authentication redirects are not followed")
            if response.status_code != 201:
                raise GitHubAppError(
                    f"GitHub App could not authorize repository (HTTP {response.status_code})"
                )
            content = _read_bounded_response(response)
            payload = json.loads(content)
    except httpx.HTTPError as error:
        raise GitHubAppError("GitHub App authentication request failed") from error
    except ValueError as error:
        raise GitHubAppError("GitHub App returned an invalid token response") from error
    token = payload.get("token") if isinstance(payload, dict) else None
    expires_at = payload.get("expires_at") if isinstance(payload, dict) else None
    permissions = payload.get("permissions") if isinstance(payload, dict) else None
    repositories = payload.get("repositories") if isinstance(payload, dict) else None
    try:
        expires = datetime.fromisoformat(str(expires_at))
    except ValueError as error:
        raise GitHubAppError("GitHub App returned an invalid token expiry") from error
    if (
        not isinstance(token, str)
        or not token
        or expires.tzinfo is None
        or not request_time
        < expires
        <= request_time + timedelta(seconds=MAX_INSTALLATION_TOKEN_SECONDS)
        or not isinstance(permissions, dict)
        or permissions.get("contents") != "read"
        or any(name != "metadata" and name != "contents" for name in permissions)
        or any(value != "read" for value in permissions.values())
        or not isinstance(repositories, list)
        or len(repositories) != 1
        or not isinstance(repositories[0], dict)
        or isinstance(repositories[0].get("id"), bool)
        or repositories[0].get("id") != repository_id
    ):
        raise GitHubAppError("GitHub App returned a token outside the requested scope")
    return GitHubInstallationToken(token, expires)


def verify_repository_access(
    repository_id: int,
    token: GitHubInstallationToken,
    *,
    transport: httpx.BaseTransport | None = None,
) -> bool:
    """Check one repository with its one-repository-scoped installation token."""
    url = f"{GITHUB_API}/repositories/{repository_id}"
    try:
        with (
            httpx.Client(
                transport=transport,
                timeout=SNAPSHOT_TIMEOUT_SECONDS,
                follow_redirects=False,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {token.value}",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "trussium-knowledge-agent",
                },
                trust_env=False,
            ) as client,
            client.stream("GET", url) as response,
        ):
            if response.is_redirect:
                raise GitHubAppError("GitHub repository authorization redirects are not followed")
            if response.status_code == 404:
                return False
            if response.status_code != 200:
                raise GitHubAppError(
                    f"GitHub repository authorization check failed (HTTP {response.status_code})"
                )
            payload: Any = json.loads(_read_bounded_response(response))
    except httpx.HTTPError as error:
        raise GitHubAppError("GitHub repository authorization check failed") from error
    except ValueError as error:
        raise GitHubAppError("GitHub returned invalid repository metadata") from error
    if (
        not isinstance(payload, dict)
        or isinstance(payload.get("id"), bool)
        or payload.get("id") != repository_id
        or payload.get("private") is not True
    ):
        raise GitHubAppError("GitHub repository identity did not match the requested source")
    return True
