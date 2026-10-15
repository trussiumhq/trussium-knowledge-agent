# ADR 0007: Private GitHub App access boundary

## Status

Accepted design; implementation is deferred. Private GitHub ingestion remains
unavailable. This ADR records security and lifecycle requirements for a
separately tracked implementation; it does not add credentials, an app, a
webhook endpoint, or private-repository behavior.

## Context

The current GitHub connector indexes explicitly selected public Markdown
repositories without credentials. Private sources require a durable link
between a selected repository and its authorization, plus safe behavior when
repository access is removed, an installation is suspended or uninstalled, a
webhook is delayed, or the application is restored from an older database
backup. A stale private snapshot must not remain searchable after the
application learns authorization was lost.

The existing source lifecycle and external journal provide restrictive
`access_lost` and deletion records. They do not implement GitHub App
authentication, authorization reconciliation, private snapshot fetching, or a
safe reactivation flow.

## Decision

### Installation and authorization

- Use a GitHub App installed by the repository owner, with the installer
  selecting only repositories intended for indexing. Do not default to all
  repositories in an organization or account.
- Request repository `Contents: read` and only the metadata access required by
  the selected GitHub API endpoints. Add no write, administration, Actions,
  issue, pull-request, organization, or webhook-management permission. The
  webhook secret is configured by the operator; it does not require granting
  webhook-management permission to the app.
- Mint short-lived GitHub App installation tokens on the self-hosted service
  side. Every token request must explicitly scope `repository_ids` to the
  single repository being refreshed and request only the required permission
  subset. Never omit the repository selector: GitHub otherwise grants the
  token access to every repository available to the installation. Treat tokens
  as bearer secrets, keep them in memory only, never log them, and discard
  them after the bounded refresh. GitHub installation tokens expire after one
  hour; do not persist them or use them as durable source identity.
- The GitHub App private key remains operator-managed deployment secret
  material. Keep it in the operator's secret manager or equivalent protected
  host configuration, not in PostgreSQL, source metadata, backup manifests,
  repository content, or logs. The Knowledge Agent must not send this key or
  an installation token to a public Trussium service.
- Bind each source to the GitHub host, immutable numeric repository ID, and
  installation/account identity needed to verify authorization. Store only
  identifiers and display metadata required for provenance and reconciliation;
  never store a token, private key, or webhook secret.

### Private snapshot transport

- Keep authenticated transport confined to HTTPS `api.github.com`. Do not
  send an Authorization header to `raw.githubusercontent.com` or another
  content host, and do not follow redirects with authenticated requests.
- Fetch the tree and Markdown blob objects through GitHub's authenticated Git
  API using the least privilege needed (`Contents: read`). Decode and validate
  blob content within the existing per-file, aggregate, path, tree, timeout,
  and chunk bounds. Reject truncated trees, unsafe paths/modes, invalid UTF-8,
  and oversized or incomplete results before changing indexed state.
- Resolve an explicitly chosen branch, tag, or commit to a full commit SHA.
  Parse and embed the entire candidate snapshot before committing source
  metadata and chunks atomically. A failed or partial refresh preserves the
  last successful snapshot, but authorization loss is handled separately and
  must make that snapshot non-retrievable.
- Private access applies only to explicitly selected Markdown files in one
  repository snapshot. It does not add issue, pull-request, wiki, release,
  arbitrary file-format, GitHub Enterprise, or custom-host ingestion.

### Authorization changes and visibility

- Implement a security-only webhook receiver for installation suspension,
  uninstall, and repository-selection changes. It must not trigger content
  synchronization. Verify `X-Hub-Signature-256` with HMAC-SHA256 using a
  constant-time comparison before processing any payload. Enforce HTTPS and
  idempotency using `X-GitHub-Delivery`; tolerate retries and out-of-order
  delivery.
- Treat a signed event as a prompt to reconcile the installation's current
  selected repository set, not as a complete delta. GitHub can send an empty
  removed-repository list when an installation changes from all repositories
  to selected repositories. Reconciliation must not rely only on a non-empty
  `repositories_removed` array.
- On confirmed loss of repository authorization, installation suspension, or
  uninstall, first mark the source `access_lost` and persist the lifecycle
  restriction in the external journal. Retrieval must exclude the source in
  the same database query that selects chunks. If journaling fails, keep the
  source hidden and fail the operation closed.
