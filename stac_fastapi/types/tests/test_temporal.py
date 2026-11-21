from datetime import UTC, datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from stac_fastapi.types.temporal import (
    filter_items_by_datetime,
    intervals_intersect,
    item_matches_interval,
    item_temporal_extent,
    normalize_to_utc,
    parse_datetime_interval,
    to_interval,
)


def parse_pair(pair) -> tuple:
    """Parse a (start, end) pair of RFC3339 strings (None = open)."""
    return tuple(
        datetime.fromisoformat(v.replace("Z", "+00:00")) if v else None for v in pair
    )


class TestNormalizeToUtc:
    def test_naive_assumed_utc(self):
        naive = datetime(2021, 1, 1, 12, 0, 0)
        assert normalize_to_utc(naive) == datetime(2021, 1, 1, 12, 0, 0, tzinfo=UTC)

    def test_offset_converted_to_same_instant(self):
        offset = datetime(2021, 1, 1, 8, 0, 0, tzinfo=timezone(timedelta(hours=8)))
        zulu = datetime(2021, 1, 1, 0, 0, 0, tzinfo=UTC)
        assert normalize_to_utc(offset) == zulu
        assert normalize_to_utc(offset).tzinfo == UTC

    def test_utc_unchanged(self):
        zulu = datetime(2021, 1, 1, 0, 0, 0, tzinfo=UTC)
        assert normalize_to_utc(zulu) is zulu


class TestParseDatetimeInterval:
    def test_none(self):
        assert parse_datetime_interval(None) is None

    def test_single_datetime_is_closed_point_interval(self):
        start, end = parse_datetime_interval("2021-01-01T00:00:00Z")
        assert start == end == datetime(2021, 1, 1, tzinfo=UTC)

    def test_closed_interval(self):
        start, end = parse_datetime_interval(
            "2021-01-01T00:00:00Z/2021-01-02T00:00:00Z"
        )
        assert start == datetime(2021, 1, 1, tzinfo=UTC)
        assert end == datetime(2021, 1, 2, tzinfo=UTC)

    def test_open_end_interval(self):
        start, end = parse_datetime_interval("2021-01-01T00:00:00Z/..")
        assert start == datetime(2021, 1, 1, tzinfo=UTC)
        assert end is None

    def test_open_start_interval(self):
        start, end = parse_datetime_interval("../2021-01-02T00:00:00Z")
        assert start is None
        assert end == datetime(2021, 1, 2, tzinfo=UTC)

    def test_double_open_interval_rejected(self):
        with pytest.raises(HTTPException):
            parse_datetime_interval("../..")

    def test_invalid_datetime_rejected(self):
        with pytest.raises(HTTPException):
            parse_datetime_interval("not-a-datetime")

    def test_offsets_normalized_to_same_utc_instant(self):
        """Same UTC instant with different offsets must give the same result."""
        assert parse_datetime_interval(
            "2021-01-01T08:00:00+08:00"
        ) == parse_datetime_interval("2021-01-01T00:00:00Z")
        assert parse_datetime_interval(
            "2021-01-01T08:00:00+08:00/2021-01-02T10:30:00+02:30"
        ) == parse_datetime_interval("2021-01-01T00:00:00Z/2021-01-02T08:00:00Z")


class TestToInterval:
    def test_none(self):
        assert to_interval(None) is None

    def test_single_datetime(self):
        point = datetime(2021, 1, 1, tzinfo=UTC)
        assert to_interval(point) == (point, point)

    def test_tuple_passthrough_with_normalization(self):
        start = datetime(2021, 1, 1, 8, tzinfo=timezone(timedelta(hours=8)))
        end = datetime(2021, 1, 2, tzinfo=UTC)
        assert to_interval((start, end)) == (
            datetime(2021, 1, 1, tzinfo=UTC),
            end,
        )

    def test_open_bounds_preserved(self):
        assert to_interval((None, datetime(2021, 1, 1, tzinfo=UTC))) == (
            None,
            datetime(2021, 1, 1, tzinfo=UTC),
        )


class TestItemTemporalExtent:
    def test_instant_item(self):
        extent = item_temporal_extent({"datetime": "2021-01-01T00:00:00Z"})
        assert extent == (
            datetime(2021, 1, 1, tzinfo=UTC),
            datetime(2021, 1, 1, tzinfo=UTC),
        )

    def test_ranged_item(self):
        extent = item_temporal_extent(
            {
                "datetime": None,
                "start_datetime": "2020-12-31T00:00:00Z",
                "end_datetime": "2021-01-05T00:00:00Z",
            }
        )
        assert extent == (
            datetime(2020, 12, 31, tzinfo=UTC),
            datetime(2021, 1, 5, tzinfo=UTC),
        )

    def test_ranged_item_with_open_end(self):
        extent = item_temporal_extent(
            {"datetime": None, "start_datetime": "2020-12-31T00:00:00Z"}
        )
        assert extent == (datetime(2020, 12, 31, tzinfo=UTC), None)

    def test_item_offsets_normalized(self):
        extent = item_temporal_extent({"datetime": "2021-01-01T08:00:00+08:00"})
        assert extent == (
            datetime(2021, 1, 1, tzinfo=UTC),
            datetime(2021, 1, 1, tzinfo=UTC),
        )

    def test_datetime_objects_accepted(self):
        extent = item_temporal_extent(
            {"datetime": datetime(2021, 1, 1, tzinfo=UTC)}
        )
        assert extent == (
            datetime(2021, 1, 1, tzinfo=UTC),
            datetime(2021, 1, 1, tzinfo=UTC),
        )


