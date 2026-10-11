# ADR 0005: GitHub Markdown source connector contract

## Status

Accepted. The explicit public-repository snapshot slice is implemented;
private-repository authentication and automatic synchronization are deferred.

## Context

The Knowledge Agent indexes explicitly selected local Markdown trees and
supports a manually refreshed snapshot of a public GitHub repository. The
local indexer bounds file size, rejects invalid UTF-8, avoids symlink traversal,
and replaces a source's chunks transactionally. A private repository connector
would add credentials, authorization-loss, and reconnection semantics that are
not needed for the anonymous public-only slice and must remain a separate,
tested design.

The implementation is public Markdown only. It does not authorize arbitrary
repository formats, issue or pull request access, write operations, private
repositories, GitHub Enterprise/custom hosts, webhooks, or automatic content
ingestion.

## Decision

### Source identity and snapshot

- Identify a source using the GitHub host and immutable numeric repository ID;
  derive the Knowledge Agent's stable source ID from that identity. Do not use
  owner/name, branch, or credential as the durable identity. A rename must not
  silently create a second source. One source per repository is supported; a
  selected ref refresh replaces that source's prior snapshot.
- Every completed ingestion is a snapshot pinned to a full Git commit SHA. A
  branch or tag is only a refresh target; it is never the revision recorded for
  indexed content.
- Read repository metadata and the selected ref through `api.github.com`,
  resolve the full commit SHA and tree SHA, and download regular Markdown blobs
  from `raw.githubusercontent.com`. Use fixed HTTPS hosts, a bounded timeout,
  disabled redirect following, and bounded responses.
- Enforce a 4 MiB tree-response limit, 20,000 tree entries, 100 Markdown files,
  4 KiB per path, 5 MiB per file, 20 MiB aggregate content, 10,000 parsed
  chunks, a 10-second request timeout, and a 120-second total download
  deadline. Reject truncated responses, traversal and
  unsafe Markdown modes; skip submodules and non-Markdown content. Decode UTF-8
  and parse/chunk the complete candidate before touching stored data.
- Remote content is untrusted evidence, not instructions or authorization. Do
  not execute repository code, install dependencies from it, follow external
  links, or log document contents or credentials.
- A complete, successfully embedded snapshot is persisted atomically with its
  numeric repository identity and pinned commit revision. A failed fetch,
  parse, validation, or embedding operation preserves the last successful
  index. A successful empty snapshot is authoritative and removes the prior
  Markdown chunks for that repository.

### Authentication and access

- Public repository reads require no GitHub credential and are implemented.
  The CLI accepts only `owner/repository` plus a caller-selected ref; callers
  cannot supply arbitrary URLs, hosts, or redirect targets.
- Private repositories are not supported in this slice. Before adding them,
  prefer a GitHub App installation token rather than a personal access token.
  Request only repository `Contents: read` and required `Metadata: read`, grant
  access only to explicitly selected repositories, and add no write permission,
  organization permission, Actions, administration, issues, pull requests, or
  webhook-management permission.
- Future tokens must be short-lived and kept in a secret store. Persist only the
  minimum installation/repository identifiers needed to refresh; never persist
  a private key or installation token in source metadata, logs, or the
  Knowledge Agent database.
- If private access is added, the connector must verify that the requested
  repository belongs to the authorized installation before reading content.
  GitHub Enterprise Server and custom hosts remain deferred until their
  endpoint validation and credential boundaries are designed.

GitHub documents that installations can be limited to selected repositories
and that apps receive only the permissions requested at installation. The
official permission matrix identifies repository metadata and contents as
separate read permissions; the implementation should request only those
needed by its chosen endpoints.

### Refresh and failure behavior

- Refresh is explicit and operator/user-triggered only. There are no webhooks,
  polling schedules, or automatic background synchronization.
- Resolve the explicitly selected ref to a commit SHA at the start of refresh. Build
  and validate a complete candidate snapshot before changing the active
  revision or indexed chunks. Commit the new revision, source metadata, and
  chunks atomically only after the whole snapshot succeeds.
- A timeout, rate limit, GitHub server error, incomplete response, parser
  failure, or resource-limit failure must preserve the last successful
  revision and chunks. Never interpret a failed or partial fetch as an empty
  repository.
- A successful complete snapshot that no longer contains a previously
  indexed Markdown file removes that file's chunks as part of the same atomic
  replacement. Indexing remains explicitly Markdown-only.
- The source revision and `indexed_at` record the last successful snapshot.
  Failures are reported to the invoking CLI and do not overwrite this success
  state; failed-attempt status is not persisted in this slice.

### Revocation, disconnection, and deletion

- A transient network or rate-limit failure does not change authorization or
  retrieval visibility; keep serving the last successful snapshot and report
  the failed refresh.
- Private authorization loss cannot occur in the public-only slice and is not
  automatically detected. If private access is added, confirmed authorization
  loss must immediately mark the source access-lost and exclude its chunks.
- Keep access-lost chunks quarantined (not retrievable) for at most 30 days to
  permit an authorized reconnection without re-fetching data. An operator may
  purge sooner. Reauthorization must revalidate repository identity before
  restoring visibility. Purge the quarantined data when the retention period
  expires.
- Explicit removal uses the stable full source ID, works without a GitHub
  request, and reports whether a source existed. It records a durable lifecycle
  deletion event outside PostgreSQL backups before deleting metadata and
  chunks. There is no credential association in the public-only slice.
- Purging the live database does not erase historical database backups; backup
  retention and restore procedures must be documented, and a restored backup
  must not reactivate a connector whose authorization was revoked or whose
  deletion was recorded later.
- The database records source kind, stable external identity, pinned revision,
  and lifecycle state. Retrieval filters inactive sources in the same SQL
  query that selects chunks. Deletion and access-loss events are reconciled
  from the external lifecycle journal before restored backups serve traffic;
  see [ADR 0006](0006-restore-safe-source-lifecycle.md).

## Consequences

- GitHub becomes a candidate input source, not a runtime dependency for local
  indexing or query operations.
- The public adapter supplies a complete, bounded snapshot and metadata for a
  pinned commit, then reuses the shared chunking, embedding, and persistence
  pipeline.
- Private access-loss events require operators to preserve the external
  lifecycle journal and run restore reconciliation before cutover.
- Refresh and deletion behavior can be tested deterministically without live
  GitHub credentials by injecting repository snapshots and authorization
  outcomes.
- This ADR does not expand the public Trussium runtime's provider, tool, or
  security boundaries. It applies only to the separate Knowledge Agent
  reference application.

## Alternatives considered

- **Personal access token:** rejected as the default because it is tied to a
  user credential and can have broader or longer-lived access than a narrowly
  installed GitHub App.
- **Write-capable GitHub App:** rejected because indexing needs read access
  only; repository writes and workflow automation are outside scope.
- **Webhook-driven refresh in the first version:** deferred because webhook
  authentication, replay protection, idempotency, delivery retry, and event
  ordering need their own tested contract.
- **Keep serving the last indexed content after confirmed access revocation:**
  rejected because private repository content would remain queryable after
  source authorization was removed.
- **Immediately delete on any failed refresh:** rejected because transient
  network, API, and rate-limit failures must not erase a valid corpus.

## References

- [GitHub App installation repository
  selection](https://docs.github.com/en/apps/using-github-apps/installing-your-own-github-app)
- [Permissions required for GitHub
  Apps](https://docs.github.com/en/rest/authentication/permissions-required-for-github-apps)
