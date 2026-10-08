"""Custom Trussium runtime app with fixed Knowledge Agent MCP tools.

This example must run in an environment where Trussium is installed. It is not
part of the Knowledge Agent's runtime dependencies.
"""

from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict, Field
from trussium.app import create_application
from trussium.tools import RemoteMCPTool, ToolExecutor, ToolRegistry


class SearchArguments(BaseModel):
    """Strict, bounded arguments for indexed-document search."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    query: str = Field(min_length=1, max_length=4000)
    limit: int = Field(default=5, ge=1, le=10)


class AuditArguments(BaseModel):
    """Strict, bounded arguments for deterministic link auditing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_findings: int = Field(default=100, ge=1, le=500)


class ReviewGuidanceArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    source_name: str = Field(min_length=1, max_length=128)
    source_relative_path: str = Field(min_length=1, max_length=512)
    guidance: str = Field(min_length=1, max_length=4000)
    limit: int = Field(default=5, ge=1, le=10)


endpoint = os.environ["KNOWLEDGE_AGENT_MCP_URL"]
token = os.environ["KNOWLEDGE_AGENT_TOOL_TOKEN"]
allow_local_http = os.environ.get("KNOWLEDGE_AGENT_ALLOW_LOCAL_HTTP") == "true"

search_tool = RemoteMCPTool(
    name="knowledge.search",
    endpoint_url=endpoint,
    remote_name="docs.search",
    arguments_model=SearchArguments,
    bearer_token=token,
    allow_local_http=allow_local_http,
).registered_tool()

audit_tool = RemoteMCPTool(
    name="knowledge.audit_links",
    endpoint_url=endpoint,
    remote_name="docs.audit_links",
    arguments_model=AuditArguments,
    bearer_token=token,
    allow_local_http=allow_local_http,
).registered_tool()

review_guidance_tool = RemoteMCPTool(
    name="knowledge.review_guidance",
    endpoint_url=endpoint,
    remote_name="docs.review_guidance",
    arguments_model=ReviewGuidanceArguments,
    bearer_token=token,
).registered_tool()

app = create_application(
    tool_executor=ToolExecutor(ToolRegistry((search_tool, audit_tool, review_guidance_tool))),
)
