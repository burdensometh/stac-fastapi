"""Tests for the Bulk Transactions extension."""

from collections.abc import Iterator
from unittest.mock import Mock

import pytest
from fastapi import Depends, HTTPException, Response, security, status
from pydantic import ValidationError
from starlette.testclient import TestClient

from stac_fastapi.api.app import StacApi
from stac_fastapi.extensions.bulk_transactions import (
    AsyncBaseBulkTransactionsClient,
    AsyncBaseIdempotentBulkTransactionsClient,
    BaseBulkTransactionsClient,
    BaseIdempotentBulkTransactionsClient,
    BulkImportReceipt,
    BulkImportReceiptExistsError,
    BulkTransaction,
    BulkTransactionExtension,
    BulkTransactionMethod,
    Items,
    bulk_import_fingerprint,
)
from stac_fastapi.types.config import ApiSettings
from stac_fastapi.types.core import BaseCoreClient

# --- UNIT TESTS ---


def test_items_model_iteration():
    """Test the Items pydantic model and its custom iterator."""
    item_data = {
        "id1": {"type": "Feature", "id": "id1"},
        "id2": {"type": "Feature", "id": "id2"},
    }
    bulk_items = Items(items=item_data, method=BulkTransactionMethod.INSERT)

    # Test iteration returns values, not keys
    items_list = list(bulk_items)
    assert len(items_list) == 2
    assert {"type": "Feature", "id": "id1"} in items_list
    assert bulk_items.method == "insert"


def test_base_client_chunks():
    """Test the static _chunks method on BaseBulkTransactionsClient."""
    test_list = [1, 2, 3, 4, 5, 6, 7]

    # Chunk by 3
    chunks = list(BaseBulkTransactionsClient._chunks(test_list, 3))
    assert chunks == [[1, 2, 3], [4, 5, 6], [7]]

    # Chunk by larger than list
    chunks_large = list(BaseBulkTransactionsClient._chunks(test_list, 10))
    assert chunks_large == [[1, 2, 3, 4, 5, 6, 7]]


def test_bulk_transaction_extension_defaults():
    """Test the default instantiation of the BulkTransactionExtension."""
    mock_client = Mock(spec=AsyncBaseBulkTransactionsClient)
    ext = BulkTransactionExtension(client=mock_client)

    assert ext.client == mock_client
    assert ext.schema_href is None
    assert ext.conformance_classes == []


def test_bulk_transaction_extension_customization():
    """Test instantiating BulkTransactionExtension with custom arguments."""
    mock_client = Mock(spec=AsyncBaseBulkTransactionsClient)
    custom_conformance = ["https://example.com/bulk-spec"]
    custom_schema = "https://example.com/bulk-schema.json"
    custom_dependencies = [Depends(must_be_bob)]

    ext = BulkTransactionExtension(
        client=mock_client,
        conformance_classes=custom_conformance,
        schema_href=custom_schema,
        route_dependencies=custom_dependencies,
    )

    assert ext.conformance_classes == custom_conformance
    assert ext.schema_href == custom_schema
    assert ext.route_dependencies == custom_dependencies


# --- INTEGRATION TESTS ---


class DummyCoreClient(BaseCoreClient):
    """Dummy core client for routing purposes."""

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


class DummyBulkTransactionsClient(AsyncBaseBulkTransactionsClient):
    """Dummy client returning a success message to verify routing and parsing."""

    async def bulk_item_insert(
        self, items: Items, **kwargs
    ) -> BulkTransaction | Response:
        """Mock bulk insert that just returns a string confirming the count."""
        count = len(list(items))
        return {
            "received": count,
            "success": count,
            "skipped": 0,
            "errors": [],
        }


