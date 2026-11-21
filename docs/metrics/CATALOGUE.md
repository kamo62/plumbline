# Bounded metric catalogue, version 1

This is a pure calculation foundation for two repository-level metrics. It is not
wired to the collection runner or legacy assessors and does not complete scoring v2
or Phase 2. A separate opt-in test-pass-rate diagnostic now connects stored snapshots
to the strict calculation, immutable diagnostic storage and authenticated JSON API/
export/verification. Review coverage remains calculation-only. Neither path sets
maturity thresholds, scores, aggregate roll-ups, retry or exemptions policy. The
current production scorer retains its existing behaviour.

Each call supplies a `providers.contract.Window` with timezone-aware start, end
and evidence cutoff (`as_of`). The event window is `[start, end)`. It also
supplies an explicit positive minimum sample; no default is a product decision.
Results retain the catalogue version, scoped repository identity, window, minimum
sample, state, numerator, denominator, percentage, candidate coverage and issues.
Percentages are unrounded floating-point values, not maturity scores.

| ID | Population and event | Numerator | Denominator | Unit | Dependencies |
| --- | --- | --- | --- | --- | --- |
| `test_pass_rate` | Explicitly eligible reported test executions in repository-scoped candidate reports whose associated completion is in `[start, end)` | Sum of reported eligible passes | Sum of reported eligible executions, both passed and non-passed | Percent | Exhaustive report candidates and valid passed/eligible counts at the cutoff |
| `review_coverage` | Repository-scoped PRs merged in `[start, end)`, including those opened before start | PRs with at least one identified non-author approval for the relevant revision, valid at merge | All PRs merged in the window | Percent | Exhaustive merge candidates and complete historical, revision-linked approvals at the cutoff |

## Test inputs

The caller supplies `TestCounts(passed, eligible)` per report. `eligible` is
exactly the reported count the caller explicitly includes, and `passed` is its
passed subset. Both must be non-negative integers and `passed <= eligible`.
The caller must establish which reported outcomes are included and provide a
consistent, non-overlapping population. No skipped, retry, duplicate, cancelled or
not-executed policy is inferred here. Counts from supplied reports are summed;
this is not a deduplication or latest-attempt algorithm. There is deliberately no
failed-count dependency: 18 passed and 20 eligible gives 90% even when failed
counts are unavailable. An observed-empty report contributes zero to both sums;
represent it with `state=OBSERVED_EMPTY` and `value=None`. Use `OBSERVED` with
`TestCounts(0, 0)` for an explicitly reported zero-count record.
An empty report inventory is also known zero sample, not a 0% pass rate.

## Review inputs

`Observation.event_at` is the actual merge time, not creation or update time.
An optional `opened_at` records PR opening time for provenance; it is not the
window-selection event, and if supplied it cannot be after the merge or an approval.
An observed PR has its author ID, relevant revision ID and a complete tuple of
approval observations. Each approval has reviewer ID, revision ID, approval time
and `valid_at_merge` (`True`, `False` or unknown). This flag must be established
from sufficient historical evidence, including relevant dismissals or reversals;
current reviewer state alone does not establish it. The caller must establish that
revision IDs are comparable and identify the revision being assessed. Approvals
after merge, known-invalid approvals, self-approvals and approvals of a different
revision do not qualify. If an approval that could affect the result has unknown
time, identity, revision or validity, the result is unavailable. An explicitly
complete empty approval tuple is a known unreviewed PR. The metric counts approval
occurrence, not compliance with branch rules. No human/bot distinction is inferred.

## Inventory, coverage and states

Use the existing provider `Identity` for a connection/project/repository scope,
with no build field. Each candidate `item_id` must be stable and unique within
that scope. Thus the same item ID in another project or repository does not
collide. The caller is responsible for normalising a snapshot into these inputs:
identity strings must use the provider's canonical form with no surrounding
whitespace before author/reviewer or revision comparisons. These comparisons are
exact and do not trim or otherwise canonicalise identities.
The diagnostic's ADO build-test-summary adapter is described below. There is no
snapshot-to-review adapter: raw provider reviewers are not automatically historical
revision-valid approvals. Missing default branches and default archive/activity flags
are not metric dispositions.

`Inventory.expected_ids` is an exhaustive tuple of candidate IDs, or `None` if
the expected inventory is unknown. A candidate may be outside the window, but
must be accounted for so it can be excluded by its event time. An independently
known expected list may remain present when discovery fails; never provide a
partial list as exhaustive. Each expected candidate has at most one observation;
missing candidates remain in the denominator. Duplicate, blank or unexpected
IDs are rejected. An explicit provider `EvidenceState` and observation time are
required for inventory and candidate records. The inventory observation must be
at or after the window end and at or before `as_of`; candidate observations must
be at or before `as_of` and no earlier than the event. A later observation cannot
establish an earlier cutoff in this foundation.

