"""Tests for conditional (versioned) PATCH Item requests."""

import copy
import threading
from collections.abc import Iterator

import pytest
from stac_pydantic.item import Item
from starlette.testclient import TestClient

from stac_fastapi.api.app import StacApi
from stac_fastapi.extensions import TransactionExtension
from stac_fastapi.extensions.transaction import BaseTransactionsClient
from stac_fastapi.extensions.transaction.request import (
    PartialItem,
    PatchOperation,
)
from stac_fastapi.types.config import ApiSettings
from stac_fastapi.types.core import BaseCoreClient
from stac_fastapi.types.errors import VersionConflictError
from stac_fastapi.types.etag import item_version, quote_etag
from stac_fastapi.types.patch import PatchError, apply_patch, conditional_patch


class DummyCoreClient(BaseCoreClient):
    def all_collections(self, *args, **kwargs):
        raise NotImplementedError

    def get_collection(self, *args, **kwargs):
        raise NotImplementedError

    def get_item(self, item_id: str, collection_id: str, **kwargs):
        return STORE.get(collection_id, item_id)

    def get_search(self, *args, **kwargs):
        raise NotImplementedError

    def post_search(self, *args, **kwargs):
        raise NotImplementedError

    def item_collection(self, *args, **kwargs):
        raise NotImplementedError


class Store:
    """A minimal versioned item store, used as the backend of the tests.

    All of an update's steps -- reading the item, comparing its version,
    computing the patched item and writing it -- happen while holding one lock,
    which is what makes the comparison and the write atomic.
    """

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict] = {}
        self.lock = threading.Lock()

    def put(self, collection_id: str, item: dict) -> None:
        self.items[(collection_id, item["id"])] = copy.deepcopy(item)

    def get(self, collection_id: str, item_id: str) -> dict | None:
        item = self.items.get((collection_id, item_id))
        return copy.deepcopy(item) if item is not None else None


STORE = Store()


class VersionedTransactionsClient(BaseTransactionsClient):
    """Applies patches to the in-process store, honouring expected versions."""

    def create_item(self, item, *args, **kwargs):
        return item

    def update_item(self, collection_id, item_id, item, **kwargs):
        return item

    def patch_item(
        self,
        collection_id: str,
        item_id: str,
        patch: PartialItem | list[PatchOperation],
        expected_version: str | None = None,
        **kwargs,
    ):
        with STORE.lock:
            stored = STORE.items.get((collection_id, item_id))
            if stored is None:
                raise KeyError(item_id)
            # Comparison, computation and write happen under the same lock.
            patched = conditional_patch(stored, patch, expected_version)
            STORE.items[(collection_id, item_id)] = patched
            return copy.deepcopy(patched)

    def delete_item(self, item_id, collection_id, **kwargs):
        return STORE.items.pop((collection_id, item_id), None)

    def create_collection(self, collection, **kwargs):
        return collection

    def update_collection(self, collection_id, collection, **kwargs):
        return collection

    def patch_collection(self, collection_id, patch, **kwargs):
        return patch

    def delete_collection(self, collection_id, **kwargs):
        return None


@pytest.fixture
def item() -> dict:
    return {
        "type": "Feature",
        "stac_version": "1.0.0",
        "stac_extensions": [],
        "id": "test_item",
        "geometry": {"type": "Point", "coordinates": [-105, 40]},
        "bbox": [-105, 40, -105, 40],
        "properties": {"datetime": "2020-06-13T13:00:00Z", "foo": "original"},
        "links": [],
        "assets": {},
        "collection": "test_collection",
    }


@pytest.fixture
def client(item: dict) -> Iterator[TestClient]:
    STORE.items = {("test_collection", "test_item"): copy.deepcopy(item)}
    settings = ApiSettings()
    api = StacApi(
        settings=settings,
        client=DummyCoreClient(),
        extensions=[
            TransactionExtension(client=VersionedTransactionsClient(), settings=settings)
        ],
    )
    with TestClient(api.app) as client:
        yield client


def read_version(client: TestClient) -> str:
    """Return the version a normal Item read exposes."""
    response = client.get("/collections/test_collection/items/test_item")
    assert response.is_success, response.text
    assert response.headers["etag"]
    return response.headers["etag"]


def test_get_item_returns_a_version(client: TestClient) -> None:
    version = read_version(client)
    assert version.startswith('"') and version.endswith('"')


def test_version_is_the_item_content_version(client: TestClient, item: dict) -> None:
    assert read_version(client) == quote_etag(item_version(item))


def test_patch_without_version_is_unchanged(client: TestClient) -> None:
    response = client.patch(
        "/collections/test_collection/items/test_item",
        json={"properties": {"foo": "legacy"}},
    )
    assert response.is_success, response.text
    assert response.json()["properties"]["foo"] == "legacy"