class DummyBulkTransactionsClientWithError(AsyncBaseBulkTransactionsClient):
    """Dummy client returning a success message to verify routing and parsing."""

    async def bulk_item_insert(
        self, items: Items, **kwargs
    ) -> BulkTransaction | Response:
        """Mock bulk insert that just returns a string confirming the count."""
        count = len(list(items)) - 1
        return {
            "received": count,
            "success": count,
            "skipped": 0,
            "errors": [
                {"id": "test_item", "msg": "Item already exists"},
            ],
        }


@pytest.fixture
def core_client() -> DummyCoreClient:
    return DummyCoreClient()


@pytest.fixture
def bulk_transactions_client() -> DummyBulkTransactionsClient:
    return DummyBulkTransactionsClient()


@pytest.fixture
def client(
    core_client: DummyCoreClient, bulk_transactions_client: DummyBulkTransactionsClient
) -> Iterator[TestClient]:
    """Fixture to set up the TestClient with the BulkTransactionExtension."""
    settings = ApiSettings()
    api = StacApi(
        settings=settings,
        client=core_client,
        extensions=[
            BulkTransactionExtension(client=bulk_transactions_client),
        ],
    )
    with TestClient(api.app) as client:
        yield client


@pytest.fixture
def client_with_error(core_client: DummyCoreClient) -> Iterator[TestClient]:
    """Fixture to set up the TestClient with the BulkTransactionExtension."""
    settings = ApiSettings()
    api = StacApi(
        settings=settings,
        client=core_client,
        extensions=[
            BulkTransactionExtension(client=DummyBulkTransactionsClientWithError()),
        ],
    )
    with TestClient(api.app) as client:
        yield client


@pytest.fixture
def item() -> dict:
    """A standard STAC Item dictionary."""
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
        "collection": "a-collection",
    }


def test_bulk_item_insert(client: TestClient, item: dict) -> None:
    """Test the bulk insert endpoint processes items correctly."""
    payload = {
        "items": {item["id"]: item, "test_item_2": {**item, "id": "test_item_2"}},
        "method": "insert",
    }

    response = client.post("/collections/a-collection/bulk_items", json=payload)

    assert response.is_success, response.text
    assert response.json() == {
        "received": 2,
        "success": 2,
        "skipped": 0,
        "errors": [],
    }


def test_bulk_item_upsert(client: TestClient, item: dict) -> None:
    """Test the bulk insert endpoint properly passes the 'upsert' method."""
    payload = {"items": {item["id"]: item}, "method": "upsert"}

    response = client.post("/collections/a-collection/bulk_items", json=payload)

    assert response.is_success, response.text
    assert response.json() == {
        "received": 1,
        "success": 1,
        "skipped": 0,
        "errors": [],
    }


def test_bulk_item_invalid_method(client: TestClient, item: dict) -> None:
    """Test that pydantic catches invalid bulk transaction methods."""
    payload = {"items": {item["id"]: item}, "method": "invalid_method"}

    response = client.post("/collections/a-collection/bulk_items", json=payload)

    assert response.status_code == 400


def test_bulk_item_insert_with_route_dependencies(item: dict) -> None:
    """Test route dependencies are applied to the bulk transactions route."""
    settings = ApiSettings()
    api = StacApi(
        settings=settings,
        client=DummyCoreClient(),
        extensions=[
            BulkTransactionExtension(
                client=DummyBulkTransactionsClient(),
                route_dependencies=[Depends(must_be_bob)],
            ),
        ],
    )
    payload = {
        "items": {item["id"]: item},
        "method": "insert",
    }

    with TestClient(api.app) as client:
        unauthenticated_response = client.post(
            "/collections/a-collection/bulk_items", json=payload
        )
        assert unauthenticated_response.status_code == 401
        assert unauthenticated_response.json() == {"detail": "Not authenticated"}

        authenticated_response = client.post(
            "/collections/a-collection/bulk_items",
            json=payload,
            auth=("bob", "dobbs"),
        )
        assert authenticated_response.is_success, authenticated_response.text
        assert authenticated_response.json() == {
            "received": 1,
            "success": 1,
            "skipped": 0,
            "errors": [],
        }


