"""End-to-end tests for the shared temporal contract.

These tests run a real (in-memory) backend through the FastAPI application
and verify that Item Search, Collection Search, and Aggregations all apply
the same interval-intersection semantics from
``stac_fastapi.types.temporal`` — i.e. the datetime contract is wired into
actual backend queries and aggregations, not just request validation.
"""

import pytest
from starlette.testclient import TestClient

from stac_fastapi.api.app import StacApi
from stac_fastapi.extensions.aggregation import AggregationExtension
from stac_fastapi.extensions.aggregation.client import BaseAggregationClient
from stac_fastapi.extensions.aggregation.types import AggregationCollection
from stac_fastapi.extensions.collection_search import CollectionSearchPostExtension
from stac_fastapi.extensions.collection_search.client import (
    BaseCollectionSearchClient,
)
from stac_fastapi.extensions.collection_search.request import (
    BaseCollectionSearchGetRequest,
)
from stac_fastapi.types import stac
from stac_fastapi.types.config import ApiSettings
from stac_fastapi.types.core import BaseCoreClient, NumType
from stac_fastapi.types.search import BaseSearchPostRequest
from stac_fastapi.types.temporal import (
    collection_matches_interval,
    filter_items_by_datetime,
    parse_datetime_interval,
)

# A catalog mixing instant captures and multi-day observations.
ITEMS = [
    {
        "id": "instant-jan01",
        "properties": {"datetime": "2021-01-01T00:00:00Z"},
    },
    {
        "id": "instant-jan10",
        "properties": {"datetime": "2021-01-10T12:00:00Z"},
    },
    {
        "id": "obs-jan05-08",
        "properties": {
            "datetime": None,
            "start_datetime": "2021-01-05T00:00:00Z",
            "end_datetime": "2021-01-08T00:00:00Z",
        },
    },
    {
        "id": "obs-jan15-20",
        "properties": {
            "datetime": None,
            "start_datetime": "2021-01-15T00:00:00Z",
            "end_datetime": "2021-01-20T00:00:00Z",
        },
    },
]

COLLECTIONS = [
    {
        "id": "imagery-jan",
        "extent": {
            "spatial": {"bbox": [[-180, -90, 180, 90]]},
            "temporal": {"interval": [["2021-01-01T00:00:00Z", "2021-01-31T00:00:00Z"]]},
        },
    },
    {
        "id": "archive-2019",
        "extent": {
            "spatial": {"bbox": [[-180, -90, 180, 90]]},
            "temporal": {"interval": [["2019-01-01T00:00:00Z", "2019-12-31T00:00:00Z"]]},
        },
    },
    {
        "id": "ongoing",
        "extent": {
            "spatial": {"bbox": [[-180, -90, 180, 90]]},
            "temporal": {"interval": [["2021-01-20T00:00:00Z", None]]},
        },
    },
]


def _item(item) -> stac.Item:
    return stac.Item(
        id=item["id"],
        type="Feature",
        geometry={"type": "Point", "coordinates": [0, 0]},
        bbox=[-1, -1, 1, 1],
        properties=item["properties"],
        links=[],
        assets={},
    )


def _collection(collection) -> stac.Collection:
    return stac.Collection(
        id=collection["id"],
        type="Collection",
        description="test collection",
        license="proprietary",
        extent=collection["extent"],
        links=[],
    )


def _match_items(interval):
    return filter_items_by_datetime(ITEMS, interval)


class TemporalCoreClient(BaseCoreClient):
    """In-memory backend filtering Items with the shared temporal contract."""

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
        # GET endpoints receive the raw datetime string; the backend
        # normalizes it with the shared contract before filtering.
        interval = parse_datetime_interval(datetime)
        return stac.ItemCollection(
            type="FeatureCollection",
            features=[_item(item) for item in _match_items(interval)],
        )

    def post_search(
        self, search_request: BaseSearchPostRequest, **kwargs
    ) -> stac.ItemCollection:
        # POST endpoints receive the request model, which exposes the same
        # normalized interval.
        return stac.ItemCollection(
            type="FeatureCollection",
            features=[
                _item(item) for item in _match_items(search_request.datetime_interval)
            ],
        )

    def item_collection(
        self,
        collection_id: str,
        bbox: list[float | int] | None = None,
        datetime: str | None = None,
        limit: int = 10,
        token: str | None = None,
        **kwargs,
    ) -> stac.ItemCollection:
        interval = parse_datetime_interval(datetime)
        return stac.ItemCollection(
            type="FeatureCollection",
            features=[_item(item) for item in _match_items(interval)],
        )

    def all_collections(self, datetime: str | None = None, **kwargs) -> stac.Collections:
        interval = parse_datetime_interval(datetime)
        return stac.Collections(
            collections=[
                _collection(collection)
                for collection in COLLECTIONS
                if collection_matches_interval(collection, interval)
            ],
            links=[],
        )

    def get_collection(self, collection_id: str, **kwargs) -> stac.Collection:
        return _collection(COLLECTIONS[0])

    def get_item(self, item_id: str, collection_id: str, **kwargs) -> stac.Item:
        return _item(ITEMS[0])


