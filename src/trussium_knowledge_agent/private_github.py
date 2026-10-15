"""Authorization reconciliation and security-only GitHub App webhooks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from trussium_knowledge_agent.github_app import (
    GitHubAppConfig,
    GitHubAppError,
    create_installation_token,
    verify_repository_access,
)
from trussium_knowledge_agent.store import (
    github_webhook_delivery_recorded,
    list_private_github_sources,
    mark_source_access_lost,
    record_github_webhook_delivery,
    verify_private_source_authorization,
)


def reconcile_private_github_authorization(
    database_url: str,
    config: GitHubAppConfig,
    *,
    transport: httpx.BaseTransport | None = None,
    now: datetime | None = None,
) -> tuple[int, int, int]:
    """Refresh leases using one-repository tokens; quarantine confirmed 404s.

    Returns (verified, access_lost, unable_to_verify). Transient API errors do
    not claim revocation and never extend the current lease.
    """
    verified = revoked = failures = 0
    checked_at = now or datetime.now(UTC)
    sources = list_private_github_sources(database_url, config.installation_id)
    for source_id, repository_id, _identity in sources:
        try:
            token = create_installation_token(config, repository_id, transport=transport)
            accessible = verify_repository_access(repository_id, token, transport=transport)
            if not accessible:
                mark_source_access_lost(database_url, source_id)
                revoked += 1
                continue
            lease = min(
                checked_at + timedelta(seconds=config.authorization_lease_seconds),
                token.expires_at,
            )
            if verify_private_source_authorization(
                database_url, source_id, config.installation_id, lease
            ):
                verified += 1
        except (GitHubAppError, OSError, RuntimeError, ValueError):
            failures += 1
    return verified, revoked, failures


def process_github_webhook(
    database_url: str,
    delivery_id: str,
    event_name: str,
    payload: dict[str, Any],
    *,
    expected_installation_id: int,
) -> bool:
    """Apply restrictive installation events; never trigger content indexing."""
    if github_webhook_delivery_recorded(database_url, delivery_id):
        return False
    installation = payload.get("installation")
    installation_id = installation.get("id") if isinstance(installation, dict) else None
    if (
        isinstance(installation_id, bool)
        or not isinstance(installation_id, int)
        or installation_id <= 0
    ):
        raise ValueError("GitHub webhook installation identity is invalid")
    if installation_id != expected_installation_id:
        raise ValueError("GitHub webhook installation identity does not match configuration")

    all_sources = list_private_github_sources(database_url, installation_id)
    targets: list[tuple[str, int, str]] = []
    action = payload.get("action")
    if event_name == "installation" and action in {"suspend", "deleted"}:
        targets = all_sources
    elif event_name == "installation_repositories" and action == "removed":
        removed = payload.get("repositories_removed")
        if not isinstance(removed, list):
            raise ValueError("GitHub webhook removed repositories are invalid")
        if not removed:
            # The all-to-selected transition may send an empty removal list.
            targets = all_sources
        else:
            removed_ids: set[int] = set()
            for item in removed:
                repository_id = item.get("id") if isinstance(item, dict) else None
                if (
                    isinstance(repository_id, bool)
                    or not isinstance(repository_id, int)
                    or repository_id <= 0
                ):
                    raise ValueError("GitHub webhook repository identity is invalid")
                removed_ids.add(repository_id)
            targets = [row for row in all_sources if row[1] in removed_ids]

    for source_id, _repository_id, _identity in targets:
        mark_source_access_lost(database_url, source_id)
    return record_github_webhook_delivery(database_url, delivery_id)
