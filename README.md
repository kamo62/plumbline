# Plumbline

Plumbline is an open-source, self-hosted engineering practice tool for Azure
DevOps, GitLab, GitHub and Bitbucket Cloud. It brings repository controls, code
review, CI health, testing and security evidence into one place to help teams
understand and improve how they build software.

The application provides a CLI, a FastAPI service, a web UI and JSON/Excel reports,
with SQLite and PostgreSQL storage. The package, CLI and image are named
`plumbline`; application settings use the `PLUMBLINE_` prefix.

## Provider support and project status

| Provider | Current status |
| --- | --- |
| Azure DevOps | Collection implemented; live API verification remains outstanding |
| GitLab | Planned |
| GitHub | Planned |
| Bitbucket Cloud | Planned |
| Synthetic dataset | Available for diagnostics and development; deliberately includes incomplete evidence |

Plumbline is under active development at version 0.1.0. Accounts, saved connections,
worker execution and the new UI are implemented foundations with integration and
verification work still open. Organisation mapping, scoring v2 and immutable
assessments remain planned. Required storage across all entry points is still
being integrated.

Engineering-practice assessments are the current focus. DORA delivery outcomes
require production deployment and incident evidence; build success, pipeline
frequency and collection-run statistics do not establish those outcomes. Scores
are configurable assessment signals, not a certification or a measure of
individual developer productivity.

See the [public provider API architecture](docs/provider-api-architecture.md) for
the evidence available from the planned providers and its limits. Segments and
Grafana are excluded from the current product direction.

## Available capabilities

- Collect repository, pull-request, build, test and branch-policy data from Azure DevOps.
- Assess quality assurance, deployment pipelines, security compliance and collaboration.
- Export JSON and Excel reports and compare two sets of Excel exports.
- Persist runs and metrics to SQLite or PostgreSQL using packaged migrations.
- Browse collection runs through the API and bundled web UI.

## Quick start

Use Python 3.11 or newer. The Python namespace and installed CLI are `plumbline`.

```bash
git clone https://github.com/kamo62/plumbline.git
cd plumbline
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
```

Set your own `PLUMBLINE_AZURE_DEVOPS_PAT` and `PLUMBLINE_AZURE_DEVOPS_ORG` in the ignored `.env`
file before collecting data. Use only organisations and repositories you are
authorised to access. Storage defaults to `sqlite:///plumbline.db`; migrate a fresh
local database before using database-backed features. ADO credentials are required
for collection, not for database migration.

```bash
# Initialise a fresh local SQLite database
plumbline migrate

# Local dashboard and API
python -m uvicorn plumbline.api.main:app --host 127.0.0.1 --port 8000

# Collect reports for one authorised project
plumbline collect-raw --project "YOUR_PROJECT_NAME_HERE" --maturity

# Collect selected projects together in one run
plumbline collect-raw --projects "PROJECT_ONE" --projects "PROJECT_TWO" --maturity
```

For account bootstrap, frontend build and worker operation, follow the
[operations guide](docs/operations.md) and [frontend instructions](frontend/README.md).
The API queues UI runs; a separate worker must be running to process them.

Visit <http://localhost:8000/ui/> for the application. `/docs` provides the
authenticated API reference and `/healthz` provides health status. Reports are
written under the ignored `exports/` directory.

Collection uses the full assessment window. Removed segment, delta, minimal and
execution-profile options are rejected. Each run owns a UUID-named output folder.
Use `plumbline collect-raw --help` for collection options.

## Development storage

For the local Compose stack, supply a fresh `POSTGRES_PASSWORD` in `.env`.
Use a URL-safe database password.

```bash
docker compose up --build -d db api
```

Compose binds the API and database ports to loopback. The stack uses plain
PostgreSQL with a new `plumbline_pgdata` volume and requests local automatic
migrations. Existing database volumes are not migrated or deleted. Inspect the API logs before
assuming migrations have completed. It is not a production deployment setup.

For a database outside Compose, export your own `PLUMBLINE_DATABASE_URL` in the shell before
running the packaged commands:

```bash
plumbline db-status
plumbline migrate
```

