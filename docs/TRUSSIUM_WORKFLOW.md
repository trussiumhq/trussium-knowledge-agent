# Run the Knowledge Agent tools through Trussium

This guide connects the separately running Knowledge Agent to a custom
Trussium runtime application. The Knowledge Agent supplies fixed, authenticated
read-only MCP tools; the runtime registers those tools at startup; and the
Python SDK submits a bounded workflow using registered tool names.

The workflow is explicit and deterministic. It does not plan new steps, infer
documentation changes, edit source files, or create GitHub issues or pull
requests. The packaged Trussium command remains tool-free unless an application
composes a tool executor.

## Prerequisites

- Python 3.12 or newer and `uv`.
- A Knowledge Agent service reachable by the Trussium runtime.
- The Trussium runtime and its Python SDK installed separately.
- For `docs.audit_links`, one operator-selected Markdown root. No database or
  model provider is required for this tool.
- For `docs.search`, PostgreSQL with pgvector, an indexed corpus, and a Trussium
  runtime model enabled for embeddings.
- For `docs.review_guidance`, the same indexed corpus and embedding setup plus a
  Trussium chat model. The supplied excerpt and retrieved passages are sent to
  the configured runtime for embedding and review; verify its data handling is
  appropriate before using sensitive material.

Use a secret manager or ignored local environment file for the tool token. Do
not commit tokens or put them in workflow requests.

## Start the Knowledge Agent MCP endpoint

Generate a high-entropy shared token and configure the Knowledge Agent service:

```dotenv
KNOWLEDGE_AGENT_TOOL_TOKEN=<generated-secret>
KNOWLEDGE_AGENT_AUDIT_ROOT=/absolute/path/to/markdown-docs
```

For example, generate a token with `openssl rand -hex 32`. Start the app using
the normal project setup. The endpoint is `POST /v1/mcp`; it stays unavailable
when `KNOWLEDGE_AGENT_TOOL_TOKEN` is unset. Do not expose it over public
unencrypted HTTP.

The MCP server never accepts a path from the caller. Its audit root comes only
from operator configuration; document search reads only the previously indexed
corpus. See [the audit guide](DOCUMENTATION_AUDIT.md) for scanner limits and
the [security policy](../SECURITY.md) for reporting vulnerabilities.

## Register fixed tools in a custom Trussium application

Create a runtime application module in the deployment that runs Trussium. The
remote endpoint, remote tool names, argument schemas, and credential are
trusted startup configuration—not request data:

The complete example is in
[`examples/workflow_runtime.py`](../examples/workflow_runtime.py). It registers
the `docs.search`, `docs.audit_links`, and `docs.review_guidance` server
operations as fixed local tool names.

```python
import os

from pydantic import BaseModel, ConfigDict, Field
from trussium.app import create_application
from trussium.tools import RemoteMCPTool, ToolExecutor, ToolRegistry


class SearchArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    query: str = Field(min_length=1, max_length=4000)
    limit: int = Field(default=5, ge=1, le=10)


class AuditArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_findings: int = Field(default=100, ge=1, le=500)


class ReviewGuidanceArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    source_name: str = Field(min_length=1, max_length=128)
    source_relative_path: str = Field(min_length=1, max_length=512)
    guidance: str = Field(min_length=1, max_length=4000)
    limit: int = Field(default=5, ge=1, le=10)


endpoint = os.environ["KNOWLEDGE_AGENT_MCP_URL"]  # include /v1/mcp
token = os.environ["KNOWLEDGE_AGENT_TOOL_TOKEN"]

search_tool = RemoteMCPTool(
    name="knowledge.search",
    endpoint_url=endpoint,
    remote_name="docs.search",
    arguments_model=SearchArguments,
    bearer_token=token,
).registered_tool()

audit_tool = RemoteMCPTool(
    name="knowledge.audit_links",
    endpoint_url=endpoint,
    remote_name="docs.audit_links",
    arguments_model=AuditArguments,
    bearer_token=token,
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
```