- Webhooks are delayed and can be missed. Before each private refresh, verify
  the installation and selected-repository authorization with GitHub; never
  treat a transient network error, rate limit, or server error as proof of
  revocation. Persist an authorization freshness deadline separately from the
  GitHub token expiry. Retrieval must exclude a private source when its
  verified authorization deadline expires. Define and test a bounded
  reconciliation interval; when GitHub is unavailable past that deadline,
  make the source temporarily unavailable rather than assuming access remains
  valid. Alert operators when webhook delivery or reconciliation is
  unhealthy. A webhook alone is not an authorization freshness guarantee.
- Keep access-lost content quarantined and non-retrievable for no more than
  the existing 30-day lifecycle window, unless an operator explicitly purges
  it sooner. Document that database backups may retain prior content until
  their configured expiration. Do not serve quarantined data merely because a
  credential or installation becomes available again.
- Reauthorization must confirm the same immutable repository identity and
  current installation grant, then fetch, validate, parse, and embed a
  complete fresh snapshot while the source remains hidden. Only after that
  succeeds may one transaction replace the snapshot and make the source
  active. Do not use the existing lifecycle reactivation primitive by itself
  to expose a stale private snapshot.
- Restored databases must be reconciled with the external lifecycle journal
  before receiving traffic. A journal restriction can only hide or delete a
  source; it cannot restore authorization or reactivate content. See
  [ADR 0006](0006-restore-safe-source-lifecycle.md).

### Availability and rollout gate

- Keep private ingestion disabled until a separate implementation issue is
  completed and operators validate private-key storage/rotation, selected
  repository scoping, authenticated host boundaries, signed webhook
  verification and replay handling, delivery monitoring, bounded
  reconciliation and an expiring authorization freshness deadline,
  access-loss exclusion, reauthorization with a fresh snapshot, journal
  failure behavior, and backup/restore recovery.
- The current `index-github` command remains public-only and tokenless. No
  private token flags, environment variables, app setup instructions, or
  endpoint are available as part of this design change.
- Automatic content refresh, push-triggered ingestion, scheduled polling,
  hosted multi-tenant access, and arbitrary GitHub hosts remain out of scope.

## Consequences

- A GitHub App is the default authorization model because its installation can
  be restricted to selected repositories and its short-lived tokens can be
  scoped to the one repository and permission subset required for a refresh.
- Security-only webhooks and periodic reconciliation are prerequisites for
  private access. The freshness deadline closes the gap when webhook delivery
  stops, including while an installation is suspended. These security events
  are distinct from deferred content-sync webhooks.
- A private source may become unavailable during GitHub or webhook outages;
  fail-closed authorization and restore behavior takes precedence over
  availability. Transient refresh failures alone do not erase an authorized
  last-good snapshot.
- Private snapshots remain sensitive data after indexing. Operators remain
  responsible for database encryption, access control, backup retention, and
  purging retained backups according to their policy.
- The public connector and existing source lifecycle APIs remain unchanged
  until a separately reviewed implementation is accepted.

## Alternatives considered

- **Personal access tokens:** rejected as the default because they bind access
  to a user credential and are harder to constrain to this app's selected
  repository refresh operation.
- **Send a private installation token to the raw-content host:** rejected
  because the credential would cross a host boundary. Authenticated blob reads
  stay on the fixed GitHub API host.
- **Trust webhook payload deltas without reconciliation:** rejected because
  events can be delayed or out of order and repository-selection transitions
  may not provide the expected non-empty removal list.
- **Automatically reactivate a quarantined snapshot after reauthorization:**
  rejected because renewed permission does not prove that stored content is a
  fresh snapshot. A complete new fetch must succeed before visibility returns.
- **Treat refresh failures as authorization loss:** rejected because rate
  limits and transient network or GitHub service failures are not revocation.

## References

- [GitHub App installation repository selection](https://docs.github.com/en/apps/using-github-apps/installing-a-github-app-from-a-third-party)
- [Generate an installation access token](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app)
- [Permissions required for GitHub Apps](https://docs.github.com/en/rest/authentication/permissions-required-for-github-apps)
- [Git trees REST API](https://docs.github.com/en/rest/git/trees)
- [Git blobs REST API](https://docs.github.com/en/rest/git/blobs)
- [Webhook events and payloads](https://docs.github.com/en/webhooks/webhook-events-and-payloads)
- [Validate webhook deliveries](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries)
- [Webhook delivery best practices](https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks)