Candidate coverage is **accounted candidates / expected candidates**, not the
metric denominator, a repository score, or an assessment roll-up. An accounted
candidate has a usable in-window observation or a known out-of-window event.
Explicit failed or partial candidate evidence remains a gap even when its supplied
event time lies outside the window: that time is not trusted to dismiss a failed
observation. Only otherwise usable observations are excluded by event time.
`expected=None` means the percentage is unknown, even when every discovered item
was observed. An empty known inventory has no coverage percentage because its
denominator is zero. When an inventory failure is separately reported, its issue
and failed state remain visible even if an independent expected list has 100%
candidate coverage. A failed, partial or missing candidate never disappears from
a known denominator. Coverage alone never establishes metric completeness.

- `measured`: complete observations and sample at least the supplied minimum.
  A measured value of zero is a genuine zero, not missing data.
- `insufficient_sample`: complete observations but fewer eligible tests or merged
  PRs than the supplied minimum, including a known empty population. Its known
  numerator and denominator remain visible; percentage is absent.
- `missing`: at least one expected candidate has no observation.
- `unavailable`: evidence, historical validity, event time or exhaustive inventory
  is unknown, partial, too early or after cutoff.
- `collection_failed`: explicit inventory/item failure or malformed metric data.
  Candidate event/observation timestamps that cannot establish a trustworthy
  cutoff are classified `unavailable`; malformed approval timestamps are
  `collection_failed` because they violate the supplied review-history contract.
- `exempt`, `not_applicable`, `inactive`: only explicit caller dispositions, not
  inferred from an absent default branch, missing reports or an empty population.

Every issue remains in the result. If several issues occur, collection failure
takes precedence over unavailable, which takes precedence over missing. An
incomplete result has no numerator, denominator or value, even if some observations
are usable. Explicit dispositions skip evidence calculation and carry unknown
candidate coverage; a later integration must supply their policy and provenance.

## Integration boundary

Future integration must establish complete scoped discovery, event and observation
times, report eligibility, and review revision/history semantics before producing
these inputs. It must preserve per-scope failure and reconcile known and unknown
inventories without presenting candidate coverage as area/project/BU coverage.
Original snapshots are persisted and content-checked; the diagnostic freezes its
own effective definition and population. Full assessment envelopes, versioned
ownership/rules/exemptions, provider expansion, outcome mapping, roll-ups, thresholds
and live provider verification remain open.
The functions perform no I/O, read no clock, and do not call a provider.

## Stored test-pass-rate diagnostic

This is an explicit measurement of previously stored evidence, not a maturity
assessment or an automatic collection step. It supports the existing ADO-shaped
snapshot format, including the built-in synthetic source. It does not read or adapt
the user's standalone `synthetic_data/` packs, add provider adapters, change the
legacy report or infer historical review validity.

Every request must supply both a positive integer `minimum_sample` and the named
population policy `ado-reported-total/v1`. There is no default. This policy adds the
reported `passed` and `total` executions from build-linked test-run summaries. Every
outcome already included in the reported total stays included. It neither removes
skipped/not-executed outcomes nor deduplicates retries or logical test cases.
This diagnostic population is not a newly selected production-scoring default.

One candidate is a build's linked-report aggregate, not one individual test report.
The adapter derives expected repository scopes and build candidates from enclosing
project inventories, reconciles their detail records and uses full connection,
project and repository identities. Candidate coverage therefore means accounted
build aggregates / expected build aggregates. It is not report-count coverage or
an organisational completeness percentage. `inventory_complete` describes scoped
discovery and reconciliation; even a known complete inventory can have failed or
unavailable test observations. Root scope issues remain visible beside repository
results, without suppressing valid known sibling scopes.

Observation times, evidence cutoff and `fresh_as_of=False` remain binding. Missing,
partial, failed, unlinked or malformed evidence cannot silently contribute zero.
Counts are checked before trusting an out-of-window exclusion. A known zero test
population retains zero counts with no percentage; 0 passed out of 20 eligible
reported executions is a genuine measured 0% when the supplied minimum allows it.

The frozen document records the original snapshot ID, exact window/cutoff, catalogue
definition, schema/normaliser/calculation versions, population definition, minimum,
scope issues and repository results with numerator, denominator and candidate coverage.
The content-addressed record is insert-only. An identical request returns the same
record; a different minimum can create another record, without rewriting the first.
JSON retrieval/export reads that frozen document, not today's settings or catalogue.

Verification reports document consistency, source snapshot/link consistency and
current-code reproducibility separately. Unsupported recorded versions or definitions
return an unavailable reproducibility result before current-code replay is attempted.
Hashes are unkeyed consistency checks, not proof against deliberate database rewriting.
Full historical engine availability and full assessment replay remain separate work.

See [operations](../operations.md#post-collection-test-pass-rate-diagnostic) for the
authenticated routes, error boundaries and request example.
