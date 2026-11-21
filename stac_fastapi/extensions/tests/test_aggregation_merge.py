"""Tests for the aggregation merge contract (fan-out across backend shards)."""

from collections.abc import Iterator

import pytest
from starlette.testclient import TestClient

from stac_fastapi.api.app import StacApi
from stac_fastapi.extensions.aggregation import AggregationExtension
from stac_fastapi.extensions.aggregation.client import BaseAggregationClient
from stac_fastapi.extensions.aggregation.merge import (
    MERGEABLE_STATISTIC_KINDS,
    NON_MERGEABLE_STATISTIC_KINDS,
    NonMergeableStatisticError,
    ShardFailure,
    ShardMergeError,
    ShardPartial,
    StatisticKind,
    StatisticPartial,
    merge_partials,
)
from stac_fastapi.extensions.aggregation.types import AggregationCollection
from stac_fastapi.types.config import ApiSettings
from stac_fastapi.types.core import BaseCoreClient


def cloud_cover(count: int, sum_: float, missing: int = 0) -> dict:
    """A single-statistic partial mapping for ``eo:cloud_cover``."""
    return {
        "eo:cloud_cover": StatisticPartial(
            kind="avg", count=count, sum=sum_, missing=missing
        )
    }


def test_global_average_is_weighted_by_sample_count() -> None:
    """Merging must not average per-shard averages (that over-weights small shards)."""
    big = ShardPartial("big", cloud_cover(count=1000, sum_=10_000.0))  # avg 10
    small = ShardPartial("small", cloud_cover(count=1, sum_=90.0))  # avg 90

    report = merge_partials([big, small])

    merged = report.statistics["eo:cloud_cover"]
    # the naive mean of the shard averages would be (10 + 90) / 2 = 50
    assert merged.average == pytest.approx(10_090.0 / 1001)
    assert merged.count == 1001
    assert merged.sum == pytest.approx(10_090.0)
    assert report.complete


def test_coverage_counts_missing_values() -> None:
    """Coverage is the share of the query population that has a value."""
    report = merge_partials(
        [
            ShardPartial("a", cloud_cover(count=8, sum_=40.0, missing=2)),
            ShardPartial("b", cloud_cover(count=0, sum_=0.0, missing=5)),
        ]
    )

    merged = report.statistics["eo:cloud_cover"]
    assert merged.population == 15
    assert merged.coverage == pytest.approx(8 / 15)
    assert merged.average == pytest.approx(5.0)


def test_shard_without_matches_reports_zero_samples() -> None:
    """A shard with no matching items contributes zero samples, not a failure."""
    empty = ShardPartial.empty("empty", {"eo:cloud_cover": "avg"})
    assert empty.statistics["eo:cloud_cover"].count == 0
    assert empty.statistics["eo:cloud_cover"].missing == 0

    report = merge_partials(
        [ShardPartial("a", cloud_cover(count=2, sum_=20.0, missing=1)), empty]
    )

    merged = report.statistics["eo:cloud_cover"]
    assert merged.average == pytest.approx(10.0)
    assert merged.coverage == pytest.approx(2 / 3)
    assert report.complete
    assert report.shards_succeeded == ("a", "empty")


def test_empty_query_population_has_no_average_or_coverage() -> None:
    """With no matches anywhere, average and coverage are undefined, not zero."""
    report = merge_partials(
        [
            ShardPartial.empty("a", {"eo:cloud_cover": "avg"}),
            ShardPartial.empty("b", {"eo:cloud_cover": "avg"}),
        ]
    )

    merged = report.statistics["eo:cloud_cover"]
    assert merged.population == 0
    assert merged.average is None
    assert merged.coverage is None
    assert report.complete


def test_failed_shard_is_not_an_empty_shard() -> None:
    """A failed shard is reported as a failure, never merged as zero samples."""
    report = merge_partials(
        [
            ShardPartial("a", cloud_cover(count=4, sum_=40.0)),
            ShardFailure("b", reason="connection timeout"),
        ]
    )

    assert not report.complete
    assert report.shards_succeeded == ("a",)
    assert [failure.shard_id for failure in report.shards_failed] == ["b"]
    assert report.shards_failed[0].reason == "connection timeout"
    # statistics of the successful shards are still merged
    assert report.statistics["eo:cloud_cover"].average == pytest.approx(10.0)


