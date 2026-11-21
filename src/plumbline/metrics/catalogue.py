"""Versioned definitions, not maturity thresholds or scoring policy."""

from dataclasses import dataclass
from types import MappingProxyType

CATALOGUE_VERSION = "1"


@dataclass(frozen=True)
class MetricDefinition:
    metric_id: str
    version: str
    population: str
    event: str
    numerator: str
    denominator: str
    unit: str
    dependency: str


TEST_PASS_RATE = MetricDefinition(
    "test_pass_rate", CATALOGUE_VERSION,
    "Explicitly eligible reported test executions in repository-scoped candidate reports",
    "Report-associated completion time in [start, end)",
    "Sum of explicitly reported eligible passed executions",
    "Sum of explicitly reported eligible executions, including passed and non-passed",
    "percent", "Complete report inventory and counts as of the cutoff",
)

REVIEW_COVERAGE = MetricDefinition(
    "review_coverage", CATALOGUE_VERSION,
    "Repository-scoped pull requests merged in the window",
    "Merge time in [start, end)",
    "Merged pull requests with an identified, non-author approval valid for the relevant revision at merge",
    "Merged pull requests in the window",
    "percent", "Complete merge inventory and revision-linked historical approvals as of the cutoff",
)

CATALOGUE = MappingProxyType({
    TEST_PASS_RATE.metric_id: TEST_PASS_RATE,
    REVIEW_COVERAGE.metric_id: REVIEW_COVERAGE,
})
