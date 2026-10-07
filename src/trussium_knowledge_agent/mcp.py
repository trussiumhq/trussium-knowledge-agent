"""Authenticated, read-only MCP tools for the indexed Knowledge Agent corpus."""

from __future__ import annotations

import hmac
import os
import sys
from typing import Any

import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from trussium_knowledge_agent.auditing import MAX_AUDIT_FINDINGS, audit_markdown_links
from trussium_knowledge_agent.embeddings import TrussiumEmbeddingsClient
from trussium_knowledge_agent.retrieval import search_index

router = APIRouter()
_MAX_REQUEST_BYTES = 1_048_576


class MCPRequest(BaseModel):
    """Minimal strict JSON-RPC envelope supported by this app-owned tool server."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    jsonrpc: str = Field(pattern="^2\\.0$")
    id: int | str | None
    method: str = Field(min_length=1, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)


class SearchArguments(BaseModel):
    """Bounded read-only search arguments for docs.search."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    query: str = Field(min_length=1, max_length=4000)
    limit: int = Field(default=5, ge=1, le=10)


class AuditLinksArguments(BaseModel):
    """Bounded read-only local Markdown audit arguments."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_findings: int = Field(default=100, ge=1, le=MAX_AUDIT_FINDINGS)


def _response(request_id: int | str | None, *, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(
    request_id: int | str | None,
    code: int,
    message: str,
    *,
    status_code: int = 200,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}},
    )


def _authorized(request: Request) -> bool:
    token = os.environ.get("KNOWLEDGE_AGENT_TOOL_TOKEN", "")
    authorization = request.headers.get("authorization", "")
    expected = f"Bearer {token}" if token else ""
    return bool(token) and hmac.compare_digest(
        authorization.encode("utf-8"), expected.encode("utf-8")
    )


async def _read_bounded_body(request: Request) -> bytes | None:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > _MAX_REQUEST_BYTES:
                return None
        except ValueError:
            return None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > _MAX_REQUEST_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/v1/mcp", tags=["tools"])
async def mcp_tools(request: Request) -> JSONResponse:
    """Execute fixed read-only documentation tools after bearer authentication."""
    if not _authorized(request):
        return JSONResponse(
            status_code=503 if not os.environ.get("KNOWLEDGE_AGENT_TOOL_TOKEN") else 401,
            content={"detail": "The MCP tool service is unavailable."},
            headers={"WWW-Authenticate": "Bearer"},
        )

    body = await _read_bounded_body(request)
    if body is None:
        return _error(None, -32600, "Request is invalid or too large.", status_code=413)
    try:
        message = MCPRequest.model_validate_json(body)
    except ValidationError:
        return _error(None, -32600, "Request is invalid.")

    if message.method != "tools/call":
        return _error(message.id, -32601, "Method not supported.")
    name = message.params.get("name")
    raw_arguments = message.params.get("arguments", {})
    if not isinstance(raw_arguments, dict) or set(message.params) != {"name", "arguments"}:
        return _error(message.id, -32602, "Tool parameters are invalid.")

    if name == "docs.audit_links":
        try:
            audit_arguments = AuditLinksArguments.model_validate(raw_arguments)
        except ValidationError:
            return _error(message.id, -32602, "Tool arguments are invalid.")
        source_root = os.environ.get("KNOWLEDGE_AGENT_AUDIT_ROOT", "").strip()
        if not source_root:
            return _error(message.id, -32000, "The documentation audit service is unavailable.")
        try:
            report = audit_markdown_links(source_root)
            output = report.as_dict()
            findings = output["findings"]
            assert isinstance(findings, list)
            output["findings"] = findings[: audit_arguments.max_findings]
            output["truncated"] = report.truncated or len(findings) > audit_arguments.max_findings
        except (OSError, RuntimeError, TypeError, ValueError):
            print("MCP documentation link audit failed.", file=sys.stderr)
            return _error(message.id, -32000, "The documentation link audit failed.")
        return JSONResponse(
            content=_response(
                message.id,
                result={"isError": False, "content": [{"type": "json", "json": output}]},
            )
        )

    if name != "docs.search":
        return _error(message.id, -32004, "Tool not found.")
    try:
        search_arguments = SearchArguments.model_validate(raw_arguments)
    except ValidationError:
        return _error(message.id, -32602, "Tool arguments are invalid.")

    database_url = os.environ.get("DATABASE_URL", "").strip()
    embedding_model = os.environ.get("TRUSSIUM_EMBEDDING_MODEL", "").strip()
    if not database_url or not embedding_model:
        return _error(message.id, -32000, "The documentation search service is unavailable.")
    base_url = os.environ.get("TRUSSIUM_URL", "http://127.0.0.1:9000")
    api_key = os.environ.get("TRUSSIUM_API_KEY") or None
    try:
        timeout_seconds = float(os.environ.get("TRUSSIUM_TIMEOUT_SECONDS", "30"))
        if not 0 < timeout_seconds <= 120:
            raise ValueError("unsupported timeout")
        with TrussiumEmbeddingsClient(
            base_url, api_key=api_key, timeout_seconds=timeout_seconds
        ) as embeddings_client:
            passages = search_index(
                search_arguments.query,
                database_url,
                embeddings_client,
                embedding_model,
                top_k=search_arguments.limit,
            )
        output = {
            "query": search_arguments.query,
            "matches": [
                {
                    "display_name": passage.display_name,
                    "revision": passage.revision,
                    "relative_path": passage.relative_path,
                    "heading_path": list(passage.heading_path),
                    "heading_anchor": passage.heading_anchor,
                    "content": passage.content,
                    "score": passage.score,
                }
                for passage in passages
            ],
        }
    except (psycopg.Error, RuntimeError, TypeError, ValueError):
        print("MCP documentation search failed.", file=sys.stderr)
        return _error(message.id, -32000, "The documentation search failed.")

    return JSONResponse(
        content=_response(
            message.id,
            result={"isError": False, "content": [{"type": "json", "json": output}]},
        )
    )
