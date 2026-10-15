# Self-hosted operations

This guide covers operating one self-hosted Trussium Knowledge Agent instance:
configuration, health checks, PostgreSQL backup and recovery, and common
failure diagnosis. It describes the application as implemented today; it is not
a multi-user security or high-availability design.

## Deployment boundary

The application consists of the FastAPI process, PostgreSQL with pgvector, and
a separately operated Trussium runtime. The app does not install or start the
runtime. Compose is provided as a local development example; production can use
a managed PostgreSQL service or an operator-managed PostgreSQL server. In
either case, the `vector` extension must be installed on the PostgreSQL server
and enabled in the application database before the app applies its migrations.
See the [architecture](ARCHITECTURE.md) and the upstream
[pgvector installation and extension instructions](https://github.com/pgvector/pgvector#installation).

The browser interface has no built-in user authentication. Keep it on loopback
or a trusted private network. Before allowing access from other users, put it
behind an authenticated TLS boundary and apply appropriate authorization,
request limits, and network policy. The read-only MCP endpoint is disabled
unless its bearer token is configured; when enabled, expose it only to trusted
clients over a protected connection. Do not treat its read-only tools as a
replacement for access control on the app or database.

## Configuration and startup

Install Python 3.12+, `uv`, PostgreSQL client utilities, and the app's locked
dependencies. Configure production values through a secret manager or a
service-manager environment mechanism. Do not commit `.env` files, include
credentials in command arguments, or copy the development password from
`.env.example` into a deployment.

| Setting | Use |
| --- | --- |
| `DATABASE_URL` | PostgreSQL connection used by indexing, search, browser questions, and MCP retrieval. Use TLS for remote databases. |
| `TRUSSIUM_URL` | Runtime base URL; defaults to `http://127.0.0.1:9000`. |
| `TRUSSIUM_EMBEDDING_MODEL` | Required for indexing, search, questions, and MCP retrieval. The selected runtime must advertise/use this model for embeddings. |
| `TRUSSIUM_CHAT_MODEL` | Required for `ask`, `docs.review_guidance`, and browser questions; it must be chat-enabled. |
| `TRUSSIUM_TIMEOUT_SECONDS` | Runtime request timeout, default 30 seconds and maximum 120 seconds. |
| `TRUSSIUM_API_KEY` | Optional runtime bearer credential. Keep it in a secret manager. |
| `KNOWLEDGE_AGENT_TOOL_TOKEN` | Optional high-entropy bearer secret. When absent, `/v1/mcp` returns 503; when set, callers must provide the matching token. |
| `KNOWLEDGE_AGENT_AUDIT_ROOT` | Optional operator-selected local Markdown root for `docs.audit_links`. Callers cannot override this path. |
| `KNOWLEDGE_AGENT_LIFECYCLE_LEDGER_PATH` | Required for source removal, GitHub access-loss recording, and expiry purge. Points to the append-only lifecycle journal on durable storage outside PostgreSQL backups. |
| `KNOWLEDGE_AGENT_GITHUB_APP_ID` | Optional GitHub App numeric ID. Required for private snapshots and authorization reconciliation. |
| `KNOWLEDGE_AGENT_GITHUB_INSTALLATION_ID` | Optional installed GitHub App installation ID. The connector only acts on this installation. |
| `KNOWLEDGE_AGENT_GITHUB_PRIVATE_KEY_PATH` | Absolute path to an operator-managed PEM key file, owned by the service account, mode `0600`, and not a symlink. Never store the key in PostgreSQL or the repository. |
| `KNOWLEDGE_AGENT_GITHUB_AUTHORIZATION_LEASE_SECONDS` | Optional retrieval authorization lease, default 900 seconds and bounded to 60–3600 seconds. Schedule reconciliation more frequently than this value. |
| `KNOWLEDGE_AGENT_GITHUB_WEBHOOK_SECRET` | Optional high-entropy secret injected from a secret manager; 32 or more UTF-8 bytes. Required to process signed GitHub webhooks. |

`POSTGRES_USER`, `POSTGRES_PASSWORD`, and `POSTGRES_DB` configure only the
development Compose database. They do not configure the app's DB connection;
`DATABASE_URL` does that. See [`.env.example`](../.env.example) for local
variable names and [the workflow guide](TRUSSIUM_WORKFLOW.md) for the MCP
composition boundary.

For an external PostgreSQL server, ask its operator to install a pgvector build
compatible with the server's PostgreSQL major version. A database administrator
must enable the extension once in the target database:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

The application applies versioned table migrations transactionally when its
database operations first run. The app DB principal therefore needs the
database/schema privileges required to create and alter its own tables. It
does not install server extension files, and application migrations do not
create the extension.

For libpq-based health and backup commands, a protected service file can keep
connection parameters out of command lines. The following entries contain no
passwords; keep the actual file readable only by the operations account and
store passwords separately in a permission-restricted `PGPASSFILE` or secret
manager:

```ini
[knowledge-agent]
host=db.example.internal
port=5432
dbname=knowledge_agent
user=knowledge_agent_app
sslmode=verify-full

[knowledge-agent-backup]
host=db.example.internal
port=5432
dbname=knowledge_agent
user=knowledge_agent_backup
sslmode=verify-full

[knowledge-agent-admin]
host=db-recovery.example.internal
port=5432
dbname=postgres
user=knowledge_agent_restore
sslmode=verify-full

[knowledge-agent-recovery]
host=db-recovery.example.internal
port=5432
dbname=knowledge_agent_recovery
user=knowledge_agent_restore
sslmode=verify-full
```

Set `PGSERVICEFILE` and `PGPASSFILE` in the service environment. `DATABASE_URL`
can use libpq service syntax such as `service=knowledge-agent`; the same
service profile can be used with `pg_isready` and `psql` as shown below.
For example, configure the paths without embedding secrets in the unit file:

```bash
export PGSERVICEFILE=/etc/knowledge-agent/pg_service.conf
export PGPASSFILE=/etc/knowledge-agent/pgpass
```

On Unix-like systems, restrict `PGPASSFILE` to its owner (mode `0600`) as
required by libpq. The service file should also be readable only by the
operations account when its connection topology is sensitive.
Provision the named database roles separately through the database operator;
the service-file example does not create them. Use distinct app and backup
principals with only the privileges each workflow needs. For TLS deployments,
configure the appropriate trusted root certificate as well as
`sslmode=verify-full`.

Install and run the app from its checked-out project directory:

```bash
uv sync --locked --no-dev
uv run --no-dev uvicorn trussium_knowledge_agent.app:app --host 127.0.0.1 --port 8000
```

Configure a service manager to supply the environment and restart the process
on failure. Keep Uvicorn bound to loopback when a same-host reverse proxy is
used. If the process must listen on a private interface, firewall it so that
only the trusted proxy or callers can connect. Do not publish the unauthenticated
browser interface directly to the public internet. Avoid debug mode in
production.

Before serving traffic, index the intended Markdown source explicitly using
the CLI `index` or `index-github` command. The app only sees that indexed
corpus; it does not watch source directories or automatically ingest changes.
Re-index after content changes. Re-indexing replaces one source transactionally.

## Explicit public GitHub refresh

The `index-github` command supports public GitHub repositories only. It does
not accept a repository URL or API host, use credentials, follow redirects,
fetch issues/pull requests, or run repository code. The operator selects one
repository and a branch, tag, or commit ref explicitly:

```bash
uv run --env-file .env trussium-knowledge-agent index-github OWNER/REPOSITORY --ref main
```

The public `index-github` command is tokenless and uses `api.github.com` and
`raw.githubusercontent.com`. Allow outbound HTTPS to those hosts if egress is
restricted. Anonymous GitHub API limits apply; a rate-limit or network failure
leaves the last successful index unchanged. For private sources, use only the
separate opt-in GitHub App flow below; GitHub Enterprise hosts are unsupported.

Each run resolves the selected ref to a full commit SHA, reads the corresponding
tree, and downloads only regular Markdown blobs. The snapshot is bounded to a
4 MiB tree response, 20,000 tree entries, 100 Markdown files, 4 KiB per path,
5 MiB per file, 20 MiB total Markdown bytes, 10,000 chunks, 10 seconds per
HTTP request, and a 120-second overall download deadline. Truncated trees,
invalid UTF-8, unsafe paths/file modes,
oversized responses, or any incomplete fetch fail before source replacement.
Symlink blobs and submodule contents are never followed. A successful complete
snapshot replaces that repository's previous index atomically; if the selected
ref contains no Markdown files, a successful empty snapshot intentionally
removes its prior chunks.

The command reports the repository, pinned commit, file/chunk counts, and full
source ID. Keep the source ID in protected operational notes if you may need to
remove the source later. Explicit removal is offline and uses the external
lifecycle journal:

```bash
uv run --env-file .env trussium-knowledge-agent remove-source SOURCE_ID
```

The source identity uses GitHub's immutable numeric repository ID, so a
repository rename does not silently create a second source. Only one active
snapshot per repository is supported; refreshing a different ref replaces the
previous snapshot. The database revision and `indexed_at` identify the last
successful snapshot; failed attempts are returned to the CLI and do not claim
freshness. See [ADR 0005](adr/0005-github-markdown-source-connector.md) for the
full connector boundary.

## Opt-in private GitHub snapshots

Private indexing is a separate, explicitly configured path. It is not enabled
by default. The GitHub App and installation credentials remain on the
self-hosted Knowledge Agent; Trussium runtime credentials are not used for
GitHub access.

Before configuring it:

1. Create a GitHub App and grant repository `Contents: read` only (GitHub
   supplies the required metadata access for repository API calls). Do not
   grant write, administration, Actions, issue, pull-request, or organization
   permissions. Subscribe to the `installation` and
   `installation_repositories` webhook events.
2. Install the app with **only the repositories selected** for indexing. Keep
   the installation ID and numeric repository IDs in protected operator
   configuration. A token is minted for exactly one repository ID per
   operation; never configure or request an installation-wide token.
3. Store the downloaded App private key in a protected secret store or file
   readable only by the service account. Set the absolute key path and the App
   and installation IDs in the service environment. Do not paste the key or
   installation token into command arguments, logs, or source metadata.
4. Configure `KNOWLEDGE_AGENT_LIFECYCLE_LEDGER_PATH` on durable storage
   outside PostgreSQL backups. Private authorization loss requires this
   journal; the operation keeps a source hidden if journal recording fails.
5. Expose `POST /webhooks/github` through HTTPS. Inject a random webhook secret
   of at least 32 bytes through the deployment secret manager. The handler
   validates GitHub's HMAC-SHA256 signature and delivery ID. Events only hide
   affected sources; they never trigger content fetching or embeddings.
6. Select an authorization lease from 60 to 3600 seconds. Schedule the
   reconciliation command no less frequently than half that lease duration.
   For the 900-second default, a five-minute schedule is appropriate. If
   GitHub cannot be reached, the source is excluded from retrieval when its
   verified lease expires; transient errors are not recorded as confirmed
   revocations.

Example service environment (provide actual values using your secret manager;
do not put them in a committed `.env` file):

```text
KNOWLEDGE_AGENT_GITHUB_APP_ID=123456
KNOWLEDGE_AGENT_GITHUB_INSTALLATION_ID=789012
KNOWLEDGE_AGENT_GITHUB_PRIVATE_KEY_PATH=/run/secrets/knowledge-agent-app.pem
KNOWLEDGE_AGENT_GITHUB_AUTHORIZATION_LEASE_SECONDS=900
KNOWLEDGE_AGENT_GITHUB_WEBHOOK_SECRET=<secret-manager-injected-value>
```

Index one repository by its immutable numeric GitHub repository ID and an
explicit ref. The repository ID is returned by GitHub's repository metadata
API. No arbitrary URL or host is accepted:

```bash
uv run --env-file .env trussium-knowledge-agent index-github-private 123456789 --ref main
```

The app obtains a short-lived installation token scoped to that single ID and
`Contents: read`, then fetches tree and blob objects only from HTTPS
`api.github.com`. It never forwards the token to `raw.githubusercontent.com`.
Each complete snapshot is validated and embedded before an atomic source
replacement. A quarantined source is not reactivated by simply restoring its
credentials: a complete fresh snapshot must succeed first.

Schedule authorization reconciliation independently from content refresh. For
the default 900-second lease, run this command at least every five minutes:

```bash
uv run --env-file .env trussium-knowledge-agent reconcile-private-github
```

The command returns a nonzero status when any repository could not be checked;
transient failures do not extend leases. Alert on failed runs. When the lease
expires, affected sources disappear from search and answers until successful
reconciliation or a full fresh index. Webhook delivery is an additional prompt
for access removal, not a replacement for this scheduled check.

After backup restoration, run the lifecycle journal reconciliation before
serving traffic. For a repository access-loss event, review its quarantine
status and re-index only after confirming the same repository is again
authorized. Use `remove-source SOURCE_ID` to purge an indexed source when it is
no longer needed; remember that historical database backups follow their own
retention policy. See [ADR 0007](adr/0007-private-github-app-access-boundary.md)
for the authorization and recovery contract.

## Health checks and routine operations

The app exposes `GET /health/live`. It confirms only that the FastAPI process
can answer requests: it deliberately does not check PostgreSQL, pgvector,
configured models, or the Trussium runtime. A `200` response must not be used as
a dependency-readiness signal.

Check each dependency separately using credentials injected through a protected
libpq service file and password file, or the hosting platform's equivalent. For
example, with a service named `knowledge-agent` configured locally:

```bash
curl --fail --silent --show-error http://127.0.0.1:8000/health/live
pg_isready --dbname='service=knowledge-agent'
psql --dbname='service=knowledge-agent' -X -v ON_ERROR_STOP=1 \
  -c "SELECT extversion FROM pg_extension WHERE extname = 'vector';"
trussium health --url "$TRUSSIUM_URL"
```

The Trussium `health` command checks the runtime readiness endpoint. If the
runtime is deployed elsewhere, run the check from a host with the same network
reachability as the app. Then perform an application-level smoke test with a
known indexed query and confirm the expected source citation:

```bash
uv run --no-dev trussium-knowledge-agent search "a known phrase in the indexed corpus" --limit 3
```

This checks the app's DB connection, embedding request, vector identity filter,
and retrieval path. An empty result can also mean that the phrase is not present
or that the corpus was indexed with a different embedding model. Use `ask` as a
separate smoke test for chat and citation validation. No single built-in
readiness endpoint currently checks this entire dependency chain.

During routine operation:

- Monitor app liveness, PostgreSQL availability and capacity, runtime
  availability, request error rates, and backup freshness independently.
- Alert on failed backups and perform periodic restore drills into an isolated
  database. A successful dump command alone is not proof of recoverability.
- Keep an inventory of the PostgreSQL major version, installed pgvector
  extension version, app revision, and configured embedding model. Re-index if
  moving to a different embedding provider/model/dimension; retrieval filters
  by that identity.
- Store backup archives encrypted and off-host with a retention policy. Indexed
  chunks include document text and vectors and may contain the same sensitive
  information as the source corpus.
- Back up original Markdown repositories and deployment configuration/secrets
  separately. The database contains indexed chunks and metadata, not the
  complete source repositories or Trussium runtime configuration. This guide
  covers logical database dumps, not point-in-time recovery; WAL archiving and
  its restore runbook must be supplied by the PostgreSQL operator if the
  required recovery point is shorter than the snapshot interval.
- Remember that deleting a source from the active database does not remove its
  content from older retained backups. Apply backup expiration policy to
  deletion requests as well.

## Source lifecycle and restore reconciliation

The database records source kind, immutable GitHub identity, lifecycle state,
access-loss time, and a quarantine expiry. Private GitHub sources also record
the installation ID and verified authorization deadline. A source with a
confirmed access loss is moved to `access_lost`; the retrieval query excludes
its chunks immediately while retaining them for up to 30 days. A private
source with an expired authorization lease is likewise excluded even if a
webhook was delayed or the API is temporarily unavailable. Private
reactivation requires the same repository identity and a complete successful
fresh snapshot. Explicit source removal deletes live chunks immediately.

Source removal, confirmed GitHub access loss, and expiry purge require an
append-only JSON Lines lifecycle journal at
`KNOWLEDGE_AGENT_LIFECYCLE_LEDGER_PATH`. Provision its parent directory with
the service account as owner, create the file with mode `0600`, and place it on
durable storage outside the PostgreSQL backup/restore set. The application
serializes appends, fsyncs each record, and rejects symlinks, insecure file
permissions, invalid or incomplete records, and journals larger than 16 MiB.
The journal contains stable source identifiers and lifecycle metadata only—no
document text or credentials. Protect it, back it up independently, and test
its recovery.

If journal recording fails after a source has been hidden in PostgreSQL, the
operation reports an error and leaves that source non-retrievable. Retry after
fixing the journal. Repeated events are idempotent. Expiry purge first marks
expired rows deleted, records the deletion, and only then removes the rows and
chunks; a failed append leaves rows hidden for a later retry.

Purge expired quarantine rows daily using an external scheduler or operator
runbook. The command needs `DATABASE_URL` and the lifecycle journal; it does
not call Trussium or need an embedding model:

```bash
uv run --no-dev trussium-knowledge-agent purge-expired-sources --limit 100
```

The default batch is 100 and the maximum is 1,000. Repeat the command until it
reports zero if a backlog exceeds the selected batch. Alert on command failure
and verify the database connection before retrying. Purging cascades to the
source's indexed chunks. This command does not purge old database backups; use
the backup retention policy and restore procedure when handling deletion
requests. No background scheduler is built into the application.

### Database restore ordering

Keep a restored database isolated from application traffic until lifecycle
reconciliation and validation finish. Restore into a separate database or
service, configure `DATABASE_URL` for that isolated target, and point
`KNOWLEDGE_AGENT_LIFECYCLE_LEDGER_PATH` to the independently recovered current
journal—not a copy rolled back to the date of the PostgreSQL backup. Run:

```bash
uv run --no-dev trussium-knowledge-agent reconcile-source-lifecycle
```

The command validates the complete journal before applying changes in one
database transaction. It deletes sources with a durable deletion event,
quarantines sources with confirmed access loss, and leaves unrelated active
sources unchanged. It is idempotent and never reactivates, reindexes, or creates
a source. A malformed journal, source-identity mismatch, or database error
causes a nonzero exit; do not expose the restored database until the failure is
resolved and reconciliation succeeds. Review the reported deleted,
quarantined, and already-absent counts against the expected restore, then run
the normal liveness, dependency, and known-query smoke tests before cutover.

Reconciliation cannot erase chunks from historical backups or recover a
journal lost in the same failure domain as PostgreSQL. Keep separate retention
controls for database dumps and the journal. The initial implementation uses a
host-local file and `flock`; it is intended for a single app host, not multiple
replicas sharing a network filesystem. Private GitHub ingestion remains
disabled until operators configure and successfully rehearse this recovery
procedure.

After reconciliation, inspect lifecycle state and confirm that deleted sources
are absent, access-lost sources remain quarantined, and unrelated sources remain
active. The search smoke test must return only passages from active sources; do
not infer retrieval eligibility from row counts alone. Run reconciliation a
second time to confirm idempotence. A repeated run may report previously
deleted sources as already absent.

#### Rehearsal record

On 2026-10-15, the restore and lifecycle replay path was exercised locally using
a disposable PostgreSQL 16.15 cluster with pgvector 0.8.7. Three synthetic
sources (one to delete, one to quarantine, and one unaffected active source)
were dumped before lifecycle events were recorded. The archive was restored to
a new database while the database containing the later lifecycle changes
remained separate. Before reconciliation, the restored database contained
three active sources and three chunks. The first reconciliation reported one
deleted, one quarantined, and zero already absent; afterward, one source was
active, one was access-lost, and two chunks remained. The retrieval check
returned only the active source. The second reconciliation reported zero
deleted, zero quarantined, and one already absent. The journal was outside the
database cluster, mode `0600`, and the test used no production data.

This validates the application recovery path in a disposable local cluster; it
does not validate a deployment's backup storage, secrets, ownership model,
network filesystem, or operational access controls. Keep private repository
configuration unset until target operators have rehearsed selected-repository
permissions, HTTPS webhook delivery, scheduled authorization reconciliation,
loss-of-access behavior, and restore recovery with the independently backed-up
journal and recovery credentials.

## PostgreSQL backup

`pg_dump` produces a consistent database backup while the database is in use.
Use the custom archive format so it can be inspected and restored with
`pg_restore`; see the PostgreSQL
[`pg_dump`](https://www.postgresql.org/docs/17/app-pgdump.html) and
[`pg_restore`](https://www.postgresql.org/docs/17/app-pgrestore.html)
references. A single-database dump does not include cluster-wide roles or
tablespaces; provision the required roles separately. Keep client utilities at
the same PostgreSQL major version as the server where practical.

Configure a private libpq service file with host, port, database, user, and
TLS settings. Store the password in a separate permission-restricted
`PGPASSFILE` or inject it through the platform secret manager. Do not put a
password in shell history, a service file, or a command-line connection URL.
Then create and inspect a backup:

```bash
umask 077
export BACKUP_FILE="/secure-backups/knowledge-agent-$(date -u +%Y%m%dT%H%M%SZ).dump"
pg_dump --dbname='service=knowledge-agent-backup' --format=custom --file="$BACKUP_FILE"
pg_restore --list "$BACKUP_FILE" >/dev/null
shasum -a 256 "$BACKUP_FILE" > "$BACKUP_FILE.sha256"
```

On Linux, use `sha256sum` in place of `shasum -a 256` if `shasum` is not
installed.

The destination directory must be encrypted/permission-restricted and copied to
a separate failure domain. Retention duration and recovery-point objectives are
deployment decisions. Keep checksums with the archive and periodically verify
the archive by restoring it; a checksum detects accidental corruption but does
not replace encryption or restore testing.

## Restore and recovery drill

**Restore into a new, isolated database first.** Do not point a restore command
at the active database. `pg_restore --clean` drops objects and can destroy
existing target data; the commands below intentionally omit it. If the archive
is not from a trusted source, inspect its contents before restoring because
PostgreSQL warns that a restore can execute SQL chosen by the source database
superuser.

1. Select a recovery server running the same or a newer compatible PostgreSQL
   major version. Confirm that the matching pgvector extension files are
   installed on that server and that the restore principal can create the
   extension and database objects. The extension package is server software;
   it is not bundled as a binary in the database archive.
2. Point a libpq service named `knowledge-agent-admin` at the recovery server's
   maintenance database and a service named `knowledge-agent-recovery` at the
   newly created, empty recovery database. Use a separate, protected password
   file. Create the database with an appropriate owner:

   ```bash
   createdb --maintenance-db='service=knowledge-agent-admin' \
     --owner=knowledge_agent knowledge_agent_recovery
   ```

3. Inspect the archive listing, then restore to the new target. The archive
   records non-system extensions; do not separately create `vector` in the
   target before this restore unless you intentionally exclude the extension
   entry from the restore list.

   ```bash
   pg_restore --list "$BACKUP_FILE" | less
   pg_restore --dbname='service=knowledge-agent-recovery' \
     --no-owner --exit-on-error "$BACKUP_FILE"
   ```

   With `--no-owner`, restored objects belong to the restore principal. Ensure
   the application DB principal has the table/schema privileges it needs, or
   use the designated app principal as the restore owner. Do not use a
   superuser as the long-running app credential.
4. Verify extension, migration, and corpus state; then refresh planner
   statistics:

   ```bash
   psql --dbname='service=knowledge-agent-recovery' -X -v ON_ERROR_STOP=1 \
     -c "SELECT extversion FROM pg_extension WHERE extname = 'vector';" \
     -c "SELECT version FROM schema_migrations ORDER BY version;" \
     -c "SELECT count(*) AS sources FROM knowledge_sources;" \
     -c "SELECT count(*) AS chunks FROM document_chunks;" \
     -c "ANALYZE;"
   ```

   Compare counts and migration versions to backup records. Start an isolated
   app process configured with the recovery DB and run the liveness, known
   search, and (if chat is enabled) answer smoke checks above. Confirm returned
   citation paths and headings are expected.
5. For a recovery cutover, update the app's `DATABASE_URL` through its secret
   manager, restart the app, and repeat checks. Keep the previous database
   isolated and available during the rollback window. Only remove it after the
   recovered service is verified and the owner explicitly approves cleanup.

The app records applied migrations in `schema_migrations` and runs pending
migrations forward during data operations. There are no automatic down
migrations. Reverting application code does not reverse a schema migration;
restore a pre-upgrade database into a separate target if rollback requires the
old schema. Test upgrades and recovery with a recent backup before production
changes.

## Troubleshooting

| Symptom | Checks and recovery |
| --- | --- |
| `/health/live` succeeds but browser/MCP requests fail | Liveness is process-only. Check PostgreSQL with `pg_isready`, runtime readiness with `trussium health`, and then perform the known-query smoke test. Inspect service logs without enabling request-body or authorization-header logging. |
| PostgreSQL connection refused or authentication fails | Verify host/port, TLS, service-manager secret injection, and DB availability from the app host. Do not paste a credential-bearing `DATABASE_URL` into support logs. |
| Migration reports missing `vector` type or function | Confirm pgvector is installed for the server's PostgreSQL major version and `vector` is enabled in this database. Have a database administrator install/enable the extension, then retry the app operation. Do not delete migration records to force reruns. |
| Search returns no passages | Confirm the source was indexed and contains the expected text. Check stored embedding provider/model/dimension against `TRUSSIUM_EMBEDDING_MODEL`; re-index with the desired model when identity changed. |
| Index, search, or ask fails against Trussium | Run `trussium health --url "$TRUSSIUM_URL"` from the app network. Verify the configured model is enabled for the required embeddings or chat capability, optional API key is correct, TLS/network policy permits access, and timeout is positive and no more than 120 seconds. |
| MCP returns HTTP 503 | `KNOWLEDGE_AGENT_TOOL_TOKEN` is unset, so the endpoint is disabled. Configure the token only if MCP is intended. |
| MCP returns HTTP 401 | The bearer token does not match. Update the app and trusted runtime configuration through their secret stores; never send the token in workflow arguments. |
| An MCP call returns a JSON-RPC service error | Check `DATABASE_URL` and the embedding/chat model settings required by that specific tool. These tool errors can be returned in a JSON-RPC envelope over HTTP 200; HTTP 503 is specifically the disabled endpoint state. |
| Link audit fails or finds no files | Check that `KNOWLEDGE_AGENT_AUDIT_ROOT` is set to a readable Markdown root. The root is fixed by operator configuration; request arguments cannot change it. Symlinks are not followed. |
| Restore fails around the vector extension | Confirm extension files are installed on the recovery PostgreSQL server and inspect the archive's extension entry. Use a new empty DB and a principal authorized to create extensions. Do not retry with `--clean` against an uncertain target. |
| Restore completes but retrieval differs | Verify source/chunk counts, extension version, embedding identities, and known citations. Restored vectors only compare with query embeddings from the matching provider/model/dimension. Re-index if changing embedding identity. |

Application errors returned to browser/MCP callers are intentionally generic.
Use protected service logs and dependency-specific health checks for diagnosis;
do not log prompts, retrieved passages, bearer tokens, or password-bearing
connection strings.
