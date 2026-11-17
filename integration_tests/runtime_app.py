"""Minimal Trussium app used only by the cross-process integration suite."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from trussium.app import create_application
from trussium.tools import RegisteredTool, RemoteMCPTool, ToolExecutor, ToolRegistry


class AuditArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_findings: int = Field(default=20, ge=1, le=50)


class WaitArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    marker_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    wait_seconds: float = Field(gt=0, le=10)


async def wait_tool(arguments: BaseModel) -> dict[str, Any]:
    parsed = WaitArguments.model_validate(arguments.model_dump())
    marker_directory = Path(os.environ["WORKFLOW_TEST_MARKER_DIRECTORY"])
    marker_directory.mkdir(parents=True, exist_ok=True)
    marker = marker_directory / parsed.marker_id
    marker.with_suffix(".started").write_text("started", encoding="utf-8")
    try:
        await asyncio.sleep(parsed.wait_seconds)
    except asyncio.CancelledError:
        marker.with_suffix(".cancelled").write_text("cancelled", encoding="utf-8")
        raise
    marker.with_suffix(".finished").write_text("finished", encoding="utf-8")
    return {"status": "finished"}


endpoint = os.environ["KNOWLEDGE_AGENT_MCP_URL"]
token = os.environ["KNOWLEDGE_AGENT_TOOL_TOKEN"]
allow_local_http = os.environ.get("KNOWLEDGE_AGENT_ALLOW_LOCAL_HTTP") == "true"

audit_tool = RemoteMCPTool(
    name="knowledge.audit_links",
    endpoint_url=endpoint,
    remote_name="docs.audit_links",
    arguments_model=AuditArguments,
    bearer_token=token,
    allow_local_http=allow_local_http,
).registered_tool()

test_tool = RegisteredTool(
    name="test.wait",
    arguments_model=WaitArguments,
    handler=wait_tool,
)

app = create_application(tool_executor=ToolExecutor(ToolRegistry((audit_tool, test_tool))))
