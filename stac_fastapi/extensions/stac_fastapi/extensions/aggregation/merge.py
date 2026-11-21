"""Aggregation merge contract for searches fanned out across backend shards.

When an aggregation request is answered by several backend shards, each shard
computes *partial* statistics over its own slice of the result set. Averaging
the per-shard averages would over-weight small shards, so partials are
expressed as mergeable primitives instead: for every requested statistic,
each shard reports, for the *same* query population, the number of samples
with a value (``count``), the sum of those values (``sum``) and the number of
samples without a value (``missing``). The correct global average is then
``sum / count`` over all shards, and ``count / (count + missing)`` describes
the coverage of the statistic over the whole query population.

Contract:

- **Same query population.** All partials merged together must be computed
  for the same request. Request parsing and collection filtering keep going
  through the existing extension (the ``AggregationExtension`` request
  models); the same request, including any ``collections`` filter, is fanned
  out to every shard unchanged. Never merge partials of different requests.
- **Zero matches.** A shard with no matching items reports zero samples
  (``count=0, sum=0, missing=0``) for every requested statistic, e.g. via
  :meth:`ShardPartial.empty`. Zero-sample partials merge cleanly.
- **Failures are not empty shards.** A shard that failed to answer is
  reported as :class:`ShardFailure`, never as a zero-sample partial. Failures
  are listed in the :class:`MergeReport` (which is then not ``complete``) and
  abort the merge when ``strict=True``.
- **Mergeable statistics only.** Only statistics derivable from
  ``(count, sum, missing)`` partials can be merged: ``count``, ``sum`` and
  ``avg`` (see :class:`StatisticKind`). Statistics such as ``min``, ``max``,
  ``median``, percentiles, cardinality or histograms cannot be derived from
  these partials and are rejected with :class:`NonMergeableStatisticError`.
"""

from collections.abc import Iterable, Mapping
from enum import StrEnum

import attr
from attrs.validators import ge, instance_of


class StatisticKind(StrEnum):
    """Statistics that can be merged from ``(count, sum, missing)`` partials."""

    COUNT = "count"
    SUM = "sum"
    AVG = "avg"


MERGEABLE_STATISTIC_KINDS: frozenset[str] = frozenset(
    kind.value for kind in StatisticKind
)
"""Statistic kinds that can be merged from ``(count, sum, missing)`` partials."""

NON_MERGEABLE_STATISTIC_KINDS: frozenset[str] = frozenset(
    {"min", "max", "median", "percentile", "cardinality", "histogram"}
)
"""Statistic kinds this contract cannot merge.

These cannot be derived from ``(count, sum, missing)`` partials; supporting
them requires different partials (e.g. sketches) and a different contract.
"""


class NonMergeableStatisticError(ValueError):
    """Raised when a statistic cannot be merged from ``(count, sum, missing)``."""


class ShardMergeError(Exception):
    """Raised when shard results cannot be merged into a consistent answer."""


def _to_kind(value: str | StatisticKind) -> StatisticKind:
    try:
        return StatisticKind(value)
    except ValueError:
        raise NonMergeableStatisticError(
            f"Statistic {value!r} cannot be merged from (count, sum, missing) "
            f"partials; mergeable kinds are {sorted(MERGEABLE_STATISTIC_KINDS)}."
        ) from None


@attr.s(frozen=True)
class StatisticPartial:
    """Partial result for one statistic on one shard.

    Attributes:
        kind: statistic kind, one of :class:`StatisticKind`
        count: number of matching items on this shard with a value
        sum: sum of the values over those items
        missing: number of matching items on this shard without a value
    """

    kind: StatisticKind = attr.ib(converter=_to_kind)
    count: int = attr.ib(validator=[instance_of(int), ge(0)])
    sum: float = attr.ib(converter=float)
    missing: int = attr.ib(validator=[instance_of(int), ge(0)])


@attr.s(frozen=True)
class ShardPartial:
    """Successful partial answer of one shard for one fanned-out request.

    Attributes:
        shard_id: identifier of the shard, unique within one merge
        statistics: partials keyed by statistic name; every shard must report
            every requested statistic, using zero samples when nothing matched
    """

    shard_id: str = attr.ib()
    statistics: dict[str, StatisticPartial] = attr.ib()

    @classmethod
    def empty(
        cls,
        shard_id: str,
        statistics: Mapping[str, str | StatisticKind] | None = None,
    ) -> "ShardPartial":
        """Build a zero-sample partial for a shard with no matching items.

        Args:
            shard_id: identifier of the shard
            statistics: requested statistics as ``{name: kind}``; each one is
                reported with ``count=0, sum=0, missing=0``

        Returns:
            a successful :class:`ShardPartial` contributing zero samples
        """
        return cls(
            shard_id=shard_id,
            statistics={
                name: StatisticPartial(kind=kind, count=0, sum=0.0, missing=0)
                for name, kind in (statistics or {}).items()
            },
        )


