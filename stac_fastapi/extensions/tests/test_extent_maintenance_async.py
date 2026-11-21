"""Collection extent maintenance for asynchronous transaction clients."""

from collections.abc import Iterator
from typing import Any

import pytest
from stac_pydantic.item import Item
from starlette.testclient import TestClient
from test_extent_maintenance import make_collection, make_item

from stac_fastapi.api.app import StacApi
from stac_fastapi.extensions import TransactionExtension
from stac_fastapi.extensions.transaction import AsyncBaseTransactionsClient
from stac_fastapi.types import stac as stac_types
from stac_fastapi.types.config import ApiSettings
from stac_fastapi.types.core import AsyncBaseCoreClient


class AsyncInMemoryCoreClient(AsyncBaseCoreClient):
    """An async in-memory backend storing collections and items in dicts."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.collections: dict[str, dict[str, Any]] = {}
        self.items: dict[str, dict[str, dict[str, Any]]] = {}

    async def all_collections(self, **kwargs: Any) -> stac_types.Collections:
        return stac_types.Collections(
            collections=[dict(value) for value in self.collections.values()],
            links=[],
        )

    async def get_collection(
        self, collection_id: str, **kwargs: Any
    ) -> stac_types.Collection:
        return dict(self.collections[collection_id])

    async def get_item(self, item_id: str, collection_id: str, **kwargs: Any) -> Any:
        return dict(self.items[collection_id][item_id])

    async def get_search(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def post_search(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def item_collection(
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
        return stac_types.ItemCollection(
            type="FeatureCollection",
            features=[dict(value) for value in stored[start : start + limit]],
            links=[],
        )


class AsyncInMemoryTransactionsClient(AsyncBaseTransactionsClient):
    """An async transaction client writing into the async core client."""

    def __init__(self, core_client: AsyncInMemoryCoreClient) -> None:
        self.core_client = core_client

    async def create_item(self, collection_id: str, item: Item, **kwargs: Any) -> Any:
        payload = item.model_dump(mode="json") if hasattr(item, "model_dump") else item
        if payload.get("type") == "FeatureCollection":
            for feature in payload["features"]:
                self.core_client.items.setdefault(collection_id, {})[feature["id"]] = (
                    feature
                )
            return payload
        self.core_client.items.setdefault(collection_id, {})[payload["id"]] = payload
        return payload

    async def update_item(
        self, collection_id: str, item_id: str, item: Item, **kwargs: Any
    ) -> Any:
        payload = item.model_dump(mode="json") if hasattr(item, "model_dump") else item
        self.core_client.items.setdefault(collection_id, {})[item_id] = payload
        return payload

    async def patch_item(
        self, collection_id: str, item_id: str, patch: Any, **kwargs: Any
    ) -> Any:
        return self.core_client.items[collection_id][item_id]

    async def delete_item(self, item_id: str, collection_id: str, **kwargs: Any) -> Any:
        return self.core_client.items.get(collection_id, {}).pop(item_id)

    async def create_collection(self, collection: Any, **kwargs: Any) -> Any:
        payload = (
            collection.model_dump(mode="json")
            if hasattr(collection, "model_dump")
            else collection
        )
        self.core_client.collections[payload["id"]] = payload
        return payload

    async def update_collection(
        self, collection_id: str, collection: Any, **kwargs: Any
    ) -> Any:
        payload = (
            collection.model_dump(mode="json")
            if hasattr(collection, "model_dump")
            else collection
        )
        self.core_client.collections[collection_id] = payload
        return payload

    async def patch_collection(
        self, collection_id: str, patch: Any, **kwargs: Any
    ) -> Any:
        return self.core_client.collections[collection_id]

    async def delete_collection(self, collection_id: str, **kwargs: Any) -> Any:
        return self.core_client.collections.pop(collection_id)


@pytest.fixture
def async_core_client() -> AsyncInMemoryCoreClient:
    client = AsyncInMemoryCoreClient()
    client.collections["test_collection"] = make_collection()
    return client


@pytest.fixture
def async_client(
    async_core_client: AsyncInMemoryCoreClient,
) -> Iterator[TestClient]:
    settings = ApiSettings(maintain_collection_extents=True)
    transactions = AsyncInMemoryTransactionsClient(async_core_client)
    api = StacApi(
        settings=settings,
        client=async_core_client,
        extensions=[TransactionExtension(client=transactions, settings=settings)],
    )
    with TestClient(api.app) as client:
        yield client


def test_async_create_item_maintains_extent(async_client: TestClient) -> None:
    response = async_client.post(
        "/collections/test_collection/items",
        json=make_item(
            "a", bbox=[10.0, 10.0, 11.0, 11.0], datetime_value="2025-01-01T00:00:00Z"
        ),
    )
    assert response.status_code == 201, response.text

    collection = async_client.get("/collections/test_collection").json()
    assert collection["extent"]["spatial"]["bbox"] == [[10.0, 10.0, 11.0, 11.0]]
    assert collection["extent"]["temporal"]["interval"] == [
        ["2025-01-01T00:00:00Z", "2025-01-01T00:00:00Z"]
    ]


def test_async_delete_last_item_clears_extent(async_client: TestClient) -> None:
    async_client.post(
        "/collections/test_collection/items",
        json=make_item(
            "a", bbox=[10.0, 10.0, 11.0, 11.0], datetime_value="2025-01-01T00:00:00Z"
        ),
    )
    response = async_client.delete("/collections/test_collection/items/a")
    assert response.is_success, response.text

    collection = async_client.get("/collections/test_collection").json()
    assert collection["extent"]["spatial"]["bbox"] == [[-180.0, -90.0, 180.0, 90.0]]
    assert collection["extent"]["temporal"]["interval"] == [[None, None]]
