"""Collection extent maintenance through the Transaction extension."""

from collections.abc import Iterator
from typing import Any

import pytest
from stac_pydantic import Collection
from stac_pydantic.item import Item
from starlette.testclient import TestClient

from stac_fastapi.api.app import StacApi
from stac_fastapi.extensions import TransactionExtension
from stac_fastapi.extensions.transaction import BaseTransactionsClient
from stac_fastapi.types import stac as stac_types
from stac_fastapi.types.config import ApiSettings
from stac_fastapi.types.core import BaseCoreClient


def make_collection(
    collection_id: str = "test_collection",
    bbox: list[list[float]] | None = None,
    interval: list[list[str | None]] | None = None,
) -> dict[str, Any]:
    return {
        "type": "Collection",
        "stac_version": "1.0.0",
        "stac_extensions": [],
        "id": collection_id,
        "description": "A test collection",
        "license": "proprietary",
        "extent": {
            "spatial": {"bbox": bbox or [[-180.0, -90.0, 180.0, 90.0]]},
            "temporal": {"interval": interval or [["2000-01-01T00:00:00Z", None]]},
        },
        "links": [],
        "assets": {},
    }


def make_item(
    item_id: str,
    collection_id: str = "test_collection",
    bbox: list[float] | None = None,
    datetime_value: str | None = "2020-06-13T13:00:00Z",
) -> dict[str, Any]:
    bbox = bbox or [-105.0, 40.0, -105.0, 40.0]
    geometry = {
        "type": "Point",
        "coordinates": [(bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2],
    }
    properties: dict[str, Any] = {}
    if datetime_value is not None:
        properties["datetime"] = datetime_value
    return {
        "type": "Feature",
        "stac_version": "1.0.0",
        "stac_extensions": [],
        "id": item_id,
        "geometry": geometry,
        "bbox": bbox,
        "properties": properties,
        "links": [],
        "assets": {},
        "collection": collection_id,
    }


class InMemoryCoreClient(BaseCoreClient):
    """A tiny synchronous backend storing collections and items in dicts."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.collections: dict[str, dict[str, Any]] = {}
        self.items: dict[str, dict[str, dict[str, Any]]] = {}

    def all_collections(self, **kwargs: Any) -> stac_types.Collections:
        return stac_types.Collections(
            collections=[dict(value) for value in self.collections.values()],
            links=[],
        )

    def get_collection(self, collection_id: str, **kwargs: Any) -> stac_types.Collection:
        return dict(self.collections[collection_id])

    def get_item(self, item_id: str, collection_id: str, **kwargs: Any) -> Any:
        return dict(self.items[collection_id][item_id])

    def get_search(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def post_search(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def item_collection(
        self,
        collection_id: str,
        bbox: Any = None,
        datetime: Any = None,
        limit: int = 10,
        token: str | None = None,
        **kwargs: Any,
    ) -> stac_types.ItemCollection:
        stored = list(self.items.get(collection_id, {}).values())
        start = int(token or 0)
        page = stored[start : start + limit]
        links = []
        if start + limit < len(stored):
            links.append(
                {
                    "rel": "next",
                    "href": f"/collections/{collection_id}/items?token={start + limit}",
                }
            )
        return stac_types.ItemCollection(
            type="FeatureCollection",
            features=[dict(item) for item in page],
            links=links,
        )


class InMemoryTransactionsClient(BaseTransactionsClient):
    """Synchronous transaction client writing into the in-memory core client."""

    def __init__(self, core_client: InMemoryCoreClient) -> None:
        self.core_client = core_client

    def create_item(self, collection_id: str, item: Item, **kwargs: Any) -> Any:
        payload = item.model_dump(mode="json") if hasattr(item, "model_dump") else item
        if payload.get("type") == "FeatureCollection":
            for feature in payload["features"]:
                self.core_client.items.setdefault(collection_id, {})[feature["id"]] = (
                    feature
                )
            return payload
        self.core_client.items.setdefault(collection_id, {})[payload["id"]] = payload
        return payload

    def update_item(
        self, collection_id: str, item_id: str, item: Item, **kwargs: Any
    ) -> Any:
        payload = item.model_dump(mode="json") if hasattr(item, "model_dump") else item
        self.core_client.items.setdefault(collection_id, {})[item_id] = payload
        return payload

    def patch_item(
        self, collection_id: str, item_id: str, patch: Any, **kwargs: Any
    ) -> Any:
        item = self.core_client.items.setdefault(collection_id, {}).setdefault(
            item_id, make_item(item_id, collection_id)
        )
        if isinstance(patch, list):
            for operation in patch:
                if operation.op in ("add", "replace"):
                    item.setdefault("properties", {})["datetime"] = operation.value
        return item

    def delete_item(self, item_id: str, collection_id: str, **kwargs: Any) -> Any:
        return self.core_client.items.get(collection_id, {}).pop(item_id)

    def create_collection(self, collection: Collection, **kwargs: Any) -> Any:
        payload = (
            collection.model_dump(mode="json")
            if hasattr(collection, "model_dump")
            else collection
        )
        self.core_client.collections[payload["id"]] = payload
        return payload

    def update_collection(
        self, collection_id: str, collection: Collection, **kwargs: Any
    ) -> Any:
        payload = (
            collection.model_dump(mode="json")
            if hasattr(collection, "model_dump")
            else collection
        )
        self.core_client.collections[collection_id] = payload
        return payload

    def patch_collection(self, collection_id: str, patch: Any, **kwargs: Any) -> Any:
        return self.core_client.collections[collection_id]

    def delete_collection(self, collection_id: str, **kwargs: Any) -> Any:
        return self.core_client.collections.pop(collection_id)


@pytest.fixture
def core_client() -> InMemoryCoreClient:
    client = InMemoryCoreClient()
    client.collections["test_collection"] = make_collection()
    return client


@pytest.fixture
def maintain_client(core_client: InMemoryCoreClient) -> Iterator[TestClient]:
    settings = ApiSettings(maintain_collection_extents=True)
    transactions = InMemoryTransactionsClient(core_client)
    api = StacApi(
        settings=settings,
        client=core_client,
        extensions=[TransactionExtension(client=transactions, settings=settings)],
    )
    with TestClient(api.app) as client:
        yield client


@pytest.fixture
def plain_client(core_client: InMemoryCoreClient) -> Iterator[TestClient]:
    settings = ApiSettings()
    transactions = InMemoryTransactionsClient(core_client)
    api = StacApi(
        settings=settings,
        client=core_client,
        extensions=[TransactionExtension(client=transactions, settings=settings)],
    )
    with TestClient(api.app) as client:
        yield client


def get_extent(client: TestClient, collection_id: str = "test_collection") -> dict:
    response = client.get(f"/collections/{collection_id}")
    assert response.status_code == 200, response.text
    return response.json()["extent"]


def test_disabled_by_default(
    plain_client: TestClient, core_client: InMemoryCoreClient
) -> None:
    """Without the setting the extent is left untouched."""
    plain_client.post(
        "/collections/test_collection/items",
        json=make_item("a", bbox=[10.0, 10.0, 11.0, 11.0]),
    )
    assert get_extent(plain_client) == make_collection()["extent"]


def test_create_item_widens_extent(maintain_client: TestClient) -> None:
    """Creating an Item outside the extent widens the spatial and temporal extent."""
    response = maintain_client.post(
        "/collections/test_collection/items",
        json=make_item(
            "a", bbox=[10.0, 10.0, 11.0, 11.0], datetime_value="2025-01-01T00:00:00Z"
        ),
    )
    assert response.status_code == 201, response.text

    extent = get_extent(maintain_client)
    assert extent["spatial"]["bbox"] == [[10.0, 10.0, 11.0, 11.0]]
    assert extent["temporal"]["interval"] == [
        ["2025-01-01T00:00:00Z", "2025-01-01T00:00:00Z"]
    ]

    # The item is now reachable through the collection extent.
    response = maintain_client.get(
        "/collections/test_collection/items", params={"limit": 10}
    )
    assert [feature["id"] for feature in response.json()["features"]] == ["a"]


def test_update_item_moves_extent(maintain_client: TestClient) -> None:
    """A PUT that changes bbox and datetime moves the extent."""
    maintain_client.post(
        "/collections/test_collection/items",
        json=make_item(
            "a", bbox=[10.0, 10.0, 11.0, 11.0], datetime_value="2021-01-01T00:00:00Z"
        ),
    )
    maintain_client.post(
        "/collections/test_collection/items",
        json=make_item(
            "b", bbox=[20.0, 20.0, 21.0, 21.0], datetime_value="2022-01-01T00:00:00Z"
        ),
    )
    assert get_extent(maintain_client)["spatial"]["bbox"] == [[10.0, 10.0, 21.0, 21.0]]

    response = maintain_client.put(
        "/collections/test_collection/items/b",
        json=make_item(
            "b", bbox=[30.0, 30.0, 31.0, 31.0], datetime_value="2023-01-01T00:00:00Z"
        ),
    )
    assert response.is_success, response.text

    extent = get_extent(maintain_client)
    assert extent["spatial"]["bbox"] == [[10.0, 10.0, 31.0, 31.0]]
    assert extent["temporal"]["interval"] == [
        ["2021-01-01T00:00:00Z", "2023-01-01T00:00:00Z"]
    ]


def test_delete_last_extreme_shrinks_extent(maintain_client: TestClient) -> None:
    """Deleting the Item which defined an extreme shrinks the extent back."""
    maintain_client.post(
        "/collections/test_collection/items",
        json=make_item(
            "a", bbox=[10.0, 10.0, 11.0, 11.0], datetime_value="2021-01-01T00:00:00Z"
        ),
    )
    maintain_client.post(
        "/collections/test_collection/items",
        json=make_item(
            "b", bbox=[20.0, 20.0, 21.0, 21.0], datetime_value="2025-01-01T00:00:00Z"
        ),
    )
    assert get_extent(maintain_client)["spatial"]["bbox"] == [[10.0, 10.0, 21.0, 21.0]]

    response = maintain_client.delete("/collections/test_collection/items/b")
    assert response.is_success, response.text

    extent = get_extent(maintain_client)
    assert extent["spatial"]["bbox"] == [[10.0, 10.0, 11.0, 11.0]]
    assert extent["temporal"]["interval"] == [
        ["2021-01-01T00:00:00Z", "2021-01-01T00:00:00Z"]
    ]


def test_delete_only_item_clears_extent(maintain_client: TestClient) -> None:
    """An empty collection falls back to the world and an open interval."""
    maintain_client.post(
        "/collections/test_collection/items",
        json=make_item(
            "a", bbox=[10.0, 10.0, 11.0, 11.0], datetime_value="2021-01-01T00:00:00Z"
        ),
    )
    maintain_client.delete("/collections/test_collection/items/a")

    extent = get_extent(maintain_client)
    assert extent["spatial"]["bbox"] == [[-180.0, -90.0, 180.0, 90.0]]
    assert extent["temporal"]["interval"] == [[None, None]]


def test_start_end_datetimes_without_datetime(maintain_client: TestClient) -> None:
    """An Item annotated with start/end datetimes uses those, never "now"."""
    item = make_item("ranged", bbox=[10.0, 10.0, 11.0, 11.0], datetime_value=None)
    item["properties"]["datetime"] = None
    item["properties"]["start_datetime"] = "2018-01-01T00:00:00Z"
    item["properties"]["end_datetime"] = "2018-01-02T00:00:00Z"
    response = maintain_client.post("/collections/test_collection/items", json=item)
    assert response.status_code == 201, response.text

    extent = get_extent(maintain_client)
    assert extent["temporal"]["interval"] == [
        ["2018-01-01T00:00:00Z", "2018-01-02T00:00:00Z"]
    ]
    assert extent["spatial"]["bbox"] == [[10.0, 10.0, 11.0, 11.0]]


def test_patch_item_updates_extent(maintain_client: TestClient) -> None:
    """A PATCH changing the datetime recomputes the temporal extent."""
    maintain_client.post(
        "/collections/test_collection/items",
        json=make_item("a", datetime_value="2021-01-01T00:00:00Z"),
    )
    response = maintain_client.patch(
        "/collections/test_collection/items/a",
        json=[
            {
                "op": "replace",
                "path": "/properties/datetime",
                "value": "2019-05-05T00:00:00Z",
            }
        ],
    )
    assert response.is_success, response.text
    assert get_extent(maintain_client)["temporal"]["interval"] == [
        ["2019-05-05T00:00:00Z", "2019-05-05T00:00:00Z"]
    ]


def test_get_collection_reflects_committed_items(
    core_client: InMemoryCoreClient, maintain_client: TestClient
) -> None:
    """Reads recompute the extent from the Items, even when storage is stale."""
    core_client.items.setdefault("test_collection", {})["a"] = make_item(
        "a", bbox=[50.0, 50.0, 51.0, 51.0], datetime_value="2030-01-01T00:00:00Z"
    )
    extent = get_extent(maintain_client)
    assert extent["spatial"]["bbox"] == [[50.0, 50.0, 51.0, 51.0]]
    assert extent["temporal"]["interval"] == [
        ["2030-01-01T00:00:00Z", "2030-01-01T00:00:00Z"]
    ]


def test_collection_listing_is_maintained(maintain_client: TestClient) -> None:
    """GET /collections also reflects the extents of the Items."""
    maintain_client.post(
        "/collections/test_collection/items",
        json=make_item(
            "a", bbox=[50.0, 50.0, 51.0, 51.0], datetime_value="2030-01-01T00:00:00Z"
        ),
    )
    response = maintain_client.get("/collections")
    assert response.status_code == 200, response.text
    collection = response.json()["collections"][0]
    assert collection["extent"]["spatial"]["bbox"] == [[50.0, 50.0, 51.0, 51.0]]


def test_collection_update_does_not_reintroduce_stale_extent(
    maintain_client: TestClient, core_client: InMemoryCoreClient
) -> None:
    """A PUT of a collection keeps the extent derived from its Items."""
    core_client.items.setdefault("test_collection", {})["a"] = make_item(
        "a", bbox=[5.0, 5.0, 6.0, 6.0], datetime_value="2020-01-01T00:00:00Z"
    )
    response = maintain_client.put("/collections/test_collection", json=make_collection())
    assert response.is_success, response.text
    assert get_extent(maintain_client)["spatial"]["bbox"] == [[5.0, 5.0, 6.0, 6.0]]


class NestingCoreClient(InMemoryCoreClient):
    """A backend whose ``item_collection`` reads the collection first.

    pgstac and other real backends do this to raise ``NotFoundError`` for an
    unknown collection, which would recurse if a collection read also recomputed
    the extent unconditionally.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.collection_reads = 0

    def get_collection(self, collection_id: str, **kwargs: Any) -> Any:
        self.collection_reads += 1
        return super().get_collection(collection_id, **kwargs)

    def item_collection(self, collection_id: str, **kwargs: Any) -> Any:
        self.get_collection(collection_id)
        return super().item_collection(collection_id, **kwargs)


def test_reads_do_not_recurse_into_item_reads() -> None:
    """Reading a collection does not recurse through ``item_collection``."""
    core = NestingCoreClient()
    core.collections["test_collection"] = make_collection()
    core.items["test_collection"] = {"a": make_item("a", bbox=[1.0, 1.0, 2.0, 2.0])}

    settings = ApiSettings(maintain_collection_extents=True)
    api = StacApi(
        settings=settings,
        client=core,
        extensions=[
            TransactionExtension(
                client=InMemoryTransactionsClient(core), settings=settings
            )
        ],
    )
    with TestClient(api.app) as client:
        response = client.get("/collections/test_collection")
        assert response.status_code == 200, response.text
        assert response.json()["extent"]["spatial"]["bbox"] == [[1.0, 1.0, 2.0, 2.0]]
        # One read for the response, one inside `item_collection`.
        assert core.collection_reads == 2


class BrokenItemCollectionClient(InMemoryCoreClient):
    """A backend whose ``item_collection`` returns an unexpected payload."""

    def item_collection(self, collection_id: str, **kwargs: Any) -> Any:
        return "not an item collection"


def test_unreadable_items_keep_the_stored_extent() -> None:
    """A read that cannot list Items leaves the extent untouched."""
    core = BrokenItemCollectionClient()
    core.collections["test_collection"] = make_collection()
    core.items["test_collection"] = {}

    settings = ApiSettings(maintain_collection_extents=True)
    api = StacApi(
        settings=settings,
        client=core,
        extensions=[
            TransactionExtension(
                client=InMemoryTransactionsClient(core), settings=settings
            )
        ],
    )
    with TestClient(api.app) as client:
        response = client.get("/collections/test_collection")
        assert response.status_code == 200, response.text
        # The original extent survives; it is not replaced by an empty one.
        assert response.json()["extent"] == make_collection()["extent"]
