"""Authenticated, security-only GitHub App webhook endpoint."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from typing import Any

import psycopg
from fastapi import APIRouter, HTTPException, Request, Response

from trussium_knowledge_agent.private_github import process_github_webhook

router = APIRouter()
MAX_WEBHOOK_BYTES = 1024 * 1024


@router.post("/webhooks/github", status_code=202, tags=["webhooks"])
async def github_webhook(request: Request) -> Response:
    """Validate a signed bounded webhook and apply restrictive lifecycle state."""
    secret = os.environ.get("KNOWLEDGE_AGENT_GITHUB_WEBHOOK_SECRET", "")
    database_url = os.environ.get("DATABASE_URL", "").strip()
    try:
        installation_id = int(os.environ.get("KNOWLEDGE_AGENT_GITHUB_INSTALLATION_ID", ""))
    except ValueError:
        installation_id = 0
    if len(secret.encode("utf-8")) < 32 or not database_url or installation_id <= 0:
        raise HTTPException(status_code=503, detail="GitHub webhook handling is unavailable.")
    body_buffer = bytearray()
    async for block in request.stream():
        body_buffer.extend(block)
        if len(body_buffer) > MAX_WEBHOOK_BYTES:
            raise HTTPException(status_code=413, detail="Webhook payload exceeds the size limit.")
    body = bytes(body_buffer)
    signature = request.headers.get("x-hub-signature-256", "")
    if len(signature) != 71 or not signature.startswith("sha256="):
        raise HTTPException(status_code=401, detail="Webhook signature is invalid.")
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=401, detail="Webhook signature is invalid.")
    delivery_id = request.headers.get("x-github-delivery", "")
    event_name = request.headers.get("x-github-event", "")
    if (
        not delivery_id
        or len(delivery_id) > 100
        or any(ord(character) < 33 for character in delivery_id)
        or event_name not in {"installation", "installation_repositories"}
    ):
        raise HTTPException(status_code=400, detail="Webhook headers are invalid.")
    try:
        payload: Any = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Webhook payload is invalid.") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Webhook payload is invalid.")
    try:
        process_github_webhook(
            database_url,
            delivery_id,
            event_name,
            payload,
            expected_installation_id=installation_id,
        )
    except (psycopg.Error, OSError, RuntimeError, ValueError):
        print("GitHub security webhook processing failed.", file=sys.stderr)
        raise HTTPException(
            status_code=503, detail="GitHub webhook processing is temporarily unavailable."
        ) from None
    return Response(status_code=202)
