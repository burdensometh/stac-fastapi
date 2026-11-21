"""Tests for bbox validation and antimeridian-aware query planning."""

import inspect

import pytest
from fastapi import HTTPException

from stac_fastapi.types.bbox import (
    COLLECTION_IDENTITY_KEYS,
    ITEM_IDENTITY_KEYS,
    bbox_crosses_antimeridian,
    bbox_query_windows,
    execute_aggregation_query,
    execute_aggregation_query_async,
    execute_bbox_query,
    execute_bbox_query_async,
    merge_aggregation_results,
    merge_search_results,
    split_antimeridian_bbox,
    validate_bbox,
)
from stac_fastapi.types.search import str2bbox


# ---------------------------------------------------------------------------
# validate_bbox
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bbox",
    [
        None,
        (-180, -90, 180, 90),
        (0.0, 0.0, 1.0, 1.0),
        (-180, -90, 0.0, 180, 90, 100.0),
        # reversed longitude order is legal: it crosses the antimeridian
        (170.0, -10.0, -170.0, 10.0),
        (170.0, -10.0, 0.0, -170.0, 10.0, 100.0),
        # zero-width ordinary window
        (10.0, -10.0, 10.0, 10.0),
    ],
)
def test_validate_bbox_accepts(bbox):
    assert validate_bbox(bbox) == bbox


@pytest.mark.parametrize(
    "bbox",
    [
        (0, 0, 1),  # wrong arity
        (0, 0, 1, 1, 1),
        (0, 0, 1, 1, 1, 1, 1),
        (-181, 0, 0, 1),  # longitude out of range
        (0, 0, 181, 1),
        (0, -91, 1, 1),  # latitude out of range
        (0, 0, 1, 91),
        (0, 20, 1, 10),  # reversed latitude order
        (0, 0, 10, 1, 1, 0),  # reversed elevation order
        (float("nan"), 0, 1, 1),
    ],
)
def test_validate_bbox_rejects(bbox):
    with pytest.raises(ValueError):
        validate_bbox(bbox)


def test_str2bbox_rejects_invalid_with_http_400():
    with pytest.raises(HTTPException) as exc:
        str2bbox("0,91,1,92")
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc:
        str2bbox("not-a-bbox")
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc:
        str2bbox("0,0,1")
    assert exc.value.status_code == 400


def test_str2bbox_accepts_antimeridian_bbox():
    assert str2bbox("170,-10,-170,10") == (170.0, -10.0, -170.0, 10.0)


# ---------------------------------------------------------------------------
# antimeridian splitting
# ---------------------------------------------------------------------------


def test_crosses_antimeridian():
    assert bbox_crosses_antimeridian((170.0, -10.0, -170.0, 10.0))
    assert not bbox_crosses_antimeridian((-170.0, -10.0, 170.0, 10.0))
    assert not bbox_crosses_antimeridian((10.0, -10.0, 10.0, 10.0))


def test_split_four_value_bbox():
    west, east = split_antimeridian_bbox((170.0, -10.0, -170.0, 10.0))
    assert west == (170.0, -10.0, 180.0, 10.0)
    assert east == (-180.0, -10.0, -170.0, 10.0)


def test_split_six_value_bbox_keeps_elevation():
    west, east = split_antimeridian_bbox(
        (170.0, -10.0, 0.0, -170.0, 10.0, 100.0)
    )
    assert west == (170.0, -10.0, 0.0, 180.0, 10.0, 100.0)
    assert east == (-180.0, -10.0, 0.0, -170.0, 10.0, 100.0)


def test_query_windows():
    assert bbox_query_windows(None) == ()
    assert bbox_query_windows((0, 0, 1, 1)) == ((0, 0, 1, 1),)
    windows = bbox_query_windows((170.0, -10.0, -170.0, 10.0))
    assert len(windows) == 2
    assert all(not bbox_crosses_antimeridian(w) for w in windows)


# ---------------------------------------------------------------------------
# sync / async search orchestration
# ---------------------------------------------------------------------------


def _item(item_id, collection="col"):
    return {
        "type": "Feature",
        "id": item_id,
        "collection": collection,
        "geometry": {"type": "Point", "coordinates": [0, 0]},
        "bbox": [-1, -1, 1, 1],
        "properties": {},
        "links": [],
        "assets": {},
    }


