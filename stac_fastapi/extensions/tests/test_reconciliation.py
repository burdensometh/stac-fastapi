"""Tests for the Reconciliation extension."""

from __future__ import annotations

import copy
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import APIRouter
from starlette.testclient import TestClient

from stac_fastapi.api.app import StacApi
from stac_fastapi.extensions.reconciliation import (
    BaseReconciliationClient,
    ReconciliationExtension,
    ReconciliationRequest,
    ReconciliationResult,
    ReconciliationScope,
)
from stac_fastapi.extensions.reconciliation.engine import (
    Reconciler,
    SnapshotStateStore,
)
from stac_fastapi.types.config import ApiSettings
from stac_fastapi.types.core import BaseCoreClient
from stac_fastapi.types.errors import (
    ConflictError,
    DatabaseError,
    InvalidSnapshotError,
    StaleSnapshotError,
)

COLLECTION_ID = "test_collection"


def make_item(item_id: str, **overrides: Any) -> dict[str, Any]:
    """Build a minimal valid STAC Item."""
    item: dict[str, Any] = {
        "type": "Feature",
        "stac_version": "1.0.0",
        "stac_extensions": [],
        "id": item_id,
        "geometry": {"type": "Point", "coordinates": [-105.0, 40.0]},
        "bbox": [-105.0, 40.0, -105.0, 40.0],
        "properties": {"datetime": "2020-06-13T13:00:00Z"},
        "links": [],
        "assets": {},
        "collection": COLLECTION_ID,
    }
    item.update(overrides)
    return item


