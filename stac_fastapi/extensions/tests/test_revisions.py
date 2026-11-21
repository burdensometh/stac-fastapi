"""Tests for the revision stream (change feed) of the transaction extensions."""

from collections.abc import Iterator

import pytest
from stac_pydantic import Collection
from stac_pydantic.item import Item
from stac_pydantic.item_collection import ItemCollection
from starlette.responses import Response
from starlette.testclient import TestClient

from stac_fastapi.api.app import StacApi
from stac_fastapi.extensions import (
    BulkTransactionExtension,
    RevisionStreamExtension,
    TransactionExtension,
)
from stac_fastapi.extensions.bulk_transactions import (
    BaseBulkTransactionsClient,
    BulkTransaction,
)
from stac_fastapi.extensions.revisions import (
    InMemoryRevisionStore,
    RevisionStreamConformanceClasses,
)
from stac_fastapi.extensions.transaction import BaseTransactionsClient
from stac_fastapi.extensions.transaction.request import (
    PartialCollection,
    PartialItem,
    PatchOperation,
)
from stac_fastapi.types.config import ApiSettings
from stac_fastapi.types.core import BaseCoreClient
from stac_fastapi.types.errors import ConflictError, NotFoundError


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


class CatalogTransactionsClient(BaseTransactionsClient):
    """In-memory transactions client mimicking a real backend."""

    def __init__(self):
        self.db: dict[tuple[str, str], dict] = {}

    def create_item(self, collection_id: str, item: Item | ItemCollection, **kwargs):
        if item.type == "Feature":
            data = item.model_dump(mode="json", exclude_none=True)
            key = (collection_id, data["id"])
            if key in self.db:
                raise ConflictError(f"Item {data['id']} already exists")
            self.db[key] = data
            return data
        features = []
        for feature in item.features:
            data = feature.model_dump(mode="json", exclude_none=True)
            key = (collection_id, data["id"])
            if key in self.db:
                raise ConflictError(f"Item {data['id']} already exists")
            self.db[key] = data
            features.append(data)
        return {"type": "FeatureCollection", "features": features, "links": []}

    def update_item(self, collection_id: str, item_id: str, item: Item, **kwargs):
        key = (collection_id, item_id)
        if key not in self.db:
            raise NotFoundError(f"Item {item_id} does not exist")
        data = item.model_dump(mode="json", exclude_none=True)
        self.db[key] = data
        return data

    def patch_item(
        self,
        collection_id: str,
        item_id: str,
        patch: PartialItem | list[PatchOperation],
        **kwargs,
    ):
        key = (collection_id, item_id)
        if key not in self.db:
            raise NotFoundError(f"Item {item_id} does not exist")
        if isinstance(patch, PartialItem):
            patch = patch.operations()
        item = self.db[key]
        for operation in patch:
            *parents, leaf = operation.path.strip("/").split("/")
            target = item
            for part in parents:
                target = target.setdefault(part, {})
            if operation.op == "remove":
                target.pop(leaf, None)
            elif operation.op in ("add", "replace"):
                target[leaf] = operation.value
        return item

    def delete_item(self, item_id: str, collection_id: str, **kwargs):
        key = (collection_id, item_id)
        if key not in self.db:
            raise NotFoundError(f"Item {item_id} does not exist")
        return self.db.pop(key)

    def create_collection(self, collection: Collection, **kwargs):
        return collection.model_dump(mode="json", exclude_none=True)

    def update_collection(self, collection_id: str, collection: Collection, **kwargs):
        return collection.model_dump(mode="json", exclude_none=True)

    def patch_collection(
        self,
        collection_id: str,
        patch: PartialCollection | list[PatchOperation],
        **kwargs,
    ):
        return {"id": collection_id}

    def delete_collection(self, collection_id: str, **kwargs):
        return {"id": collection_id}