def test_execute_bbox_query_ordinary_window_calls_once():
    calls = []

    def execute(bbox):
        calls.append(bbox)
        return {"type": "FeatureCollection", "features": [_item("a")], "links": []}

    result = execute_bbox_query(
        (0, 0, 1, 1),
        execute,
        result_key="features",
        identity_keys=ITEM_IDENTITY_KEYS,
        limit=10,
    )
    assert calls == [(0, 0, 1, 1)]
    assert [f["id"] for f in result["features"]] == ["a"]


def test_execute_bbox_query_crossing_splits_dedupes_and_limits():
    calls = []

    def execute(bbox):
        calls.append(bbox)
        if bbox[0] == 170.0:  # western window [170, 180]
            features = [_item("a"), _item("b"), _item("shared")]
        else:  # eastern window [-180, -170]
            features = [_item("shared"), _item("c")]
        return {
            "type": "FeatureCollection",
            "features": features,
            "links": [
                {"rel": "self", "href": "self"},
                {"rel": "next", "href": "window-token"},
            ],
        }

    result = execute_bbox_query(
        (170.0, -10.0, -170.0, 10.0),
        execute,
        result_key="features",
        identity_keys=ITEM_IDENTITY_KEYS,
        limit=2,
    )

    # two executable windows, neither crossing the antimeridian
    assert len(calls) == 2
    assert all(not bbox_crosses_antimeridian(c) for c in calls)
    # deduped by Item identity, limit applied after dedup
    assert [f["id"] for f in result["features"]] == ["a", "b"]
    # window-specific pagination tokens are not merged through
    rels = [link["rel"] for link in result["links"]]
    assert "next" not in rels
    assert "self" in rels


def test_execute_bbox_query_number_matched_counts_real_extent():
    def execute(bbox):
        if bbox[0] == 170.0:
            features = [_item("a"), _item("shared")]
            matched = 7
        else:
            features = [_item("shared"), _item("c")]
            matched = 4
        return {
            "type": "FeatureCollection",
            "features": features,
            "links": [],
            "numberMatched": matched,
            "numberReturned": len(features),
        }

    result = execute_bbox_query(
        (170.0, -10.0, -170.0, 10.0),
        execute,
        result_key="features",
        identity_keys=ITEM_IDENTITY_KEYS,
    )
    # 7 + 4 - 1 duplicate observed on both pages
    assert result["numberMatched"] == 10
    assert result["numberReturned"] == 3


def test_merge_search_results_dedupes_collections_by_id():
    left = {
        "collections": [{"id": "c1"}, {"id": "c2"}],
        "links": [{"rel": "self", "href": "x"}],
    }
    right = {
        "collections": [{"id": "c2"}, {"id": "c3"}],
        "links": [{"rel": "self", "href": "x"}],
    }
    merged = merge_search_results(
        [left, right],
        result_key="collections",
        identity_keys=COLLECTION_IDENTITY_KEYS,
        limit=10,
    )
    assert [c["id"] for c in merged["collections"]] == ["c1", "c2", "c3"]


def test_merge_search_results_partial_number_matched():
    # only the second window reports numberMatched; the duplicate it
    # contains is subtracted, but nothing is subtracted on the first window
    left = {
        "features": [_item("a"), _item("shared")],
        "links": [],
    }
    right = {
        "features": [_item("shared"), _item("b")],
        "links": [],
        "numberMatched": 5,
        "numberReturned": 2,
    }
    merged = merge_search_results(
        [left, right],
        result_key="features",
        identity_keys=ITEM_IDENTITY_KEYS,
    )
    assert merged["numberMatched"] == 4
    assert merged["numberReturned"] == 3


def test_merge_search_results_first_window_without_matched():
    # first window reports no total; the duplicate copy observed in the
    # second window (which does report a total) is subtracted from it
    left = {"features": [_item("a"), _item("shared")], "links": []}
    right = {
        "features": [_item("shared"), _item("b")],
        "links": [],
        "numberMatched": 8,
    }
    merged = merge_search_results(
        [left, right],
        result_key="features",
        identity_keys=ITEM_IDENTITY_KEYS,
    )
    # 8 - 1 duplicate counted in the matched window
    assert merged["numberMatched"] == 7
    assert [f["id"] for f in merged["features"]] == ["a", "shared", "b"]


def test_merge_search_results_multiple_duplicates():
    first = {
        "features": [_item("a"), _item("b")],
        "links": [],
        "numberMatched": 2,
    }
    second = {
        "features": [_item("a"), _item("b"), _item("c")],
        "links": [],
        "numberMatched": 3,
    }
    merged = merge_search_results(
        [first, second],
        result_key="features",
        identity_keys=ITEM_IDENTITY_KEYS,
    )
    # 2 + 3 - two duplicates observed in matched windows
    assert merged["numberMatched"] == 3
    assert [f["id"] for f in merged["features"]] == ["a", "b", "c"]