class InMemoryStore:
    """A tiny persisted collection used by the fake clients."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fail_on: str | None = None
        self.max_page_size: int | None = None

    def clone(self, item: dict[str, Any]) -> dict[str, Any]:
        return copy.deepcopy(item)


class InMemoryCoreClient(BaseCoreClient):
    """Core client reading from an :class:`InMemoryStore`."""

    def __init__(self, store: InMemoryStore) -> None:
        self.store = store

    def all_collections(self, *args, **kwargs):
        raise NotImplementedError

    def get_collection(self, *args, **kwargs):
        raise NotImplementedError

    def get_item(self, item_id: str, collection_id: str, **kwargs):
        return self.store.clone(self.store.items[item_id])

    def get_search(self, *args, **kwargs):
        raise NotImplementedError

    def post_search(self, *args, **kwargs):
        raise NotImplementedError

    def item_collection(
        self,
        collection_id: str,
        bbox=None,
        datetime=None,
        limit: int = 10,
        token: str | None = None,
        **kwargs,
    ):
        ids = sorted(self.store.items)
        start = int(token) if token else 0
        limit = self.store.max_page_size or limit
        page = ids[start : start + limit]
        next_start = start + limit
        links = []
        if next_start < len(ids):
            links.append(
                {"rel": "next", "href": f"/items?token={next_start}", "method": "GET"}
            )
        return {
            "type": "FeatureCollection",
            "features": [self.store.clone(self.store.items[i]) for i in page],
            "links": links,
        }


class InMemoryTransactionsClient:
    """Transaction client writing to an :class:`InMemoryStore`."""

    def __init__(self, store: InMemoryStore) -> None:
        self.store = store

    def _maybe_fail(self, operation: str) -> None:
        self.store.calls.append((operation, ()))
        if self.store.fail_on == operation:
            raise DatabaseError(f"{operation} failed")

    def create_item(self, collection_id: str, item: dict[str, Any], **kwargs):
        self._maybe_fail("create_item")
        self.store.items[item["id"]] = self.store.clone(item)
        return item

    def update_item(
        self, collection_id: str, item_id: str, item: dict[str, Any], **kwargs
    ):
        self._maybe_fail("update_item")
        assert item["id"] == item_id
        self.store.items[item_id] = self.store.clone(item)
        return item

    def patch_item(self, *args, **kwargs):
        raise NotImplementedError

    def delete_item(self, item_id: str, collection_id: str, **kwargs):
        self._maybe_fail("delete_item")
        self.store.items.pop(item_id, None)
        return {}


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def reconciler(store: InMemoryStore) -> Reconciler:
    return Reconciler(
        core_client=InMemoryCoreClient(store),
        transactions_client=InMemoryTransactionsClient(store),
    )


def snapshot(version: int, items: list[dict], **overrides) -> ReconciliationRequest:
    return ReconciliationRequest(
        version=version,
        scope=ReconciliationScope.COMPLETE,
        items={item["id"]: item for item in items},
        item_count=len(items),
        **overrides,
    )


# --- planning and application ------------------------------------------------


@pytest.mark.asyncio
async def test_add_update_retire(reconciler: Reconciler, store: InMemoryStore) -> None:
    store.items = {
        "keep": make_item("keep"),
        "change": make_item("change", properties={"datetime": "2000-01-01T00:00:00Z"}),
        "gone": make_item("gone"),
    }

    result = await reconciler.reconcile(
        COLLECTION_ID,
        snapshot(
            1,
            [
                make_item("keep"),
                make_item("change"),
                make_item("new"),
            ],
        ),
    )

    assert result.status == "complete"
    assert result.added == 1
    assert result.updated == 1
    assert result.retired == 1
    assert result.unchanged == 1
    assert {a.item_id: a.action.value for a in result.actions} == {
        "new": "add",
        "change": "update",
        "gone": "retire",
    }
    assert set(store.items) == {"keep", "change", "new"}
    assert (
        store.items["change"]["properties"]["datetime"] == "2020-06-13T13:00:00Z"
    )


@pytest.mark.asyncio
async def test_partial_scope_never_retires(
    reconciler: Reconciler, store: InMemoryStore
) -> None:
    store.items = {"gone": make_item("gone"), "keep": make_item("keep")}

    result = await reconciler.reconcile(
        COLLECTION_ID,
        ReconciliationRequest(
            version=1,
            scope=ReconciliationScope.PARTIAL,
            items={"keep": make_item("keep"), "new": make_item("new")},
        ),
    )

    assert result.retired == 0
    assert "gone" in store.items
    assert "new" in store.items


@pytest.mark.asyncio
async def test_unchanged_items_are_not_written(
    reconciler: Reconciler, store: InMemoryStore
) -> None:
    store.items = {"same": make_item("same")}

    result = await reconciler.reconcile(
        COLLECTION_ID, snapshot(1, [make_item("same")])
    )

    assert result.actions == []
    assert result.unchanged == 1
    assert store.calls == []


@pytest.mark.asyncio
async def test_link_and_collection_changes_do_not_count_as_updates(
    reconciler: Reconciler, store: InMemoryStore
) -> None:
    """A server may rewrite links or drop collection on read; that is not drift."""
    persisted = make_item("same")
    persisted["links"] = [{"rel": "self", "href": "/collections/c/items/same"}]
    persisted.pop("collection")
    store.items = {"same": persisted}

    result = await reconciler.reconcile(
        COLLECTION_ID, snapshot(1, [make_item("same")])
    )

    assert result.updated == 0
    assert result.actions == []


@pytest.mark.asyncio
async def test_datetime_formatting_is_normalized(
    reconciler: Reconciler, store: InMemoryStore
) -> None:
    persisted = make_item("same", properties={"datetime": "2020-06-13T13:00:00+00:00"})
    store.items = {"same": persisted}

    result = await reconciler.reconcile(
        COLLECTION_ID, snapshot(1, [make_item("same")])
    )

    assert result.updated == 0


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(
    reconciler: Reconciler, store: InMemoryStore
) -> None:
    store.items = {"gone": make_item("gone")}

    result = await reconciler.reconcile(
        COLLECTION_ID, snapshot(1, [make_item("new")], dry_run=True)
    )

    assert result.dry_run is True
    assert result.added == 1
    assert result.retired == 1
    assert set(store.items) == {"gone"}
    assert store.calls == []
    # A dry run must not leave a watermark behind.
    assert reconciler.state_store.get_watermark(COLLECTION_ID) is None


@pytest.mark.asyncio
async def test_persisted_matches_snapshot_after_complete(
    reconciler: Reconciler, store: InMemoryStore
) -> None:
    store.items = {"a": make_item("a"), "b": make_item("b")}
    items = [make_item("b"), make_item("c")]

    await reconciler.reconcile(COLLECTION_ID, snapshot(1, items))

    assert set(store.items) == {"b", "c"}


# --- identity ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_mapping_key_must_match_item_id(
    reconciler: Reconciler, store: InMemoryStore
) -> None:
    request = ReconciliationRequest(
        version=1,
        scope=ReconciliationScope.PARTIAL,
        items={"key": make_item("other")},
    )

    with pytest.raises(InvalidSnapshotError, match="does not match"):
        await reconciler.reconcile(COLLECTION_ID, request)
    assert store.items == {}


@pytest.mark.asyncio
async def test_duplicate_identity_is_rejected(
    reconciler: Reconciler, store: InMemoryStore
) -> None:
    request = ReconciliationRequest(
        version=1,
        scope=ReconciliationScope.PARTIAL,
        items=[make_item("a"), make_item("a")],
    )

    with pytest.raises(InvalidSnapshotError, match="more than once"):
        await reconciler.reconcile(COLLECTION_ID, request)


@pytest.mark.asyncio
async def test_conflicting_duplicate_identity_is_a_conflict(
    reconciler: Reconciler, store: InMemoryStore
) -> None:
    request = ReconciliationRequest(
        version=1,
        scope=ReconciliationScope.PARTIAL,
        items=[
            make_item("a"),
            make_item("a", properties={"datetime": "2001-01-01T00:00:00Z"}),
        ],
    )

    with pytest.raises(ConflictError):
        await reconciler.reconcile(COLLECTION_ID, request)


@pytest.mark.asyncio
async def test_item_from_another_collection_is_rejected(
    reconciler: Reconciler, store: InMemoryStore
) -> None:
    request = ReconciliationRequest(
        version=1,
        scope=ReconciliationScope.PARTIAL,
        items=[make_item("a", collection="other")],
    )

    with pytest.raises(InvalidSnapshotError, match="belongs to collection"):
        await reconciler.reconcile(COLLECTION_ID, request)


@pytest.mark.asyncio
async def test_declared_collection_must_match_path(
    reconciler: Reconciler, store: InMemoryStore
) -> None:
    request = ReconciliationRequest(
        version=1,
        scope=ReconciliationScope.PARTIAL,
        items=[],
        collection_id="other",
    )

    with pytest.raises(InvalidSnapshotError, match="declares collection"):
        await reconciler.reconcile(COLLECTION_ID, request)


@pytest.mark.asyncio
async def test_invalid_item_is_rejected(
    reconciler: Reconciler, store: InMemoryStore
) -> None:
    request = ReconciliationRequest(
        version=1,
        scope=ReconciliationScope.PARTIAL,
        items=[{"id": "a"}],
    )

    with pytest.raises(InvalidSnapshotError):
        await reconciler.reconcile(COLLECTION_ID, request)


@pytest.mark.asyncio
async def test_duplicate_persisted_identity_is_a_conflict(
    store: InMemoryStore,
) -> None:
    class DuplicatingCoreClient(InMemoryCoreClient):
        def item_collection(self, collection_id: str, **kwargs):
            return {
                "type": "FeatureCollection",
                "features": [make_item("a"), make_item("a")],
                "links": [],
            }

    reconciler = Reconciler(
        core_client=DuplicatingCoreClient(store),
        transactions_client=InMemoryTransactionsClient(store),
    )

    with pytest.raises(ConflictError, match="not unique"):
        await reconciler.reconcile(
            COLLECTION_ID, snapshot(1, [make_item("a")])
        )


# --- completeness and failure safety ----------------------------------------


@pytest.mark.asyncio
async def test_count_mismatch_blocks_retirement(
    reconciler: Reconciler, store: InMemoryStore
) -> None:
    store.items = {"gone": make_item("gone")}
    request = ReconciliationRequest(
        version=1,
        scope=ReconciliationScope.COMPLETE,
        items=[make_item("a")],
        item_count=2,
    )

    with pytest.raises(InvalidSnapshotError, match="declares 2 Items"):
        await reconciler.reconcile(COLLECTION_ID, request)
    assert "gone" in store.items
    assert store.items.keys() == {"gone"}


@pytest.mark.asyncio
async def test_checksum_mismatch_blocks_retirement(
    reconciler: Reconciler, store: InMemoryStore
) -> None:
    store.items = {"gone": make_item("gone")}
    request = ReconciliationRequest(
        version=1,
        scope=ReconciliationScope.COMPLETE,
        items=[make_item("a")],
        item_count=1,
        checksum="0" * 64,
    )

    with pytest.raises(InvalidSnapshotError, match="checksum"):
        await reconciler.reconcile(COLLECTION_ID, request)
    assert "gone" in store.items


@pytest.mark.asyncio
async def test_checksum_match_is_accepted(
    reconciler: Reconciler, store: InMemoryStore
) -> None:
    from stac_fastapi.extensions.reconciliation.utils import snapshot_digest

    items = [make_item("a")]
    request = ReconciliationRequest(
        version=1,
        scope=ReconciliationScope.COMPLETE,
        items=items,
        item_count=1,
        checksum=snapshot_digest(items),
    )

    result = await reconciler.reconcile(COLLECTION_ID, request)
    assert result.added == 1


@pytest.mark.asyncio
async def test_segmented_snapshot_is_pending_until_all_segments_arrive(
    reconciler: Reconciler, store: InMemoryStore
) -> None:
    store.items = {"gone": make_item("gone")}

    first = await reconciler.reconcile(
        COLLECTION_ID,
        ReconciliationRequest(
            version=1,
            scope=ReconciliationScope.COMPLETE,
            items=[make_item("a")],
            item_count=2,
            segments=2,
            segment=1,
        ),
    )
    assert first.status == "pending"
    assert first.missing_segments == [2]
    assert first.actions == []
    assert "gone" in store.items
    assert store.calls == []

    second = await reconciler.reconcile(
        COLLECTION_ID,
        ReconciliationRequest(
            version=1,
            scope=ReconciliationScope.COMPLETE,
            items=[make_item("b")],
            item_count=2,
            segments=2,
            segment=2,
        ),
    )
    assert second.status == "complete"
    assert second.added == 2
    assert second.retired == 1
    assert set(store.items) == {"a", "b"}


@pytest.mark.asyncio
async def test_segments_must_agree_on_declarations(
    reconciler: Reconciler, store: InMemoryStore
) -> None:
    await reconciler.reconcile(
        COLLECTION_ID,
        ReconciliationRequest(
            version=1,
            scope=ReconciliationScope.COMPLETE,
            items=[make_item("a")],
            item_count=2,
            segments=2,
            segment=1,
        ),
    )

    with pytest.raises(InvalidSnapshotError, match="item_count"):
        await reconciler.reconcile(
            COLLECTION_ID,
            ReconciliationRequest(
                version=1,
                scope=ReconciliationScope.COMPLETE,
                items=[make_item("b")],
                item_count=3,
                segments=2,
                segment=2,
            ),
        )
    assert store.calls == []


@pytest.mark.asyncio
async def test_failed_segment_does_not_stage_partial_state(
    reconciler: Reconciler, store: InMemoryStore
) -> None:
    with pytest.raises(InvalidSnapshotError):
        await reconciler.reconcile(
            COLLECTION_ID,
            ReconciliationRequest(
                version=1,
                scope=ReconciliationScope.COMPLETE,
                items=[make_item("a", collection="other")],
                item_count=1,
                segments=2,
                segment=1,
            ),
        )
    assert reconciler.state_store.get_segments(COLLECTION_ID, 1) == {}

    result = await reconciler.reconcile(
        COLLECTION_ID,
        ReconciliationRequest(
            version=1,
            scope=ReconciliationScope.COMPLETE,
            items=[make_item("b")],
            item_count=1,
            segments=2,
            segment=2,
        ),
    )
    assert result.status == "pending"
    assert result.missing_segments == [1]


@pytest.mark.asyncio
async def test_write_failure_prevents_retirement(store: InMemoryStore) -> None:
    store.items = {"gone": make_item("gone"), "old": make_item("old")}
    store.fail_on = "create_item"
    reconciler = Reconciler(
        core_client=InMemoryCoreClient(store),
        transactions_client=InMemoryTransactionsClient(store),
        verify=False,
    )

    with pytest.raises(DatabaseError):
        await reconciler.reconcile(
            COLLECTION_ID,
            snapshot(
                1,
                [
                    make_item("old"),
                    make_item("new"),
                ],
            ),
        )

    assert "gone" in store.items
    assert "new" not in store.items


@pytest.mark.asyncio
async def test_verification_catches_unconverged_collection(
    store: InMemoryStore,
) -> None:
    class ForgetfulTransactionsClient(InMemoryTransactionsClient):
        def create_item(self, collection_id, item, **kwargs):
            return item  # silently drops the write

    reconciler = Reconciler(
        core_client=InMemoryCoreClient(store),
        transactions_client=ForgetfulTransactionsClient(store),
    )

    with pytest.raises(DatabaseError, match="verification failed"):
        await reconciler.reconcile(
            COLLECTION_ID, snapshot(1, [make_item("a")])
        )


# --- version handling --------------------------------------------------------


@pytest.mark.asyncio
async def test_replaying_an_older_snapshot_is_rejected(
    reconciler: Reconciler, store: InMemoryStore
) -> None:
    await reconciler.reconcile(
        COLLECTION_ID,
        snapshot(2, [make_item("new", properties={"datetime": "2024-01-01T00:00:00Z"})]),
    )

    with pytest.raises(StaleSnapshotError):
        await reconciler.reconcile(COLLECTION_ID, snapshot(1, [make_item("new")]))

    assert store.items["new"]["properties"]["datetime"] == "2024-01-01T00:00:00Z"


@pytest.mark.asyncio
async def test_replaying_the_same_snapshot_is_a_noop(
    reconciler: Reconciler, store: InMemoryStore
) -> None:
    request = snapshot(1, [make_item("a")])
    await reconciler.reconcile(COLLECTION_ID, request)
    store.calls.clear()

    replay = await reconciler.reconcile(COLLECTION_ID, request)

    assert replay.status == "complete"
    assert replay.actions == []
    assert store.calls == []


@pytest.mark.asyncio
async def test_same_version_with_different_content_conflicts(
    reconciler: Reconciler, store: InMemoryStore
) -> None:
    await reconciler.reconcile(COLLECTION_ID, snapshot(1, [make_item("a")]))

    with pytest.raises(ConflictError, match="different content"):
        await reconciler.reconcile(COLLECTION_ID, snapshot(1, [make_item("b")]))


@pytest.mark.asyncio
async def test_newer_snapshot_is_applied(
    reconciler: Reconciler, store: InMemoryStore
) -> None:
    await reconciler.reconcile(COLLECTION_ID, snapshot(1, [make_item("a")]))
    result = await reconciler.reconcile(COLLECTION_ID, snapshot(2, [make_item("a")]))

    assert result.status == "complete"
    assert result.retired == 0
    assert set(store.items) == {"a"}


# --- pagination --------------------------------------------------------------


@pytest.mark.asyncio
async def test_pages_through_persisted_items(
    reconciler: Reconciler, store: InMemoryStore
) -> None:
    store.max_page_size = 2
    store.items = {
        f"item_{index}": make_item(f"item_{index}") for index in range(5)
    }

    result = await reconciler.reconcile(
        COLLECTION_ID, snapshot(1, [make_item("item_0"), make_item("other")])
    )

    assert result.retired == 4
    assert set(store.items) == {"item_0", "other"}


@pytest.mark.asyncio
async def test_non_advancing_pagination_raises(store: InMemoryStore) -> None:
    class LoopingCoreClient(InMemoryCoreClient):
        calls = 0

        def item_collection(self, collection_id: str, **kwargs):
            LoopingCoreClient.calls += 1
            return {
                "type": "FeatureCollection",
                "features": [make_item(f"a{self.calls}")],
                "links": [{"rel": "next", "href": "/items?token=same"}],
            }

    reconciler = Reconciler(
        core_client=LoopingCoreClient(store),
        transactions_client=InMemoryTransactionsClient(store),
    )

    with pytest.raises(DatabaseError, match="did not advance"):
        await reconciler.reconcile(COLLECTION_ID, snapshot(2, [make_item("a")]))


# --- HTTP --------------------------------------------------------------------


@pytest.fixture
def client(store: InMemoryStore) -> Iterator[TestClient]:
    settings = ApiSettings()
    api = StacApi(
        settings=settings,
        client=InMemoryCoreClient(store),
        extensions=[
            ReconciliationExtension(
                core_client=InMemoryCoreClient(store),
                transactions_client=InMemoryTransactionsClient(store),
            )
        ],
    )
    with TestClient(api.app) as client:
        yield client


def test_reconcile_endpoint(client: TestClient, store: InMemoryStore) -> None:
    store.items = {"gone": make_item("gone")}

    response = client.post(
        f"/collections/{COLLECTION_ID}/reconcile",
        json={
            "version": 7,
            "scope": "complete",
            "item_count": 2,
            "items": [make_item("a"), make_item("gone", assets={"x": {"href": "y"}})],
        },
    )

    assert response.is_success, response.text
    body = response.json()
    assert body["status"] == "complete"
    assert body["version"] == 7
    assert body["added"] == 1
    assert body["updated"] == 1
    assert set(store.items) == {"a", "gone"}


def test_reconcile_endpoint_requires_declared_version_and_scope(
    client: TestClient, store: InMemoryStore
) -> None:
    response = client.post(
        f"/collections/{COLLECTION_ID}/reconcile",
        json={"items": [make_item("a")]},
    )

    assert response.status_code == 400
    assert store.items == {}


def test_reconcile_endpoint_returns_400_for_invalid_snapshot(
    client: TestClient, store: InMemoryStore
) -> None:
    response = client.post(
        f"/collections/{COLLECTION_ID}/reconcile",
        json={
            "version": 1,
            "scope": "complete",
            "item_count": 5,
            "items": [make_item("a")],
        },
    )

    assert response.status_code == 400
    assert response.json()["code"] == "InvalidSnapshotError"


def test_reconcile_endpoint_returns_409_for_stale_snapshot(
    client: TestClient, store: InMemoryStore
) -> None:
    payload = {
        "version": 2,
        "scope": "complete",
        "item_count": 1,
        "items": [make_item("a")],
    }
    assert client.post(
        f"/collections/{COLLECTION_ID}/reconcile", json=payload
    ).is_success

    response = client.post(
        f"/collections/{COLLECTION_ID}/reconcile",
        json={**payload, "version": 1, "items": [make_item("a")]},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "StaleSnapshotError"


def test_reconcile_endpoint_complete_requires_item_count(
    client: TestClient, store: InMemoryStore
) -> None:
    store.items = {"gone": make_item("gone")}

    response = client.post(
        f"/collections/{COLLECTION_ID}/reconcile",
        json={"version": 1, "scope": "complete", "items": [make_item("a")]},
    )

    assert response.status_code == 400
    assert "item_count" in response.json()["description"]
    assert set(store.items) == {"gone"}


def test_reconcile_response_model_is_a_result(
    reconciler: Reconciler, store: InMemoryStore
) -> None:
    import asyncio

    result = asyncio.run(
        reconciler.reconcile(COLLECTION_ID, snapshot(1, [make_item("a")]))
    )
    assert isinstance(result, ReconciliationResult)


# --- snapshot directories ----------------------------------------------------


def test_load_snapshot_from_directory(tmp_path) -> None:
    import json

    from stac_fastapi.extensions.reconciliation import load_snapshot

    (tmp_path / "b.json").write_text(json.dumps(make_item("b")))
    (tmp_path / "a.json").write_text(
        json.dumps({"type": "FeatureCollection", "features": [make_item("a")]})
    )
    (tmp_path / "c.json").write_text(json.dumps([make_item("c")]))
    (tmp_path / "notes.txt").write_text("ignored")

    items = load_snapshot(tmp_path, COLLECTION_ID)

    assert [item["id"] for item in items] == ["a", "b", "c"]


def test_load_snapshot_rejects_duplicates(tmp_path) -> None:
    import json

    from stac_fastapi.extensions.reconciliation import load_snapshot

    (tmp_path / "a.json").write_text(json.dumps(make_item("a")))
    (tmp_path / "b.json").write_text(json.dumps(make_item("a")))

    with pytest.raises(InvalidSnapshotError, match="more than once"):
        load_snapshot(tmp_path, COLLECTION_ID)


def test_load_snapshot_rejects_malformed_json(tmp_path) -> None:
    from stac_fastapi.extensions.reconciliation import load_snapshot

    (tmp_path / "a.json").write_text("{not json")

    with pytest.raises(InvalidSnapshotError, match="could not read"):
        load_snapshot(tmp_path, COLLECTION_ID)


def test_load_snapshot_rejects_empty_directory(tmp_path) -> None:
    from stac_fastapi.extensions.reconciliation import load_snapshot

    with pytest.raises(InvalidSnapshotError, match="no .json files"):
        load_snapshot(tmp_path, COLLECTION_ID)


def test_directory_snapshot_reconciles(
    reconciler: Reconciler, store: InMemoryStore, tmp_path
) -> None:
    import asyncio
    import json

    from stac_fastapi.extensions.reconciliation import (
        load_snapshot,
        snapshot_declarations,
    )

    store.items = {"gone": make_item("gone")}
    (tmp_path / "a.json").write_text(json.dumps(make_item("a")))

    items = load_snapshot(tmp_path, COLLECTION_ID)
    count, checksum = snapshot_declarations(items)
    result = asyncio.run(
        reconciler.reconcile(
            COLLECTION_ID,
            ReconciliationRequest(
                version=1,
                scope=ReconciliationScope.COMPLETE,
                items=items,
                item_count=count,
                checksum=checksum,
            ),
        )
    )

    assert result.added == 1
    assert result.retired == 1
    assert set(store.items) == {"a"}


# --- Version replay safety ---------------------------------------------------


@pytest.mark.asyncio
async def test_old_snapshot_cannot_overwrite_newer_items_across_restart(
    store: InMemoryStore,
) -> None:
    """A persisted state store keeps the watermark across a process restart."""
    state_store = SnapshotStateStore()
    core, transactions = InMemoryCoreClient(store), InMemoryTransactionsClient(store)
    await Reconciler(
        core_client=core,
        transactions_client=transactions,
        state_store=state_store,
    ).reconcile(
        COLLECTION_ID,
        snapshot(
            5,
            [make_item("a", properties={"datetime": "2024-01-01T00:00:00Z"})],
        ),
    )

    # Simulate a restart: fresh clients, but the same persisted state store.
    store.calls.clear()
    restarted = Reconciler(
        core_client=InMemoryCoreClient(store),
        transactions_client=InMemoryTransactionsClient(store),
        state_store=state_store,
    )

    with pytest.raises(StaleSnapshotError, match="older than"):
        await restarted.reconcile(
            COLLECTION_ID,
            snapshot(
                2,
                [make_item("a", properties={"datetime": "1999-01-01T00:00:00Z"})],
            ),
        )

    assert store.items["a"]["properties"]["datetime"] == "2024-01-01T00:00:00Z"
    assert store.calls == []


@pytest.mark.asyncio
async def test_old_snapshot_cannot_retire_newer_items_across_restart(
    store: InMemoryStore,
) -> None:
    state_store = SnapshotStateStore()
    await Reconciler(
        core_client=InMemoryCoreClient(store),
        transactions_client=InMemoryTransactionsClient(store),
        state_store=state_store,
    ).reconcile(COLLECTION_ID, snapshot(5, [make_item("a")]))

    restarted = Reconciler(
        core_client=InMemoryCoreClient(store),
        transactions_client=InMemoryTransactionsClient(store),
        state_store=state_store,
    )

    with pytest.raises(StaleSnapshotError, match="older than"):
        await restarted.reconcile(COLLECTION_ID, snapshot(1, [make_item("other")]))

    assert "a" in store.items


@pytest.mark.asyncio
async def test_writes_do_not_add_server_only_fields(
    reconciler: Reconciler, store: InMemoryStore
) -> None:
    """The collection read at /items must match the confirmed source exactly."""
    source = make_item("a")
    await reconciler.reconcile(COLLECTION_ID, snapshot(1, [source]))

    assert store.items["a"] == source


@pytest.mark.asyncio
async def test_newer_version_with_same_content_is_a_noop(
    reconciler: Reconciler, store: InMemoryStore
) -> None:
    await reconciler.reconcile(COLLECTION_ID, snapshot(1, [make_item("a")]))
    result = await reconciler.reconcile(COLLECTION_ID, snapshot(2, [make_item("a")]))

    assert result.updated == 0
    assert result.actions == []


# --- extension wiring --------------------------------------------------------


class StubReconciliationClient(BaseReconciliationClient):
    """Minimal custom client, to verify the extension delegates."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ReconciliationRequest]] = []

    async def reconcile(self, collection_id, snapshot, **kwargs):
        self.calls.append((collection_id, snapshot))
        return ReconciliationResult(
            collection_id=collection_id,
            version=snapshot.version,
            status="complete",
            received=len(snapshot.items),
        )