class CatalogBulkClient(BaseBulkTransactionsClient):
    """Bulk client writing to the same db; duplicates are reported as errors."""

    def __init__(self, db: dict):
        self.db = db

    def bulk_item_insert(self, items, **kwargs) -> BulkTransaction:
        errors, written = [], []
        for item in items:
            key = (item.get("collection"), item["id"])
            if key in self.db:
                errors.append({"id": item["id"], "msg": "Item already exists"})
            else:
                written.append((key, item))
        for key, item in written:
            self.db[key] = item
        return {
            "received": len(items.items),
            "success": len(written),
            "skipped": len(errors),
            "errors": errors,
        }


@pytest.fixture
def core_client() -> DummyCoreClient:
    return DummyCoreClient()


@pytest.fixture
def store() -> InMemoryRevisionStore:
    return InMemoryRevisionStore(retention=100)


@pytest.fixture
def transactions_client() -> CatalogTransactionsClient:
    return CatalogTransactionsClient()


@pytest.fixture
def client(
    core_client: DummyCoreClient,
    transactions_client: CatalogTransactionsClient,
    store: InMemoryRevisionStore,
) -> Iterator[TestClient]:
    settings = ApiSettings()
    api = StacApi(
        settings=settings,
        client=core_client,
        extensions=[
            TransactionExtension(
                client=transactions_client,
                settings=settings,
                revision_store=store,
            ),
            BulkTransactionExtension(
                client=CatalogBulkClient(transactions_client.db),
                revision_store=store,
            ),
            RevisionStreamExtension(client=store),
        ],
    )
    with TestClient(api.app) as client:
        yield client


@pytest.fixture
def item() -> dict:
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


def get_all_events(client: TestClient, cursor: int | None = None) -> list[dict]:
    """Read the whole stream from `cursor`, following `next_cursor`."""
    events = []
    while True:
        url = "/revisions" if cursor is None else f"/revisions?cursor={cursor}"
        response = client.get(url)
        assert response.status_code == 200, response.text
        feed = response.json()
        events.extend(feed["events"])
        if not feed["events"] or feed["next_cursor"] == cursor:
            return events
        cursor = feed["next_cursor"]


def test_create_item_records_upsert_event(
    client: TestClient, store: InMemoryRevisionStore, item: dict
) -> None:
    response = client.post("/collections/test_collection/items", json=item)
    assert response.status_code == 201, response.text

    events = get_all_events(client)
    assert len(events) == 1
    event = events[0]
    assert event["seq"] == 1
    assert event["batch"]
    assert event["op"] == "upsert"
    assert event["collection_id"] == "test_collection"
    assert event["item_id"] == "test_item"
    assert event["item"]["id"] == "test_item"
    assert "ts" in event


def test_update_patch_delete_join_the_same_sequence(
    client: TestClient, item: dict
) -> None:
    assert client.post("/collections/test_collection/items", json=item).is_success

    updated = {**item, "properties": {"datetime": "2021-01-01T00:00:00Z", "foo": "bar"}}
    assert client.put(
        "/collections/test_collection/items/test_item", json=updated
    ).is_success

    assert client.patch(
        "/collections/test_collection/items/test_item",
        json={"properties": {"patched": True}},
    ).is_success

    assert client.delete("/collections/test_collection/items/test_item").is_success

    events = get_all_events(client)
    assert [event["seq"] for event in events] == [1, 2, 3, 4]
    assert [event["op"] for event in events] == [
        "upsert",
        "upsert",
        "upsert",
        "delete",
    ]
    # the patch event carries the committed item, with the patch applied
    assert events[2]["item"]["properties"]["patched"] is True
    assert events[2]["item"]["properties"]["foo"] == "bar"
    # the delete event carries enough identity to remove the item downstream
    delete = events[3]
    assert delete["collection_id"] == "test_collection"
    assert delete["item_id"] == "test_item"
    assert "item" not in delete


def test_item_collection_create_shares_one_batch(client: TestClient, item: dict) -> None:
    other = {**item, "id": "test_item_2"}
    response = client.post(
        "/collections/test_collection/items",
        json={
            "type": "FeatureCollection",
            "features": [item, other],
            "links": [],
        },
    )
    assert response.is_success, response.text

    events = get_all_events(client)
    assert [event["seq"] for event in events] == [1, 2]
    assert {event["batch"] for event in events} == {events[0]["batch"]}
    assert {event["item_id"] for event in events} == {"test_item", "test_item_2"}