def test_bulk_item_insert_with_error(client_with_error: TestClient, item: dict) -> None:
    """Test the bulk insert endpoint processes items correctly."""
    payload = {
        "items": {item["id"]: item, "test_item_2": {**item, "id": "test_item_2"}},
        "method": "insert",
    }

    response = client_with_error.post(
        "/collections/a-collection/bulk_items", json=payload
    )

    assert response.is_success, response.text
    assert response.json() == {
        "received": 1,
        "success": 1,
        "skipped": 0,
        "errors": [{"id": "test_item", "msg": "Item already exists"}],
    }


def must_be_bob(
    credentials: security.HTTPBasicCredentials = Depends(security.HTTPBasic()),
) -> bool:
    """Require HTTP basic auth for user bob."""
    if credentials.username == "bob":
        return True

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="You're not Bob",
        headers={"WWW-Authenticate": "Basic"},
    )


# --- IDEMPOTENT IMPORT TESTS ---


def test_items_model_request_id_optional() -> None:
    """Test that request_id defaults to None and rejects empty values."""
    bulk_items = Items(items={"id1": {"type": "Feature", "id": "id1"}})
    assert bulk_items.request_id is None

    bulk_items = Items(
        items={"id1": {"type": "Feature", "id": "id1"}}, request_id="import-1"
    )
    assert bulk_items.request_id == "import-1"

    with pytest.raises(ValidationError):
        Items(items={"id1": {"type": "Feature", "id": "id1"}}, request_id="")


def test_bulk_import_fingerprint_identifies_import() -> None:
    """Test the fingerprint covers collection, method and actual content."""
    items = {
        "id1": {"type": "Feature", "id": "id1", "properties": {"a": 1, "b": 2}},
        "id2": {"type": "Feature", "id": "id2"},
    }
    fingerprint = bulk_import_fingerprint("a-collection", "insert", items)

    # JSON object key order does not matter, at any nesting level
    reordered = {
        "id2": {"type": "Feature", "id": "id2"},
        "id1": {"id": "id1", "type": "Feature", "properties": {"b": 2, "a": 1}},
    }
    assert bulk_import_fingerprint("a-collection", "insert", reordered) == fingerprint

    # the collection, the write method and the actual content all identify
    # the import
    assert (
        bulk_import_fingerprint("other-collection", "insert", items) != fingerprint
    )
    assert bulk_import_fingerprint("a-collection", "upsert", items) != fingerprint
    assert (
        bulk_import_fingerprint(
            "a-collection",
            "insert",
            {"id1": {"type": "Feature", "id": "id1", "properties": {"a": 1}}},
        )
        != fingerprint
    )


class DummyIdempotentBulkTransactionsClient(AsyncBaseIdempotentBulkTransactionsClient):
    """In-memory idempotent client simulating an atomic data+receipt backend."""

    def __init__(self) -> None:
        self.receipts: dict[tuple[str, str], BulkImportReceipt] = {}
        self.commits = 0
        # simulate a receipt lookup that ran before a concurrent import
        # committed
        self.miss_next_lookup = False

    async def get_bulk_import_receipt(
        self, collection_id: str, request_id: str, **kwargs
    ) -> BulkImportReceipt | None:
        if self.miss_next_lookup:
            self.miss_next_lookup = False
            return None
        return self.receipts.get((collection_id, request_id))

    async def bulk_item_insert(self, items: Items, **kwargs) -> BulkTransaction:
        collection_id = kwargs["request"].path_params["collection_id"]
        key = (collection_id, items.request_id)
        if key in self.receipts:
            # unique constraint on (collection_id, request_id); the whole
            # transaction (items + receipt) is rolled back
            raise BulkImportReceiptExistsError(
                f"receipt already committed for request {items.request_id!r}"
            )
        errors = [
            {"id": item_id, "msg": "Item already exists"}
            for item_id, item in items.items.items()
            if item.get("properties", {}).get("fail")
        ]
        result: BulkTransaction = {
            "received": len(items.items),
            "success": len(items.items) - len(errors),
            "skipped": 0,
            "errors": errors,
        }
        self.receipts[key] = {
            "request_id": items.request_id,
            "content_hash": kwargs["content_hash"],
            "result": result,
        }
        self.commits += 1
        return result