@attr.s(frozen=True)
class ShardFailure:
    """A shard that failed to answer; never masquerades as an empty shard.

    Attributes:
        shard_id: identifier of the failed shard
        reason: optional human-readable failure reason
    """

    shard_id: str = attr.ib()
    reason: str | None = attr.ib(default=None)


@attr.s(frozen=True)
class MergedStatistic:
    """Global statistic merged from the partials of all successful shards.

    Attributes:
        name: statistic name
        kind: statistic kind
        count: total number of samples with a value
        sum: total sum of the values
        missing: total number of samples without a value
    """

    name: str = attr.ib()
    kind: StatisticKind = attr.ib()
    count: int = attr.ib()
    sum: float = attr.ib()
    missing: int = attr.ib()

    @property
    def population(self) -> int:
        """Total number of matching items the statistic was computed over."""
        return self.count + self.missing

    @property
    def average(self) -> float | None:
        """Global average ``sum / count``; None when no sample has a value."""
        return self.sum / self.count if self.count else None

    @property
    def coverage(self) -> float | None:
        """Share of the query population with a value.

        None when the query population is empty (no matches on any shard).
        """
        return self.count / self.population if self.population else None

    @property
    def value(self) -> float | int | None:
        """Headline value for the statistic kind (count, sum or average)."""
        if self.kind is StatisticKind.COUNT:
            return self.count
        if self.kind is StatisticKind.SUM:
            return self.sum
        return self.average


@attr.s(frozen=True)
class MergeReport:
    """Result of merging shard results for one fanned-out request.

    Attributes:
        statistics: merged statistics keyed by statistic name
        shards_succeeded: ids of the shards that answered successfully
        shards_failed: shards that failed to answer
    """

    statistics: dict[str, MergedStatistic] = attr.ib()
    shards_succeeded: tuple[str, ...] = attr.ib(converter=tuple)
    shards_failed: tuple[ShardFailure, ...] = attr.ib(converter=tuple)

    @property
    def complete(self) -> bool:
        """Whether every shard answered; False when any shard failed."""
        return not self.shards_failed


def merge_partials(
    results: Iterable[ShardPartial | ShardFailure], *, strict: bool = False
) -> MergeReport:
    """Merge per-shard results into global statistics.

    Args:
        results: one :class:`ShardPartial` or :class:`ShardFailure` per shard,
            all computed for the same request (same query population)
        strict: when True, raise :class:`ShardMergeError` if any shard failed
            instead of returning an incomplete report

    Returns:
        the :class:`MergeReport` with the merged statistics, the successful
        shard ids and the shard failures

    Raises:
        ShardMergeError: if ``strict`` and any shard failed, if two shards
            share an id, or if shards report different statistics or kinds;
            a shard with no matches must report zero samples (e.g. via
            :meth:`ShardPartial.empty`), not omit a statistic
        TypeError: if a result is neither a :class:`ShardPartial` nor a
            :class:`ShardFailure`
    """
    partials: list[ShardPartial] = []
    failures: list[ShardFailure] = []
    for result in results:
        if isinstance(result, ShardPartial):
            partials.append(result)
        elif isinstance(result, ShardFailure):
            failures.append(result)
        else:
            raise TypeError(
                f"Expected ShardPartial or ShardFailure, got {type(result).__name__}; "
                "a failed shard must be reported as ShardFailure, not as an "
                "empty ShardPartial."
            )

    if strict and failures:
        failed = ", ".join(f.shard_id for f in failures)
        raise ShardMergeError(f"Cannot merge: {len(failures)} shard(s) failed: {failed}.")

    succeeded = [partial.shard_id for partial in partials]
    if len(set(succeeded)) != len(succeeded):
        raise ShardMergeError(
            f"Duplicate shard id(s) in merge: {sorted(succeeded)}; "
            "each shard must contribute at most one partial."
        )

    merged: dict[str, MergedStatistic] = {}
    if partials:
        expected = set(partials[0].statistics)
        for partial in partials[1:]:
            if set(partial.statistics) != expected:
                raise ShardMergeError(
                    f"Shard {partial.shard_id!r} reported statistics "
                    f"{sorted(partial.statistics)} but {sorted(expected)} were "
                    "expected; a shard with no matches must report zero samples "
                    "(e.g. ShardPartial.empty), not omit statistics."
                )
        for name, first in partials[0].statistics.items():
            kinds = {partial.statistics[name].kind for partial in partials}
            if len(kinds) != 1:
                raise ShardMergeError(
                    f"Statistic {name!r} was reported with kinds "
                    f"{sorted(kinds)}; all shards must use the same kind."
                )
            merged[name] = MergedStatistic(
                name=name,
                kind=first.kind,
                count=sum(partial.statistics[name].count for partial in partials),
                sum=sum(partial.statistics[name].sum for partial in partials),
                missing=sum(partial.statistics[name].missing for partial in partials),
            )

    return MergeReport(
        statistics=merged,
        shards_succeeded=succeeded,
        shards_failed=failures,
    )