def test_patch_with_current_version_merge(client: TestClient) -> None:
    version = read_version(client)
    response = client.patch(
        "/collections/test_collection/items/test_item",
        json={"properties": {"foo": "updated"}},
        headers={"If-Match": version},
    )
    assert response.is_success, response.text
    assert response.json()["properties"]["foo"] == "updated"

    # The response carries the new version, and a re-read agrees with it.
    assert response.headers["etag"] == read_version(client)
    assert response.headers["etag"] != version


def test_patch_with_current_version_json_patch(client: TestClient) -> None:
    version = read_version(client)
    response = client.patch(
        "/collections/test_collection/items/test_item",
        json=[{"op": "replace", "path": "/properties/foo", "value": "updated"}],
        headers={"If-Match": version},
    )
    assert response.is_success, response.text
    assert response.json()["properties"]["foo"] == "updated"


def test_patch_with_version_query_parameter(client: TestClient) -> None:
    version = read_version(client)
    response = client.patch(
        "/collections/test_collection/items/test_item",
        json={"properties": {"foo": "updated"}},
        params={"expected_version": version},
    )
    assert response.is_success, response.text
    assert response.json()["properties"]["foo"] == "updated"


def test_patch_with_the_version_alias(client: TestClient) -> None:
    version = read_version(client)
    response = client.patch(
        "/collections/test_collection/items/test_item",
        json={"properties": {"foo": "updated"}},
        params={"version": version},
    )
    assert response.is_success, response.text
    assert response.json()["properties"]["foo"] == "updated"


def test_stale_version_alias_conflicts(client: TestClient) -> None:
    stale = read_version(client)
    client.patch(
        "/collections/test_collection/items/test_item",
        json={"properties": {"foo": "first"}},
        params={"expected_version": stale},
    )
    response = client.patch(
        "/collections/test_collection/items/test_item",
        json={"properties": {"foo": "second"}},
        params={"version": stale},
    )
    assert response.status_code == 409
    assert (
        client.get("/collections/test_collection/items/test_item").json()["properties"][
            "foo"
        ]
        == "first"
    )


def test_stale_patch_conflicts_and_writes_nothing(client: TestClient) -> None:
    stale = read_version(client)

    first = client.patch(
        "/collections/test_collection/items/test_item",
        json={"properties": {"foo": "first"}},
        headers={"If-Match": stale},
    )
    assert first.is_success, first.text

    second = client.patch(
        "/collections/test_collection/items/test_item",
        json={"properties": {"foo": "second"}},
        headers={"If-Match": stale},
    )
    assert second.status_code == 409, second.text

    # The first write is still effective, and a re-read returns it unchanged.
    reread = client.get("/collections/test_collection/items/test_item")
    assert reread.json()["properties"]["foo"] == "first"


def test_reread_after_conflict_allows_the_patch_to_continue(client: TestClient) -> None:
    stale = read_version(client)
    client.patch(
        "/collections/test_collection/items/test_item",
        json={"properties": {"foo": "first"}},
        headers={"If-Match": stale},
    )
    assert (
        client.patch(
            "/collections/test_collection/items/test_item",
            json={"properties": {"foo": "second"}},
            headers={"If-Match": stale},
        ).status_code
        == 409
    )

    current = read_version(client)
    assert (
        client.patch(
            "/collections/test_collection/items/test_item",
            json={"properties": {"foo": "second"}},
            headers={"If-Match": current},
        ).status_code
        == 200
    )
    assert (
        client.get("/collections/test_collection/items/test_item").json()["properties"][
            "foo"
        ]
        == "second"
    )


def test_stale_json_patch_conflicts_and_writes_nothing(client: TestClient) -> None:
    stale = read_version(client)
    client.patch(
        "/collections/test_collection/items/test_item",
        json=[{"op": "replace", "path": "/properties/foo", "value": "first"}],
        headers={"If-Match": stale},
    )

    response = client.patch(
        "/collections/test_collection/items/test_item",
        json=[{"op": "replace", "path": "/properties/foo", "value": "second"}],
        headers={"If-Match": stale},
    )
    assert response.status_code == 409, response.text
    assert (
        client.get("/collections/test_collection/items/test_item").json()["properties"][
            "foo"
        ]
        == "first"
    )


def test_wildcard_version_matches_any_item(client: TestClient) -> None:
    response = client.patch(
        "/collections/test_collection/items/test_item",
        json={"properties": {"foo": "wildcard"}},
        headers={"If-Match": "*"},
    )
    assert response.is_success, response.text


