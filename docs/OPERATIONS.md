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
the CLI `index` command. The app only sees that indexed corpus; it does not
watch source directories or automatically ingest changes. Re-index after
content changes. Re-indexing replaces one source transactionally.

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

## Source access-loss quarantine

The database records source kind, an immutable external identity for a future
GitHub source, lifecycle state, access-loss time, and a quarantine expiry. A
confirmed access loss must move a GitHub source to `access_lost`; the retrieval
query excludes its chunks immediately while retaining them for up to 30 days.
Reactivation is allowed only before expiry and when the verified external
identity exactly matches the stored identity. Explicit source removal deletes
the live source and chunks immediately. The current app has these database
primitives but does not yet connect to GitHub or automatically detect
authorization changes.

Purge expired quarantine rows daily using an external scheduler or operator
runbook. The command needs only `DATABASE_URL`; it does not call Trussium or
need an embedding model:

```bash
uv run --no-dev trussium-knowledge-agent purge-expired-sources --limit 100
```

The default batch is 100 and the maximum is 1,000. Repeat the command until it
reports zero if a backlog exceeds the selected batch. Alert on command failure
and verify the database connection before retrying. Purging cascades to the
source's indexed chunks. This command does not purge old database backups; use
the backup retention policy and restore procedure when handling deletion
requests. No background scheduler is built into the application.

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
