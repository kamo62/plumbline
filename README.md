# Plumbline

Plumbline is an open-source, self-hosted engineering practice tool. It collects
repository controls, code review, CI, testing and security evidence from Azure
DevOps. An offline demo also shows fictional evidence from GitHub, GitLab and
Bitbucket Cloud.

The application provides a CLI, a FastAPI service, a web UI and JSON/Excel reports,
with SQLite and PostgreSQL storage. The package, CLI and image are named
`plumbline`; application settings use the `PLUMBLINE_` prefix.

## Provider support and project status

| Provider | Current status |
| --- | --- |
| Azure DevOps | Collection implemented and offline demo available. Live API verification remains outstanding. |
| GitLab | Offline demo available. Collection planned. |
| GitHub | Offline demo available. Collection planned. |
| Bitbucket Cloud | Offline demo available. Collection planned. |
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
the evidence available from the planned providers and its limits. The
[provider evidence research](docs/provider-evidence-research.md) covers capability
differences, metric definitions and the remaining product and validation questions.
Segments and Grafana are excluded from the current product direction.

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
written under the ignored `exports/` directory by default. Set the same absolute
`PLUMBLINE_EXPORTS_DIR` for API and worker processes started from different
directories. Stored report paths are absolute; API listings and output names are
relative to their run/attempt directory. Existing stored relative paths are not
migrated. Remote requests cannot choose an output path or internal worker controls;
the CLI retains its custom-output option.

A separate [metric foundation](docs/metrics/CATALOGUE.md) defines test pass rate
and merged-PR review coverage with explicit evidence states and candidate coverage.
It is not connected to production scoring, and does not replace the legacy
assessment or establish scoring v2 completion. Test pass rate also has an opt-in
post-collection diagnostic for stored ADO-shaped snapshots: an administrator supplies
an explicit population policy and minimum sample, then retrieves a frozen result,
JSON export and verification. It makes no provider requests and does not consume
the example files in `synthetic_data/`. See the
[diagnostic contract](docs/metrics/CATALOGUE.md#stored-test-pass-rate-diagnostic)
and [operations](docs/operations.md#post-collection-test-pass-rate-diagnostic).

Collection uses the provider snapshot as the collection boundary for ADO and
synthetic runs. The event window is the current calendar year up to collection
start, with an exclusive end. Live snapshots record collection start and
completion; their evidence cutoff is completion and individual observation times
are retained. Explicit historical provider requests retain their requested cutoff;
observations made later cannot establish historical certainty. Builds are selected by completion time in that window, including builds queued earlier; returned completed builds with unknown finish times remain incomplete evidence. The build list uses the server finish-time lower bound; malformed records hidden by that filter cannot be detected. Running builds observed after the window are excluded. Pull request candidates include old open requests and requests closed in the window; a closure at or after the end leaves historical status uncertain and blocks scoring. Legacy exports use
a compatibility projection. Incomplete evidence, malformed policy data and ambiguous project-scoped IDs
block maturity scoring. ADO policy configurations are projected into the existing
repository and branch policy format with applicable scope provenance. Prefix scopes use documented branch-folder matching; ambiguous non-folder prefixes block scoring for the run. Live verification remains outstanding. Coverage without an uncovered-line count has no inferred percentage; collection and report caveats identify affected builds. The legacy scorer still treats that metric as missing. Test failures use the documented `unanalyzedTests` field; absent failed counts stay unknown in exports. Security scan evidence retains the existing
artifact-name heuristic and is not proof that a scan ran successfully. Configured
exemption files must be readable and valid.
Saved worker runs persist the same fetched snapshot inside the lease fence.
Database-backed CLI runs now persist the original sanitised provider snapshot too.
New persisted runs have an immutable run-to-snapshot link, exposed as `snapshot_id`
in run status and listings. Loading a snapshot checks its content hash before
reconstructing evidence. This detects inconsistent stored content, not deliberate
rewriting by someone who controls the database. Existing job snapshot IDs remain
readable; historical run links are not backfilled. File-only CLI runs do not gain
database snapshots or replay guarantees.

Authenticated clients can read `GET /connections/providers` for the supported
ADO/synthetic metadata and connection requirements. Saved-connection validation and
diagnostics share that registry. GitHub, GitLab and Bitbucket remain planned; no
custom provider host or additional credential fields are enabled.

Collection uses the full assessment window. Removed segment, delta, minimal and
execution-profile options are rejected. Each run owns a UUID-named output folder.
Use `plumbline collect-raw --help` for collection options.

## Offline demo

From a source checkout or the source ZIP, install Python and frontend
dependencies using the usual toolchain. Build the UI, initialise a local SQLite
database and start the app:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
(cd frontend && bun install --frozen-lockfile && bun run build)
plumbline migrate
plumbline bootstrap-admin --username demo-admin
python -m uvicorn plumbline.api.main:app --host 127.0.0.1 --port 8000
```

Open <http://localhost:8000/ui/demo>, sign in with the generated password,
then set a new password and sign in again. Select a provider to see the
repositories, evidence window, response inventory and sample facts. The demo
reads the four checked-in [fictional packs](synthetic_data/README) and marks
explicitly unknown values as unavailable. Once dependencies are installed, the
demo needs only the local app and database. It requires no provider credentials,
worker or provider requests.

See the [initial version status](docs/status.md) for the verification evidence
and remaining provider work.

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
- [Synthetic provider examples](synthetic_data/README): four entirely fictional,
  data-only provider JSON packs. They are not live captures, replay adapters or
  implementations of the planned providers.

Historical samples are aggregate extracts, not raw API responses or validated
Plumbline v2 assessments. Request templates do not replay those samples.

## Tests

Source archives include backend/frontend tests, test configuration and helper scripts,
plus operations, OIDC, provider architecture and accepted architecture decisions under
`docs/`, including the bounded metric catalogue. Project files and `.gitignore` sit
directly at the ZIP root, without an enclosing folder or `.git` directory.
Dependencies, bundled wheels/runtimes, compiled binaries, generated reports/builds,
screenshots, Docker files and
agent/task/planning notes remain excluded. The shared `~/Code/package-core.py` reads
tracked working-tree files; explicitly opt in required new files with `--include-untracked`.
Each ZIP has an adjacent SHA256 checksum; existing ZIPs and checksums are not overwritten.
Historical metrics datasets and Storybook assets require the full repository; source
exports retain only the request templates needed by tests, not those datasets.
The separate `synthetic_data/` folder is included with its README and four fictional
provider JSON files. Its original bytes are retained; nothing is wired into the app.

```bash
scripts/test-all.sh
```

The script runs the full Python suite, including doctests, from an isolated
temporary directory with synthetic settings. It uses `.venv/bin/python` when
available; set `PYTHON` to select another interpreter. Failures produce a non-zero
exit code. Tests use mocks for remote services; no live collection is needed.

## Licence

[MIT](LICENSE). Copyright Kamogelo.
