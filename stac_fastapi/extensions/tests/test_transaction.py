from collections.abc import Iterator

import pytest
from stac_pydantic import Collection
from stac_pydantic.item import Item
from stac_pydantic.item_collection import ItemCollection
from starlette.responses import Response
from starlette.testclient import TestClient

from stac_fastapi.api.app import StacApi
from stac_fastapi.extensions import TransactionExtension
from stac_fastapi.extensions.transaction import BaseTransactionsClient
from stac_fastapi.extensions.transaction.request import (
    PartialCollection,
    PartialItem,
    PatchOperation,
)
from stac_fastapi.types.config import ApiSettings
from stac_fastapi.types.core import BaseCoreClient


class DummyCoreClient(BaseCoreClient):
    def all_collections(self, *args, **kwargs):
        raise NotImplementedError

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


class DummyTransactionsClient(BaseTransactionsClient):
    """Dummy client returning parts of the request, rather than proper STAC items."""

    def create_item(self, item: Item | ItemCollection, *args, **kwargs):
        return {"created": True, "type": item.type}

    def update_item(self, collection_id: str, item_id: str, item: Item, **kwargs):
        return {
            "path_collection_id": collection_id,
            "path_item_id": item_id,
            "type": item.type,
        }

    def patch_item(
        self,
        collection_id: str,
        item_id: str,
        patch: PartialItem | list[PatchOperation],
        **kwargs,
    ):
        if isinstance(patch, PartialItem):
            patch = patch.operations()

        return {
            "path_collection_id": collection_id,
            "path_item_id": item_id,
            "patch": patch,
        }

    def delete_item(self, item_id: str, collection_id: str, **kwargs):
        return {
            "path_collection_id": collection_id,
            "path_item_id": item_id,
        }

    def create_collection(self, collection: Collection, **kwargs):
        return {"type": collection.type}

    def update_collection(self, collection_id: str, collection: Collection, **kwargs):
        return {"path_collection_id": collection_id, "type": collection.type}

    def patch_collection(
        self,
        collection_id: str,
        patch: PartialCollection | list[PatchOperation],
        **kwargs,
    ):
        if isinstance(patch, PartialCollection):
            patch = patch.operations()

        return {
            "path_collection_id": collection_id,
            "patch": patch,
        }

    def delete_collection(self, collection_id: str, **kwargs):
        return {"path_collection_id": collection_id}


class ResponseTransactionsClient(DummyTransactionsClient):
    """Client returning raw `Response` objects."""

    def create_item(self, item: Item | ItemCollection, *args, **kwargs):
        return Response(status_code=201)

    def create_collection(self, collection: Collection, **kwargs):
        return Response(status_code=201, headers={"Location": "/custom-location"})


@pytest.fixture
def client(
    core_client: DummyCoreClient, transactions_client: DummyTransactionsClient
) -> Iterator[TestClient]:
    settings = ApiSettings()
    api = StacApi(
        settings=settings,
        client=core_client,
        extensions=[
            TransactionExtension(client=transactions_client, settings=settings),
        ],
    )
    with TestClient(api.app) as client:
        yield client


@pytest.fixture
def core_client() -> DummyCoreClient:
    return DummyCoreClient()


@pytest.fixture
def transactions_client() -> DummyTransactionsClient:
    return DummyTransactionsClient()


@pytest.fixture
def response_client(core_client: DummyCoreClient) -> Iterator[TestClient]:
    settings = ApiSettings()
    api = StacApi(
        settings=settings,
        client=core_client,
        extensions=[
            TransactionExtension(client=ResponseTransactionsClient(), settings=settings),
        ],
    )
    with TestClient(api.app) as client:
        yield client


@pytest.fixture
def item_collection(item: Item) -> ItemCollection:
    return {
        "type": "FeatureCollection",
        "features": [item],
        "links": [],
        "context": None,
    }


@pytest.fixture
def item() -> Item:
    return {
        "type": "Feature",
        "stac_version": "1.0.0",
        "stac_extensions": [],
        "id": "test_item",
        "geometry": {"type": "Point", "coordinates": [-105, 40]},
        "bbox": [-105, 40, -105, 40],
        "properties": {"datetime": "2020-06-13T13:00:00Z"},
        "links": [],
        "assets": {},
        "collection": "test_collection",
    }