def test_strict_merge_raises_on_failed_shard() -> None:
    with pytest.raises(ShardMergeError, match="shard\\(s\\) failed: b"):
        merge_partials(
            [ShardPartial("a", cloud_cover(count=4, sum_=40.0)), ShardFailure("b")],
            strict=True,
        )


def test_all_shards_failed_reports_no_statistics() -> None:
    report = merge_partials([ShardFailure("a"), ShardFailure("b")])

    assert not report.complete
    assert report.statistics == {}
    assert len(report.shards_failed) == 2


@pytest.mark.parametrize(
    "kind", ["median", "percentile", "min", "max", "cardinality", "histogram"]
)
def test_non_mergeable_statistics_are_rejected(kind: str) -> None:
    """Statistics that cannot be merged from (count, sum, missing) say so."""
    with pytest.raises(NonMergeableStatisticError, match="cannot be merged"):
        StatisticPartial(kind=kind, count=1, sum=1.0, missing=0)


def test_mergeable_kinds_are_documented() -> None:
    assert MERGEABLE_STATISTIC_KINDS == {"count", "sum", "avg"}
    assert NON_MERGEABLE_STATISTIC_KINDS
    assert not NON_MERGEABLE_STATISTIC_KINDS & MERGEABLE_STATISTIC_KINDS


def test_kind_is_normalized_to_enum() -> None:
    partial = StatisticPartial(kind="avg", count=1, sum=2.0, missing=0)
    assert partial.kind is StatisticKind.AVG


@pytest.mark.parametrize("kind", ["count", "sum", "avg"])
def test_value_follows_statistic_kind(kind: str) -> None:
    merged = merge_partials(
        [
            ShardPartial("a", {"stat": StatisticPartial(kind, 2, 6.0, 1)}),
            ShardPartial("b", {"stat": StatisticPartial(kind, 4, 14.0, 1)}),
        ]
    ).statistics["stat"]

    expected = {"count": 6, "sum": 20.0, "avg": 20.0 / 6}[kind]
    assert merged.value == pytest.approx(expected)


def test_shards_must_report_the_same_statistics() -> None:
    """A shard with no matches must report zero samples, not omit a statistic."""
    with pytest.raises(ShardMergeError, match="zero samples"):
        merge_partials(
            [
                ShardPartial("a", {"x": StatisticPartial("avg", 1, 1.0, 0)}),
                ShardPartial("b", {"y": StatisticPartial("avg", 1, 1.0, 0)}),
            ]
        )


def test_shards_must_agree_on_statistic_kind() -> None:
    with pytest.raises(ShardMergeError, match="same kind"):
        merge_partials(
            [
                ShardPartial("a", {"x": StatisticPartial("avg", 1, 1.0, 0)}),
                ShardPartial("b", {"x": StatisticPartial("count", 1, 1.0, 0)}),
            ]
        )


def test_duplicate_shard_ids_are_rejected() -> None:
    with pytest.raises(ShardMergeError, match="Duplicate shard"):
        merge_partials([ShardPartial("a", {}), ShardPartial("a", {})])


def test_unknown_result_type_is_rejected() -> None:
    with pytest.raises(TypeError, match="ShardFailure"):
        merge_partials([ShardPartial("a", {}), object()])  # type: ignore[list-item]


def test_negative_counts_are_rejected() -> None:
    with pytest.raises(ValueError):
        StatisticPartial(kind="avg", count=-1, sum=0.0, missing=0)


def test_merge_with_no_shards_is_complete_and_empty() -> None:
    report = merge_partials([])

    assert report.complete
    assert report.statistics == {}


class DummyCoreClient(BaseCoreClient):
    def all_collections(self, *args, **kwargs):
        return {"collections": [], "links": []}

    def get_collection(self, *args, **kwargs):
        raise NotImplementedError

    def get_item(self, *args, **kwargs):
        raise NotImplementedError

    def get_search(self, *args, **kwargs):
        raise NotImplementedError

    def post_search(self, *args, **kwargs):
        raise NotImplementedError

    def item_collection(self, *args, **kwargs):
        raise NotImplementedError


class ShardBackendError(Exception):
    """Raised when a shard backend cannot answer."""