class DummySyncIdempotentBulkTransactionsClient(BaseIdempotentBulkTransactionsClient):
    """Synchronous in-memory idempotent client."""

    def __init__(self) -> None:
        self.receipts: dict[tuple[str, str], BulkImportReceipt] = {}
        self.commits = 0

    def get_bulk_import_receipt(
        self, collection_id: str, request_id: str, **kwargs
    ) -> BulkImportReceipt | None:
        return self.receipts.get((collection_id, request_id))

    def bulk_item_insert(
        self, items: Items, chunk_size: int | None = None, **kwargs
    ) -> BulkTransaction:
        collection_id = kwargs["request"].path_params["collection_id"]
        key = (collection_id, items.request_id)
        if key in self.receipts:
            raise BulkImportReceiptExistsError(
                f"receipt already committed for request {items.request_id!r}"
            )
        count = len(items.items)
        result: BulkTransaction = {
            "received": count,
            "success": count,
            "skipped": 0,
            "errors": [],
        }
        self.receipts[key] = {
            "request_id": items.request_id,
            "content_hash": kwargs["content_hash"],
            "result": result,
        }
        self.commits += 1
        return result


@pytest.fixture
def idempotent_client() -> DummyIdempotentBulkTransactionsClient:
    return DummyIdempotentBulkTransactionsClient()


@pytest.fixture
def client_with_idempotency(
    core_client: DummyCoreClient,
    idempotent_client: DummyIdempotentBulkTransactionsClient,
) -> Iterator[TestClient]:
    """Fixture with a backend supporting idempotent bulk imports."""
    settings = ApiSettings()
    api = StacApi(
        settings=settings,
        client=core_client,
        extensions=[BulkTransactionExtension(client=idempotent_client)],
    )
    with TestClient(api.app) as client:
        yield client


def test_bulk_item_insert_with_request_id_replays_committed_result(
    client_with_idempotency: TestClient,
    idempotent_client: DummyIdempotentBulkTransactionsClient,
    item: dict,
) -> None:
    """A retry with the same request id gets the stored per-item result."""
    payload = {
        "items": {
            item["id"]: item,
            "bad_item": {
                **item,
                "id": "bad_item",
                "properties": {"datetime": "2020-06-13T13:00:00Z", "fail": True},
            },
        },
        "method": "upsert",
        "request_id": "import-1",
    }

    first = client_with_idempotency.post(
        "/collections/a-collection/bulk_items", json=payload
    )
    assert first.is_success, first.text
    expected = {
        "received": 2,
        "success": 1,
        "skipped": 0,
        "errors": [{"id": "bad_item", "msg": "Item already exists"}],
    }
    assert first.json() == expected

    # retrying after e.g. a network timeout returns the committed per-item
    # result and does not import the items again
    replay = client_with_idempotency.post(
        "/collections/a-collection/bulk_items", json=payload
    )
    assert replay.is_success, replay.text
    assert replay.json() == expected
    assert idempotent_client.commits == 1


