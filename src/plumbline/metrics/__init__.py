"""Bounded metric foundation; not connected to the production assessment runner."""

from .catalogue import CATALOGUE, CATALOGUE_VERSION, MetricDefinition
from .compute import calculate_review_coverage, calculate_test_pass_rate
from .model import (
    Approval, Coverage, Inventory, Issue, MetricResult, Observation, PullRequest,
    ResultState, TestCounts,
)

__all__ = [
    "Approval", "CATALOGUE", "CATALOGUE_VERSION", "Coverage", "Inventory", "Issue",
    "MetricDefinition", "MetricResult", "Observation", "PullRequest", "ResultState",
    "TestCounts", "calculate_review_coverage", "calculate_test_pass_rate",
]