def test_extension_uses_a_custom_client(store: InMemoryStore) -> None:
    custom = StubReconciliationClient()
    api = StacApi(
        settings=ApiSettings(),
        client=InMemoryCoreClient(store),
        extensions=[ReconciliationExtension(client=custom)],
    )

    with TestClient(api.app) as client:
        response = client.post(
            f"/collections/{COLLECTION_ID}/reconcile",
            json={"version": 3, "scope": "partial", "items": [make_item("a")]},
        )

    assert response.is_success, response.text
    assert response.json()["version"] == 3
    assert len(custom.calls) == 1
    assert custom.calls[0][0] == COLLECTION_ID


def test_extension_requires_a_client_or_clients() -> None:
    with pytest.raises(ValueError, match="requires either a client"):
        ReconciliationExtension()


def test_extension_registers_under_router_prefix(store: InMemoryStore) -> None:
    settings = ApiSettings()
    api = StacApi(
        settings=settings,
        client=InMemoryCoreClient(store),
        extensions=[
            ReconciliationExtension(
                core_client=InMemoryCoreClient(store),
                transactions_client=InMemoryTransactionsClient(store),
            )
        ],
        router=APIRouter(prefix="/api"),
    )

    with TestClient(api.app) as client:
        response = client.post(
            f"/api/collections/{COLLECTION_ID}/reconcile",
            json={
                "version": 1,
                "scope": "complete",
                "item_count": 1,
                "items": [make_item("a")],
            },
        )

    assert response.is_success, response.text
    assert set(store.items) == {"a"}


