# Changelog

## 0.1.0 (implementation in progress)

- Added an authenticated, offline provider demo that reads the four fictional
  source packs and shows the evidence and limits for each. Added PostgreSQL
  audit-column support for immutable measurement IDs.
- Repaired the offline UI smoke check after the package rename and kept the
  historical baseline screenshots.

- Added opt-in post-collection test-pass-rate diagnostics from verified original
  snapshots, using explicit minimum samples and the versioned ADO reported-total
  population. Preserve scoped inventories, observation cutoffs and evidence gaps.
- Store immutable diagnostic results with frozen definitions and version-aware
  verification; add authenticated retrieval/JSON export, transactional request audit
  and deletion/cleanup protection via an independent additive 0008 migration.
  Collection, legacy scoring and the data-only `synthetic_data/` packs are unchanged.

- Added a shared ADO/synthetic saved-connection registry and authenticated provider
  metadata, with consistent unknown-provider rejection and diagnostic dispatch.
- Persist original provider snapshots for database-backed CLI runs as well as
  workers, with immutable run-evidence links, checked content addresses and linked
  evidence deletion/retention guards. Register additive OIDC schema tables without
  enabling OIDC routes. Historical links are not backfilled.
- Corrected `migrate --check` to inspect without applying migrations: exit 0 current,
  2 pending and 1 on error. Added API-schema drift coverage and regenerated contracts.
- Preserve linked run metadata/reports on a rejected standalone retry, and reject
  malformed SQLite URI checks without creating files. Use SQLAlchemy-compatible URI
  booleans and redact provider diagnostic logs.

- Restricted remote run requests to server-owned output storage and internal
  execution controls; added a shared export-root setting, absolute stored paths
  and attempt-relative downloads verified through a synthetic saved-connection
  API/worker journey.
- Added bounded transient database recovery and graceful SIGTERM handling for
  workers, preserving lease expiry as the fallback when release cannot reach storage.
  Recovery covers polling/claiming; failed final saves return safe error text and
  require resubmission.
- Added an isolated, provider-neutral foundation for test pass rate and merged-PR
  review coverage with explicit uncertainty and candidate inventories. It is not
  connected to production scoring.

- Include backend/frontend tests, their configuration/helpers and selected product
  documentation in source archives. Packaging-only change; application version unchanged.
- Put plain project files and `.gitignore` directly at the source ZIP root, retain
  the bounded metric catalogue and reject bundled wheels/builds/runtimes. Preserve
  existing ZIP/checksum files and write a companion SHA256 checksum.
- Include the four fictional `synthetic_data/` provider JSON packs and their README
  in source archives without changing data, application behaviour or version.

- Preserve unavailable test counts in exports, document missing coverage denominators, and align ADO test failure fields, wildcard policy scopes and repository progress.

- Correct ADO completion-window collection, test details, policy scope and coverage projection; retain uncertainty and scope provenance. Failed assessments retain evidence without maturity reports.

- Updated the README for engineering-practices scope, provider availability and
  sanitised examples. Declared HTTP test-client and OIDC dependencies required
  to collect the existing test suite in a fresh environment.

- Began the independent migration to Plumbline through the Phase 2c target.
- Removed Grafana dashboards and provisioning. Development Compose uses plain
  PostgreSQL in a separate volume; existing database volumes remain untouched.
- Excluded DO-NOT-RUN reference material from Git and Docker contexts.
- Routed collect-raw through the shared full-window runner, with UUID-owned output
  folders, effective run metadata and explicit rejection of removed options.
- Guarded mutations and run-owned downloads, corrected exemption updates and
  removed obsolete validation, leaderboard and database-clear routes. Repaired
  the interim UI with system fonts, explicit errors and API-key transport.
- Preserved independent collection evidence on enrichment failure, added explicit
  completeness metadata and blocked interim scoring of incomplete collections.
  Corrected ADO build linkage, commit paging and bounded request retries.
- Added selected-project lists as one consolidated run, with matching repeated
  CLI --projects options and explicit partial-collection exit behaviour.
- Removed remaining segment/validator implementations and company-name filtering;
  retained destination guards. Updated workbook comparison and merged file-only
  runs into database-backed listings. Corrected the PostgreSQL conflict predicate.
- Corrected policy exports, per-project labels and maturity formatting; exports
  preserve missing scores and disclose collection quality.
- Added an isolated full-suite test script, removed source coverage omissions and
  aligned the Python floor to 3.11, CI to 3.11/3.12 and Docker to 3.12.

- Renamed the package, CLI and image to plumbline, with PLUMBLINE_ application
  configuration and root-once stderr logging. Legacy imports and CLI are removed.

- Added packaged SQLite/PostgreSQL migrations with serialised bootstrap, UUID
  run identity and idempotent snapshots; unsupported legacy schemas are refused.
- Added provider evidence contracts, deterministic synthetic data and doctor
  diagnostics without stored responses.
- Added local accounts, session/CSRF and token access, encrypted saved connections,
  the React account and run controls, and leased worker execution with bounded
  recovery and repository progress. Saved connections can be selected for runs.
- Added immutable provider evidence snapshots with conservative field checkpoints.
  ADO and synthetic worker runs now persist the fetched provider snapshot.
  Collection still fetches full windows; incremental reconstruction remains
  outstanding. ADO project policies are projected to the existing legacy format;
  incomplete or malformed evidence and identity collisions block legacy scoring.
  Configured exemptions are applied and load failures fail the run. Security scan
  detection retains the existing artifact-name heuristic.

The new React UI, accounts, worker and Phase 2 features remain in progress.