@pytest.fixture
def collection() -> Collection:
    return {
        "type": "Collection",
        "stac_version": "1.0.0",
        "stac_extensions": [],
        "id": "test_collection",
        "description": "A test collection",
        "extent": {
            "spatial": {"bbox": [[-180, -90, 180, 90]]},
            "temporal": {"interval": [["2000-01-01T00:00:00Z", "2024-01-01T00:00:00Z"]]},
        },
        "links": [],
        "assets": {},
        "license": "proprietary",
    }


def test_create_item(client: TestClient, item: Item) -> None:
    response = client.post("/collections/a-collection/items", json=item)
    assert response.status_code == 201, response.text
    assert response.json()["type"] == "Feature"
    assert response.headers["location"].endswith(
        "/collections/a-collection/items/test_item"
    )


def test_create_item_collection(
    client: TestClient, item_collection: ItemCollection
) -> None:
    response = client.post("/collections/a-collection/items", json=item_collection)
    assert response.is_success, response.text
    assert response.json()["type"] == "FeatureCollection"
    assert "location" not in response.headers


def test_create_item_response_location(response_client: TestClient, item: Item) -> None:
    """A `Location` header is added when the client returns a `Response`."""
    response = response_client.post("/collections/a-collection/items", json=item)
    assert response.status_code == 201, response.text
    assert response.headers["location"].endswith(
        "/collections/a-collection/items/test_item"
    )


def test_create_item_collection_with_response_models(
    core_client: DummyCoreClient, item: Item, item_collection: ItemCollection
) -> None:
    class EchoTransactionsClient(DummyTransactionsClient):
        def create_item(self, item: Item | ItemCollection, *args, **kwargs):
            return item.model_dump(mode="json")

    settings = ApiSettings(enable_response_models=True)
    api = StacApi(
        settings=settings,
        client=core_client,
        extensions=[
            TransactionExtension(client=EchoTransactionsClient(), settings=settings),
        ],
    )
    with TestClient(api.app) as client:
        response = client.post("/collections/a-collection/items", json=item_collection)
        assert response.status_code == 201, response.text
        assert response.json()["type"] == "FeatureCollection"

        response = client.post("/collections/a-collection/items", json=item)
        assert response.status_code == 201, response.text
        assert response.json()["type"] == "Feature"


def test_update_item(client: TestClient, item: Item) -> None:
    response = client.put("/collections/a-collection/items/an-item", json=item)
    assert response.is_success, response.text
    assert response.json()["path_collection_id"] == "a-collection"
    assert response.json()["path_item_id"] == "an-item"
    assert response.json()["type"] == "Feature"


def test_patch_operation_item(client: TestClient) -> None:
    response = client.patch(
        "/collections/a-collection/items/an-item",
        json=[{"op": "add", "path": "/properties/foo", "value": "bar"}],
    )
    assert response.is_success, response.text
    assert response.json()["path_collection_id"] == "a-collection"
    assert response.json()["path_item_id"] == "an-item"
    assert response.json()["patch"] == [
        {"op": "add", "path": "/properties/foo", "value": "bar"}
    ]


def test_patch_merge_item(client: TestClient) -> None:
    response = client.patch(
        "/collections/a-collection/items/an-item",
        json={"properties": {"hello": "world", "foo": None}},
    )
    assert response.is_success, response.text
    assert response.json()["path_collection_id"] == "a-collection"
    assert response.json()["path_item_id"] == "an-item"
    assert response.json()["patch"] == [
        {"op": "add", "path": "/properties/hello", "value": "world"},
        {"op": "remove", "path": "/properties/foo"},
    ]


def test_delete_item(client: TestClient) -> None:
    response = client.delete("/collections/a-collection/items/an-item")
    assert response.is_success, response.text
    assert response.json()["path_collection_id"] == "a-collection"
    assert response.json()["path_item_id"] == "an-item"


def test_create_collection(client: TestClient, collection: Collection) -> None:
    response = client.post("/collections", json=collection)
    assert response.status_code == 201, response.text
    assert response.json()["type"] == "Collection"
    assert response.headers["location"].endswith("/collections/test_collection")


def test_create_collection_response_location(
    response_client: TestClient, collection: Collection
) -> None:
    """A `Location` header set by the client is preserved."""
    response = response_client.post("/collections", json=collection)
    assert response.status_code == 201, response.text
    assert response.headers["location"] == "/custom-location"


def test_update_collection(client: TestClient, collection: Collection) -> None:
    response = client.put("/collections/a-collection", json=collection)
    assert response.is_success, response.text
    assert response.json()["path_collection_id"] == "a-collection"
    assert response.json()["type"] == "Collection"