def test_patch_openapi_documents_the_expected_version(client: TestClient) -> None:
    operation = client.get("/api").json()["paths"][
        "/collections/{collection_id}/items/{item_id}"
    ]["patch"]
    parameters = {parameter["name"] for parameter in operation["parameters"]}
    assert "If-Match" in parameters
    assert "expected_version" in parameters
    assert "409" in operation["responses"]


def test_conditional_patch_does_not_modify_the_item_on_conflict(item: dict) -> None:
    before = copy.deepcopy(item)
    with pytest.raises(VersionConflictError):
        conditional_patch(item, {"properties": {"foo": "x"}}, '"stale"')
    assert item == before


def test_conditional_patch_does_not_modify_the_item_on_a_bad_operation(
    item: dict,
) -> None:
    before = copy.deepcopy(item)
    with pytest.raises(PatchError):
        apply_patch(item, [{"op": "remove", "path": "/properties/missing"}])
    assert item == before


def test_concurrent_patches_from_the_same_version() -> None:
    """Two jobs patching the same read: exactly one write lands."""
    STORE.items = {
        ("test_collection", "test_item"): {
            "type": "Feature",
            "stac_version": "1.0.0",
            "id": "test_item",
            "geometry": None,
            "properties": {"datetime": "2020-06-13T13:00:00Z", "foo": "original"},
            "links": [],
            "assets": {},
            "collection": "test_collection",
        }
    }
    settings = ApiSettings()
    api = StacApi(
        settings=settings,
        client=DummyCoreClient(),
        extensions=[
            TransactionExtension(
                client=VersionedTransactionsClient(), settings=settings
            )
        ],
    )

    with TestClient(api.app) as client:
        version = read_version(client)
        statuses: list[int] = []
        barrier = threading.Barrier(2)

        def patch(value: str) -> None:
            barrier.wait()
            response = client.patch(
                "/collections/test_collection/items/test_item",
                json={"properties": {"foo": value}},
                headers={"If-Match": version},
            )
            statuses.append(response.status_code)

        threads = [threading.Thread(target=patch, args=(value,)) for value in "ab"]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sorted(statuses) == [200, 409]
        final = client.get("/collections/test_collection/items/test_item")
        assert final.json()["properties"]["foo"] in {"a", "b"}


def test_legacy_client_without_the_version_argument_still_works(item: dict) -> None:
    """A client written before the argument existed is not passed it."""

    class LegacyClient(VersionedTransactionsClient):
        def patch_item(self, collection_id, item_id, patch, **kwargs):
            return {
                "path_collection_id": collection_id,
                "path_item_id": item_id,
                "patch": patch.model_dump() if isinstance(patch, PartialItem) else patch,
            }

    settings = ApiSettings()
    api = StacApi(
        settings=settings,
        client=DummyCoreClient(),
        extensions=[
            TransactionExtension(client=LegacyClient(), settings=settings)
        ],
    )
    with TestClient(api.app) as client:
        response = client.patch(
            "/collections/test_collection/items/test_item",
            json={"properties": {"foo": "updated"}},
            headers={"If-Match": '"whatever"'},
        )
        assert response.is_success, response.text
        assert response.json()["patch"] == {
            "properties": {"foo": "updated"}
        }


@pytest.mark.parametrize(
    "settings",
    [
        ApiSettings(enable_response_models=True),
        ApiSettings(enable_direct_response=True),
    ],
    ids=["response_models", "direct_response"],
)
def test_conditional_patch_in_other_response_modes(settings, item: dict) -> None:
    """The version works the same with response models and direct responses."""
    item = {
        **item,
        "links": [
            {
                "rel": "self",
                "href": "http://testserver/collections/test_collection/items/test_item",
                "type": "application/geo+json",
            },
            {
                "rel": "root",
                "href": "http://testserver/",
                "type": "application/json",
            },
            {
                "rel": "collection",
                "href": "http://testserver/collections/test_collection",
                "type": "application/json",
            },
        ],
    }
    STORE.items = {("test_collection", "test_item"): copy.deepcopy(item)}
    api = StacApi(
        settings=settings,
        client=DummyCoreClient(),
        extensions=[
            TransactionExtension(client=VersionedTransactionsClient(), settings=settings)
        ],
    )
    with TestClient(api.app) as client:
        response = client.get("/collections/test_collection/items/test_item")
        version = response.headers["etag"]

        patched = client.patch(
            "/collections/test_collection/items/test_item",
            json={"properties": {"foo": "updated"}},
            headers={"If-Match": version},
        )
        assert patched.is_success, patched.text
        assert patched.headers["etag"] == read_version(client)

        stale = client.patch(
            "/collections/test_collection/items/test_item",
            json={"properties": {"foo": "stale"}},
            headers={"If-Match": version},
        )
        assert stale.status_code == 409
        assert (
            client.get("/collections/test_collection/items/test_item").json()[
                "properties"
            ]["foo"]
            == "updated"
        )
