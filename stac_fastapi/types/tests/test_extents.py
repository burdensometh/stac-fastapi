"""Tests for the collection extent helpers."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from stac_pydantic import Collection

from stac_fastapi.types.config import ApiSettings
from stac_fastapi.types.extents import (
    DEFAULT_SPATIAL_BBOX,
    DEFAULT_TEMPORAL_INTERVAL,
    aget_collection_items,
    apply_extent,
    compute_collection_extent,
    compute_spatial_extent,
    compute_temporal_extent,
    get_collection_items,
    item_bbox,
    item_temporal_interval,
    maintenance_enabled,
    resolve_core_client,
)


def item(
    bbox=None,
    geometry=None,
    datetime_value=None,
    start=None,
    end=None,
    properties=None,
):
    props = dict(properties or {})
    if datetime_value is not None:
        props["datetime"] = datetime_value
    if start is not None:
        props["start_datetime"] = start
    if end is not None:
        props["end_datetime"] = end
    result = {"id": "i", "type": "Feature", "properties": props}
    if bbox is not None:
        result["bbox"] = bbox
    if geometry is not None:
        result["geometry"] = geometry
    return result


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_maintenance_disabled_by_default() -> None:
    assert ApiSettings().maintain_collection_extents is False
    assert maintenance_enabled(ApiSettings()) is False


def test_maintenance_can_be_enabled() -> None:
    settings = ApiSettings(maintain_collection_extents=True)
    assert settings.maintain_collection_extents is True
    assert maintenance_enabled(settings) is True


@pytest.mark.parametrize(
    "alias",
    [
        "update_collection_extents",
        "collection_extent_maintenance",
        "enable_collection_extent_maintenance",
        "enable_extent_maintenance",
        "extent_maintenance",
        "maintain_extents",
    ],
)
def test_maintenance_aliases(alias: str) -> None:
    settings = ApiSettings(**{alias: True})
    assert settings.maintain_collection_extents is True


def test_maintenance_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAINTAIN_COLLECTION_EXTENTS", "true")
    assert ApiSettings().maintain_collection_extents is True


# ---------------------------------------------------------------------------
# Spatial extent
# ---------------------------------------------------------------------------


def test_item_bbox_prefers_declared_bbox() -> None:
    assert item_bbox(item(bbox=[1, 2, 3, 4], geometry=None)) == [1.0, 2.0, 3.0, 4.0]


def test_item_bbox_derived_from_geometry() -> None:
    geometry = {
        "type": "LineString",
        "coordinates": [[-10, -5], [3, 8], [-2, 6]],
    }
    assert item_bbox(item(geometry=geometry)) == [-10.0, -5.0, 3.0, 8.0]


def test_item_bbox_geometry_collection() -> None:
    geometry = {
        "type": "GeometryCollection",
        "geometries": [
            {"type": "Point", "coordinates": [1, 2]},
            {"type": "Point", "coordinates": [-4, 6]},
        ],
    }
    assert item_bbox(item(geometry=geometry)) == [-4.0, 2.0, 1.0, 6.0]


def test_item_bbox_missing() -> None:
    assert item_bbox(item()) is None


def test_spatial_extent_union() -> None:
    extent = compute_spatial_extent([item(bbox=[-10, -5, 0, 0]), item(bbox=[3, 4, 9, 8])])
    assert extent == {"bbox": [[-10.0, -5.0, 9.0, 8.0]]}


def test_spatial_extent_3d() -> None:
    extent = compute_spatial_extent(
        [item(bbox=[0, 0, 0, 1, 1, 10]), item(bbox=[-1, -1, -5, 0, 0, 0])]
    )
    assert extent == {"bbox": [[-1.0, -1.0, -5.0, 1.0, 1.0, 10.0]]}


def test_spatial_extent_ignores_items_without_geometry() -> None:
    extent = compute_spatial_extent([item(bbox=[1, 2, 3, 4]), item()])
    assert extent == {"bbox": [[1.0, 2.0, 3.0, 4.0]]}


def test_spatial_extent_empty() -> None:
    assert compute_spatial_extent([]) == {"bbox": [list(DEFAULT_SPATIAL_BBOX[0])]}


def test_spatial_extent_antimeridian_falls_back_to_world() -> None:
    extent = compute_spatial_extent([item(bbox=[170, -10, -170, 10])])
    assert extent == {"bbox": [list(DEFAULT_SPATIAL_BBOX[0])]}


# ---------------------------------------------------------------------------
# Temporal extent
# ---------------------------------------------------------------------------


def test_item_temporal_interval_datetime() -> None:
    assert item_temporal_interval(item(datetime_value="2020-01-01T00:00:00Z")) == (
        "2020-01-01T00:00:00Z",
        "2020-01-01T00:00:00Z",
    )


def test_item_temporal_interval_start_end() -> None:
    assert item_temporal_interval(
        item(datetime_value=None, start="2020-01-01T00:00:00Z", end=None)
    ) == ("2020-01-01T00:00:00Z", None)


def test_item_temporal_interval_datetime_takes_precedence() -> None:
    interval = item_temporal_interval(
        item(
            datetime_value="2020-01-01T00:00:00Z",
            start="1990-01-01T00:00:00Z",
            end="1991-01-01T00:00:00Z",
        )
    )
    assert interval == ("2020-01-01T00:00:00Z", "2020-01-01T00:00:00Z")


def test_item_temporal_interval_missing() -> None:
    assert item_temporal_interval(item()) is None


def test_item_temporal_interval_invalid_is_ignored() -> None:
    assert item_temporal_interval(item(datetime_value="not-a-date")) is None


def test_temporal_extent_min_max() -> None:
    extent = compute_temporal_extent(
        [
            item(datetime_value="2020-01-01T00:00:00Z"),
            item(datetime_value="2022-01-01T00:00:00Z"),
            item(datetime_value="2021-01-01T00:00:00Z"),
        ]
    )
    assert extent == {"interval": [["2020-01-01T00:00:00Z", "2022-01-01T00:00:00Z"]]}


def test_temporal_extent_open_interval() -> None:
    extent = compute_temporal_extent(
        [item(datetime_value=None, start="2020-01-01T00:00:00Z", end=None)]
    )
    assert extent == {"interval": [["2020-01-01T00:00:00Z", None]]}


def test_temporal_extent_skips_undated_items() -> None:
    extent = compute_temporal_extent(
        [item(datetime_value="2020-01-01T00:00:00Z"), item()]
    )
    assert extent == {"interval": [["2020-01-01T00:00:00Z", "2020-01-01T00:00:00Z"]]}


def test_temporal_extent_empty() -> None:
    assert compute_temporal_extent([]) == {
        "interval": [list(DEFAULT_TEMPORAL_INTERVAL[0])]
    }


def test_temporal_extent_accepts_datetime_objects() -> None:
    extent = compute_temporal_extent(
        [item(properties={"datetime": datetime(2020, 1, 1, tzinfo=UTC)})]
    )
    assert extent["interval"][0][0].startswith("2020-01-01T00:00:00")


def test_temporal_extent_normalizes_offsets() -> None:
    extent = compute_temporal_extent(
        [
            item(
                properties={
                    "datetime": datetime(2020, 1, 1, tzinfo=timezone(timedelta(hours=2)))
                }
            )
        ]
    )
    assert extent["interval"][0][0].endswith("Z")


# ---------------------------------------------------------------------------
# Whole extent
# ---------------------------------------------------------------------------


def test_compute_collection_extent() -> None:
    extent = compute_collection_extent(
        [item(bbox=[0, 0, 1, 1], datetime_value="2020-01-01T00:00:00Z")]
    )
    assert extent == {
        "spatial": {"bbox": [[0.0, 0.0, 1.0, 1.0]]},
        "temporal": {"interval": [["2020-01-01T00:00:00Z", "2020-01-01T00:00:00Z"]]},
    }


def test_compute_collection_extent_empty() -> None:
    assert compute_collection_extent([]) == {
        "spatial": {"bbox": [list(DEFAULT_SPATIAL_BBOX[0])]},
        "temporal": {"interval": [list(DEFAULT_TEMPORAL_INTERVAL[0])]},
    }


def test_apply_extent_replaces_extent() -> None:
    collection = {
        "id": "c",
        "extent": {
            "spatial": {"bbox": [[-180, -90, 180, 90]]},
            "temporal": {"interval": [["1900-01-01T00:00:00Z", "1901-01-01T00:00:00Z"]]},
        },
    }
    updated = apply_extent(
        collection, [item(bbox=[0, 0, 1, 1], datetime_value="2020-01-01T00:00:00Z")]
    )
    assert updated["extent"]["spatial"]["bbox"] == [[0.0, 0.0, 1.0, 1.0]]
    # The original object is not mutated.
    assert collection["extent"]["spatial"]["bbox"] == [[-180, -90, 180, 90]]


def test_apply_extent_preserves_pydantic_model() -> None:
    collection = Collection(
        id="c",
        type="Collection",
        stac_version="1.0.0",
        description="d",
        license="proprietary",
        extent={
            "spatial": {"bbox": [[-180, -90, 180, 90]]},
            "temporal": {"interval": [["1900-01-01T00:00:00Z", "1901-01-01T00:00:00Z"]]},
        },
        links=[],
    )
    updated = apply_extent(
        collection, [item(bbox=[0, 0, 1, 1], datetime_value="2020-01-01T00:00:00Z")]
    )
    assert isinstance(updated, Collection)
    assert list(updated.extent.spatial.bbox[0]) == [0.0, 0.0, 1.0, 1.0]


# ---------------------------------------------------------------------------
# Reading Items
# ---------------------------------------------------------------------------


class SyncClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = 0

    def item_collection(self, collection_id, **kwargs):
        page = self.pages[self.calls]
        self.calls += 1
        return page


class AsyncClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = 0

    async def item_collection(self, collection_id, **kwargs):
        page = self.pages[self.calls]
        self.calls += 1
        return page


def page(features, next_token=None):
    links = []
    if next_token is not None:
        links.append(
            {
                "rel": "next",
                "href": f"https://example.com/collections/c/items?token={next_token}",
            }
        )
    return {"type": "FeatureCollection", "features": features, "links": links}


def test_get_collection_items_follows_pagination() -> None:
    client = SyncClient(
        [
            page([item(bbox=[0, 0, 1, 1])], next_token="2"),
            page([item(bbox=[1, 1, 2, 2])]),
        ]
    )
    items = get_collection_items(client, "c")
    assert len(items) == 2
    assert client.calls == 2


def test_get_collection_items_stops_on_repeated_token() -> None:
    client = SyncClient(
        [
            page([item(bbox=[0, 0, 1, 1])], next_token="same"),
            page([item(bbox=[1, 1, 2, 2])], next_token="same"),
        ]
    )
    assert len(get_collection_items(client, "c")) == 2


@pytest.mark.asyncio
async def test_aget_collection_items_follows_pagination() -> None:
    client = AsyncClient(
        [
            page([item(bbox=[0, 0, 1, 1])], next_token="2"),
            page([item(bbox=[1, 1, 2, 2])]),
        ]
    )
    items = await aget_collection_items(client, "c")
    assert len(items) == 2


def test_get_collection_items_without_client() -> None:
    assert get_collection_items(None, "c") == []


def test_resolve_core_client_from_state() -> None:
    core = SyncClient([])

    class State:
        client = core

    class App:
        state = State()

    class Request:
        app = App()

    assert resolve_core_client(Request()) is core


def test_resolve_core_client_directly() -> None:
    core = SyncClient([])
    assert resolve_core_client(core) is core


def test_resolve_core_client_missing() -> None:
    assert resolve_core_client(object()) is None