def test_single_window_limit_truncates_and_updates_number_returned():
    def execute(bbox):
        return {
            "type": "FeatureCollection",
            "features": [_item("a"), _item("b"), _item("c")],
            "links": [{"rel": "next", "href": "token"}],
            "numberMatched": 3,
            "numberReturned": 3,
        }

    result = execute_bbox_query(
        (0, 0, 1, 1),
        execute,
        result_key="features",
        identity_keys=ITEM_IDENTITY_KEYS,
        limit=2,
    )
    assert [f["id"] for f in result["features"]] == ["a", "b"]
    assert result["numberReturned"] == 2
    assert result["numberMatched"] == 3
    # single-window responses keep their own pagination links
    assert [link["rel"] for link in result["links"]] == ["next"]


def test_single_window_collections_limit():
    def execute(bbox):
        return {
            "collections": [{"id": "c1"}, {"id": "c2"}, {"id": "c3"}],
            "links": [],
        }

    result = execute_bbox_query(
        (0, 0, 1, 1),
        execute,
        result_key="collections",
        identity_keys=COLLECTION_IDENTITY_KEYS,
        limit=1,
    )
    assert [c["id"] for c in result["collections"]] == ["c1"]


def test_execute_bbox_query_async_matches_sync():
    import asyncio

    calls = []

    async def execute(bbox):
        calls.append(bbox)
        if bbox[0] == 170.0:
            features = [_item("a"), _item("shared")]
        else:
            features = [_item("shared"), _item("b")]
        return {"type": "FeatureCollection", "features": features, "links": []}

    async def run():
        return await execute_bbox_query_async(
            (170.0, -10.0, -170.0, 10.0),
            execute,
            result_key="features",
            identity_keys=ITEM_IDENTITY_KEYS,
        )

    resolved = asyncio.run(run())
    assert inspect.iscoroutinefunction(execute)
    assert len(calls) == 2
    assert [f["id"] for f in resolved["features"]] == ["a", "shared", "b"]


# ---------------------------------------------------------------------------
# aggregation orchestration
# ---------------------------------------------------------------------------


def _agg_response(bbox):
    if bbox[0] == 170.0:
        buckets = [
            {"key": "cloudy", "data_type": "string", "frequency": {"count": 5}},
            {"key": "clear", "data_type": "string", "frequency": {"count": 3}},
        ]
    else:
        buckets = [
            {"key": "cloudy", "data_type": "string", "frequency": {"count": 2}},
            {"key": "snow", "data_type": "string", "frequency": {"count": 4}},
        ]
    return {
        "type": "AggregationCollection",
        "aggregations": [{"name": "weather", "data_type": "string", "buckets": buckets}],
        "links": [{"rel": "self", "href": "x"}],
    }


def test_merge_aggregation_results_sums_bucket_frequencies():
    merged = merge_aggregation_results(
        [_agg_response((170.0, -10, 180.0, 10)), _agg_response((-180.0, -10, -170.0, 10))]
    )
    aggregation = merged["aggregations"][0]
    frequencies = {b["key"]: b["frequency"]["count"] for b in aggregation["buckets"]}
    assert frequencies == {"cloudy": 7, "clear": 3, "snow": 4}


def test_execute_aggregation_query_crossing():
    calls = []

    def execute(bbox):
        calls.append(bbox)
        return _agg_response(bbox)

    result = execute_aggregation_query((170.0, -10.0, -170.0, 10.0), execute)
    assert len(calls) == 2
    assert len(result["aggregations"][0]["buckets"]) == 3


def test_execute_aggregation_query_ordinary_window_calls_once():
    calls = []

    def execute(bbox):
        calls.append(bbox)
        return {"type": "AggregationCollection", "aggregations": [], "links": []}

    execute_aggregation_query((0, 0, 1, 1), execute)
    assert calls == [(0, 0, 1, 1)]


def test_execute_aggregation_query_async():
    import asyncio

    calls = []

    async def execute(bbox):
        calls.append(bbox)
        return _agg_response(bbox)

    coro = execute_aggregation_query_async((170.0, -10.0, -170.0, 10.0), execute)
    result = asyncio.run(coro)
    assert len(calls) == 2
    assert len(result["aggregations"][0]["buckets"]) == 3
