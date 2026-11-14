# Sanitised example runs

`metrics-new/` contains four small historical aggregate extracts derived from the
local metrics-new July–October 2026 exports. The originals were read only. Three
projects and nine repositories are represented consistently across four runs.
The manifest records source hashes, source periods, row counts and limitations.

Project/repository/organisation labels, record IDs, run IDs and scope labels are
replaced. Dates are shifted by 1,000 days. Only allowlisted structural metadata,
machine enums and numeric measurements survive; other free text is removed.
No reverse identity map is saved. People, executive summaries, segments, URLs,
credentials, presentation decks and AI-usage data are excluded.

These are aggregate fixtures, not raw API responses or validated Plumbline v2
assessment results. Numeric values and missingness remain from the selected source
records. The subset cannot reproduce original organisation totals, and the data
has not been imported into a running application or used to contact a provider.
Sanitisation reduces identifying content; exact numerical histories can still be
recognisable to somebody with the original reports. Nothing has been published.

| File | Meaning |
| --- | --- |
| `metrics-new/run-01.json` … `run-04.json` | Sanitised repository metric records in chronological order |
| `metrics-new/run-history.json` | Matching example history records with shifted dates |
| `metrics-new/manifest.json` | Provenance, checksums, selection and coverage limits |
| `run-requests.json` | Example request bodies for existing collection options; placeholder connection IDs are not usable credentials |

The request templates and historical exports are separate examples. A collection
request does not replay the historical fixtures. Synthetic requests refer to the
existing diagnostic dataset, which deliberately has incomplete evidence and does
not produce a maturity assessment.
