# Changelog

## 0.1.0 (implementation in progress)

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
  Collection still fetches full windows; incremental reconstruction and ADO
  snapshot integration remain outstanding.

The new React UI, accounts, worker and Phase 2 features remain in progress.
