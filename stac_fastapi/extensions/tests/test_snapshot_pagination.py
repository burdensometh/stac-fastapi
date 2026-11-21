"""Tests for snapshot based token pagination."""

from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.testclient import TestClient

from stac_fastapi.api.app import StacApi
from stac_fastapi.api.models import (
    ItemCollectionUri as BaseItemCollectionUri,
)
from stac_fastapi.api.models import (
    create_post_request_model,
    create_request_model,
)
from stac_fastapi.extensions import (
    InMemorySnapshotBackend,
    TokenPaginationExtension,
)
from stac_fastapi.extensions.pagination.snapshot import (
    Snapshot,
    criteria_fingerprint,
    decode_token,
    encode_token,
    normalize_criteria,
)
from stac_fastapi.types.config import ApiSettings
from stac_fastapi.types.core import BaseCoreClient
from stac_fastapi.types.search import BaseSearchGetRequest


class SearchBackend:
    """A tiny in-memory 'database' with keyset pagination."""

    def __init__(self, items: list[dict[str, Any]]):
        self.items = list(items)

    def search(self, criteria: dict[str, Any], token: str | None, limit: int):
        matching = [item for item in self.items if self._matches(item, criteria)]
        start = int(token or 0)
        page = matching[start : start + limit]
        links: list[dict[str, Any]] = []
        if start + limit < len(matching):
            links.append(
                {
                    "rel": "next",
                    "href": (
                        f"http://testserver/search?limit={limit}&token={start + limit}"
                    ),
                }
            )
        return {
            "type": "FeatureCollection",
            "features": page,
            "links": links,
            "numberReturned": len(page),
        }

    @staticmethod
    def _matches(item, criteria) -> bool:
        collections = criteria.get("collections")
        if collections and item["collection"] not in collections:
            return False
        date = criteria.get("datetime")
        if date is not None:
            if not (date[1] <= item["properties"]["datetime"] <= date[2]):
                return False
        return True


class SnapshotCoreClient(BaseCoreClient):
    def __init__(self, backend: SearchBackend):
        self.backend = backend
        self.calls = 0

    def all_collections(self, *args, **kwargs):
        return self.backend.search({}, kwargs.get("token"), kwargs.get("limit") or 10)

    def get_collection(self, *args, **kwargs):
        raise NotImplementedError

    def get_item(self, *args, **kwargs):
        raise NotImplementedError

    def _criteria(self, collections, ids, bbox, intersects, datetime, **kwargs):
        return {"collections": collections, "datetime": datetime}

    def get_search(
        self,
        collections=None,
        ids=None,
        bbox=None,
        intersects=None,
        datetime=None,
        limit=10,
        token=None,
        **kwargs,
    ):
        self.calls += 1
        criteria = self._criteria(collections, ids, bbox, intersects, datetime)
        return self.backend.search(criteria, token, limit or 10)

    def post_search(self, search_request, **kwargs):
        self.calls += 1
        criteria = {
            "collections": search_request.collections,
            "datetime": search_request.datetime,
        }
        return self.backend.search(criteria, search_request.token, search_request.limit)

    def item_collection(
        self, collection_id, bbox=None, datetime=None, limit=10, token=None, **kwargs
    ):
        self.calls += 1
        return self.backend.search(
            {"collections": [collection_id], "datetime": datetime}, token, limit or 10
        )


def make_items(n: int, collection: str = "c1") -> list[dict[str, Any]]:
    return [
        {
            "id": f"item-{i}",
            "collection": collection,
            "properties": {"datetime": f"2024-01-{i + 1:02d}T00:00:00Z"},
        }
        for i in range(n)
    ]


@pytest.fixture
def backend() -> SearchBackend:
    return SearchBackend(make_items(10))


@pytest.fixture
def extension() -> TokenPaginationExtension:
    return TokenPaginationExtension(
        snapshot_backend=InMemorySnapshotBackend(),
        snapshot_secret="secret",
    )


@pytest.fixture
def client(
    backend: SearchBackend, extension: TokenPaginationExtension
) -> Iterator[TestClient]:
    settings = ApiSettings()
    search_get_request_model = create_request_model(
        model_name="SearchGetRequest",
        base_model=BaseSearchGetRequest,
        mixins=[extension.GET],
        request_type="GET",
    )
    api = StacApi(
        settings=settings,
        client=SnapshotCoreClient(backend),
        extensions=[extension],
        search_get_request_model=search_get_request_model,
        search_post_request_model=create_post_request_model([extension]),
    )
    with TestClient(api.app) as client:
        yield client


