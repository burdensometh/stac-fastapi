"""End-to-end tests for antimeridian-crossing bbox search windows.

A bbox whose west longitude is greater than its east longitude describes a
narrow extent crossing the 180th meridian, not an empty result. The API must
split the extent into two executable query windows, deduplicate matches on
both sides by Item/Collection identity, and only then apply the limit and
aggregations.
"""

import pytest
from fastapi.testclient import TestClient

from stac_fastapi.api.app import StacApi

from stac_fastapi.extensions import (
    AggregationExtension,
    CollectionSearchPostExtension,
)
from stac_fastapi.extensions.aggregation.client import (
    AsyncBaseAggregationClient,
    BaseAggregationClient,
)
from stac_fastapi.extensions.collection_search.client import (
    BaseCollectionSearchClient,
)
from stac_fastapi.extensions.collection_search.request import (
    BaseCollectionSearchPostRequest,
)
from stac_fastapi.types import stac
from stac_fastapi.types.config import ApiSettings
from stac_fastapi.types.core import (
    AsyncBaseCoreClient,
    BaseCoreClient,
    NumType,
)
from stac_fastapi.types.search import BaseSearchPostRequest

CROSSING_BBOX = (170.0, -10.0, -170.0, 10.0)
WEST_WINDOW = (170.0, -10.0, 180.0, 10.0)
EAST_WINDOW = (-180.0, -10.0, -170.0, 10.0)


def _item(item_id, collection="test_collection"):
    return stac.Item(
        type="Feature",
        stac_version="1.1.0",
        id=item_id,
        collection=collection,
        geometry={"type": "Point", "coordinates": [175, 0]},
        bbox=[174.9, -0.1, 175.1, 0.1],
        properties={"datetime": "2000-01-01T00:00:00Z"},
        links=[],
        assets={},
    )


# Item fixtures per side of the antimeridian; "shared" intersects both windows.
WEST_ITEMS = [_item("west-a"), _item("west-b"), _item("shared")]
EAST_ITEMS = [_item("shared"), _item("east-c"), _item("east-d")]

WEST_COLLECTIONS = [
    {"id": "col-a", "type": "Collection", "links": [], "license": "proprietary"},
    {"id": "shared-col", "type": "Collection", "links": [], "license": "proprietary"},
]
EAST_COLLECTIONS = [
    {"id": "shared-col", "type": "Collection", "links": [], "license": "proprietary"},
    {"id": "col-b", "type": "Collection", "links": [], "license": "proprietary"},
]


def _items_for_window(bbox, west, east):
    if bbox is None:
        return west + east
    if bbox[0] == 170.0:
        return list(west)
    return list(east)


def _collections_for_window(bbox):
    if bbox is None:
        return WEST_COLLECTIONS + EAST_COLLECTIONS
    if bbox[0] == 170.0:
        return [dict(c) for c in WEST_COLLECTIONS]
    return [dict(c) for c in EAST_COLLECTIONS]


class RecordingCoreClient(BaseCoreClient):
    """Synchronous client that records the bbox windows handed to it."""

    def __init__(self):
        self.search_bboxes = []
        self.item_collection_bboxes = []

    def _window_item_collection(self, bbox):
        features = _items_for_window(bbox, WEST_ITEMS, EAST_ITEMS)
        return stac.ItemCollection(
            type="FeatureCollection",
            features=features,
            links=[],
            numberMatched=len(features) + 10,
            numberReturned=len(features),
        )

    def get_search(
        self,
        collections: list[str] | None = None,
        ids: list[str] | None = None,
        bbox: list[NumType] | None = None,
        intersects: str | None = None,
        datetime: str | None = None,
        limit: int | None = 10,
        **kwargs,
    ) -> stac.ItemCollection:
        return self.search_items_with_bbox(
            bbox,
            lambda window: self._record_and_search(window, self.search_bboxes),
            limit=limit,
        )

    def post_search(
        self, search_request: BaseSearchPostRequest, **kwargs
    ) -> stac.ItemCollection:
        return self.search_items_with_bbox(
            search_request.bbox,
            lambda window: self._record_and_search(window, self.search_bboxes),
            limit=search_request.limit,
        )

    def _record_and_search(self, window, sink):
        sink.append(tuple(window))
        return self._window_item_collection(window)

    def item_collection(
        self,
        collection_id: str,
        bbox: list[float | int] | None = None,
        datetime: str | None = None,
        limit: int = 10,
        token: str = None,
        **kwargs,
    ) -> stac.ItemCollection:
        return self.search_items_with_bbox(
            bbox,
            lambda window: self._record_and_search(
                window, self.item_collection_bboxes
            ),
            limit=limit,
        )

    def all_collections(self, **kwargs) -> stac.Collections:
        bbox = kwargs.get("bbox")
        return self.search_collections_with_bbox(
            bbox,
            self._window_collections,
            limit=kwargs.get("limit"),
        )

    def _window_collections(self, bbox):
        return stac.Collections(
            collections=_collections_for_window(bbox),
            links=[],
            numberMatched=len(_collections_for_window(bbox)),
            numberReturned=len(_collections_for_window(bbox)),
        )

    def get_collection(self, collection_id: str, **kwargs) -> stac.Collection:
        raise NotImplementedError

    def get_item(self, item_id: str, collection_id: str, **kwargs) -> stac.Item:
        raise NotImplementedError