def test_failed_commits_record_no_events(client: TestClient, item: dict) -> None:
    assert client.post("/collections/test_collection/items", json=item).is_success
    assert len(get_all_events(client)) == 1

    # duplicate create -> 409 Conflict
    response = client.post("/collections/test_collection/items", json=item)
    assert response.status_code == 409

    # update / patch / delete of a missing item -> 404
    assert (
        client.put("/collections/test_collection/items/missing", json=item).status_code
        == 404
    )
    assert (
        client.patch(
            "/collections/test_collection/items/missing",
            json={"properties": {"foo": "bar"}},
        ).status_code
        == 404
    )
    assert client.delete("/collections/test_collection/items/missing").status_code == 404

    events = get_all_events(client)
    assert len(events) == 1
    assert events[0]["seq"] == 1


def test_error_response_records_no_events(
    core_client: DummyCoreClient, store: InMemoryRevisionStore, item: dict
) -> None:
    """A client returning an error `Response` records no events."""

    class ErrorResponseClient(CatalogTransactionsClient):
        def delete_item(self, item_id: str, collection_id: str, **kwargs):
            return Response(status_code=500)

    settings = ApiSettings()
    api = StacApi(
        settings=settings,
        client=core_client,
        extensions=[
            TransactionExtension(
                client=ErrorResponseClient(),
                settings=settings,
                revision_store=store,
            ),
            RevisionStreamExtension(client=store),
        ],
    )
    with TestClient(api.app) as client:
        assert client.post("/collections/test_collection/items", json=item).is_success
        response = client.delete("/collections/test_collection/items/test_item")
        assert response.status_code == 500

    events = [event for event in get_all_events(client)]
    assert [event["op"] for event in events] == ["upsert"]


def test_create_with_response_returning_client(
    core_client: DummyCoreClient, store: InMemoryRevisionStore, item: dict
) -> None:
    """A client returning a bare `Response` still records the created Item,
    using the request payload."""

    class BareResponseClient(CatalogTransactionsClient):
        def create_item(self, collection_id, item, **kwargs):
            super().create_item(collection_id, item, **kwargs)
            return Response(status_code=201)

    settings = ApiSettings()
    api = StacApi(
        settings=settings,
        client=core_client,
        extensions=[
            TransactionExtension(
                client=BareResponseClient(),
                settings=settings,
                revision_store=store,
            ),
            RevisionStreamExtension(client=store),
        ],
    )
    with TestClient(api.app) as client:
        assert client.post("/collections/test_collection/items", json=item).is_success

    events = get_all_events(client)
    assert len(events) == 1
    assert events[0]["op"] == "upsert"
    assert events[0]["item"]["id"] == "test_item"


def test_bulk_insert_joins_the_same_sequence(client: TestClient, item: dict) -> None:
    assert client.post("/collections/test_collection/items", json=item).is_success

    payload = {
        "items": {
            "bulk_1": {**item, "id": "bulk_1"},
            "bulk_2": {**item, "id": "bulk_2"},
            # already exists -> reported in errors, must not be recorded
            "test_item": item,
        },
        "method": "upsert",
    }
    response = client.post("/collections/test_collection/bulk_items", json=payload)
    assert response.is_success, response.text
    assert response.json()["success"] == 2
    assert response.json()["errors"] == [
        {"id": "test_item", "msg": "Item already exists"}
    ]

    events = get_all_events(client)
    assert [event["seq"] for event in events] == [1, 2, 3]
    bulk_events = events[1:]
    assert {event["item_id"] for event in bulk_events} == {"bulk_1", "bulk_2"}
    assert all(event["op"] == "upsert" for event in bulk_events)
    # the whole bulk write is one batch in the same sequence as other writes
    assert {event["batch"] for event in bulk_events} == {bulk_events[0]["batch"]}
    assert bulk_events[0]["batch"] != events[0]["batch"]


