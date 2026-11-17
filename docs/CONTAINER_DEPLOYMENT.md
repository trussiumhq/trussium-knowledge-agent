# Container deployment

This guide runs the open-source Trussium Knowledge Agent and PostgreSQL with
pgvector using Docker Compose. The Trussium runtime remains a separate service;
the compose stack does not install, configure, or host a model runtime. No
GitHub App configuration is needed for the local demo or for public sources.

This is a self-hosted deployment recipe, not a managed public demo. The browser
interface has no built-in user authentication. The included Compose mapping
binds it to loopback only; do not change that to a public interface unless the
deployment adds authentication, authorization, TLS, request limits, and
appropriate network controls. See [the security boundary](WEB_INTERFACE.md#security-and-deployment-boundary).

## Requirements

- Docker Engine with the Compose plugin (or Docker Desktop with Compose).
- A reachable Trussium runtime configured with an embeddings-enabled model;
  questions also require a chat-enabled model.
- A host machine with enough disk for PostgreSQL and the indexed corpus.

The application image uses Python 3.12 and installs the locked production
dependencies. The Compose database uses PostgreSQL 17 with pgvector.

## Configure and start

From the repository root, create a private local environment file and replace
the example database password. A hexadecimal secret avoids characters that
need URL-encoding in the Compose-generated PostgreSQL connection string:

```bash
cp .env.example .env
openssl rand -hex 24
```

Put the generated value in `POSTGRES_PASSWORD` in `.env`. Set the model names
and runtime URL for the models enabled on your runtime. When that runtime is
running directly on the Docker host, Compose uses the
`TRUSSIUM_CONTAINER_URL` value from `.env`, which defaults to
`http://host.docker.internal:9000`. If the runtime is on another Compose
network or remote host, set `TRUSSIUM_CONTAINER_URL` to an address resolvable
and reachable from the app container. (`TRUSSIUM_URL` remains the setting for
running the app directly on the host during development.) Configure
`TRUSSIUM_API_KEY` only if the runtime requires authentication. Keep `.env`
untracked and never put credentials in source control.

Start both services and check their state:

```bash
docker compose up -d --build
docker compose ps
curl --fail --silent --show-error http://127.0.0.1:8000/health/live
```

Open <http://127.0.0.1:8000/>. The liveness endpoint confirms only that the
application process can respond; it does not verify the database, vector
extension, model, or Trussium runtime. See
[health checks](OPERATIONS.md#health-checks-and-routine-operations) for
independent dependency checks.

## Index a corpus and try the demo

The container includes the synthetic sample corpus. Index it, then search or
ask a question:

```bash
docker compose run --rm app trussium-knowledge-agent index /app/fixtures/evaluation-corpus
docker compose run --rm app trussium-knowledge-agent search "how do I configure retries?" --limit 5
docker compose run --rm app trussium-knowledge-agent ask "How do I configure retries?" --limit 5
```

To index a local Markdown directory, mount only the chosen directory read-only
and pass its in-container path:

```bash
docker compose run --rm \
  --volume /absolute/path/to/markdown-repo:/sources/docs:ro \
  app trussium-knowledge-agent index /sources/docs
```

To create a bounded snapshot of a public GitHub repository, use
`index-github OWNER/REPOSITORY --ref main`. This uses the public tokenless
connector; private GitHub settings stay blank unless an operator separately
chooses to enable the opt-in GitHub App workflow. See
[public GitHub refresh](OPERATIONS.md#explicit-public-github-refresh) and
[the private-source boundary](adr/0007-private-github-app-access-boundary.md).

## Data persistence, backup, and recovery

Compose stores PostgreSQL data and the append-only source lifecycle journal in
separate named volumes:

- `knowledge-agent-postgres` contains indexed chunks, vectors, and source
  metadata.
- `knowledge-agent-lifecycle` contains the lifecycle journal needed for
  fail-closed removal and restore reconciliation.

Do not treat the journal as disposable cache. Back it up independently and
follow the coordinated recovery procedure in
[source lifecycle and restore reconciliation](OPERATIONS.md#source-lifecycle-and-restore-reconciliation).
The PostgreSQL backup guide describes logical backups and restore drills.

Stopping or recreating services preserves named volumes:

```bash
docker compose down
docker compose up -d
```

Do not use `docker compose down --volumes` for routine cleanup: it deletes both
volumes and their data. For intentional full erasure, first verify the exact
volume names and follow your retention and deletion policy.

Before upgrading, record the current image/revision and take verified backups
of both persistence volumes. Rebuild from the new revision with
`docker compose up -d --build`; database migrations are applied by the
application. If application startup or migration fails, stop the new app
container and restore the coordinated database and lifecycle-journal backups
using the documented recovery ordering. Do not restore only PostgreSQL and
serve traffic before journal reconciliation.

## Limits and production boundary

- The Compose app port and database port bind to loopback on the host by
  default. Keep this boundary unless you have designed and tested a trusted
  access layer.
- There is no multi-user authentication, tenant isolation, or public-demo
  abuse/cost protection in this application. A public deployment requires
  additional controls and is not created by these instructions.
- The Trussium runtime and any provider/model credentials are outside this
  Compose stack. Requests and indexed text are sent to the configured runtime
  for embeddings and chat; choose a data-handling boundary appropriate for the
  selected corpus.
- Docker named volumes are host-local persistence, not backups or high
  availability. Plan encrypted off-host backups and test restores.
- Private GitHub snapshots are optional and require the separate GitHub App
  permissions, durable lifecycle journal, webhook, and reconciliation setup.
  Do not add private App credentials just to run the public-source demo.

For production operations, provider configuration, dependency monitoring,
database backup/restore, and troubleshooting, continue with the
[self-hosted operations guide](OPERATIONS.md).
