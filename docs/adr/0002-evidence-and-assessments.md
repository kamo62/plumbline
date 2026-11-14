# ADR 0002: Explicit evidence and immutable assessments

Status: accepted design goal; not yet implemented as the target model.

## Context and decision

An assessment must distinguish measured results from missing, insufficient,
unavailable, exempt, not-applicable, inactive and collection-failed evidence.
Completeness, coverage and provenance should be visible instead of silently
converting missing evidence into a zero or a successful collection.

The target stores immutable assessments with their evidence, effective rules and
organisation and exemption snapshots, input window, as-of time and fingerprints.
Replaying an assessment must not depend on later changes to names, archive status
or settings.

## Consequences

Collection failures and freshness must reach assessment results. Exports should
use the same assessment and expose definitions, statuses, coverage and provenance.
Synthetic expected results and replay checks are needed to validate this design;
existing collection and reports do not by themselves establish those guarantees.
