# ADR 0001: Independent Plumbline direction

Status: accepted target; implementation in progress.

## Context and decision

The project will be independently implemented and renamed Plumbline. The target
package, CLI and image name is `plumbline`, with configuration under `PLUMBLINE_`.
The package, CLI and image rename landed in 0.1.0 with the `PLUMBLINE_`
configuration prefix. Legacy imports and executable names are removed. This decision does not establish registry
availability or trademark clearance.

The target uses required SQLite or PostgreSQL storage, worker execution, named
accounts and a new UI. These are future design decisions, not a claim that the
current checkout implements them. The target removes segments and Grafana; Grafana
and segment implementations have been removed.

Reference text and images are read-only design inputs. Reference material is never
executed or copied into the implementation. Work is independently authored.

## Consequences

The migration needs explicit compatibility and legacy-data decisions, and tests
for the supported storage and execution paths. Describe implemented capabilities
and remaining work separately in user-facing documentation.