class AsyncRecordingCoreClient(AsyncBaseCoreClient):
    """Asynchronous counterpart of RecordingCoreClient."""

    def __init__(self):
        self.search_bboxes = []

    async def _window_item_collection(self, bbox):
        features = _items_for_window(bbox, WEST_ITEMS, EAST_ITEMS)
        return stac.ItemCollection(
            type="FeatureCollection",
            features=features,
            links=[],
            numberMatched=len(features) + 10,
            numberReturned=len(features),
        )

    async def get_search(
        self,
        collections: list[str] | None = None,
        ids: list[str] | None = None,
        bbox: list[NumType] | None = None,
        intersects: str | None = None,
        datetime: str | None = None,
        limit: int | None = 10,
        **kwargs,
    ) -> stac.ItemCollection:
        async def execute(window):
            self.search_bboxes.append(tuple(window))
            return await self._window_item_collection(window)

        return await self.search_items_with_bbox(bbox, execute, limit=limit)

    async def post_search(
        self, search_request: BaseSearchPostRequest, **kwargs
    ) -> stac.ItemCollection:
        async def execute(window):
            self.search_bboxes.append(tuple(window))
            return await self._window_item_collection(window)

        return await self.search_items_with_bbox(
            search_request.bbox, execute, limit=search_request.limit
        )

    async def all_collections(self, **kwargs) -> stac.Collections:
        raise NotImplementedError

    async def get_collection(self, collection_id: str, **kwargs) -> stac.Collection:
        raise NotImplementedError

    async def get_item(self, item_id: str, collection_id: str, **kwargs) -> stac.Item:
        raise NotImplementedError

    async def item_collection(self, *args, **kwargs) -> stac.ItemCollection:
        raise NotImplementedError


class RecordingCollectionSearchClient(BaseCollectionSearchClient):
    def __init__(self):
        self.bboxes = []

    def post_all_collections(
        self, search_request: BaseCollectionSearchPostRequest, **kwargs
    ) -> dict:
        return self.search_collections_with_bbox(
            search_request.bbox,
            self._execute,
            limit=search_request.limit,
        )

    def _execute(self, bbox):
        self.bboxes.append(tuple(bbox))
        collections = _collections_for_window(bbox)
        return {
            "collections": collections,
            "links": [],
            "numberMatched": len(collections),
            "numberReturned": len(collections),
        }


class RecordingAggregationClient(BaseAggregationClient):
    def __init__(self):
        self.bboxes = []

    def aggregate(self, *args, **kwargs):
        bbox = kwargs.get("bbox")
        if bbox is None and args:
            bbox = getattr(args[0], "bbox", None)
        return self.aggregate_with_bbox(bbox, self._execute)

    def _execute(self, bbox):
        self.bboxes.append(bbox)
        if bbox is not None and bbox[0] == 170.0:
            count = 3
        else:
            count = 2
        return {
            "type": "AggregationCollection",
            "aggregations": [
                {
                    "name": "total_count",
                    "data_type": "integer",
                    "buckets": [
                        {
                            "key": "all",
                            "data_type": "string",
                            "frequency": {"count": count},
                        }
                    ],
                }
            ],
            "links": [],
        }


class AsyncRecordingAggregationClient(AsyncBaseAggregationClient):
    def __init__(self):
        self.bboxes = []

    async def aggregate(self, *args, **kwargs):
        # GET passes bbox as a keyword; POST passes the pydantic search model
        # positionally as the first argument.
        bbox = kwargs.get("bbox")
        if bbox is None and args:
            bbox = getattr(args[0], "bbox", None)

        async def execute(window):
            self.bboxes.append(window)
            count = 3 if window is not None and window[0] == 170.0 else 2
            return {
                "type": "AggregationCollection",
                "aggregations": [
                    {
                        "name": "total_count",
                        "data_type": "integer",
                        "buckets": [
                            {
                                "key": "all",
                                "data_type": "string",
                                "frequency": {"count": count},
                            }
                        ],
                    }
                ],
                "links": [],
            }

        return await self.aggregate_with_bbox(bbox, execute)


