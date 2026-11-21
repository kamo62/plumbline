# Local operations during the Plumbline migration

The package and CLI are `plumbline`; application configuration uses `PLUMBLINE_`.
The default database is `sqlite:///plumbline.db`. Set `PLUMBLINE_DATABASE_URL` for
PostgreSQL. Do not point development checks at a database containing important data.

## First administrator and worker

```sh
plumbline migrate
plumbline bootstrap-admin --username YOUR_ADMIN_NAME_HERE
python -m uvicorn plumbline.api.main:app --host 127.0.0.1 --port 8000
# In a separate process using the same configuration:
plumbline worker
```

Bootstrap displays a generated password once. Sign in at `/ui/`, change the
password, then sign in again. Password changes revoke sessions and tokens.
Bootstrap refuses an existing account database. Migration refuses unsupported
legacy schemas without converting or deleting their contents. Back them up and
choose a fresh database; automatic legacy-data conversion is unavailable.

Use `plumbline migrate --check` (also `db-migrate --check`) to inspect without
applying migrations: exit 0 means current, 2 means migrations are pending, and 1
means the check failed. A missing ordinary SQLite file is reported as pending
without creating it. Applying migrations remains an explicit `plumbline migrate`
operation. The current schema includes the existing OIDC identity/transaction
tables, run-evidence links and an additive diagnostic-measurement table. Creating
those tables does not mount OIDC
routes or establish a working identity-provider integration.

The API queues runs in the database. A worker must be running to collect them.
Recognised transient database failures in startup checks, polling and claiming
receive up to five retries with jittered backoff; a successful poll resets this
budget. This does not directly retry a failed final assessment/report save. If
the failure can be recorded, the job fails with a safe database-error message and
must be submitted again. If storage also rejects that status write, the lease may
expire and permit recovery within the existing job-attempt limit. Permanent errors
and exhausted polling retries stop the worker, so an operator or process supervisor
must restart it.
SIGTERM cancels work and releases its claim when the database is reachable. If
release fails, another worker must wait for the lease to expire.

API and worker processes must use the same database and export directory. Set
`PLUMBLINE_EXPORTS_DIR` to the same absolute shared path if they start from
different working directories; its default is `exports/` resolved when settings
are loaded. Reports are stored under a run UUID and worker attempt UUID. Downloads
are confined to that recorded attempt; sibling attempts and symbolic links beneath
the export root are excluded. Stored paths are absolute, while API output names
are relative. Existing stored relative paths are not migrated. A relative SQLite
database URL also depends on the working directory; use an absolute database path
when processes start from different directories.
An empty `PLUMBLINE_EXPORTS_DIR` uses the default `exports/`. CLI custom output
outside this root remains a local file path; its run listing may show the run
identifier, but API downloads reject it because it is outside the shared root.

Worker recovery, lease and download checks use SQLite and synthetic data; consult
the current acceptance record before using sensitive sources.
Local/CI cookies permit HTTP by default; dev/prod cookies require HTTPS. Set
`PLUMBLINE_COOKIE_SECURE` explicitly when needed. Forwarded headers do not decide
cookie security. The application uses session CSRF protection and revocable bearer
tokens. The former shared API key no longer grants access.

## Connection keys and rotation

ADO credentials are encrypted using a Fernet key supplied through
`PLUMBLINE_ENCRYPTION_KEY`. Generate a key with the installed `cryptography` package
and place it directly into your protected service secret configuration. Never
commit the key, put it in a URL, or include it in screenshots or logs. Connection
responses omit encrypted and plaintext credentials.

Before rotating, stop API, worker and CLI writers. Back up the database and retain
a separately secured copy of its current key. Securely generate a new key, then:

```sh
plumbline rotate-connection-key --old-key-file /protected/old-key --new-key-file /protected/new-key
```

Alternatively supply `PLUMBLINE_OLD_ENCRYPTION_KEY` and
`PLUMBLINE_NEW_ENCRYPTION_KEY` through the environment. Rotation re-encrypts all
credentials in one transaction; any decryption failure rolls it back. After
success, update `PLUMBLINE_ENCRYPTION_KEY` and restart the services. Keep each
backup paired with its matching key. Lost keys cannot recover encrypted credentials;
restore the matching key or replace the credentials.

