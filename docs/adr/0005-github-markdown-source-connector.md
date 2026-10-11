# ADR 0005: GitHub Markdown source connector contract

## Status

Accepted as the design boundary for a future connector. No GitHub connector is
implemented by this ADR.

## Context

The Knowledge Agent currently indexes an explicitly selected local Markdown
tree. Its indexer bounds file size, rejects invalid UTF-8, avoids symlink
traversal, and replaces a source's chunks transactionally. A hosted repository
connector would add remote credentials, refresh lifecycle, authorization-loss,
and deletion semantics that the local source model does not currently
represent. The roadmap therefore defers connectors until those boundaries are
documented and testable.

This decision scopes the first connector candidate to Markdown in GitHub
repositories. It does not authorize arbitrary repository formats, issue or pull
request access, write operations, webhooks, or automatic content ingestion.

## Decision

### Source identity and snapshot

- Identify a connector by the GitHub host, immutable repository ID, and the
  Knowledge Agent's own stable connector ID. Do not use a clone path, branch
  name, or credential as the durable identity. A rename must not silently
  create a second source.
- Every completed ingestion is a snapshot pinned to a full Git commit SHA. A
  branch or tag is only a refresh target; it is never the revision recorded for
  indexed content.
- The adapter must produce a bounded snapshot for the existing Markdown
  discovery, parsing, chunking, and storage pipeline. It must enforce file,
  total-byte, path, UTF-8, and content-size limits before replacing data; reject
  traversal and unsafe filesystem objects; and exclude submodule contents.
- Remote content is untrusted evidence, not instructions or authorization. Do
  not execute repository code, install dependencies from it, follow external
  links, or log document contents or credentials.
- The transport is intentionally not selected here. A later implementation
  must demonstrate equivalent bounds for its chosen GitHub API or Git transport
  and must not weaken the existing local ingestion guarantees.

### Authentication and access

- Public repositories may be read without credentials. Never request broader
  permissions merely to simplify public-repository ingestion.
- For private repositories, prefer a GitHub App installation token rather than
  a personal access token. Request only repository `Contents: read` and the
  required `Metadata: read`; grant access only to repositories explicitly
  selected by the user. No write permission, organization permission, Actions,
  administration, issues, pull requests, or webhook-management permission is
  in scope.
- Tokens must be short-lived and kept in a secret store. Persist only the
  minimum installation/repository identifiers needed to refresh; never persist
  a private key or installation token in source metadata, logs, or the
  Knowledge Agent database.
- The connector must verify that the requested repository belongs to the
  authorized installation before reading content. A caller cannot supply an
  arbitrary URL, API host, or redirect target. GitHub Enterprise Server and
  custom hosts are deferred until their endpoint validation and credential
  boundaries are designed.

GitHub documents that installations can be limited to selected repositories
and that apps receive only the permissions requested at installation. The
official permission matrix identifies repository metadata and contents as
separate read permissions; the implementation should request only those
needed by its chosen endpoints.

### Refresh and failure behavior

- The initial connector supports explicit, operator/user-triggered refresh
  only. Webhooks, polling schedules, and automatic background synchronization
  require a separate idempotency, replay, rate-limit, and operational design.
- Resolve the configured branch to a commit SHA at the start of refresh. Build
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
- Store the last attempted/successful refresh timestamps, pinned commit SHA,
  and bounded operational status. Do not claim freshness when refresh failed.

### Revocation, disconnection, and deletion

- A transient network or rate-limit failure does not change authorization or
  retrieval visibility; keep serving the last successful snapshot and report
  the failed refresh.
- If GitHub confirms that the installation or repository authorization was
  removed, immediately mark the connector access-lost and exclude its chunks
  from retrieval. Do not expose cached private content while the source is no
  longer authorized.
- Keep access-lost chunks quarantined (not retrievable) for at most 30 days to
  permit an authorized reconnection without re-fetching data. An operator may
  purge sooner. Reauthorization must revalidate repository identity before
  restoring visibility. Purge the quarantined data when the retention period
  expires.
- An explicit user removal immediately stops refresh and transactionally
  deletes the connector record, source metadata, and indexed chunks. Any
  credential association is revoked/removed. The deletion operation must be
  idempotent and report whether a source existed.
- Purging the live database does not erase historical database backups; backup
  retention and restore procedures must be documented, and a restored backup
  must not reactivate a connector whose authorization was revoked or whose
  deletion was recorded later.
- Model connector lifecycle and retrieval eligibility explicitly in the
  database before implementation. Existing `knowledge_sources` and retrieval
  queries do not currently encode active, access-lost, quarantined, or deleted
  states. Retrieval must filter inactive sources in the same query that
  selects chunks.

## Consequences

- GitHub becomes a candidate input source, not a runtime dependency for local
  indexing or query operations.
- The adapter can reuse the current Markdown pipeline only after it supplies a
  complete, bounded snapshot and metadata for a pinned commit.
- The access-lost quarantine policy requires lifecycle state, retrieval
  filtering, expiry/purge behavior, and backup-aware deletion tests; it cannot
  be safely represented by the current source schema alone.
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