# ---------------------------------------------------------------------------
# Item search
# ---------------------------------------------------------------------------


@pytest.fixture
def item_client():
    core = RecordingCoreClient()
    api = StacApi(settings=ApiSettings(), client=core)
    with TestClient(api.app) as client:
        yield client, core


def test_get_search_crossing_bbox_splits_and_dedupes(item_client):
    client, core = item_client
    response = client.get(
        "/search",
        params={"bbox": ",".join(str(v) for v in CROSSING_BBOX)},
    )
    assert response.status_code == 200, response.text
    body = response.json()

    # the backend received two ordinary, non-crossing windows
    assert sorted(core.search_bboxes) == sorted([WEST_WINDOW, EAST_WINDOW])
    assert all(bbox[0] <= bbox[2] for bbox in core.search_bboxes)

    ids = [feature["id"] for feature in body["features"]]
    # "shared" appears on both sides but is returned once
    assert sorted(ids) == sorted(
        ["west-a", "west-b", "shared", "east-c", "east-d"]
    )
    assert len(ids) == len(set(ids))
    # per-window matched: (3 + 10) + (3 + 10) - 1 duplicate
    assert body["numberMatched"] == 25
    assert body["numberReturned"] == 5


def test_get_search_limit_applied_after_dedup(item_client):
    client, core = item_client
    response = client.get(
        "/search",
        params={
            "bbox": ",".join(str(v) for v in CROSSING_BBOX),
            "limit": 2,
        },
    )
    assert response.status_code == 200, response.text
    features = response.json()["features"]
    assert len(features) == 2


def test_post_search_crossing_bbox(item_client):
    client, core = item_client
    response = client.post(
        "/search",
        json={"bbox": list(CROSSING_BBOX), "limit": 10},
    )
    assert response.status_code == 200, response.text
    assert sorted(core.search_bboxes) == sorted([WEST_WINDOW, EAST_WINDOW])
    ids = [f["id"] for f in response.json()["features"]]
    assert len(ids) == len(set(ids)) == 5


def test_search_six_value_crossing_bbox(item_client):
    client, core = item_client
    bbox = [170.0, -10.0, 0.0, -170.0, 10.0, 100.0]
    response = client.post("/search", json={"bbox": bbox})
    assert response.status_code == 200, response.text
    west, east = core.search_bboxes
    assert west == (170.0, -10.0, 0.0, 180.0, 10.0, 100.0)
    assert east == (-180.0, -10.0, 0.0, -170.0, 10.0, 100.0)


def test_ordinary_bbox_uses_a_single_window(item_client):
    client, core = item_client
    response = client.get("/search", params={"bbox": "-10,-10,10,10"})
    assert response.status_code == 200, response.text
    assert core.search_bboxes == [(-10.0, -10.0, 10.0, 10.0)]


def test_item_collection_crossing_bbox(item_client):
    client, core = item_client
    response = client.get(
        "/collections/test_collection/items",
        params={"bbox": ",".join(str(v) for v in CROSSING_BBOX)},
    )
    assert response.status_code == 200, response.text
    assert sorted(core.item_collection_bboxes) == sorted(
        [WEST_WINDOW, EAST_WINDOW]
    )
    ids = [f["id"] for f in response.json()["features"]]
    assert len(ids) == len(set(ids)) == 5


def test_invalid_latitude_rejected(item_client):
    client, _ = item_client
    response = client.get("/search", params={"bbox": "170,-91,-170,91"})
    assert response.status_code == 400

    # POST validation errors are normalized to 400 by the STAC error handler
    response = client.post("/search", json={"bbox": [0, 20, 1, 10]})
    assert response.status_code == 400

    response = client.post("/search", json={"bbox": [0, 0, 1, 91]})
    assert response.status_code == 400


def test_invalid_bbox_shape_rejected(item_client):
    client, _ = item_client
    response = client.get("/search", params={"bbox": "0,0,1"})
    assert response.status_code == 400

    response = client.post("/search", json={"bbox": [0, 0, 1]})
    assert response.status_code == 400


