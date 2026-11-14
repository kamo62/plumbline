# Plumbline

Plumbline is an engineering-practices application. The package, CLI and image are
now named `plumbline` (version 0.1.0). Phase 1 foundations are in progress; remaining
Phase 0 verification gaps are recorded in the local implementation plan.
Phase 2c is the target stopping point, not the current completion status.

An open-source, self-hosted project for understanding software delivery performance
and engineering maturity across repositories, projects and teams. Its focus is
holistic DORA reporting: delivery throughput, stability and the engineering
capabilities that support sustainable improvement, rather than a score for any
single platform.

The application provides a CLI, a FastAPI service and web dashboard, report
generation and a SQLite/PostgreSQL storage foundation. Required storage across
all run paths is still being integrated.

## Reporting scope and current implementation

A useful DORA view connects production deployment frequency, change lead time,
change failure rate and failed-deployment recovery time with their supporting
evidence. Deployment rework and wider reliability context help explain those
outcomes. Quality assurance, pipeline automation, security practices and
collaboration provide complementary maturity signals; they do not replace delivery
outcomes or measure individual developer productivity.

The reporting scope is platform-independent. The currently implemented collector
is **Azure DevOps only**. Other source-control, deployment, incident-management and
observability integrations are not implemented. Describing the broader scope does
not imply that the application already collects or calculates every DORA metric.

The project is under development. Maturity scores are a configurable assessment
model, not a certification. Grafana dashboards have been removed. Collection-run
counts and durations are not validated DORA measurements.
Use synthetic data and disposable credentials while evaluating the application.

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

Visit <http://localhost:8000/ui/> for the dashboard, `/docs` for the API reference,
and `/healthz` for health. Reports are written under the ignored `exports/` directory.

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
  Email examples use the reserved `example.com` domain. No collected reports are bundled.
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
