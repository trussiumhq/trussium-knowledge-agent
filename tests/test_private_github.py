from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trussium_knowledge_agent import private_github
from trussium_knowledge_agent.github_app import (
    GitHubAppConfig,
    GitHubAppError,
    GitHubInstallationToken,
)

INSTALLATION_ID = 4567
SOURCES = [
    ("a" * 64, 101, "github.com:repository:101"),
    ("b" * 64, 202, "github.com:repository:202"),
]


def _config(tmp_path: Path) -> GitHubAppConfig:
    key_path = tmp_path / "app.pem"
    key_path.write_text("-----BEGIN PRIVATE KEY-----\nfixture\n", encoding="utf-8")
    key_path.chmod(0o600)
    return GitHubAppConfig(1234, INSTALLATION_ID, key_path, 300)


def test_reconcile_uses_single_repo_tokens_and_quarantines_confirmed_404(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    created: list[int] = []
    marked: list[tuple[str, str]] = []
    renewed: list[tuple[str, int, datetime]] = []
    lease_expiry = datetime.now(UTC) + timedelta(minutes=40)

    monkeypatch.setattr(private_github, "list_private_github_sources", lambda *_: SOURCES)

    def mint(
        _config: GitHubAppConfig, repository_id: int, **_kwargs: object
    ) -> GitHubInstallationToken:
        created.append(repository_id)
        return GitHubInstallationToken("token", lease_expiry)

    def mark_lost(_database_url: str, source_id: str) -> bool:
        marked.append(("lost", source_id))
        return True

    monkeypatch.setattr(private_github, "create_installation_token", mint)
    monkeypatch.setattr(
        private_github,
        "verify_repository_access",
        lambda repository_id, *_args, **_kwargs: repository_id == 101,
    )
    monkeypatch.setattr(private_github, "mark_source_access_lost", mark_lost)

    def renew(
        _database_url: str, source_id: str, installation_id: int, verified_until: datetime
    ) -> bool:
        renewed.append((source_id, installation_id, verified_until))
        return True

    monkeypatch.setattr(private_github, "verify_private_source_authorization", renew)
    now = datetime.now(UTC)
    result = private_github.reconcile_private_github_authorization("db", _config(tmp_path), now=now)

    assert result == (1, 1, 0)
    assert created == [101, 202]
    assert marked == [("lost", "b" * 64)]
    assert renewed == [("a" * 64, INSTALLATION_ID, min(now + timedelta(seconds=300), lease_expiry))]


def test_transient_reconcile_failure_does_not_mark_revoked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    marked: list[str] = []
    monkeypatch.setattr(private_github, "list_private_github_sources", lambda *_: SOURCES[:1])
    monkeypatch.setattr(
        private_github,
        "create_installation_token",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(GitHubAppError("temporary")),
    )

    def mark_lost(_database_url: str, source_id: str) -> bool:
        marked.append(source_id)
        return True

    monkeypatch.setattr(private_github, "mark_source_access_lost", mark_lost)

    assert private_github.reconcile_private_github_authorization("db", _config(tmp_path)) == (
        0,
        0,
        1,
    )
    assert marked == []


def test_repository_removal_webhook_quarantines_only_removed_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marked: list[str] = []
    monkeypatch.setattr(private_github, "github_webhook_delivery_recorded", lambda *_: False)
    monkeypatch.setattr(private_github, "list_private_github_sources", lambda *_: SOURCES)

    def mark_lost(_database_url: str, source_id: str) -> bool:
        marked.append(source_id)
        return True

    monkeypatch.setattr(private_github, "mark_source_access_lost", mark_lost)
    monkeypatch.setattr(private_github, "record_github_webhook_delivery", lambda *_: True)

    completed = private_github.process_github_webhook(
        "db",
        "delivery-1",
        "installation_repositories",
        {
            "action": "removed",
            "installation": {"id": INSTALLATION_ID},
            "repositories_removed": [{"id": 202}],
        },
        expected_installation_id=INSTALLATION_ID,
    )

    assert completed
    assert marked == ["b" * 64]


def test_empty_removal_list_fails_closed_for_all_installation_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marked: list[str] = []
    monkeypatch.setattr(private_github, "github_webhook_delivery_recorded", lambda *_: False)
    monkeypatch.setattr(private_github, "list_private_github_sources", lambda *_: SOURCES)

    def mark_lost(_database_url: str, source_id: str) -> bool:
        marked.append(source_id)
        return True

    monkeypatch.setattr(private_github, "mark_source_access_lost", mark_lost)
    monkeypatch.setattr(private_github, "record_github_webhook_delivery", lambda *_: True)

    private_github.process_github_webhook(
        "db",
        "delivery-2",
        "installation_repositories",
        {
            "action": "removed",
            "installation": {"id": INSTALLATION_ID},
            "repositories_removed": [],
        },
        expected_installation_id=INSTALLATION_ID,
    )

    assert marked == ["a" * 64, "b" * 64]


def test_webhook_wrong_installation_rejected_before_source_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(private_github, "github_webhook_delivery_recorded", lambda *_: False)
    monkeypatch.setattr(
        private_github,
        "list_private_github_sources",
        lambda *_: pytest.fail("wrong installation must be rejected first"),
    )

    with pytest.raises(ValueError, match="does not match configuration"):
        private_github.process_github_webhook(
            "db",
            "delivery-3",
            "installation",
            {"action": "deleted", "installation": {"id": 9999}},
            expected_installation_id=INSTALLATION_ID,
        )
