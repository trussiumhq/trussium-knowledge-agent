"""Fail-closed access and resource controls for an explicitly enabled public demo."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import threading
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from typing import Any

import jwt
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_DEMO_BODY_BYTES = 65_536
MAX_TOKEN_LIFETIME_SECONDS = 300
DEMO_IDENTITY_COOKIE = "__Host-tka-demo-identity"


class PublicDemoConfigurationError(ValueError):
    """The public-demo mode is incomplete or unsafe to start."""


@dataclass(frozen=True, slots=True)
class PublicDemoConfig:
    """Validated public-demo configuration; secrets are excluded from repr."""

    identity_secret: bytes = field(repr=False)
    expected_origin: str
    token_issuer: str
    token_audience: str
    allowed_source_ids: tuple[str, ...]
    daily_model_call_limit: int
    requests_per_identity_per_minute: int
    requests_per_ip_per_minute: int
    max_concurrent_requests: int
    max_body_bytes: int


@dataclass(frozen=True, slots=True)
class DemoPrincipal:
    """Authenticated identity claims signed by the trusted ingress."""

    subject: str
    client_ip: str


def _positive_int(name: str, value: str, *, minimum: int, maximum: int) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise PublicDemoConfigurationError(f"{name} must be an integer") from error
    if not minimum <= result <= maximum:
        raise PublicDemoConfigurationError(f"{name} is outside its supported range")
    return result


def load_public_demo_config(
    environ: Mapping[str, str] | None = None,
) -> PublicDemoConfig | None:
    """Return validated configuration or None when the public demo is disabled."""
    env = os.environ if environ is None else environ
    enabled_value = env.get("KNOWLEDGE_AGENT_PUBLIC_DEMO_MODE", "false").strip().lower()
    if enabled_value not in {"true", "false"}:
        raise PublicDemoConfigurationError("public demo mode must be true or false")
    if enabled_value == "false":
        return None

    private_settings = (
        "KNOWLEDGE_AGENT_GITHUB_APP_ID",
        "KNOWLEDGE_AGENT_GITHUB_INSTALLATION_ID",
        "KNOWLEDGE_AGENT_GITHUB_PRIVATE_KEY_PATH",
        "KNOWLEDGE_AGENT_GITHUB_WEBHOOK_SECRET",
        "KNOWLEDGE_AGENT_TOOL_TOKEN",
    )
    if any(env.get(name, "").strip() for name in private_settings):
        raise PublicDemoConfigurationError(
            "private GitHub access and MCP tools must be disabled in public demo mode"
        )

    secret = env.get("KNOWLEDGE_AGENT_PUBLIC_DEMO_IDENTITY_SECRET", "").encode("utf-8")
    if len(secret) < 32:
        raise PublicDemoConfigurationError("public demo identity secret must be at least 32 bytes")

    origin = env.get("KNOWLEDGE_AGENT_PUBLIC_DEMO_ORIGIN", "").strip().rstrip("/")
    if not re.fullmatch(r"https://[^/\s?#]+", origin):
        raise PublicDemoConfigurationError("public demo origin must be an HTTPS origin")

    source_ids = tuple(
        dict.fromkeys(
            item.strip()
            for item in env.get("KNOWLEDGE_AGENT_PUBLIC_DEMO_SOURCE_IDS", "").split(",")
            if item.strip()
        )
    )
    if not source_ids or any(not re.fullmatch(r"[0-9a-f]{64}", item) for item in source_ids):
        raise PublicDemoConfigurationError(
            "public demo requires an explicit allowlist of SHA-256 source IDs"
        )

    issuer = env.get(
        "KNOWLEDGE_AGENT_PUBLIC_DEMO_TOKEN_ISSUER", "trussium-knowledge-agent-ingress"
    ).strip()
    audience = env.get(
        "KNOWLEDGE_AGENT_PUBLIC_DEMO_TOKEN_AUDIENCE", "trussium-knowledge-agent-demo"
    ).strip()
    if not issuer or not audience:
        raise PublicDemoConfigurationError("public demo token issuer and audience are required")

    return PublicDemoConfig(
        identity_secret=secret,
        expected_origin=origin,
        token_issuer=issuer,
        token_audience=audience,
        allowed_source_ids=source_ids,
        daily_model_call_limit=_positive_int(
            "KNOWLEDGE_AGENT_PUBLIC_DEMO_DAILY_MODEL_CALL_LIMIT",
            env.get("KNOWLEDGE_AGENT_PUBLIC_DEMO_DAILY_MODEL_CALL_LIMIT", "100"),
            minimum=1,
            maximum=10_000,
        ),
        requests_per_identity_per_minute=_positive_int(
            "KNOWLEDGE_AGENT_PUBLIC_DEMO_IDENTITY_RATE_LIMIT",
            env.get("KNOWLEDGE_AGENT_PUBLIC_DEMO_IDENTITY_RATE_LIMIT", "10"),
            minimum=1,
            maximum=100,
        ),
        requests_per_ip_per_minute=_positive_int(
            "KNOWLEDGE_AGENT_PUBLIC_DEMO_IP_RATE_LIMIT",
            env.get("KNOWLEDGE_AGENT_PUBLIC_DEMO_IP_RATE_LIMIT", "20"),
            minimum=1,
            maximum=100,
        ),
        max_concurrent_requests=_positive_int(
            "KNOWLEDGE_AGENT_PUBLIC_DEMO_MAX_CONCURRENCY",
            env.get("KNOWLEDGE_AGENT_PUBLIC_DEMO_MAX_CONCURRENCY", "4"),
            minimum=1,
            maximum=100,
        ),
        max_body_bytes=_positive_int(
            "KNOWLEDGE_AGENT_PUBLIC_DEMO_MAX_BODY_BYTES",
            env.get("KNOWLEDGE_AGENT_PUBLIC_DEMO_MAX_BODY_BYTES", str(MAX_DEMO_BODY_BYTES)),
            minimum=1,
            maximum=MAX_DEMO_BODY_BYTES,
        ),
    )


def authenticate_demo_request(scope: Scope, config: PublicDemoConfig) -> DemoPrincipal | None:
    """Verify the short-lived, ingress-signed identity and same-origin request."""
    headers = {key.lower(): value for key, value in scope.get("headers", [])}
    origin = headers.get(b"origin", b"").decode("latin-1")
    if origin.rstrip("/") != config.expected_origin:
        return None

    cookie_header = headers.get(b"cookie", b"").decode("latin-1")
    try:
        cookie = SimpleCookie()
        cookie.load(cookie_header)
        token = cookie[DEMO_IDENTITY_COOKIE].value
    except (KeyError, TypeError, ValueError):
        return None

    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            config.identity_secret,
            algorithms=["HS256"],
            audience=config.token_audience,
            issuer=config.token_issuer,
            leeway=5,
            options={"require": ["exp", "iat", "sub", "aud", "iss", "client_ip", "origin"]},
        )
        if claims["origin"].rstrip("/") != config.expected_origin:
            return None
        issued_at = int(claims["iat"])
        expires_at = int(claims["exp"])
        if expires_at <= issued_at or expires_at - issued_at > MAX_TOKEN_LIFETIME_SECONDS:
            return None
        subject = claims["sub"]
        if not isinstance(subject, str) or not subject.strip() or len(subject) > 200:
            return None
        client_ip = str(ipaddress.ip_address(claims["client_ip"]))
    except (jwt.PyJWTError, KeyError, TypeError, ValueError, OverflowError):
        return None
    return DemoPrincipal(subject=subject, client_ip=client_ip)


class SlidingWindowLimiter:
    """Small process-local limiter for one demo worker; keys are never stored raw."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._windows: dict[str, deque[float]] = {}

    @staticmethod
    def _key(kind: str, value: str) -> str:
        return hashlib.sha256(f"{kind}:{value}".encode()).hexdigest()

    def allow(self, principal: DemoPrincipal, config: PublicDemoConfig) -> bool:
        now = time.monotonic()
        window_start = now - 60.0
        keys_and_limits = (
            (self._key("subject", principal.subject), config.requests_per_identity_per_minute),
            (self._key("ip", principal.client_ip), config.requests_per_ip_per_minute),
        )
        with self._lock:
            for key, limit in keys_and_limits:
                entries = self._windows.setdefault(key, deque())
                while entries and entries[0] <= window_start:
                    entries.popleft()
                if len(entries) >= limit:
                    return False
            for key, _ in keys_and_limits:
                self._windows[key].append(now)
            if len(self._windows) > 20_000:
                self._windows = {
                    key: entries
                    for key, entries in self._windows.items()
                    if entries and entries[-1] > window_start
                }
        return True