class TestIntervalsIntersect:
    INTERSECTING = [
        # overlapping ranges
        (("2021-01-01T00:00:00Z", "2021-01-10T00:00:00Z"),
         ("2021-01-05T00:00:00Z", "2021-01-20T00:00:00Z")),
        # containment both ways
        (("2021-01-01T00:00:00Z", "2021-01-10T00:00:00Z"),
         ("2021-01-03T00:00:00Z", "2021-01-04T00:00:00Z")),
        (("2021-01-03T00:00:00Z", "2021-01-04T00:00:00Z"),
         ("2021-01-01T00:00:00Z", "2021-01-10T00:00:00Z")),
        # boundary touching counts as intersecting (closed intervals)
        (("2021-01-01T00:00:00Z", "2021-01-10T00:00:00Z"),
         ("2021-01-10T00:00:00Z", "2021-01-20T00:00:00Z")),
        (("2021-01-10T00:00:00Z", "2021-01-20T00:00:00Z"),
         ("2021-01-01T00:00:00Z", "2021-01-10T00:00:00Z")),
        # instant inside range
        (("2021-01-05T00:00:00Z", "2021-01-05T00:00:00Z"),
         ("2021-01-01T00:00:00Z", "2021-01-10T00:00:00Z")),
        # instant exactly on the boundary
        (("2021-01-10T00:00:00Z", "2021-01-10T00:00:00Z"),
         ("2021-01-01T00:00:00Z", "2021-01-10T00:00:00Z")),
        # identical instants
        (("2021-01-10T00:00:00Z", "2021-01-10T00:00:00Z"),
         ("2021-01-10T00:00:00Z", "2021-01-10T00:00:00Z")),
        # open ends
        ((None, "2021-01-10T00:00:00Z"),
         ("2021-01-05T00:00:00Z", None)),
        ((None, None),
         ("2021-01-05T00:00:00Z", "2021-01-06T00:00:00Z")),
        (("2021-01-05T00:00:00Z", "2021-01-06T00:00:00Z"),
         (None, None)),
    ]

    DISJOINT = [
        # disjoint ranges
        (("2021-01-01T00:00:00Z", "2021-01-05T00:00:00Z"),
         ("2021-01-06T00:00:00Z", "2021-01-10T00:00:00Z")),
        # instant outside range
        (("2021-01-11T00:00:00Z", "2021-01-11T00:00:00Z"),
         ("2021-01-01T00:00:00Z", "2021-01-10T00:00:00Z")),
        # distinct instants
        (("2021-01-01T00:00:00Z", "2021-01-01T00:00:00Z"),
         ("2021-01-02T00:00:00Z", "2021-01-02T00:00:00Z")),
        # open ends still disjoint
        ((None, "2021-01-01T00:00:00Z"),
         ("2021-01-02T00:00:00Z", None)),
    ]

    @pytest.mark.parametrize("first,second", INTERSECTING)
    def test_intersecting(self, first, second):
        assert intervals_intersect(parse_pair(first), parse_pair(second))
        # intersection is symmetric
        assert intervals_intersect(parse_pair(second), parse_pair(first))

    @pytest.mark.parametrize("first,second", DISJOINT)
    def test_disjoint(self, first, second):
        assert not intervals_intersect(parse_pair(first), parse_pair(second))
        assert not intervals_intersect(parse_pair(second), parse_pair(first))


class TestItemMatchesInterval:
    def test_no_interval_matches_everything(self):
        assert item_matches_interval({"datetime": "2021-01-01T00:00:00Z"}, None)

    def test_instant_query_inside_multi_day_observation(self):
        properties = {
            "datetime": None,
            "start_datetime": "2020-12-31T00:00:00Z",
            "end_datetime": "2021-01-05T00:00:00Z",
        }
        assert item_matches_interval(
            properties, parse_datetime_interval("2021-01-01T00:00:00Z")
        )
        assert not item_matches_interval(
            properties, parse_datetime_interval("2021-02-01T00:00:00Z")
        )

    def test_offset_equivalent_query_same_result(self):
        properties = {"datetime": "2021-01-01T00:00:00Z"}
        assert item_matches_interval(
            properties, parse_datetime_interval("2021-01-01T08:00:00+08:00")
        )


class TestFilterItemsByDatetime:
    items = [
        {
            "id": "instant",
            "type": "Feature",
            "properties": {"datetime": "2021-01-01T00:00:00Z"},
        },
        {
            "id": "range",
            "type": "Feature",
            "properties": {
                "datetime": None,
                "start_datetime": "2020-12-31T00:00:00Z",
                "end_datetime": "2021-01-05T00:00:00Z",
            },
        },
    ]

    def ids(self, items):
        return [item["id"] for item in items]

    def test_no_interval_returns_all(self):
        assert self.ids(filter_items_by_datetime(self.items, None)) == [
            "instant",
            "range",
        ]

    def test_instant_query_matches_instant_and_range(self):
        interval = parse_datetime_interval("2021-01-01T00:00:00Z")
        assert self.ids(filter_items_by_datetime(self.items, interval)) == [
            "instant",
            "range",
        ]

    def test_query_window_intersection(self):
        interval = parse_datetime_interval(
            "2021-01-03T00:00:00Z/2021-01-04T00:00:00Z"
        )
        assert self.ids(filter_items_by_datetime(self.items, interval)) == ["range"]

    def test_objects_with_properties_attribute(self):
        class Item:
            def __init__(self, properties):
                self.properties = properties

        items = [Item({"datetime": "2021-01-01T00:00:00Z"})]
        interval = parse_datetime_interval("2021-01-01T08:00:00+08:00")
        assert filter_items_by_datetime(items, interval) == items
