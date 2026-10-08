# ADR 0004: Cross-process tool boundary

## Status

Implemented and validated for the first read-only workflow integration.

## Context

The Knowledge Agent is a separately deployed reference application. Trussium's
workflow endpoint executes only tools registered by its application, so the
Knowledge Agent needs a narrow protocol boundary for its own read-only tools.
The service indexes user-selected Markdown and treats indexed passages as
untrusted data.

## Decision

- Expose application-owned fixed tools at the authenticated `POST /v1/mcp`
  endpoint: `docs.search` for indexed evidence and `docs.audit_links` for a
  deterministic local Markdown link check.
- Keep the name and Pydantic argument schema fixed in application code. Reject
  all unknown tool names, extra arguments, and caller-provided destinations.
- Require a separately configured bearer token; when the token is absent, the
  endpoint is unavailable. Use a high-entropy secret shared only with an
  explicitly configured Trussium runtime adapter.
- Restrict the tool to retrieval from the already indexed corpus, with bounded
  query length, result count, request bytes, and runtime timeout. Return source
  metadata alongside evidence.
- Configure the audit root only through operator environment configuration.
  Bound file, link, per-file byte, and finding counts; reject symlink traversal,
  ignore external URLs, and never write to the source tree.
- Treat all queries and retrieved passage text as data, never authorization or
  instructions. Keep this endpoint read-only; repository writes require a
  separate human-approval design.
- Return stable JSON-RPC errors and never log query text, passages, tokens, or
  remote exception details.

## Consequences

- A custom Trussium application registers fixed remote MCP adapters for the
  Knowledge Agent without enabling arbitrary tools in the packaged runtime.
- The endpoint is disabled by default and must not be exposed without HTTPS and
  a secret manager in deployed environments.
- The runtime adapter, typed Python SDK workflow method, authenticated
  Knowledge Agent tools, and first end-to-end audit workflow are implemented
  and validated. The integration does not add automatic source edits, issue
  creation, or general-purpose agent execution. See the
  [`Trussium workflow guide`](../TRUSSIUM_WORKFLOW.md), runtime
  [ADR 0045](https://github.com/trussiumhq/trussium/blob/main/docs/adr/0045-cross-process-agent-tool-boundary.md),
  and the published [Agent Workflows guide](https://trussiumhq.github.io/agent-workflows/).

## Alternatives considered

- **Accept arbitrary HTTP destinations in tool arguments:** rejected because
  it would permit SSRF and credential disclosure.
- **Enable dynamic remote tool discovery:** rejected because the active tool
  allowlist must be reviewable in trusted application configuration.
- **Allow issue creation or repository writes:** deferred until a separate
  human-approval and credential-scope contract is implemented.