class TemporalCollectionSearchClient(BaseCollectionSearchClient):
    """Collection-Search backend filtering on collection temporal extents."""

    def post_all_collections(self, search_request, **kwargs) -> stac.Collections:
        interval = search_request.datetime_interval
        return stac.Collections(
            collections=[
                _collection(collection)
                for collection in COLLECTIONS
                if collection_matches_interval(collection, interval)
            ],
            links=[],
        )


class TemporalAggregationClient(BaseAggregationClient):
    """Aggregation backend counting Items matched by the shared contract."""

    def aggregate(
        self,
        collection_id: str | None = None,
        datetime: str | None = None,
        **kwargs,
    ) -> AggregationCollection:
        # POST /aggregate passes the request model as first positional arg.
        if isinstance(collection_id, BaseSearchPostRequest):
            interval = collection_id.datetime_interval
        else:
            interval = parse_datetime_interval(datetime)
        return AggregationCollection(
            type="AggregationCollection",
            aggregations=[
                {
                    "name": "total_count",
                    "data_type": "integer",
                    "value": len(_match_items(interval)),
                }
            ],
            links=[],
        )


@pytest.fixture
def client() -> TestClient:
    settings = ApiSettings(enable_response_models=False)
    api = StacApi(
        settings=settings,
        client=TemporalCoreClient(),
        collections_get_request_model=BaseCollectionSearchGetRequest,
        extensions=[
            CollectionSearchPostExtension(
                client=TemporalCollectionSearchClient(),
                settings=settings,
            ),
            AggregationExtension(client=TemporalAggregationClient()),
        ],
    )
    with TestClient(api.app) as client:
        yield client


def feature_ids(response) -> list[str]:
    assert response.status_code == 200, response.text
    return [feature["id"] for feature in response.json()["features"]]


def collection_ids(response) -> list[str]:
    assert response.status_code == 200, response.text
    return [collection["id"] for collection in response.json()["collections"]]


def test_all_request_models_share_the_same_interval():
    """Item Search, Collection Search, and Aggregation request models (GET and
    POST) must normalize a datetime query to the exact same interval."""
    from stac_fastapi.extensions.aggregation.request import (
        AggregationExtensionGetRequest,
        AggregationExtensionPostRequest,
    )
    from stac_fastapi.extensions.collection_search.request import (
        BaseCollectionSearchPostRequest,
    )
    from stac_fastapi.types.search import BaseSearchGetRequest

    models = [
        BaseSearchGetRequest,
        BaseSearchPostRequest,
        BaseCollectionSearchGetRequest,
        BaseCollectionSearchPostRequest,
        AggregationExtensionGetRequest,
        AggregationExtensionPostRequest,
    ]
    for value in [
        "2021-01-01T08:00:00+08:00",
        "2021-01-01T00:00:00Z/2021-01-02T12:00:00+02:00",
        "2021-01-01T00:00:00Z/..",
        "../2021-01-02T00:00:00Z",
        None,
    ]:
        intervals = {model(datetime=value).datetime_interval for model in models}
        assert len(intervals) == 1, (value, intervals)