def test_downstream_catalog_syncs_by_cursor(
    client: TestClient, transactions_client: CatalogTransactionsClient, item: dict
) -> None:
    """A mirror catalog resumes from its cursor and applies by collection/id."""
    mirror: dict[tuple[str, str], dict] = {}
    cursor = 0

    def sync() -> None:
        nonlocal cursor
        while True:
            response = client.get(f"/revisions?cursor={cursor}&limit=2")
            assert response.status_code == 200, response.text
            feed = response.json()
            for event in feed["events"]:
                key = (event["collection_id"], event["item_id"])
                if event["op"] == "delete":
                    mirror.pop(key, None)
                else:
                    mirror[key] = event["item"]
            cursor = feed["next_cursor"]
            if len(feed["events"]) < 2:
                return

    client.post("/collections/test_collection/items", json=item)
    client.post("/collections/test_collection/items", json={**item, "id": "test_item_2"})
    sync()
    assert set(mirror) == {
        ("test_collection", "test_item"),
        ("test_collection", "test_item_2"),
    }

    # a day of writes: update, patch, bulk insert and a delete
    client.put(
        "/collections/test_collection/items/test_item",
        json={**item, "properties": {"datetime": "2021-01-01T00:00:00Z"}},
    )
    client.patch(
        "/collections/test_collection/items/test_item_2",
        json={"properties": {"patched": True}},
    )
    client.post(
        "/collections/test_collection/bulk_items",
        json={"items": {"bulk_1": {**item, "id": "bulk_1"}}, "method": "insert"},
    )
    client.delete("/collections/test_collection/items/test_item_2")

    sync()
    assert mirror == transactions_client.db
    assert set(mirror) == {
        ("test_collection", "test_item"),
        ("test_collection", "bulk_1"),
    }
    assert mirror[("test_collection", "test_item")]["properties"]["datetime"] == (
        "2021-01-01T00:00:00Z"
    )


def test_cursor_beyond_retention_requires_rebaseline(
    core_client: DummyCoreClient, item: dict
) -> None:
    store = InMemoryRevisionStore(retention=2)
    settings = ApiSettings()
    transactions_client = CatalogTransactionsClient()
    api = StacApi(
        settings=settings,
        client=core_client,
        extensions=[
            TransactionExtension(
                client=transactions_client,
                settings=settings,
                revision_store=store,
            ),
            RevisionStreamExtension(client=store),
        ],
    )
    with TestClient(api.app) as client:
        for i in range(3):
            assert client.post(
                "/collections/test_collection/items",
                json={**item, "id": f"item_{i}"},
            ).is_success

        # the first event was trimmed: a reader at cursor 0 missed it
        response = client.get("/revisions?cursor=0")
        assert response.status_code == 410
        assert response.json()["code"] == "RebaselineRequiredError"

        # a cursor ahead of the stream head is rejected as well
        response = client.get("/revisions?cursor=99")
        assert response.status_code == 410
        assert response.json()["code"] == "RebaselineRequiredError"

        # reading without a cursor starts at the oldest retained event and
        # exposes the retention window
        feed = client.get("/revisions").json()
        assert feed["oldest_cursor"] == 2
        assert feed["latest_cursor"] == 3
        assert [event["seq"] for event in feed["events"]] == [2, 3]
        assert feed["next_cursor"] == 3

        # the retention horizon itself is still a valid cursor
        feed = client.get("/revisions?cursor=1").json()
        assert [event["seq"] for event in feed["events"]] == [2, 3]


def test_empty_stream(client: TestClient) -> None:
    feed = client.get("/revisions").json()
    assert feed == {
        "events": [],
        "next_cursor": 0,
        "oldest_cursor": 0,
        "latest_cursor": 0,
    }
    # cursor 0 is the beginning of time and stays valid
    assert client.get("/revisions?cursor=0").json()["events"] == []
    # anything else is ahead of the stream head
    assert client.get("/revisions?cursor=1").status_code == 410