class InMemoryShard:
    """In-memory backend shard answering fanned-out aggregation requests."""

    def __init__(self, shard_id: str, items: list[dict]):
        self.shard_id = shard_id
        self.items = items
        self.requests: list[dict] = []

    def query(self, **request) -> ShardPartial:
        self.requests.append(request)
        collections = request.get("collections")
        matched = [
            item
            for item in self.items
            if not collections or item["collection"] in collections
        ]
        values = [
            item["eo:cloud_cover"]
            for item in matched
            if item["eo:cloud_cover"] is not None
        ]
        return ShardPartial(
            shard_id=self.shard_id,
            statistics={
                "eo:cloud_cover": StatisticPartial(
                    kind="avg",
                    count=len(values),
                    sum=sum(values),
                    missing=len(matched) - len(values),
                )
            },
        )


class ShardedAggregationClient(BaseAggregationClient):
    """Aggregation client fanning requests out to shards and merging partials.

    Collection filtering keeps going through the existing extension: the
    request parsed by the ``AggregationExtension`` request models is fanned
    out to every shard unchanged, so all partials describe the same query
    population.
    """

    def __init__(self, shards: list[InMemoryShard], strict: bool = False):
        self.shards = shards
        self.strict = strict

    def aggregate(self, collection_id: str | None = None, **request):
        results: list[ShardPartial | ShardFailure] = []
        for shard in self.shards:
            try:
                results.append(shard.query(**request))
            except ShardBackendError as exc:
                results.append(ShardFailure(shard_id=shard.shard_id, reason=str(exc)))
        report = merge_partials(results, strict=self.strict)
        merged = report.statistics["eo:cloud_cover"]
        return AggregationCollection(
            type="AggregationCollection",
            aggregations=[
                {
                    "name": "avg_eo:cloud_cover",
                    "data_type": "float",
                    "value": merged.average,
                },
                {
                    "name": "eo:cloud_cover_coverage",
                    "data_type": "float",
                    "value": merged.coverage,
                },
            ],
            links=[],
        )


@pytest.fixture
def shards() -> list[InMemoryShard]:
    return [
        InMemoryShard(
            "shard-a",
            [
                {"collection": "landsat", "eo:cloud_cover": 10.0},
                {"collection": "landsat", "eo:cloud_cover": 20.0},
                {"collection": "sentinel", "eo:cloud_cover": 99.0},
            ],
        ),
        InMemoryShard(
            "shard-b",
            [
                {"collection": "landsat", "eo:cloud_cover": 70.0},
                {"collection": "landsat", "eo:cloud_cover": None},
            ],
        ),
    ]


@pytest.fixture
def app_client(shards: list[InMemoryShard]) -> Iterator[TestClient]:
    api = StacApi(
        settings=ApiSettings(),
        client=DummyCoreClient(),
        extensions=[AggregationExtension(client=ShardedAggregationClient(shards))],
    )
    with TestClient(api.app) as client:
        yield client


def test_fanned_out_request_keeps_existing_collection_filter(
    app_client: TestClient, shards: list[InMemoryShard]
) -> None:
    response = app_client.get("/aggregate", params={"collections": "landsat"})
    assert response.status_code == 200, response.text

    # the collection filter parsed by the existing extension reached every shard
    for shard in shards:
        assert shard.requests[0]["collections"] == ["landsat"]

    aggregations = {agg["name"]: agg for agg in response.json()["aggregations"]}
    # shard-a: count=2 sum=30, shard-b: count=1 sum=70 missing=1; the naive
    # mean of the shard averages (15 and 70) would be 42.5
    assert aggregations["avg_eo:cloud_cover"]["value"] == pytest.approx(100.0 / 3)
    assert aggregations["eo:cloud_cover_coverage"]["value"] == pytest.approx(3 / 4)


def test_failed_shard_cannot_masquerade_as_empty_shard(
    shards: list[InMemoryShard],
) -> None:
    class FailingShard(InMemoryShard):
        def query(self, **request) -> ShardPartial:
            raise ShardBackendError("backend unavailable")

    api = StacApi(
        settings=ApiSettings(),
        client=DummyCoreClient(),
        extensions=[
            AggregationExtension(
                client=ShardedAggregationClient(
                    [shards[0], FailingShard("shard-b", [])], strict=True
                )
            )
        ],
    )
    with TestClient(api.app, raise_server_exceptions=False) as client:
        response = client.get("/aggregate")
    # the request fails loudly instead of returning shard-a's answer as if
    # shard-b had simply matched nothing
    assert response.status_code == 500