## Retention and evidence

Database-backed CLI and worker runs store the original sanitised fetched snapshot,
not a provider-neutral conversion in its place. New persisted runs link to exactly
one snapshot. Repeating the same link is safe; replacing it with different evidence
is rejected. Worker snapshot, link and raw-metric writes remain within the existing
lease/cancellation transaction. File exports are not made transactional with the
database by this change.
Standalone database-backed retries validate their snapshot link before report writes.
A conflicting reuse of a run UUID leaves the existing status, metadata and reports
unchanged. A fresh collection should use a new run UUID.

Run detail/list responses prefer the canonical link's `snapshot_id`, falling back
to existing worker job metadata for older unlinked runs. No historical links are
invented or backfilled. Snapshot loading checks the stored payload's SHA256 content
address, including fields that do not affect a score. This is a consistency check,
not authentication or tamper resistance against someone with database write access.
It does not introduce immutable assessment results or scoring-v2 replay.

The export cleanup script requires a migrated database and only removes expired,
owned terminal-run directories with no stored evidence. Active runs, unknown
ownership, stored metrics, run-linked snapshots and diagnostic measurements are
protected. API run deletion also refuses stored measurements. Use `--dry-run` first. Until explicit
baseline expiry is implemented, protection is deliberately conservative.

## Post-collection test-pass-rate diagnostic

An administrator can request a measurement from a run's canonical stored original
snapshot, without another provider request. Migrate the selected database explicitly
before using the new routes. A file-only run or older job with only a mutable
`job.result` snapshot ID cannot be measured until a canonical run-evidence link is
available; this feature performs no historical backfill.

```http
POST /v1/runs/{run_id}/measurements/test-pass-rate
Content-Type: application/json

{"minimum_sample": 20, "population_policy": "ado-reported-total/v1"}
```

Both fields are required. The minimum must be a positive JSON integer, not a string,
float or Boolean. The named population explicitly includes all reported executions
in ADO-shaped build-linked test summaries; it does not infer a skipped-test or retry
deduplication policy. It is a diagnostic choice, not a production-scoring default.
See the [metric contract](metrics/CATALOGUE.md#stored-test-pass-rate-diagnostic).

The POST uses the existing Admin guard, cookie CSRF protection and forced-password-
change rules. A bearer token does not use cookie CSRF. Measurement and request audit
commit in one transaction. Repeating identical inputs returns the same frozen record
and records another request audit event; changing the minimum creates a different
measurement rather than modifying the first. Existing collection and legacy scoring
are unchanged.

Authenticated Viewers and Admins can read:

- `GET /v1/runs/{run_id}/measurements`: measurements stored for that run.
- `GET /v1/measurements/{id}`: the frozen record and its original snapshot link.
- `GET /v1/measurements/{id}/export.json`: that same record as a JSON attachment.
- `GET /v1/measurements/{id}/verification`: content consistency, source snapshot/link
  consistency and supported-code reproducibility as separate fields.

These routes retain the existing instance-wide Viewer audience, not a new provider-
membership or scoped-access system. They expose diagnostic results, not raw snapshot
payloads, credentials or provider exception bodies. JSON export creates no server-side
report file. GET/export return the stored result without recalculation; verification
alone attempts supported-version replay. An unavailable older version does not
overwrite the stored record. An intact diagnostic can remain readable if its source
later fails validation; verification reports that source inconsistency explicitly.
The hashes are consistency checks, not authenticity guarantees against a database writer.

Missing runs/measurements return 404, absent/inconsistent evidence or a conflicting
record returns 409, invalid request data returns 422, and database failures return a
safe 503. Older API contracts must be regenerated for the new routes. Live ADO,
PostgreSQL runtime and full scoring-v2/assessment acceptance remain separate work.

SQLite and synthetic checks are local evidence. PostgreSQL locking, Docker images,
live ADO, Entra and production deployment require separate verification.