def test_extension_route_dependencies(store: InMemoryStore) -> None:
    from fastapi import Depends, HTTPException, security, status

    def must_be_bob(
        credentials: security.HTTPBasicCredentials = Depends(security.HTTPBasic()),
    ) -> bool:
        if credentials.username != "bob":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated",
                headers={"WWW-Authenticate": "Basic"},
            )
        return True

    api = StacApi(
        settings=ApiSettings(),
        client=InMemoryCoreClient(store),
        extensions=[
            ReconciliationExtension(
                core_client=InMemoryCoreClient(store),
                transactions_client=InMemoryTransactionsClient(store),
                route_dependencies=[Depends(must_be_bob)],
            )
        ],
    )
    payload = {
        "version": 1,
        "scope": "partial",
        "items": [make_item("a")],
    }

    with TestClient(api.app) as client:
        assert (
            client.post(
                f"/collections/{COLLECTION_ID}/reconcile", json=payload
            ).status_code
            == 401
        )
        response = client.post(
            f"/collections/{COLLECTION_ID}/reconcile",
            json=payload,
            auth=("bob", "dobbs"),
        )
    assert response.is_success, response.text


def test_extension_advertises_custom_conformance_classes() -> None:
    url = "https://stac-utils.github.io/stac-fastapi/reconciliation"
    extension = ReconciliationExtension(client=StubReconciliationClient())

    assert extension.conformance_classes == []

    extension = ReconciliationExtension(
        client=StubReconciliationClient(), conformance_classes=[url]
    )
    assert extension.conformance_classes == [url]
