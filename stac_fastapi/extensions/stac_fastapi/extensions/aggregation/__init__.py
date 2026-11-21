"""Aggregation extension module."""

from .aggregation import AggregationConformanceClasses, AggregationExtension
from .merge import (
    MERGEABLE_STATISTIC_KINDS,
    NON_MERGEABLE_STATISTIC_KINDS,
    MergeReport,
    MergedStatistic,
    NonMergeableStatisticError,
    ShardFailure,
    ShardMergeError,
    ShardPartial,
    StatisticKind,
    StatisticPartial,
    merge_partials,
)

__all__ = [
    "AggregationExtension",
    "AggregationConformanceClasses",
    "MERGEABLE_STATISTIC_KINDS",
    "NON_MERGEABLE_STATISTIC_KINDS",
    "MergeReport",
    "MergedStatistic",
    "NonMergeableStatisticError",
    "ShardFailure",
    "ShardMergeError",
    "ShardPartial",
    "StatisticKind",
    "StatisticPartial",
    "merge_partials",
]