Migrations modify the configured database. Review the target and back up important
data first. TimescaleDB is no longer required. Collection still supports transitional
file-only operation; use `--write-db --db-strict` for persisted collection until worker
integration is complete. The removed `--db-reset` option is rejected.

## Compatibility change in 0.1.0

The previous `devops_metrics` import and `devops-metrics` executable are removed.
Update application environment names from `ADO_<setting>` to
`PLUMBLINE_<setting>`. Standalone `DATABASE_URL`, `RUN_ENV`,
`AUTO_RUN_MIGRATIONS`, `MIGRATION_TIMEOUT`, `ORPHAN_CLEANUP_GRACE_SECONDS`,
`EXPORTS_RETENTION_DAYS` and `DISABLE_TIMESCALE_FEATURES` also gain the
`PLUMBLINE_` prefix. Native `POSTGRES_` settings keep their names. See
[.env.example](.env.example) for the current configuration.

Existing database volumes and collected files are not automatically converted.
Packaged migrations refuse legacy or unknown schemas without changing them. Back up
legacy data and select a fresh database; automatic legacy-data conversion is unavailable.
Required-storage enforcement across all entry points remains in progress.

## Provider diagnostics

```bash
plumbline doctor --provider synthetic
plumbline doctor --provider ado --project "YOUR_PROJECT_NAME_HERE" \
  --start 2026-01-01T00:00:00Z --end 2026-02-01T00:00:00Z \
  --as-of 2026-02-02T00:00:00Z
```

Doctor returns capability and evidence-state counts without writing collected
responses. The synthetic example intentionally contains partial and failed evidence
and exits 1. ADO diagnostics contact the configured source and require its credentials;
they cannot reconstruct historical evidence that the source no longer exposes.

## Configuration and data

- Documentation uses `YOUR_..._HERE` placeholders. They are not usable credentials.
- Automated tests use synthetic accounts, tokens, IDs, timestamps and metrics.
  Email examples use the reserved `example.com` domain. Sanitised historical
  aggregate examples are included under [samples/](samples/README.md); raw
  collected reports are excluded.
- Manual organisation mappings arrive in Phase 2. Segment assessment and
  security-group entitlement collection have been removed.
- No projects or metrics are exempted by default. Configure your own exemptions locally
  through `PLUMBLINE_EXEMPTIONS_FILE` or the local UI.
- Environment-file loading is limited to `.env` in the working directory, without
  searching parent folders. Authentication uses the explicitly configured PAT,
  not inherited Azure Pipelines tokens.
- Company names are not blocked. Requests remain scoped to the configured Azure
  DevOps organisation over HTTPS; redirects and traversal are rejected. Use only
  data and destinations you are authorised to access.
- The repository retains source, configuration and approved product documentation.
  Generated reports, spreadsheets and presentations are excluded.
  JSON/Excel generation remains available when you explicitly run a collection.

The API uses local accounts, revocable sessions/tokens, CSRF and Viewer/Admin
checks. Bootstrap and worker setup, encrypted connection keys, backup and rotation
are described in [operations](docs/operations.md). Keep the development service on
loopback. Integrated security review and production deployment checks remain open.
Never commit completed environment files, keys, exports or private organisation data.

## Documentation and examples

- [Operations](docs/operations.md): accounts, worker processes, connection keys and backups.
- [Frontend development](frontend/README.md): build and development commands.
- [Public provider API architecture](docs/provider-api-architecture.md): conceptual evidence diagram and vendor references.
- [Sanitised sample data](samples/README.md): four historical runs with 270 metric records across three projects and nine repositories.
- [Run request templates](samples/run-requests.json): example collection requests with placeholder connection IDs.

Historical samples are aggregate extracts, not raw API responses or validated
Plumbline v2 assessments. Request templates do not replay those samples.

## Tests

```bash
scripts/test-all.sh
```

The script runs the full Python suite, including doctests, from an isolated
temporary directory with synthetic settings. It uses `.venv/bin/python` when
available; set `PYTHON` to select another interpreter. Failures produce a non-zero
exit code. Tests use mocks for remote services; no live collection is needed.

## Licence

[MIT](LICENSE). Copyright Kamogelo Mashishi.
