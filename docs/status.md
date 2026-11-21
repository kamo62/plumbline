# Initial version status

Verified on 21 November 2026. See the [offline demo setup](../README.md#offline-demo).

## Available

- Azure DevOps collection, stored evidence, and JSON and Excel reports. The local
  app also has saved connections, accounts, a worker and a run history.
- The **Synthetic demo** page shows four fictional provider packs. A viewer can
  choose Azure DevOps, GitHub, GitLab or Bitbucket Cloud and inspect repositories,
  the evidence window, example responses and known facts. Azure DevOps shows
  18 of 20 reported tests passing. The GitHub sample leaves its test counts
  unavailable.
- The [test-pass-rate diagnostic](metrics/CATALOGUE.md#stored-test-pass-rate-diagnostic)
  can calculate and verify a frozen result for a stored ADO-shaped snapshot.
  The demo displays the facts supplied by the pack.

## Verification

The [test log](evidence/test-run.txt) records 542 passed, 1 skipped and 85%
source coverage from `scripts/test-all.sh`. The earlier local baseline had a
PostgreSQL DDL failure because measurement audit subjects require 64 characters.
The repaired migration passes the offline DDL check and the SQLite upgrade test.
A disposable PostgreSQL runtime was unavailable locally.

Frontend lint, type checking, five unit tests and the production build passed.
The legacy offline browser smoke passed at 1440×900, 1366×768 and 1280×800.
The built React app opened on a fresh SQLite database. The browser loaded all
four packs, accepted keyboard provider selection, switched between light,
dark and high-contrast themes, and fit a 720-pixel viewport without
page-level horizontal scrolling.

| View | Evidence |
| --- | --- |
| Runs navigation, 1440×900 | [Before](screenshots/initial-version/before-runs-1440x900.png) · [After](screenshots/initial-version/after-runs-1440x900.png) |
| Runs navigation, 1366×768 | [Before](screenshots/initial-version/before-runs-1366x768.png) · [After](screenshots/initial-version/after-runs-1366x768.png) |
| Runs navigation, 1280×800 | [Before](screenshots/initial-version/before-runs-1280x800.png) · [After](screenshots/initial-version/after-runs-1280x800.png) |
| Synthetic demo | [Azure DevOps](screenshots/initial-version/demo-azure-devops-1440x900.png) · [GitHub](screenshots/initial-version/demo-github-1440x900.png) · [GitLab](screenshots/initial-version/demo-gitlab-1366x768.png) · [Bitbucket Cloud](screenshots/initial-version/demo-bitbucket-1280x800.png) |

## Remaining provider work

The sample packs are the evidence for the demo. Live scans were outside this
validation. The reusable SCM collection adapter, full provider replay through
the runner, and resulting four persisted assessments remain planned.