def next_link(payload: dict[str, Any]) -> dict[str, Any]:
    return next(link for link in payload["links"] if link["rel"] == "next")


def page_ids(payload: dict[str, Any]) -> list[str]:
    return [feature["id"] for feature in payload["features"]]


def token_of(link: dict[str, Any]) -> str:
    body = link.get("body")
    if body is not None:
        return body["token"]
    return parse_qs(urlsplit(link["href"]).query)["token"][0]


def test_snapshot_is_stable_after_insert(backend, client):
    """Inserted items do not appear when following next links."""
    first = client.get("/search", params={"limit": 4}).json()
    assert page_ids(first) == [f"item-{i}" for i in range(4)]
    assert first["numberMatched"] == 10

    backend.items.insert(0, {**make_items(1, "c1")[0], "id": "inserted"})

    seen = page_ids(first)
    link = next_link(first)
    while link is not None:
        payload = client.get(link["href"]).json()
        seen.extend(page_ids(payload))
        assert "inserted" not in page_ids(payload)
        link = next((link for link in payload["links"] if link["rel"] == "next"), None)

    assert seen == [f"item-{i}" for i in range(10)]
    assert len(set(seen)) == len(seen)


def test_snapshot_is_stable_after_delete(backend, client):
    """Deleted items are still served from the snapshot taken by the first page."""
    first = client.get("/search", params={"limit": 5}).json()
    assert page_ids(first) == [f"item-{i}" for i in range(5)]

    del backend.items[6]

    second = client.get(next_link(first)["href"]).json()
    assert page_ids(second) == [f"item-{i}" for i in range(5, 10)]
    assert second["numberMatched"] == 10


def test_no_duplicates_or_skipped_rows(backend, client):
    """Every matching item is returned exactly once."""
    seen: list[str] = []
    response = client.get("/search", params={"limit": 3}).json()
    seen.extend(page_ids(response))
    while link := next(
        (link for link in response["links"] if link["rel"] == "next"), None
    ):
        response = client.get(link["href"]).json()
        seen.extend(page_ids(response))
    assert seen == [f"item-{i}" for i in range(10)]


def test_criteria_mismatch_is_rejected(client):
    """A token can not be replayed with different search parameters."""
    first = client.get("/search", params={"limit": 4}).json()
    link = next_link(first)

    response = client.get(
        "/search",
        params={"limit": 4, "token": token_of(link), "collections": "other"},
    )
    assert response.status_code == 400, response.json()
    assert "does not match" in response.json()["description"]

    response = client.post(
        "/search",
        json={"collections": ["other"], "limit": 4, "token": token_of(link)},
    )
    assert response.status_code == 400, response.json()


def test_token_cannot_be_replayed_on_another_endpoint(backend, extension):
    """Tokens are bound to the endpoint which issued them."""
    collections_request_model = create_request_model(
        model_name="CollectionsGetRequest",
        base_model=BaseSearchGetRequest,
        mixins=[extension.GET],
        request_type="GET",
    )
    api = StacApi(
        settings=ApiSettings(),
        client=SnapshotCoreClient(backend),
        extensions=[extension],
        collections_get_request_model=collections_request_model,
    )
    with TestClient(api.app) as client:
        first = client.get("/search", params={"limit": 4}).json()
        token = token_of(next_link(first))

        response = client.get("/collections", params={"limit": 4, "token": token})
        assert response.status_code == 400, response.json()
        assert "does not match" in response.json()["description"]


def test_expired_snapshot_asks_for_a_new_search(client, extension):
    """An expired snapshot responds with 410 Gone."""
    first = client.get("/search", params={"limit": 4}).json()
    token = token_of(next_link(first))

    payload = decode_token(token, extension.snapshot_secret)
    snapshot = extension.snapshot_backend.load(payload["snapshot"])
    assert snapshot is not None
    extension.snapshot_backend.save(
        Snapshot(
            id=snapshot.id,
            criteria=snapshot.criteria,
            items=snapshot.items,
            links=snapshot.links,
            envelope=snapshot.envelope,
            item_key=snapshot.item_key,
            created_at=snapshot.created_at,
            expires_at=0,
        )
    )

    response = client.get("/search", params={"limit": 4, "token": token})
    assert response.status_code == 410, response.json()
    assert "re-run the search" in response.json()["description"]


