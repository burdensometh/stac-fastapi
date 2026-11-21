"""Tests for local fallback filtering of unsupported conditions."""

from stac_fastapi.api.federation.filters import (
    apply_local_filters,
    geometries_intersect,
    geometry_intersects_bbox,
    matches_datetime,
)

from .fakes import make_item


def test_datetime_instant_match():
    item = make_item("i", "c", when="2020-06-01T00:00:00Z")
    assert matches_datetime(item, "2020-01-01T00:00:00Z/2020-12-31T00:00:00Z")
    assert not matches_datetime(item, "2021-01-01T00:00:00Z/2022-01-01T00:00:00Z")


def test_datetime_open_intervals():
    item = make_item("i", "c", when="2020-06-01T00:00:00Z")
    assert matches_datetime(item, "../2021-01-01T00:00:00Z")
    assert matches_datetime(item, "2019-01-01T00:00:00Z/..")
    assert not matches_datetime(item, "../2019-01-01T00:00:00Z")


def test_datetime_range_item():
    item = make_item("i", "c")
    item["properties"] = {
        "datetime": None,
        "start_datetime": "2020-01-01T00:00:00Z",
        "end_datetime": "2020-12-31T00:00:00Z",
    }
    # Query interval overlaps the item range.
    assert matches_datetime(item, "2019-12-01T00:00:00Z/2020-02-01T00:00:00Z")
    assert not matches_datetime(item, "2022-01-01T00:00:00Z/2023-01-01T00:00:00Z")


def test_item_without_datetime_excluded_when_filtered():
    item = make_item("i", "c")
    del item["properties"]["datetime"]
    assert not matches_datetime(item, "2020-01-01T00:00:00Z")
    assert matches_datetime(item, None)


def test_bbox_filter_uses_item_geometry():
    inside = make_item("in", "c", lon=0.0, lat=0.0)
    outside = make_item("out", "c", lon=50.0, lat=50.0)
    kept = apply_local_filters(
        [inside, outside],
        bbox=(-10, -10, 10, 10),
        intersects=None,
        datetime_query=None,
    )
    assert [item["id"] for item in kept] == ["in"]


def test_bbox_edge_touching_is_kept():
    item = make_item("edge", "c", lon=10.0, lat=10.0)
    assert geometry_intersects_bbox(item["geometry"], (-10, -10, 10, 10))


def test_intersects_filter():
    item = make_item("i", "c", lon=0.0, lat=0.0)
    polygon = {
        "type": "Polygon",
        "coordinates": [
            [
                [-1, -1],
                [-1, 1],
                [1, 1],
                [1, -1],
                [-1, -1],
            ]
        ],
    }
    assert geometries_intersect(item["geometry"], polygon)
    far_polygon = {
        "type": "Polygon",
        "coordinates": [
            [
                [40, 40],
                [40, 41],
                [41, 41],
                [41, 40],
                [40, 40],
            ]
        ],
    }
    assert not geometries_intersect(item["geometry"], far_polygon)


def test_combined_local_filters():
    spatial_match = make_item("m", "c", lon=0.0, lat=0.0, when="2020-06-01T00:00:00Z")
    spatial_only = make_item("s", "c", lon=0.0, lat=0.0, when="2010-06-01T00:00:00Z")
    temporal_only = make_item("t", "c", lon=50.0, lat=50.0, when="2020-06-01T00:00:00Z")
    kept = apply_local_filters(
        [spatial_match, spatial_only, temporal_only],
        bbox=(-10, -10, 10, 10),
        intersects=None,
        datetime_query="2020-01-01T00:00:00Z/2021-01-01T00:00:00Z",
    )
    assert [item["id"] for item in kept] == ["m"]