def test_bulk_item_insert_concurrent_request_commits_once(
    client_with_idempotency: TestClient,
    idempotent_client: DummyIdempotentBulkTransactionsClient,
    item: dict,
) -> None:
    """Two identical racing requests commit only once.

    The loser of the race fails to record its receipt and instead returns
    the receipt stored by the winner.
    """
    payload = {
        "items": {item["id"]: item},
        "method": "insert",
        "request_id": "import-1",
    }

    first = client_with_idempotency.post(
        "/collections/a-collection/bulk_items", json=payload
    )
    assert first.is_success, first.text

    # simulate a concurrent request whose receipt lookup ran before the
    # first import committed
    idempotent_client.miss_next_lookup = True
    replay = client_with_idempotency.post(
        "/collections/a-collection/bulk_items", json=payload
    )
    assert replay.is_success, replay.text
    assert replay.json() == first.json()
    assert idempotent_client.commits == 1


def test_bulk_item_insert_request_id_conflict_on_different_items(
    client_with_idempotency: TestClient,
    idempotent_client: DummyIdempotentBulkTransactionsClient,
    item: dict,
) -> None:
    """The same request id with different content is a 409 conflict."""
    payload = {
        "items": {item["id"]: item},
        "method": "insert",
        "request_id": "import-1",
    }
    response = client_with_idempotency.post(
        "/collections/a-collection/bulk_items", json=payload
    )
    assert response.is_success, response.text

    conflicting = client_with_idempotency.post(
        "/collections/a-collection/bulk_items",
        json={
            "items": {"other_item": {**item, "id": "other_item"}},
            "method": "insert",
            "request_id": "import-1",
        },
    )
    assert conflicting.status_code == 409
    assert conflicting.json()["code"] == "ConflictError"
    assert idempotent_client.commits == 1


def test_bulk_item_insert_request_id_conflict_on_different_method(
    client_with_idempotency: TestClient,
    idempotent_client: DummyIdempotentBulkTransactionsClient,
    item: dict,
) -> None:
    """The write method is part of the import identity."""
    payload = {
        "items": {item["id"]: item},
        "method": "insert",
        "request_id": "import-1",
    }
    response = client_with_idempotency.post(
        "/collections/a-collection/bulk_items", json=payload
    )
    assert response.is_success, response.text

    conflicting = client_with_idempotency.post(
        "/collections/a-collection/bulk_items",
        json={**payload, "method": "upsert"},
    )
    assert conflicting.status_code == 409
    assert idempotent_client.commits == 1


def test_bulk_item_insert_request_id_scoped_to_collection(
    client_with_idempotency: TestClient,
    idempotent_client: DummyIdempotentBulkTransactionsClient,
    item: dict,
) -> None:
    """The same request id on another collection is an independent import."""
    payload = {
        "items": {item["id"]: item},
        "method": "insert",
        "request_id": "import-1",
    }
    for collection_id in ("a-collection", "another-collection"):
        response = client_with_idempotency.post(
            f"/collections/{collection_id}/bulk_items", json=payload
        )
        assert response.is_success, response.text

    assert idempotent_client.commits == 2


def test_bulk_item_insert_request_id_unsupported_backend(
    client: TestClient, item: dict
) -> None:
    """Backends without idempotent import support reject request ids."""
    payload = {
        "items": {item["id"]: item},
        "method": "insert",
        "request_id": "import-1",
    }
    response = client.post("/collections/a-collection/bulk_items", json=payload)
    assert response.status_code == 400


def test_bulk_item_insert_with_request_id_sync_client(
    core_client: DummyCoreClient, item: dict
) -> None:
    """Synchronous idempotent clients get the same replay behavior."""
    sync_client = DummySyncIdempotentBulkTransactionsClient()
    settings = ApiSettings()
    api = StacApi(
        settings=settings,
        client=core_client,
        extensions=[BulkTransactionExtension(client=sync_client)],
    )
    payload = {
        "items": {item["id"]: item},
        "method": "insert",
        "request_id": "import-1",
    }
    with TestClient(api.app) as client:
        first = client.post("/collections/a-collection/bulk_items", json=payload)
        assert first.is_success, first.text
        replay = client.post("/collections/a-collection/bulk_items", json=payload)
        assert replay.is_success, replay.text
        assert replay.json() == first.json()

    assert sync_client.commits == 1