def test_token_with_missing_signature_is_rejected(client):
    """A token we issued but whose signature was stripped is rejected."""
    unsigned = encode_token("snapshot", "criteria", 0)
    response = client.get("/search", params={"token": unsigned})
    assert response.status_code == 400, response.json()
    assert "signature" in response.json()["description"]


def test_foreign_token_is_forwarded(client):
    """Opaque backend tokens which we did not issue keep working."""
    response = client.get("/search", params={"limit": 4, "token": "4"})
    assert response.status_code == 200, response.json()
    assert page_ids(response.json()) == [f"item-{i}" for i in range(4, 8)]


def test_signed_token_rejects_tampering(client, extension):
    """A token with a modified offset is rejected."""
    first = client.get("/search", params={"limit": 4}).json()
    token = token_of(next_link(first))

    payload = decode_token(token, extension.snapshot_secret)
    forged = encode_token(
        payload["snapshot"], payload["criteria"], 8, secret="other-secret"
    )
    response = client.get("/search", params={"token": forged})
    assert response.status_code == 400, response.json()
    assert "signature" in response.json()["description"]


def test_get_and_post_share_the_same_result_set(client):
    """A GET token can continue a POST search with equivalent parameters."""
    first = client.get("/search", params={"limit": 4, "collections": "c1"}).json()
    get_token = token_of(next_link(first))

    response = client.post(
        "/search", json={"limit": 4, "token": get_token, "collections": ["c1"]}
    )
    assert response.status_code == 200, response.json()
    assert page_ids(response.json()) == [f"item-{i}" for i in range(4, 8)]

    post_token = token_of(next_link(response.json()))
    response = client.get(
        "/search", params={"limit": 4, "token": post_token, "collections": "c1"}
    )
    assert response.status_code == 200, response.json()
    assert page_ids(response.json()) == [f"item-{i}" for i in range(8, 10)]


def test_post_next_link_keeps_post_method(client):
    """POST pagination links keep the POST method and the request body."""
    response = client.post("/search", json={"limit": 4})
    payload = response.json()
    link = next_link(payload)
    assert link["method"] == "POST"
    assert link["body"]["token"]

    second = client.post(link["href"], json=link["body"])
    assert second.status_code == 200, second.json()
    assert page_ids(second.json()) == [f"item-{i}" for i in range(4, 8)]


def test_self_and_prev_links(client):
    """The second page has a `prev` link pointing back to the first page."""
    first = client.get("/search", params={"limit": 4}).json()
    second = client.get(next_link(first)["href"]).json()

    prev = next(link for link in second["links"] if link["rel"] in ("prev", "previous"))
    back = client.get(prev["href"]).json()
    assert page_ids(back) == page_ids(first)

    self_link = next(link for link in first["links"] if link["rel"] == "self")
    assert "token" not in parse_qs(urlsplit(self_link["href"]).query)


def test_single_page_is_untouched(backend, client):
    """When everything fits on one page no snapshot is needed."""
    payload = client.get("/search", params={"limit": 50}).json()
    assert page_ids(payload) == [f"item-{i}" for i in range(10)]
    assert all(link["rel"] != "next" for link in payload["links"])


def test_without_backend_tokens_are_forwarded(backend):
    """Without a snapshot backend the historical behaviour is preserved."""
    extension = TokenPaginationExtension()
    search_get_request_model = create_request_model(
        model_name="SearchGetRequest",
        base_model=BaseSearchGetRequest,
        mixins=[extension.GET],
        request_type="GET",
    )
    api = StacApi(
        settings=ApiSettings(),
        client=SnapshotCoreClient(backend),
        extensions=[extension],
        search_get_request_model=search_get_request_model,
    )
    with TestClient(api.app) as client:
        response = client.get("/search", params={"limit": 4})
        assert response.status_code == 200
        assert page_ids(response.json()) == [f"item-{i}" for i in range(4)]

        next_href = next_link(response.json())["href"]
        second = client.get(next_href)
        assert page_ids(second.json()) == [f"item-{i}" for i in range(4, 8)]