def test_patch_operation_collection(client: TestClient) -> None:
    response = client.patch(
        "/collections/a-collection",
        json=[{"op": "add", "path": "/properties/foo", "value": "bar"}],
    )
    assert response.is_success, response.text
    assert response.json()["path_collection_id"] == "a-collection"
    assert response.json()["patch"] == [
        {"op": "add", "path": "/properties/foo", "value": "bar"}
    ]


def test_patch_merge_collection(client: TestClient) -> None:
    response = client.patch(
        "/collections/a-collection",
        json={"summaries": {"hello": "world", "foo": None}},
    )
    assert response.is_success, response.text
    assert response.json()["path_collection_id"] == "a-collection"
    assert response.json()["patch"] == [
        {"op": "add", "path": "/summaries/hello", "value": "world"},
        {"op": "remove", "path": "/summaries/foo"},
    ]


def test_patch_merge_collection_links(client: TestClient) -> None:
    links = [{"rel": "self", "href": "https://example.com/collections/a-collection"}]
    response = client.patch("/collections/a-collection", json={"links": links})
    assert response.is_success, response.text
    assert response.json()["patch"] == [{"op": "add", "path": "/links", "value": links}]


def test_patch_merge_collection_extra_fields(client: TestClient) -> None:
    response = client.patch(
        "/collections/a-collection",
        json={
            "title": "A title",
            "sci:doi": "10.1000/xyz123",
            "renders": {"visual": {"assets": ["visual"]}, "old": None},
        },
    )
    assert response.is_success, response.text
    assert response.json()["path_collection_id"] == "a-collection"
    assert response.json()["patch"] == [
        {"op": "add", "path": "/title", "value": "A title"},
        {"op": "add", "path": "/sci:doi", "value": "10.1000/xyz123"},
        {"op": "add", "path": "/renders/visual/assets", "value": ["visual"]},
        {"op": "remove", "path": "/renders/old"},
    ]


def test_delete_collection(client: TestClient, collection: Collection) -> None:
    response = client.delete("/collections/a-collection")
    assert response.is_success, response.text
    assert response.json()["path_collection_id"] == "a-collection"


class RebasingTransactionsClient(DummyTransactionsClient):
    """Record the payload the client receives so rebasing can be asserted."""

    def __init__(self):
        self.received: list[Item | ItemCollection] = []
        self.updated: list[Item] = []

    def create_item(self, item: Item | ItemCollection, *args, **kwargs):
        self.received.append(item)
        return item.model_dump(mode="json")

    def update_item(self, collection_id: str, item_id: str, item: Item, **kwargs):
        self.updated.append(item)
        return item.model_dump(mode="json")


def make_transaction_app(core_client, client, **extension_kwargs):
    settings = ApiSettings()
    api = StacApi(
        settings=settings,
        client=core_client,
        extensions=[
            TransactionExtension(client=client, settings=settings, **extension_kwargs)
        ],
    )
    return TestClient(api.app)


@pytest.fixture
def rebasing_client(core_client: DummyCoreClient):
    client = RebasingTransactionsClient()
    api = make_transaction_app(
        core_client,
        client,
        prefix_mappings={"https://stac.internal/catalog": "s3://catalog-bucket/catalog"},
    )
    with api as test_client:
        yield test_client, client


def imported_item() -> dict:
    item = {
        "type": "Feature",
        "stac_version": "1.0.0",
        "stac_extensions": [],
        "id": "imported_item",
        "geometry": {"type": "Point", "coordinates": [-105, 40]},
        "bbox": [-105, 40, -105, 40],
        "properties": {"datetime": "2020-06-13T13:00:00Z"},
        "links": [
            {
                "rel": "self",
                "href": "https://stac.internal/catalog/collections/test_collection/items/imported_item",  # noqa: E501
            }
        ],
        "assets": {
            "visual": {"href": "data/visual.tif"},
            "external": {"href": "https://elsewhere.example.com/external.tif"},
        },
        "collection": "test_collection",
    }
    return item


def test_create_item_rebases_relative_assets(rebasing_client) -> None:
    client, transactions_client = rebasing_client

    response = client.post(
        "/collections/test_collection/items", json=imported_item()
    )
    assert response.status_code == 201, response.text

    received = transactions_client.received[-1]
    assert (
        received.assets["visual"].href
        == "s3://catalog-bucket/catalog/collections/test_collection/items/data/visual.tif"
    )
    assert (
        received.assets["external"].href
        == "https://elsewhere.example.com/external.tif"
    )


