# ADR 0006: Restore-safe source lifecycle journal

## Status

Accepted.

## Context

Deleting a source or marking a GitHub source access-lost in PostgreSQL does not
change older database backups. Restoring such a backup can restore deleted
content and active retrieval state. A journal stored only in the same database
would be rolled back with that database and would not close the gap.

## Decision

- Keep an append-only JSON Lines lifecycle journal on operator-managed durable
  storage outside the PostgreSQL backup/restore set.
- Journal only stable source IDs, source kind and immutable external identity,
  lifecycle event kind, schema version, and timestamp. Never include credentials
  or indexed document contents.
- Serialize writes with an OS file lock; append complete validated records,
  flush them to disk, and enforce restrictive file permissions and a fixed size
  limit. A malformed, incomplete, insecure, oversized, or identity-conflicting
  journal fails closed.
- A source is hidden in PostgreSQL before its access-loss or deletion event is
  appended. If journaling fails, the source remains hidden and the operation
  reports failure. Explicit removal and expiry purge append a deletion event
  before hard-deleting source rows and chunks.
- Reconcile the complete journal against an isolated restored database before
  exposing it to application traffic. Reconciliation is transactional,
  idempotent, identity-checked, and restrictive only: access loss quarantines;
  deletion removes; no journal event activates or reindexes a source.
- A journal restriction remains authoritative during restore. Re-authorization
  and reactivation require a separately designed verified refresh flow; this
  ADR intentionally prefers an unavailable source over serving a possibly
  stale snapshot.
- Keep private GitHub ingestion disabled until operators configure the journal
  on independently durable storage and test restore reconciliation.

## Consequences

- Operators must protect and back up the journal independently from PostgreSQL
  dumps, and restore it before reconciling a database backup.
- Source removal, GitHub access-loss handling, and expiry purge require the
  journal path to be configured and writable. Indexing local sources and
  retrieval remain available without it.
- The journal cannot erase indexed data from historical backups. Backup
  expiration and access controls remain necessary, and a restore must stay
  isolated until reconciliation succeeds.
- The current initial implementation uses a local POSIX file with `flock` and
  is intended for a single-host self-hosted deployment. Shared multi-host
  journal storage and hosted service operation require a separate design.

## Alternatives considered

- **Store tombstones in PostgreSQL:** rejected as the sole authority because a
  restore of an older PostgreSQL backup also restores an older tombstone set.
- **Replay a manually maintained list:** rejected because manual lifecycle
  records are prone to omission and do not provide atomic, serialized appends.
- **Automatically reactivate from a later database snapshot:** rejected because
  a lifecycle record alone cannot prove that repository access or content was
  revalidated.

