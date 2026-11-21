# Engineering-practice evidence: provider research and open decisions

Research date: 16 November 2026. Scope: GitHub.com, GitLab.com and Bitbucket
Cloud public documentation. Self-hosted provider editions are outside this pass.

This supplements the [API architecture](provider-api-architecture.md), including
its authentication, endpoint, permission, pagination and retention references.
It describes available evidence and its limits, not integration instructions.
GitHub, GitLab and Bitbucket adapters remain planned. Public documentation is not
proof of access or behaviour in a particular account. No authenticated provider
requests, sample-data extraction, application execution or reference-code access
were performed for this research.

Vendor facts below have linked primary sources. Statements labelled “Research
conclusion” are interpretations. Proposed assessment definitions and acceptance
evidence are discussion material, not newly approved product rules or implemented
features. The [existing assessment decision](adr/0002-evidence-and-assessments.md)
remains the target.

## 1. Which practices can the APIs describe?

“Available” here means a documented public surface, subject to access, product
features and retained evidence. It does not mean Plumbline currently collects it.

| Practice | GitHub | GitLab | Bitbucket Cloud | Boundary for comparison |
| --- | --- | --- | --- | --- |
| Review participation and approvals | PRs and submitted reviews [G1](https://docs.github.com/en/rest/pulls/reviews) | MRs, approvers and rule-specific approval state [L1](https://docs.gitlab.com/api/merge_request_approvals/) | PR participants and approval/change-request activity [B1](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-pullrequests/) | Requested reviewers, actual approval and satisfied merge requirements are different facts. |
| Production-branch controls | Active rules for a named branch plus classic protection [G2](https://docs.github.com/en/rest/repos/rules), [G3](https://docs.github.com/en/rest/branches/branch-protection) | Protected branches plus MR approval requirements [L1](https://docs.gitlab.com/api/merge_request_approvals/), [L2](https://docs.gitlab.com/api/protected_branches/) | Branch restrictions and merge checks [B2](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-branch-restrictions/), [B3](https://support.atlassian.com/bitbucket-cloud/docs/configure-a-projects-branch-restrictions) | Branch name, scope, enforcement, exceptions and plan matter. A configured recommendation is not an enforced gate. |
| CI results and elapsed time | Actions runs/attempts and check runs [G4](https://docs.github.com/en/rest/actions/workflow-runs), [G5](https://docs.github.com/en/rest/checks/runs) | Pipelines and jobs, including retry/child distinctions [L3](https://docs.gitlab.com/api/pipelines/), [L4](https://docs.gitlab.com/api/jobs/) | Pipelines and steps [B4](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-pipelines/) | A check, job, attempt and pipeline are different units. External CI may publish only statuses. |
| Test counts | Published report artefacts or producer-specific check output [G5](https://docs.github.com/en/rest/checks/runs), [G6](https://docs.github.com/en/rest/actions/artifacts) | Pipeline test report and summary; published JUnit evidence [L3](https://docs.gitlab.com/api/pipelines/), [L5](https://docs.gitlab.com/ci/testing/unit_test_reports/) | Step test-report summary and test cases [B4](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-pipelines/) | A green pipeline is not a test count. These selected GitHub surfaces do not define universal passed/total counters. |
| Code coverage | Published artefacts/check output, with producer-specific meaning [G5](https://docs.github.com/en/rest/checks/runs), [G6](https://docs.github.com/en/rest/actions/artifacts) | Coverage reporting and coverage visualisation use separate mechanisms [L6](https://docs.gitlab.com/ci/testing/code_coverage/) | Commit reports can carry COVERAGE data [B5](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-reports/) | A percentage without its population cannot safely be combined with another percentage. Publishing is a prerequisite. |
| Security hygiene | Code-scanning analyses/alerts, subject to feature access [G7](https://docs.github.com/en/rest/code-scanning/code-scanning) | Security/vulnerability surfaces with endpoint-specific tier restrictions [L7](https://docs.gitlab.com/api/vulnerabilities/) | SECURITY commit reports and annotations [B5](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-reports/) | Configuration, a completed analysis and findings are separate. A report title or empty alerts list does not establish a successful scan. |
| Change flow and size | PR timestamps and change details [G8](https://docs.github.com/en/rest/pulls/pulls) | MR timestamps and change details [L8](https://docs.gitlab.com/api/merge_requests/) | PR activity and diff statistics [B1](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-pullrequests/) | Review-stage timing, size and work-item linkage need explicit definitions; current state is not a complete historic timeline. |
| Delivery outcomes | Deployment-related surfaces in the architecture | Deployment-related surfaces in the architecture | Deployment-related surfaces in the architecture | Production designation, incident attribution and restoration are additional evidence; see section 7. |

Source keys link to the exact documentation in section 9. No universal
cross-provider maturity threshold follows from these APIs.

## 2. Provider findings that change interpretation

### GitHub

Submitted reviews carry identity, state and commit context. Rules may dismiss
stale approvals or require approval from someone other than the last pusher.
Research conclusion: an old approval cannot automatically be counted as approval
of the version that merged. Review occurrence, approval of a revision and
compliance with a merge rule need separate definitions. [G1](https://docs.github.com/en/rest/pulls/reviews), [G9](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets)

The branch-rules endpoint returns applicable active rules across configuration
levels; it excludes evaluate/disabled rulesets. Classic branch protection has its
own endpoint and requires Administration read permission. Research conclusion:
absence from one surface does not establish absence of every branch control, and
a ruleset inventory alone does not establish effective protection. [G2](https://docs.github.com/en/rest/repos/rules), [G3](https://docs.github.com/en/rest/branches/branch-protection)

Actions exposes a specific run-attempt resource. Check runs have their own
identity and outcome. Selected workflow-run filters have a documented 1,000-result
search ceiling, so following page links alone does not prove complete history.
Research conclusion: reruns and search ceilings affect CI denominators independently
of throttling. [G4](https://docs.github.com/en/rest/actions/workflow-runs), [G5](https://docs.github.com/en/rest/checks/runs)

Test and coverage contents depend on what the workflow or reporting producer
publishes. Artefact metadata and download availability are separate. A green check
or an artefact called “coverage” is insufficient evidence of a particular
numerator, denominator or scanner result. [G5](https://docs.github.com/en/rest/checks/runs), [G6](https://docs.github.com/en/rest/actions/artifacts)

### GitLab

The approvals response lists approvers whether or not they satisfy a rule.
The documented Enterprise Edition semantics can report approved when no approval
rules apply and nobody has approved; Community Edition semantics differ. The
rule-specific approval-state resource answers a different question. Some approval
endpoints are Free; others require Premium/Ultimate. Research conclusion: neither
an `approved` Boolean nor a blanket tier assumption is a portable review metric.
Approval resets and author/committer eligibility also matter. [L1](https://docs.gitlab.com/api/merge_request_approvals/)

Pipeline listings distinguish sources, and child pipelines are not included by
default. Jobs-by-pipeline excludes retried jobs unless requested. Research
conclusion: a “latest pipeline” or default jobs response cannot establish the
number of attempts or all work in a parent/child pipeline estate. [L3](https://docs.gitlab.com/api/pipelines/), [L4](https://docs.gitlab.com/api/jobs/)

JUnit reports do not determine job status by themselves. GitLab documents report
size limits and duplicate-name handling. Coverage reporting and visualisation
read different inputs and do not configure each other. Research conclusion:
successful ingestion is not proof that all tests were represented, and a coverage
value is not evidence that line-level coverage is available. [L5](https://docs.gitlab.com/ci/testing/unit_test_reports/), [L6](https://docs.gitlab.com/ci/testing/code_coverage/)

MR APIs expose creation, update and merge timestamps and distinct filters.
Research conclusion: “changed since” and “merged during” are different populations;
a creation-only window loses older MRs merged in the period. Project-scoped MR
IIDs remain distinct from global identifiers. [L8](https://docs.gitlab.com/api/merge_requests/)

### Bitbucket Cloud

PR activity includes comments, updates, approvals and requests for changes.
An assigned/default reviewer is not the same as an approval. Research conclusion:
current participant state alone cannot prove which approvals applied at a past
cutoff. [B1](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-pullrequests/)

Branch restrictions and merge checks are related but distinct. Atlassian documents
recommended checks that can remain unresolved at merge, while enforcement and
some additional controls depend on Premium. Research conclusion: a configured
minimum approval count is not enough to claim that merges were prevented. [B2](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-branch-restrictions/), [B3](https://support.atlassian.com/bitbucket-cloud/docs/configure-a-projects-branch-restrictions)

Native test reporting is scoped to a pipeline step, with summary and test-case
resources. Commit reports expose report type, result and producer-defined data,
including TEST, COVERAGE and SECURITY. Research conclusion: step reports need
attempt and identity context; a Code Insights report is a producer assertion,
not a universal scanner or coverage schema. [B4](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-pipelines/), [B5](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-reports/)

The current REST introduction documents personal API tokens with Basic
authentication using the Atlassian email and token. The public changelog also
explicitly supports Bearer API-token authentication. App passwords are deprecated.
Research conclusion: Basic is a documented option, not the only supported API-token
scheme. Personal API tokens, resource access tokens and OAuth grants must not be
conflated. Their scope names, lifetimes and quota attribution differ. [B6](https://developer.atlassian.com/cloud/bitbucket/rest/), [B7](https://developer.atlassian.com/cloud/bitbucket/changelog/)

## 3. History, limits and missing evidence

The architecture already records published rate and artefact-retention limits.
The additional conclusions are:

| Question | Publicly supported answer | Still requires verification |
| --- | --- | --- |
| Does pagination prove completeness? | No. GitHub has filtered-run search ceilings; GitLab keyset support is resource/order-specific; Bitbucket next links and totals vary. [G4](https://docs.github.com/en/rest/actions/workflow-runs), [L9](https://docs.gitlab.com/api/rest/), [B6](https://developer.atlassian.com/cloud/bitbucket/rest/) | Multi-page behaviour, selected date boundaries and accessible inventory in the target account. |
| Does a larger account guarantee more requests? | No universal guarantee. GitHub quotas depend on authentication and secondary limits. Bitbucket scaled limits have plan, paid-user and credential eligibility. [G10](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api), [B8](https://support.atlassian.com/bitbucket-cloud/docs/api-request-limits/) | Shared consumers, actual headers, endpoint groups and sustained throughput. |
| Can a past assessment be rebuilt from current APIs? | Not established. Current rules, approvals and retained report contents do not establish all past states. | Whether saved evidence covers the requested cutoff and later changes. |
| Does an empty response mean no practice? | Not by itself. Permissions, feature availability, publication and retention are separate conditions. | Expected inventory, scope grants and deliberately empty cases. |
| Is documented schema coverage live parity? | No. Documentation examples describe contracts, not every omission/null/permission combination. | Sanitised, authorised account observations, particularly zero-valued counters, retries and expired reports. |

Research conclusion: evidence has at least three times: the source event, the
observation, and the assessment cutoff. A retrieval timestamp cannot substitute
for the event time. A pipeline completed after a cutoff cannot be counted as
completed before it. A PR opened before a window may still be relevant when merged
inside it. Unknown history needs an explicit status rather than an invented past.
These are interpretation requirements, not claims of historical API support.

## 4. Metric catalogue questions public APIs cannot settle

The agreed practice families remain Code Review, Change Control, CI Health,
Testing, Security Hygiene, and Flow & Traceability. Each metric definition needs:
identity/version, purpose, eligible population, event/window, numerator and
denominator or duration endpoints, unit, minimum sample, evidence states, source
capabilities, exclusions, threshold rationale, roll-up and trend compatibility.
No researched vendor source supplies a universal maturity scale.

The following are candidate definitions/questions for the existing Phase 2
catalogue, not replacements for approved semantics:

| Metric family | Definition requiring agreement | Evidence/uncertainty to expose |
| --- | --- | --- |
| Review coverage | Eligible merged changes with a qualifying non-author approval / eligible merged changes; settle revision validity, bots and bypasses. | Actual approval versus rule compliance; unknown historical review state. |
| First-review time and PR stages | Creation or ready-for-review to first qualifying human review; settle drafts, comments, approvals and working/calendar time. | Unreviewed changes are censored observations, not zero-duration reviews. |
| PR size | Additions/deletions or changed files for a specified revision; settle generated, binary and vendor files. | Truncated/absent diff evidence and source differences. |
| Change control | Named requirements on the configured production branches, assessed at a specified time. | Exact scope, effective enforcement, bypasses and unreadable controls. |
| CI success/duration/recovery | Define production pipeline population, attempt versus logical run, completed statuses and what ends a failure episode. | Running, skipped, cancelled, allowed failures and external CI. |
| Tests reported/pass rate | Separate publication from passed / eligible reported tests; decide skipped, retried and duplicate tests. | No report, observed empty, malformed counts and inaccessible report are distinct. |
| Coverage | Name the line/branch/function measure and eligible revision/population; settle report aggregation. | Missing denominator is unavailable percentage, not 0% or 100%. |
| Scanning present | Specify configured tool, observed execution or successful analysis as separate claims. | Scanner family, revision, freshness, analysis status; zero findings is not proof of execution. |
| Branch-to-merge flow, naming and hotfix share | Define branch prefixes, first event, eligible merged changes and classification precedence. | Deleted/renamed branches, squashes and unclassified changes. Prefix conventions alone do not establish an incident. |
| Work-item traceability | Define acceptable link type/system and eligible changes. | A matching text pattern is a claimed link until its target/relationship is verified. |

Illustrative arithmetic, not benchmark thresholds: a known 8 passed out of 10
eligible tests is 80%. If only two of three eligible repositories have measurable
evidence, measurement coverage is 2/3, separately from the result. If the third
repository is hidden from the inventory, even that coverage denominator is unknown.

The settled hierarchy averages repository results to Team and then BU. Retain
underlying samples and coverage because averaging percentages answers a different
question from pooling events: repositories with 1/1 and 1/9 successes have an equal
repository mean of 55.56%, while pooled event success is 2/10 = 20%. Neither figure
is interchangeable with the other. Exemption and unavailable-state treatment in
those roll-ups still needs an explicit product decision.

Research conclusion: trends require compatible definitions, windows, population
and ownership context. Otherwise a changed score may describe a changed measuring
rule or repository mix rather than an improvement.

## 5. Reproducibility and organisation

The accepted [assessment ADR](adr/0002-evidence-and-assessments.md) already requires
frozen evidence, rules, organisation and exemptions. Research conclusion: vendor
snapshots alone cannot fulfil that decision because scores also depend on local
meaning and population.

A reviewable assessment specification still needs to identify:

- The exact evidence set, scoped identities, selection window, cutoff, observation
  times, missing scopes and retained provenance.
- Catalogue/calculation version, effective thresholds, minimum samples, production
  branches and classification rules.
- Effective BU/Team/Repository mapping and exemptions, including dates and reasons.
- Per-metric state, sample, numerator/denominator, coverage, result and explanation.
- Comparison compatibility and fingerprints with a defined representation.
  A matching hash alone does not prove that the selected evidence was sufficient.

Acceptance evidence: replay after repository rename, archival, ownership change,
threshold change and exemption expiry retains the saved result. New assessments
may use new context; comparisons disclose it. This is a target proof, not a test
performed for this document.

Organisation questions remain product-specific: mapping authority and overrides,
unassigned repositories, overlapping ownership, effective dates, transfers and
whether a repository can belong to multiple teams for reporting. Provider groups,
workspaces and projects are access/container structures; treating them as the
business hierarchy is an unverified organisational assumption. No segments are
reintroduced.

## 6. Report access, privacy, operations and pilot evidence

OWASP recommends default denial and permission checks on every request. This does
not select Plumbline's report audience: instance-wide Viewer access versus scoped
report access remains an explicit product decision. It must cover UI, API,
downloads and retained assessments consistently. Provider token revocation does not
retract stored or exported copies. [O1](https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html)

The data-lifecycle specification still needs purposes, recipients, retention,
expiry/deletion behaviour, audit retention and backup disposition for identities,
review text, raw evidence, assessments and exports. Sanitisation must consider
linkable IDs, URLs, branch/commit text, timestamps and small populations as well as
names/emails. Removing obvious PII is not proof of anonymity. No sample dataset
was collected or anonymised by this research.

The existing [operations document](operations.md) covers migration and encryption
key handling. Public database documentation confirms that SQLite has a consistent
online-backup mechanism and PostgreSQL has distinct logical, filesystem and
continuous-archiving approaches. It does not prove that Plumbline's database,
artefacts and encryption key can be restored together. Supported deployment
versions, acceptable data loss, recovery time, upgrade/rollback boundaries and a
measured restore remain operational acceptance gaps. [O2](https://sqlite.org/backup.html), [O3](https://www.postgresql.org/docs/current/backup.html)

W3C describes reflow at an equivalent width of 320 CSS pixels, including the
1280-pixel/400% zoom example and exceptions for genuinely two-dimensional tables.
Automated axe results and screenshots do not establish full WCAG conformance.
Keyboard completion, screen-reader interpretation and actual zoom behaviour of
the complete assessment/evidence journey remain separate acceptance evidence. [O4](https://www.w3.org/WAI/WCAG22/Understanding/reflow.html)

Proposed pilot evidence, with outcomes recorded rather than implied by a green suite:

| Journey | Evidence needed before claiming it works |
| --- | --- |
| Saved connection → queued job → worker → report | Authorised ADO collection with known expected inventory; restart/cancellation; retrievable files belonging to that attempt; absence of credentials in outputs/logs. |
| Finding → repository evidence → correction | A team can explain one finding, identify uncertainty and verify a deliberate improvement on a later comparable assessment. |
| Partial provider failure | Healthy evidence survives; affected metrics and denominator gaps are visible; no invented zero or complete label. This is a target beyond today's conservative whole-assessment block. |
| Replay and comparison | Frozen result survives context changes; changed population/rules are disclosed in comparison. |
| Access and recovery | Intended roles can/cannot retrieve reports as specified; restore recovers evidence, artefacts and usable matching credentials. |

Public sources cannot produce pilot feedback, prove PostgreSQL worker locking or
resolve the existing attempt-folder download integration gap. Those require
separate authorised runtime work. No runtime acceptance is claimed here.

## 7. Delivery outcomes and the wider product claim

DORA's current model has five metrics: change lead time, deployment frequency,
failed deployment recovery time, change fail rate and deployment rework rate.
The latter concerns unplanned deployments arising from production incidents.
DORA recommends application/service context and cautions against disparate
comparisons and competitive targets. The existing later-phase four-metric plan
therefore needs a deliberate scope/version decision; this research does not
silently expand Phase 4 or equate engineering-practice scores with DORA. [O5](https://dora.dev/guides/dora-metrics/)

Required evidence remains production designation, change-to-deployment linkage,
failure/intervention, restoration and incident-related rework. CI success, branch
prefixes and a deployment record do not jointly guarantee all those facts.

SPACE research also rejects a single metric or activity dimension as a complete
measure of developer productivity. Research conclusion: Plumbline can substantiate
engineering-practice observations and help teams investigate improvements; SCM
activity alone cannot prove developer productivity, wellbeing or business impact.
The agreed product direction does not require individual rankings. [O6](https://www.microsoft.com/en-us/research/publication/the-space-of-developer-productivity-theres-more-to-it-than-you-think/)

## 8. What this research closes and leaves open

| Provider | Public documentation | New fixture/contract execution in this research | Authenticated live verification |
| --- | --- | --- | --- |
| GitHub.com | Reviewed; sources G1–G10 | None | Not performed |
| GitLab.com | Reviewed; sources L1–L9 | None | Not performed |
| Bitbucket Cloud | Reviewed; sources B1–B8 | None | Not performed |

A future response-contract evidence pack still needs source/version-labelled
examples for missing versus null versus zero counts, malformed items, empty and
inaccessible resources, multiple pages, retry attempts, old changes completed
in-window, effective controls and expired reports. Public examples, invented
edge cases and sanitised live observations must be labelled separately. This
research identifies those cases; it does not supply or validate live fixtures.


Closed for documentation: provider capability/evidence differences, review versus
rule semantics, CI retry/child/attempt distinctions, reporting dependencies,
history and quota caveats, and primary references for broader readiness criteria.

Still open: live account permissions/plans and inventories; zero/null response
behaviour; real retention and collection cost; catalogue thresholds and edge-case
choices; immutable-assessment specification; mapping authority; report audience and
lifecycle; pilot outcomes; supported-runtime/restore/accessibility proof. Public
research narrows these questions but cannot answer them on behalf of the adopter.

Recommended dependency order is unchanged: a verifiable ADO pilot and report
journey, explicit metric meaning and replay, ownership/evidence workflow, then
provider expansion and release assurance. The private `task.md` remains the
completion/backlog record. This is not a claim of Phase 2c completion.

## 9. Primary sources

All links below were consulted on 16 November 2026. They are living documentation;
record edition/API version again when validating an actual integration.

| Key | Official source |
| --- | --- |
| G1 | [GitHub PR reviews](https://docs.github.com/en/rest/pulls/reviews) |
| G2 | [GitHub rules for a branch](https://docs.github.com/en/rest/repos/rules) |
| G3 | [GitHub classic branch protection](https://docs.github.com/en/rest/branches/branch-protection) |
| G4 | [GitHub workflow runs and attempts](https://docs.github.com/en/rest/actions/workflow-runs) |
| G5 | [GitHub check runs](https://docs.github.com/en/rest/checks/runs) |
| G6 | [GitHub Actions artefacts](https://docs.github.com/en/rest/actions/artifacts) |
| G7 | [GitHub code scanning](https://docs.github.com/en/rest/code-scanning/code-scanning) |
| G8 | [GitHub pull requests](https://docs.github.com/en/rest/pulls/pulls) |
| G9 | [GitHub ruleset rule semantics](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets) |
| G10 | [GitHub REST rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api) |
| L1 | [GitLab MR approvals](https://docs.gitlab.com/api/merge_request_approvals/) |
| L2 | [GitLab protected branches](https://docs.gitlab.com/api/protected_branches/) |
| L3 | [GitLab pipelines and test reports](https://docs.gitlab.com/api/pipelines/) |
| L4 | [GitLab jobs and retries](https://docs.gitlab.com/api/jobs/) |
| L5 | [GitLab unit test report behaviour](https://docs.gitlab.com/ci/testing/unit_test_reports/) |
| L6 | [GitLab coverage mechanisms](https://docs.gitlab.com/ci/testing/code_coverage/) |
| L7 | [GitLab vulnerabilities API](https://docs.gitlab.com/api/vulnerabilities/) |
| L8 | [GitLab merge requests](https://docs.gitlab.com/api/merge_requests/) |
| L9 | [GitLab REST pagination](https://docs.gitlab.com/api/rest/) |
| B1 | [Bitbucket pull requests and activity](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-pullrequests/) |
| B2 | [Bitbucket branch restrictions API](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-branch-restrictions/) |
| B3 | [Bitbucket recommended and required merge checks](https://support.atlassian.com/bitbucket-cloud/docs/configure-a-projects-branch-restrictions) |
| B4 | [Bitbucket pipelines, steps and test reports](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-pipelines/) |
| B5 | [Bitbucket commit reports](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-reports/) |
| B6 | [Bitbucket REST authentication and pagination](https://developer.atlassian.com/cloud/bitbucket/rest/) |
| B7 | [Bitbucket authentication changes](https://developer.atlassian.com/cloud/bitbucket/changelog/) |
| B8 | [Bitbucket rate-limit eligibility](https://support.atlassian.com/bitbucket-cloud/docs/api-request-limits/) |
| O1 | [OWASP authorisation guidance](https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html) |
| O2 | [SQLite online backup](https://sqlite.org/backup.html) |
| O3 | [PostgreSQL backup and restore](https://www.postgresql.org/docs/current/backup.html) |
| O4 | [W3C WCAG 2.2 reflow explanation](https://www.w3.org/WAI/WCAG22/Understanding/reflow.html) |
| O5 | [DORA software delivery performance metrics](https://dora.dev/guides/dora-metrics/) |
| O6 | [SPACE original research](https://www.microsoft.com/en-us/research/publication/the-space-of-developer-productivity-theres-more-to-it-than-you-think/) |