def test_create_item_rebasing_is_idempotent(rebasing_client) -> None:
    client, transactions_client = rebasing_client

    client.post("/collections/test_collection/items", json=imported_item())
    first = transactions_client.received[-1].assets["visual"].href

    # Re-import the payload the backend would read back; the prefix must not stack.
    stored = transactions_client.received[-1].model_dump(mode="json")
    client.post("/collections/test_collection/items", json=stored)
    second = transactions_client.received[-1].assets["visual"].href

    assert second == first


def test_create_item_without_original_reference_is_rejected(rebasing_client) -> None:
    client, transactions_client = rebasing_client

    item = imported_item()
    item["links"] = []
    response = client.post("/collections/test_collection/items", json=item)

    assert response.status_code == 400, response.text
    assert transactions_client.received == []


def test_create_item_collection_rebases_every_feature(rebasing_client) -> None:
    client, transactions_client = rebasing_client

    response = client.post(
        "/collections/test_collection/items",
        json={
            "type": "FeatureCollection",
            "features": [imported_item()],
            "links": [],
        },
    )
    assert response.status_code == 201, response.text

    received = transactions_client.received[-1]
    assert received.features[0].assets["visual"].href.startswith("s3://catalog-bucket")


def test_rebasing_is_opt_in(core_client: DummyCoreClient) -> None:
    """Without prefix mappings the payload reaches the client untouched."""
    transactions_client = RebasingTransactionsClient()
    api = make_transaction_app(core_client, transactions_client)

    with api as client:
        response = client.post(
            "/collections/test_collection/items", json=imported_item()
        )

    assert response.status_code == 201, response.text
    received = transactions_client.received[-1]
    assert received.assets["visual"].href == "data/visual.tif"
    assert received.links[0].href.startswith("https://stac.internal/catalog")


def test_update_item_is_not_rebased(rebasing_client) -> None:
    """Already stored objects are updated verbatim; only imports are rebased."""
    client, transactions_client = rebasing_client

    item = imported_item()
    response = client.put("/collections/test_collection/items/imported_item", json=item)

    assert response.is_success, response.text
    assert transactions_client.received == []
    assert transactions_client.updated[-1].assets["visual"].href == "data/visual.tif"


class InMemoryStore:
    """Minimal backend that stores rebased Items and serves both read paths."""

    def __init__(self):
        self.items: dict[str, dict] = {}


class StoringTransactionsClient(DummyTransactionsClient):
    """Store created Items so they can be read back through both read paths."""

    def __init__(self, store: InMemoryStore):
        self.store = store

    def create_item(self, item, *args, **kwargs):
        items = item.features if item.type == "FeatureCollection" else [item]
        for feature in items:
            self.store.items[feature.id] = feature.model_dump(mode="json")
        return item.model_dump(mode="json")


@pytest.fixture
def store_client(core_client: DummyCoreClient):
    store = InMemoryStore()
    settings = ApiSettings()

    class StoredCoreClient(core_client.__class__):
        def get_item(self, item_id, collection_id, **kwargs):
            return store.items[item_id]

        def get_search(self, *args, **kwargs):
            return {
                "type": "FeatureCollection",
                "features": list(store.items.values()),
                "links": [],
            }

        def post_search(self, *args, **kwargs):
            return self.get_search()

    api = StacApi(
        settings=settings,
        client=StoredCoreClient(),
        extensions=[
            TransactionExtension(
                client=StoringTransactionsClient(store),
                settings=settings,
                prefix_mappings={
                    "https://stac.internal/catalog": "s3://catalog-bucket/catalog"
                },
            )
        ],
    )
    with TestClient(api.app) as test_client:
        yield test_client


def test_item_and_search_read_paths_resolve_to_the_same_target(store_client) -> None:
    response = store_client.post(
        "/collections/test_collection/items", json=imported_item()
    )
    assert response.status_code == 201, response.text

    item_href = store_client.get(
        "/collections/test_collection/items/imported_item"
    ).json()["assets"]["visual"]["href"]
    get_search_href = store_client.get("/search").json()["features"][0]["assets"][
        "visual"
    ]["href"]
    post_search_href = store_client.post("/search", json={}).json()["features"][0][
        "assets"
    ]["visual"]["href"]

    expected = (
        "s3://catalog-bucket/catalog/collections/test_collection/items/data/visual.tif"
    )
    assert item_href == expected
    assert get_search_href == expected
    assert post_search_href == expected
