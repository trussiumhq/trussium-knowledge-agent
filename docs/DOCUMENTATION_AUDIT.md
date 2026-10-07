# Run a bounded Markdown link audit

`docs.audit_links` checks local links in Markdown documentation and returns
deterministic findings. It is an opt-in tool for a Trussium workflow; it is not
a general-purpose crawler or repository fixer.

## Configure the audit root

Set `KNOWLEDGE_AGENT_AUDIT_ROOT` to the local Markdown repository or directory
that an operator has selected. Keep it in the service's local `.env` or secret
configuration, not in a request or committed configuration file. The MCP
endpoint also requires `KNOWLEDGE_AGENT_TOOL_TOKEN`; use a high-entropy token
and configure the same token in the trusted Trussium remote-tool adapter.

```dotenv
KNOWLEDGE_AGENT_TOOL_TOKEN=<strong-secret>
KNOWLEDGE_AGENT_AUDIT_ROOT=/srv/docs/project
```

If the root is unset or invalid, the tool is unavailable or returns a generic
service error. Do not expose this endpoint publicly without HTTPS and a
secret-management solution.

## Findings and limits

Call the fixed tool `docs.audit_links` with optional `max_findings` from 1 to
500 (default 100). Callers cannot supply a root, path, URL, or glob. Each report
contains `files_checked`, `links_checked`, `findings`, and `truncated`. Findings
include a stable `rule_id`, root-relative `source_path`, one-based `line`, the
link `target`, and a short explanation. Report paths never include the local
absolute root.

The scanner recognizes inline Markdown links and images. It ignores external
URLs and links inside fenced or inline code, and does not currently parse
reference-style links or raw HTML links. Fragment checks use Markdown heading
slugs (including duplicate-heading suffixes) and explicit HTML `id`/`name`
anchors. It never fetches remote targets or follows symlinks. The scan is
bounded to 2,000 Markdown files, 20,000 links, 5 MiB per Markdown file, 50 MiB
total source data, and 500 findings before the requested response cap is
applied. A report is advisory: review every proposed correction before
changing documentation.

## Direct endpoint check

With the service running and the root configured, send the fixed JSON-RPC call:

```bash
curl --fail-with-body http://127.0.0.1:8000/v1/mcp \
  -H "Authorization: Bearer $KNOWLEDGE_AGENT_TOOL_TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":"audit-1","method":"tools/call","params":{"name":"docs.audit_links","arguments":{"max_findings":100}}}'
```

The service returns a JSON-RPC `result` containing one JSON report. An
unconfigured token disables the endpoint; a missing audit root produces a
generic unavailable error. Neither error response reveals a local path or
exception detail.

## Trussium workflow boundary

Register the endpoint as one fixed `RemoteMCPTool` in a custom Trussium
application, with the `docs.audit_links` name and a strict argument model that
only permits `max_findings`. Keep caller input limited to audit options. Do not
register broad filesystem, shell, arbitrary URL, or repository-write tools as
part of this audit. The packaged Trussium runtime stays tool-free unless an
application explicitly composes tools. See
[`adr/0004-cross-process-tool-boundary.md`](adr/0004-cross-process-tool-boundary.md)
for the security boundary.

## Recovery and troubleshooting

- `401` / endpoint unavailable: check the configured bearer token and confirm
  the client sends it over a protected connection.
- Audit service unavailable: confirm `KNOWLEDGE_AGENT_AUDIT_ROOT` is set to an
  existing readable directory and restart the service after changing config.
- Audit failed: inspect the service's generic audit failure event, then check
  file permissions, UTF-8 encoding, symlinks, and the documented size/count
  limits. Do not log document contents or secrets to diagnose the failure.
- Unexpected findings: validate the target spelling and heading slug; the
  current scanner intentionally does not resolve external URLs, reference
  definitions, or raw HTML links.

This tool never modifies the source. To undo its operational setup, unset
`KNOWLEDGE_AGENT_AUDIT_ROOT` and remove the audit tool from the custom runtime's
explicit tool registry.