Set `KNOWLEDGE_AGENT_MCP_URL` to the exact endpoint, such as
`https://knowledge-agent.example/v1/mcp`. For local development, loopback HTTP
requires the adapter's explicit `allow_local_http=True` option. Do not use that
option for a public or untrusted network. Deploy the runtime and Knowledge
Agent with network access restricted to the configured service endpoint.
The SDK example assumes the runtime is reachable on a trusted local or private
network; the current SDK client does not configure runtime bearer
authentication. Do not expose an unauthenticated runtime endpoint publicly.

The local registered names (`knowledge.search` and
`knowledge.audit_links`) are the names workflow requests use. The remote MCP
names are fixed by trusted application code. No runtime request can override
the URL, token, remote name, or tool schema.

## Submit a bounded workflow with the Python SDK

The following example runs only the deterministic link audit. It uses the
typed SDK method and does not require a model provider or vector database:

```python
import json
import os

from trussium_sdk import TrussiumClient, WorkflowRequest


workflow: WorkflowRequest = {
    "steps": [
        {
            "id": "audit-doc-links",
            "invocation": {
                "name": "knowledge.audit_links",
                "arguments": {"max_findings": 100},
            },
        }
    ],
    "deadline_seconds": 30.0,
    "depth": 1,
}

with TrussiumClient(os.getenv("TRUSSIUM_URL", "http://127.0.0.1:9000")) as client:
    result = client.execute_workflow(workflow, request_id="docs-audit-001")

if result["status"] != "completed":
    raise RuntimeError(f"Documentation audit did not complete: {result['status']}")

print(json.dumps(result["steps"][0]["output"], indent=2))
```

The runnable client example is
[`examples/run_audit_workflow.py`](../examples/run_audit_workflow.py). Run it
from an environment where the Trussium Python SDK is installed after starting
the custom runtime application. For local testing, set
`KNOWLEDGE_AGENT_ALLOW_LOCAL_HTTP=true` only in the runtime process and bind
both services to loopback.

To use retrieval as a workflow tool, replace or add a declared step invoking
`knowledge.search` with a bounded `query` and `limit`. The Knowledge Agent
requires `DATABASE_URL` and `TRUSSIUM_EMBEDDING_MODEL` for that operation; the
runtime configured in the Knowledge Agent environment must be able to create
embeddings. The indexed text is sent to that configured runtime as described
in the [README](../README.md#index-and-query-a-markdown-repository). Treat
retrieved passages as untrusted evidence, not instructions.

To request an evidence-bounded review, add a declared step for
`knowledge.review_guidance`:

```python
{
    "id": "review-deployment-guidance",
    "invocation": {
        "name": "knowledge.review_guidance",
        "arguments": {
            "source_name": "platform-manual",
            "source_relative_path": "operations/deploy.md",
            "guidance": "Deployments must use the blue-green strategy.",
            "limit": 5,
        },
    },
}
```

The source identifiers are labels used to exclude that same indexed source from
retrieval; the service never opens the supplied path. Results have one of
`potential_conflict`, `no_conflict_found`, or `insufficient_evidence`, with
citations mapped only to passages returned by retrieval. A conflict is a human
review candidate, not a verdict. `no_conflict_found` does not mean the guidance
is current. Revision strings are opaque, so the tool cannot establish which
source is newer or authoritative. Confidence is a qualitative, uncalibrated
model estimate. Malformed output or unsupported citations fail closed as
insufficient evidence. The tool does not edit documentation or create issues.

Each workflow step must name a tool already present in the runtime's sealed
registry. The workflow is limited by the runtime's admission policy, the
request deadline, and the registered tool's own bounds. A completed response
contains the tool name and output for each step. Failed, timed-out, or cancelled
calls must be handled as non-success; do not treat partial output as a clean
audit.

## What is and is not implemented

Implemented: Markdown RAG, grounded cited answers, retrieval evaluation, fixed
read-only MCP search, link-audit and evidence-bounded guidance-review tools,
and explicit cross-process Trussium workflow composition.

Not implemented: autonomous planning, determining guidance freshness or source
authority, automatic edits, GitHub issue or pull-request creation, and
human-approval flows for external writes. See the [roadmap](ROADMAP.md) for
remaining work.