@pytest.mark.parametrize("method", ["get", "post"])
class TestItemSearchContract:
    def query(self, client, method, datetime):
        if method == "get":
            return client.get("/search", params={"datetime": datetime})
        return client.post("/search", json={"datetime": datetime})

    def test_instant_query_matches_instant_and_containing_observation(
        self, client, method
    ):
        ids = feature_ids(self.query(client, method, "2021-01-01T00:00:00Z"))
        assert ids == ["instant-jan01"]

        ids = feature_ids(self.query(client, method, "2021-01-06T00:00:00Z"))
        assert ids == ["obs-jan05-08"]

    def test_same_instant_different_offset_same_result(self, client, method):
        zulu = feature_ids(self.query(client, method, "2021-01-01T00:00:00Z"))
        offset = feature_ids(self.query(client, method, "2021-01-01T08:00:00+08:00"))
        negative = feature_ids(
            self.query(client, method, "2020-12-31T19:00:00-05:00")
        )
        assert zulu == offset == negative == ["instant-jan01"]

    def test_window_intersects_multi_day_observation(self, client, method):
        """A window overlapping part of an observation matches it, even
        though neither the observation's start nor end falls inside."""
        ids = feature_ids(
            self.query(client, method, "2021-01-06T00:00:00Z/2021-01-07T00:00:00Z")
        )
        assert ids == ["obs-jan05-08"]

    def test_boundary_touching_matches(self, client, method):
        """Closed intervals: an observation ending exactly at the window
        start (or starting exactly at the window end) matches."""
        ids = feature_ids(
            self.query(client, method, "2021-01-08T00:00:00Z/2021-01-15T00:00:00Z")
        )
        # obs-jan05-08 ends at the window start, obs-jan15-20 starts at the
        # window end, and instant-jan10 falls inside the window
        assert ids == ["instant-jan10", "obs-jan05-08", "obs-jan15-20"]

    def test_open_end_intervals(self, client, method):
        ids = feature_ids(self.query(client, method, "2021-01-09T00:00:00Z/.."))
        assert ids == ["instant-jan10", "obs-jan15-20"]

        ids = feature_ids(self.query(client, method, "../2021-01-02T00:00:00Z"))
        assert ids == ["instant-jan01"]

    def test_no_datetime_returns_everything(self, client, method):
        if method == "get":
            response = client.get("/search")
        else:
            response = client.post("/search", json={})
        assert len(feature_ids(response)) == len(ITEMS)


def test_item_collection_endpoint_uses_same_contract(client):
    response = client.get(
        "/collections/test/items",
        params={"datetime": "2021-01-06T00:00:00Z"},
    )
    assert feature_ids(response) == ["obs-jan05-08"]


@pytest.mark.parametrize("method", ["get", "post"])
class TestCollectionSearchContract:
    def query(self, client, method, datetime):
        if method == "get":
            return client.get("/collections", params={"datetime": datetime})
        return client.post("/collections", json={"datetime": datetime})

    def test_instant_query(self, client, method):
        ids = collection_ids(self.query(client, method, "2021-01-15T00:00:00Z"))
        assert ids == ["imagery-jan"]

    def test_offset_equivalent_instant(self, client, method):
        zulu = collection_ids(self.query(client, method, "2021-01-15T00:00:00Z"))
        offset = collection_ids(
            self.query(client, method, "2021-01-15T08:00:00+08:00")
        )
        assert zulu == offset == ["imagery-jan"]

    def test_window_intersection_and_open_extent(self, client, method):
        ids = collection_ids(
            self.query(client, method, "2021-01-25T00:00:00Z/2021-02-01T00:00:00Z")
        )
        # overlaps the end of imagery-jan and the open-ended ongoing extent
        assert ids == ["imagery-jan", "ongoing"]

    def test_boundary_touching_matches(self, client, method):
        ids = collection_ids(
            self.query(client, method, "2020-01-01T00:00:00Z/2021-01-01T00:00:00Z")
        )
        # archive-2019 is disjoint; imagery-jan starts exactly at window end
        assert ids == ["imagery-jan"]


@pytest.mark.parametrize("method", ["get", "post"])
class TestAggregationContract:
    def total_count(self, client, method, datetime):
        if method == "get":
            response = client.get("/aggregate", params={"datetime": datetime})
        else:
            response = client.post("/aggregate", json={"datetime": datetime})
        assert response.status_code == 200, response.text
        (aggregation,) = response.json()["aggregations"]
        assert aggregation["name"] == "total_count"
        return aggregation["value"]

    def test_aggregation_counts_follow_same_contract(self, client, method):
        assert self.total_count(client, method, "2021-01-01T00:00:00Z") == 1
        assert self.total_count(client, method, "2021-01-06T00:00:00Z") == 1
        assert (
            self.total_count(
                client, method, "2021-01-06T00:00:00Z/2021-01-16T00:00:00Z"
            )
            == 3
        )

    def test_aggregation_offset_equivalence(self, client, method):
        assert self.total_count(
            client, method, "2021-01-01T08:00:00+08:00"
        ) == self.total_count(client, method, "2021-01-01T00:00:00Z")

    def test_aggregation_open_interval(self, client, method):
        assert self.total_count(client, method, "2021-01-09T00:00:00Z/..") == 2