def test_result_set_too_large_is_reported(backend):
    """A result set larger than the configured limit responds with 413."""
    extension = TokenPaginationExtension(
        snapshot_backend=InMemorySnapshotBackend(), snapshot_max_items=3
    )
    search_get_request_model = create_request_model(
        model_name="SearchGetRequest",
        base_model=BaseSearchGetRequest,
        mixins=[extension.GET],
        request_type="GET",
    )
    api = StacApi(
        settings=ApiSettings(),
        client=SnapshotCoreClient(backend),
        extensions=[extension],
        search_get_request_model=search_get_request_model,
    )
    with TestClient(api.app) as client:
        response = client.get("/search", params={"limit": 2})
        assert response.status_code == 413, response.json()
        assert "too large" in response.json()["description"]


def test_snapshot_ttl_expiry(backend):
    """Snapshots expire after their configured TTL."""
    extension = TokenPaginationExtension(
        snapshot_backend=InMemorySnapshotBackend(), snapshot_ttl=-1
    )
    search_get_request_model = create_request_model(
        model_name="SearchGetRequest",
        base_model=BaseSearchGetRequest,
        mixins=[extension.GET],
        request_type="GET",
    )
    api = StacApi(
        settings=ApiSettings(),
        client=SnapshotCoreClient(backend),
        extensions=[extension],
        search_get_request_model=search_get_request_model,
    )
    with TestClient(api.app) as client:
        first = client.get("/search", params={"limit": 4}).json()
        token = token_of(next_link(first))
        response = client.get("/search", params={"limit": 4, "token": token})
        assert response.status_code == 410, response.json()


def test_item_collection_is_snapshotted(backend, extension):
    """The `/collections/{id}/items` endpoint is snapshot aware too."""
    collection_request_model = create_request_model(
        model_name="ItemsGetRequest",
        base_model=BaseItemCollectionUri,
        mixins=[extension.GET],
        request_type="GET",
    )
    api = StacApi(
        settings=ApiSettings(),
        client=SnapshotCoreClient(backend),
        extensions=[extension],
        items_get_request_model=collection_request_model,
    )
    with TestClient(api.app) as client:
        first = client.get("/collections/c1/items", params={"limit": 4}).json()
        assert page_ids(first) == [f"item-{i}" for i in range(4)]
        second = client.get(next_link(first)["href"]).json()
        assert page_ids(second) == [f"item-{i}" for i in range(4, 8)]


def test_criteria_fingerprint_is_verb_independent():
    """GET and POST spellings of the same search share a fingerprint."""
    get_criteria = {
        "collections": ["c1", "c2"],
        "bbox": (0.0, 0.0, 1.0, 1.0),
        "sortby": ["-datetime", "id"],
        "fields": ["id", "-geometry"],
        "limit": 10,
    }
    post_criteria = {
        "collections": ["c2", "c1"],
        "bbox": [0, 0, 1, 1],
        "sortby": [
            {"field": "datetime", "direction": "desc"},
            {"field": "id", "direction": "asc"},
        ],
        "fields": {"include": ["id"], "exclude": ["geometry"]},
        "limit": 50,
        "token": "ignored",
    }
    assert normalize_criteria(get_criteria) == normalize_criteria(post_criteria)
    assert criteria_fingerprint(get_criteria, "search") == criteria_fingerprint(
        post_criteria, "search"
    )


def test_criteria_fingerprint_distinguishes_filters():
    """Different filters produce different fingerprints."""
    assert criteria_fingerprint(
        {"filter_expr": {"op": "=", "args": []}}, "search"
    ) != criteria_fingerprint({"filter_expr": {"op": ">", "args": []}}, "search")
    assert criteria_fingerprint(
        {"collections": ["c1"]}, "search"
    ) != criteria_fingerprint({"collections": ["c2"]}, "search")


def test_token_roundtrip():
    """Tokens encode and decode the snapshot, criteria and offset."""
    token = encode_token("snap", "criteria", 12, secret="s")
    payload = decode_token(token, "s")
    assert payload["snapshot"] == "snap"
    assert payload["criteria"] == "criteria"
    assert payload["offset"] == 12
