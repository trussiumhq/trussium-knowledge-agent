"""Public demo configuration and ASGI enforcement tests."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from typing import Any, Self

import jwt
import pytest
from fastapi.testclient import TestClient
from starlette.types import ASGIApp, Message, Receive, Scope, Send

import trussium_knowledge_agent.app as application
from trussium_knowledge_agent.public_demo import (
    DEMO_IDENTITY_COOKIE,
    DemoPrincipal,
    PublicDemoConfigurationError,
    PublicDemoSafetyMiddleware,
    SlidingWindowLimiter,
    load_public_demo_config,
)
from trussium_knowledge_agent.store import reserve_public_demo_model_call

_SECRET = "s" * 40
_SOURCE_ID = "a" * 64


def _environment() -> dict[str, str]:
    return {
        "KNOWLEDGE_AGENT_PUBLIC_DEMO_MODE": "true",
        "KNOWLEDGE_AGENT_PUBLIC_DEMO_IDENTITY_SECRET": _SECRET,
        "KNOWLEDGE_AGENT_PUBLIC_DEMO_ORIGIN": "https://demo.example.test",
        "KNOWLEDGE_AGENT_PUBLIC_DEMO_SOURCE_IDS": _SOURCE_ID,
    }


def _token(**overrides: object) -> str:
    now = int(time.time())
    claims: dict[str, object] = {
        "iss": "trussium-knowledge-agent-ingress",
        "aud": "trussium-knowledge-agent-demo",
        "sub": "anonymous-session-123",
        "client_ip": "192.0.2.10",
        "origin": "https://demo.example.test",
        "iat": now,
        "exp": now + 120,
    }
    claims.update(overrides)
    return jwt.encode(claims, _SECRET, algorithm="HS256")


def test_demo_config_is_disabled_by_default() -> None:
    assert load_public_demo_config({}) is None


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("KNOWLEDGE_AGENT_PUBLIC_DEMO_IDENTITY_SECRET", "short"),
        ("KNOWLEDGE_AGENT_PUBLIC_DEMO_ORIGIN", "http://demo.example.test"),
        ("KNOWLEDGE_AGENT_PUBLIC_DEMO_SOURCE_IDS", "not-a-hash"),
        ("KNOWLEDGE_AGENT_PUBLIC_DEMO_MAX_BODY_BYTES", "65537"),
        ("KNOWLEDGE_AGENT_PUBLIC_DEMO_DAILY_MODEL_CALL_LIMIT", "0"),
    ],
)
def test_demo_config_rejects_missing_or_unsafe_values(name: str, value: str) -> None:
    env = _environment()
    env[name] = value

    with pytest.raises(PublicDemoConfigurationError):
        load_public_demo_config(env)


@pytest.mark.parametrize(
    "name",
    [
        "KNOWLEDGE_AGENT_GITHUB_APP_ID",
        "KNOWLEDGE_AGENT_GITHUB_INSTALLATION_ID",
        "KNOWLEDGE_AGENT_GITHUB_PRIVATE_KEY_PATH",
        "KNOWLEDGE_AGENT_GITHUB_WEBHOOK_SECRET",
        "KNOWLEDGE_AGENT_TOOL_TOKEN",
    ],
)
def test_demo_config_rejects_private_integrations(name: str) -> None:
    env = _environment()
    env[name] = "must-not-be-present"

    with pytest.raises(PublicDemoConfigurationError, match="must be disabled"):
        load_public_demo_config(env)


def _request_headers(
    token: str | None, origin: str = "https://demo.example.test"
) -> list[tuple[bytes, bytes]]:
    headers = [(b"origin", origin.encode())]
    if token is not None:
        headers.append((b"cookie", f"{DEMO_IDENTITY_COOKIE}={token}".encode()))
    return headers


def _run_request(
    middleware: ASGIApp,
    *,
    path: str = "/api/ask",
    headers: list[tuple[bytes, bytes]] | None = None,
    body: bytes = b"{}",
    state: dict[str, object] | None = None,
) -> tuple[list[Message], list[dict[str, object]]]:
    messages: list[Message] = [{"type": "http.request", "body": body, "more_body": False}]
    responses: list[Message] = []
    app_calls: list[dict[str, object]] = []
    captured_state = {} if state is None else state

    async def receive() -> Message:
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        responses.append(dict(message))

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        app_calls.append({"state": scope["state"]})
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers or [],
        "server": ("demo.example.test", 443),
        "client": ("192.0.2.10", 1234),
        "state": captured_state,
    }
    if isinstance(middleware, PublicDemoSafetyMiddleware):
        middleware.app = app

    async def run() -> None:
        await middleware(scope, receive, send)

    asyncio.run(run())
    return responses, app_calls


def _status(responses: list[Message]) -> int:
    return next(int(item["status"]) for item in responses if item["type"] == "http.response.start")


def test_demo_middleware_rejects_missing_invalid_identity_and_wrong_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KNOWLEDGE_AGENT_PUBLIC_DEMO_MODE", "true")
    monkeypatch.setenv("KNOWLEDGE_AGENT_PUBLIC_DEMO_IDENTITY_SECRET", _SECRET)
    monkeypatch.setenv("KNOWLEDGE_AGENT_PUBLIC_DEMO_ORIGIN", "https://demo.example.test")
    monkeypatch.setenv("KNOWLEDGE_AGENT_PUBLIC_DEMO_SOURCE_IDS", _SOURCE_ID)
    config = load_public_demo_config()
    assert config is not None
    middleware = PublicDemoSafetyMiddleware(lambda *_: None)  # type: ignore[arg-type]

    missing, called = _run_request(middleware, state={"public_demo_config": config})
    invalid, _ = _run_request(
        middleware,
        headers=_request_headers("not-a-token"),
        state={"public_demo_config": config},
    )
    wrong_origin, _ = _run_request(
        middleware,
        headers=_request_headers(_token(), "https://attacker.example.test"),
        state={"public_demo_config": config},
    )

    assert _status(missing) == 401
    assert _status(invalid) == 401
    assert _status(wrong_origin) == 401
    assert called == []


def test_demo_middleware_allows_signed_identity_and_blocks_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KNOWLEDGE_AGENT_PUBLIC_DEMO_MODE", "true")
    monkeypatch.setenv("KNOWLEDGE_AGENT_PUBLIC_DEMO_IDENTITY_SECRET", _SECRET)
    monkeypatch.setenv("KNOWLEDGE_AGENT_PUBLIC_DEMO_ORIGIN", "https://demo.example.test")
    monkeypatch.setenv("KNOWLEDGE_AGENT_PUBLIC_DEMO_SOURCE_IDS", _SOURCE_ID)
    config = load_public_demo_config()
    assert config is not None
    middleware = PublicDemoSafetyMiddleware(lambda *_: None)  # type: ignore[arg-type]

    accepted, called = _run_request(
        middleware,
        headers=_request_headers(_token()),
        state={"public_demo_config": config},
    )
    mcp, mcp_called = _run_request(middleware, path="/v1/mcp", state={"public_demo_config": config})

    assert _status(accepted) == 200
    assert isinstance(called[0]["state"], dict)
    principal = called[0]["state"]["public_demo_principal"]
    assert isinstance(principal, DemoPrincipal)
    assert principal.subject == "anonymous-session-123"
    assert _status(mcp) == 404
    assert mcp_called == []


def test_demo_middleware_enforces_body_cap_kill_switch_and_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KNOWLEDGE_AGENT_PUBLIC_DEMO_MODE", "true")
    monkeypatch.setenv("KNOWLEDGE_AGENT_PUBLIC_DEMO_IDENTITY_SECRET", _SECRET)
    monkeypatch.setenv("KNOWLEDGE_AGENT_PUBLIC_DEMO_ORIGIN", "https://demo.example.test")
    monkeypatch.setenv("KNOWLEDGE_AGENT_PUBLIC_DEMO_SOURCE_IDS", _SOURCE_ID)
    monkeypatch.setenv("KNOWLEDGE_AGENT_PUBLIC_DEMO_MAX_BODY_BYTES", "4")
    config = load_public_demo_config()
    assert config is not None
    middleware = PublicDemoSafetyMiddleware(lambda *_: None)  # type: ignore[arg-type]
    signed_headers = _request_headers(_token())

    oversized, _ = _run_request(
        middleware, headers=signed_headers, body=b"12345", state={"public_demo_config": config}
    )
    assert _status(oversized) == 413

    monkeypatch.setenv("KNOWLEDGE_AGENT_PUBLIC_DEMO_KILL_SWITCH", "true")
    disabled, _ = _run_request(
        middleware, headers=signed_headers, state={"public_demo_config": config}
    )
    assert _status(disabled) == 503
    monkeypatch.delenv("KNOWLEDGE_AGENT_PUBLIC_DEMO_KILL_SWITCH")

    monkeypatch.setenv("KNOWLEDGE_AGENT_PUBLIC_DEMO_IDENTITY_RATE_LIMIT", "1")
    config = load_public_demo_config()
    assert config is not None
    limited_middleware = PublicDemoSafetyMiddleware(lambda *_: None)  # type: ignore[arg-type]
    first, _ = _run_request(
        limited_middleware, headers=signed_headers, body=b"{}", state={"public_demo_config": config}
    )
    second, _ = _run_request(
        limited_middleware, headers=signed_headers, body=b"{}", state={"public_demo_config": config}
    )

    assert _status(first) == 200
    assert _status(second) == 429


def test_limiter_uses_identity_and_ip_buckets() -> None:
    config = load_public_demo_config(_environment())
    assert config is not None
    limiter = SlidingWindowLimiter()
    principal = DemoPrincipal("one", "192.0.2.1")
    one_per_identity = replace(config, requests_per_identity_per_minute=1)

    assert limiter.allow(principal, one_per_identity)
    assert not limiter.allow(principal, one_per_identity)

    ip_limiter = SlidingWindowLimiter()
    one_per_ip = replace(config, requests_per_ip_per_minute=1)
    assert ip_limiter.allow(DemoPrincipal("first", "192.0.2.1"), one_per_ip)
    assert not ip_limiter.allow(DemoPrincipal("second", "192.0.2.1"), one_per_ip)


def test_limiter_window_resets(monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_public_demo_config(_environment())
    assert config is not None
    config = replace(config, requests_per_identity_per_minute=1)
    limiter = SlidingWindowLimiter()
    principal = DemoPrincipal("one", "192.0.2.1")
    current = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: current[0])

    assert limiter.allow(principal, config)
    assert not limiter.allow(principal, config)
    current[0] += 60.1
    assert limiter.allow(principal, config)


def test_token_lifetime_is_bounded() -> None:
    config = load_public_demo_config(_environment())
    assert config is not None
    middleware = PublicDemoSafetyMiddleware(lambda *_: None)  # type: ignore[arg-type]
    too_long = _token(exp=int(time.time()) + 600)
    responses, called = _run_request(
        middleware,
        headers=_request_headers(too_long),
        state={"public_demo_config": config},
    )

    assert _status(responses) == 401
    assert called == []


def test_demo_startup_validates_allowlist_then_requires_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KNOWLEDGE_AGENT_PUBLIC_DEMO_MODE", "true")
    monkeypatch.setenv("KNOWLEDGE_AGENT_PUBLIC_DEMO_IDENTITY_SECRET", _SECRET)
    monkeypatch.setenv("KNOWLEDGE_AGENT_PUBLIC_DEMO_ORIGIN", "https://demo.example.test")
    monkeypatch.setenv("KNOWLEDGE_AGENT_PUBLIC_DEMO_SOURCE_IDS", _SOURCE_ID)
    monkeypatch.setenv("DATABASE_URL", "postgresql://demo")
    validated: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        application,
        "validate_public_demo_sources",
        lambda _url, ids: validated.append(ids),
    )

    with TestClient(application.app) as client:
        response = client.post("/api/ask", json={"question": "hello"})

    assert validated == [(_SOURCE_ID,)]
    assert response.status_code == 401


def test_daily_budget_reservation_uses_atomic_postgres_upsert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements: list[tuple[str, tuple[Any, ...] | None]] = []

    class Cursor:
        def fetchone(self) -> tuple[int] | None:
            return (1,)

    class FakeConnection:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def execute(self, query: str, params: tuple[Any, ...] | None = None) -> Cursor:
            statements.append((query, params))
            return Cursor()

    monkeypatch.setattr(
        "trussium_knowledge_agent.store.psycopg.connect", lambda _url: FakeConnection()
    )
    monkeypatch.setattr(
        "trussium_knowledge_agent.store._apply_migrations", lambda _connection: None
    )

    assert reserve_public_demo_model_call("postgresql://demo", 100)
    budget_query = next(query for query, _ in statements if "public_demo_model_usage" in query)
    assert "ON CONFLICT (usage_day) DO UPDATE" in budget_query
    assert "WHERE public_demo_model_usage.model_calls < %s" in budget_query