def public_demo_kill_switch_enabled() -> bool:
    """Any non-empty value other than an explicit false keeps demo traffic off."""
    value = os.environ.get("KNOWLEDGE_AGENT_PUBLIC_DEMO_KILL_SWITCH", "").strip().lower()
    return value not in {"", "false", "0", "no"}


async def _reply(send: Send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}, separators=(",", ":")).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        (b"cache-control", b"no-store"),
        (b"x-content-type-options", b"nosniff"),
    ]
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": headers,
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _read_body(receive: Receive, limit: int) -> tuple[bytes | None, str | None]:
    body = bytearray()
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return None, "disconnect"
        if message["type"] != "http.request":
            continue
        body.extend(message.get("body", b""))
        if len(body) > limit:
            return None, "too_large"
        if not message.get("more_body", False):
            return bytes(body), None


class PublicDemoSafetyMiddleware:
    """Apply a bounded request envelope and enforce demo-only trust controls."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._limiter = SlidingWindowLimiter()
        self._semaphore: asyncio.Semaphore | None = None
        self._semaphore_limit: int | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        config = state.get("public_demo_config")
        if config is None:
            config = load_public_demo_config()
            state["public_demo_config"] = config
        path = scope.get("path", "")
        is_demo_question = config is not None and path == "/api/ask"
        if config is not None and public_demo_kill_switch_enabled() and path.startswith("/api/"):
            await _reply(send, 503, "The public demo is temporarily unavailable.")
            return
        if config is not None and path == "/v1/mcp":
            await _reply(send, 404, "The requested endpoint is unavailable.")
            return

        if config is None:
            await self.app(scope, receive, send)
            return

        max_body = config.max_body_bytes
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                if int(declared) < 0:
                    await _reply(send, 400, "Invalid request body length.")
                    return
                if int(declared) > max_body:
                    await _reply(send, 413, "Request body exceeds the configured limit.")
                    return
            except ValueError:
                await _reply(send, 400, "Invalid request body length.")
                return

        body, error = await _read_body(receive, max_body)
        if error == "too_large":
            await _reply(send, 413, "Request body exceeds the configured limit.")
            return
        if error == "disconnect" or body is None:
            return
        consumed = False

        async def replay_receive() -> Message:
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.request", "body": b"", "more_body": False}

        if is_demo_question:
            principal = authenticate_demo_request(scope, config)
            if principal is None:
                await _reply(send, 401, "A valid authenticated demo session is required.")
                return
            if not self._limiter.allow(principal, config):
                await _reply(send, 429, "The public demo request limit was reached.")
                return
            state["public_demo_principal"] = principal
            if self._semaphore is None or self._semaphore_limit != config.max_concurrent_requests:
                self._semaphore = asyncio.Semaphore(config.max_concurrent_requests)
                self._semaphore_limit = config.max_concurrent_requests
            try:
                await asyncio.wait_for(self._semaphore.acquire(), timeout=0.1)
            except TimeoutError:
                await _reply(send, 429, "The public demo is at its concurrency limit.")
                return
            try:
                await self.app(scope, replay_receive, send)
            finally:
                self._semaphore.release()
            return

        await self.app(scope, replay_receive, send)