def test_revision_stream_is_optional(core_client: DummyCoreClient, item: dict) -> None:
    """Without a revision store, transactions behave as before and no stream
    is exposed."""
    settings = ApiSettings()
    api = StacApi(
        settings=settings,
        client=core_client,
        extensions=[
            TransactionExtension(client=CatalogTransactionsClient(), settings=settings),
        ],
    )
    with TestClient(api.app) as client:
        response = client.post("/collections/test_collection/items", json=item)
        assert response.status_code == 201, response.text
        assert client.get("/revisions").status_code == 404


def test_conformance_classes(
    core_client: DummyCoreClient, store: InMemoryRevisionStore
) -> None:
    settings = ApiSettings()
    api = StacApi(
        settings=settings,
        client=core_client,
        extensions=[RevisionStreamExtension(client=store)],
    )
    with TestClient(api.app) as client:
        response = client.get("/conformance")
        assert response.status_code == 200, response.text
        assert RevisionStreamConformanceClasses.REVISIONS in response.json()["conformsTo"]


# --- STORE UNIT TESTS ---


def test_append_batch_assigns_shared_batch_and_gapless_seq() -> None:
    store = InMemoryRevisionStore(retention=10)
    from stac_fastapi.types.revisions import delete_event, upsert_event

    first = store.append_batch([upsert_event("c", "1"), upsert_event("c", "2")])
    second = store.append_batch([delete_event("c", "1")])

    assert [event.seq for event in first] == [1, 2]
    assert second[0].seq == 3
    assert len({event.batch for event in first}) == 1
    assert first[0].batch != second[0].batch
    assert store.latest_seq == 3
    assert store.oldest_seq == 1


def test_append_empty_batch_is_noop() -> None:
    store = InMemoryRevisionStore(retention=10)
    assert store.append_batch([]) == []
    assert store.latest_seq == 0


def test_retention_trims_oldest_events() -> None:
    from stac_fastapi.types.revisions import upsert_event

    store = InMemoryRevisionStore(retention=2)
    for i in range(4):
        store.append_batch([upsert_event("c", str(i))])

    assert store.latest_seq == 4
    assert store.oldest_seq == 3
    feed = store.get_revisions()
    assert [event.seq for event in feed.events] == [3, 4]
    assert feed.oldest_cursor == 3
    assert feed.latest_cursor == 4


def test_async_transactions_client_is_supported(
    core_client: DummyCoreClient, store: InMemoryRevisionStore, item: dict
) -> None:
    """Recording also works with an async transactions client."""
    from stac_fastapi.extensions.transaction import AsyncBaseTransactionsClient

    class AsyncCatalogClient(AsyncBaseTransactionsClient):
        def __init__(self):
            self.db = {}

        async def create_item(self, collection_id, item, **kwargs):
            data = item.model_dump(mode="json", exclude_none=True)
            self.db[(collection_id, data["id"])] = data
            return data

        async def update_item(self, collection_id, item_id, item, **kwargs):
            raise NotImplementedError

        async def patch_item(self, collection_id, item_id, patch, **kwargs):
            raise NotImplementedError

        async def delete_item(self, item_id, collection_id, **kwargs):
            raise NotImplementedError

        async def create_collection(self, collection, **kwargs):
            raise NotImplementedError

        async def update_collection(self, collection_id, collection, **kwargs):
            raise NotImplementedError

        async def patch_collection(self, collection_id, patch, **kwargs):
            raise NotImplementedError

        async def delete_collection(self, collection_id, **kwargs):
            raise NotImplementedError

    settings = ApiSettings()
    api = StacApi(
        settings=settings,
        client=core_client,
        extensions=[
            TransactionExtension(
                client=AsyncCatalogClient(),
                settings=settings,
                revision_store=store,
            ),
            RevisionStreamExtension(client=store),
        ],
    )
    with TestClient(api.app) as client:
        assert client.post("/collections/test_collection/items", json=item).is_success
        events = get_all_events(client)
        assert len(events) == 1
        assert events[0]["item_id"] == "test_item"