def test_async_client_crossing_bbox():
    core = AsyncRecordingCoreClient()
    api = StacApi(settings=ApiSettings(), client=core)
    with TestClient(api.app) as client:
        response = client.get(
            "/search",
            params={"bbox": ",".join(str(v) for v in CROSSING_BBOX)},
        )
    assert response.status_code == 200, response.text
    assert sorted(core.search_bboxes) == sorted([WEST_WINDOW, EAST_WINDOW])
    ids = [f["id"] for f in response.json()["features"]]
    assert len(ids) == len(set(ids)) == 5


# ---------------------------------------------------------------------------
# Collection search
# ---------------------------------------------------------------------------


def test_collection_search_get_crossing_bbox():
    core = RecordingCoreClient()
    from stac_fastapi.extensions import CollectionSearchExtension
    from stac_fastapi.extensions.collection_search.request import (
        BaseCollectionSearchGetRequest,
    )

    api = StacApi(
        settings=ApiSettings(),
        client=core,
        extensions=[CollectionSearchExtension()],
        collections_get_request_model=BaseCollectionSearchGetRequest,
    )
    with TestClient(api.app) as client:
        response = client.get(
            "/collections",
            params={"bbox": ",".join(str(v) for v in CROSSING_BBOX)},
        )
    assert response.status_code == 200, response.text
    ids = [c["id"] for c in response.json()["collections"]]
    assert sorted(ids) == ["col-a", "col-b", "shared-col"]
    assert len(ids) == len(set(ids))


def test_collection_search_post_crossing_bbox():
    core = RecordingCoreClient()
    collection_client = RecordingCollectionSearchClient()
    api = StacApi(
        settings=ApiSettings(),
        client=core,
        extensions=[
            CollectionSearchPostExtension(
                client=collection_client,
                settings=ApiSettings(),
            )
        ],
    )
    with TestClient(api.app) as client:
        response = client.post(
            "/collections",
            json={"bbox": list(CROSSING_BBOX), "limit": 2},
        )
    assert response.status_code == 200, response.text
    assert sorted(collection_client.bboxes) == sorted([WEST_WINDOW, EAST_WINDOW])
    body = response.json()
    ids = [c["id"] for c in body["collections"]]
    # limit applied after dedup
    assert len(ids) == 2
    assert len(ids) == len(set(ids))


def test_collection_search_rejects_invalid_latitude():
    collection_client = RecordingCollectionSearchClient()
    api = StacApi(
        settings=ApiSettings(),
        client=RecordingCoreClient(),
        extensions=[
            CollectionSearchPostExtension(
                client=collection_client,
                settings=ApiSettings(),
            )
        ],
    )
    with TestClient(api.app) as client:
        response = client.post("/collections", json={"bbox": [0, -91, 1, 90]})
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def test_aggregation_crossing_bbox_sync():
    aggregation_client = RecordingAggregationClient()
    api = StacApi(
        settings=ApiSettings(),
        client=RecordingCoreClient(),
        extensions=[AggregationExtension(client=aggregation_client)],
    )
    with TestClient(api.app) as client:
        response = client.get(
            "/aggregate",
            params={"bbox": ",".join(str(v) for v in CROSSING_BBOX)},
        )
    assert response.status_code == 200, response.text
    assert sorted(aggregation_client.bboxes) == sorted([WEST_WINDOW, EAST_WINDOW])
    buckets = response.json()["aggregations"][0]["buckets"]
    assert buckets[0]["frequency"]["count"] == 5  # 3 + 2 across the extent


def test_aggregation_crossing_bbox_async():
    aggregation_client = AsyncRecordingAggregationClient()
    api = StacApi(
        settings=ApiSettings(),
        client=AsyncRecordingCoreClient(),
        extensions=[AggregationExtension(client=aggregation_client)],
    )
    with TestClient(api.app) as client:
        response = client.post(
            "/aggregate",
            json={"bbox": list(CROSSING_BBOX)},
        )
    assert response.status_code == 200, response.text
    assert sorted(aggregation_client.bboxes) == sorted([WEST_WINDOW, EAST_WINDOW])
    buckets = response.json()["aggregations"][0]["buckets"]
    assert buckets[0]["frequency"]["count"] == 5


def test_aggregation_ordinary_bbox_single_window():
    aggregation_client = RecordingAggregationClient()
    api = StacApi(
        settings=ApiSettings(),
        client=RecordingCoreClient(),
        extensions=[AggregationExtension(client=aggregation_client)],
    )
    with TestClient(api.app) as client:
        response = client.get("/aggregate", params={"bbox": "-10,-10,10,10"})
    assert response.status_code == 200, response.text
    assert aggregation_client.bboxes == [(-10.0, -10.0, 10.0, 10.0)]
