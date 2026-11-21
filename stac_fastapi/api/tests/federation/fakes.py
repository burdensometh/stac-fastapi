"""In-memory fake STAC backends used to exercise the federation.

The fakes intentionally record every request they receive so tests can assert
which conditions were forwarded to which source (capability-aware dispatch),
and they can be configured to fail on demand to cover the partial-coverage
paths. They support both the sync and async client interfaces.
"""

import copy

from stac_fastapi.types.core import AsyncBaseCoreClient, BaseCoreClient
from stac_fastapi.types.search import BaseSearchPostRequest


def make_collection(collection_id: str, license_: str, bbox=None, interval=None):
    return {
        "type": "Collection",
        "id": collection_id,
        "title": f"Collection {collection_id}",
        "description": f"Description of {collection_id}",
        "license": license_,
        "keywords": [],
        "providers": [],
        "extent": {
            "spatial": {"bbox": [bbox or [-180, -90, 180, 90]]},
            "temporal": {"interval": [interval or ["2000-01-01T00:00:00Z", None]]},
        },
        "summaries": {},
        "assets": {},
        "links": [],
    }


def make_item(item_id, collection_id, lon=0.0, lat=0.0, when="2020-01-01T00:00:00Z"):
    return {
        "type": "Feature",
        "stac_version": "1.1.0",
        "id": item_id,
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "bbox": [lon, lat, lon, lat],
        "properties": {"datetime": when},
        "links": [],
        "assets": {},
        "collection": collection_id,
    }


class FakeBackend:
    """Shared backend logic parameterized by sync/async execution."""

    def __init__(self, source_id, collections, items_by_collection, fail_methods=()):
        self.source_id = source_id
        self._collections = {c["id"]: c for c in collections}
        self._items = copy.deepcopy(items_by_collection)
        self.fail_methods = set(fail_methods)
        # Record of (method, kwargs) calls for dispatch assertions.
        self.calls = []

    def _maybe_fail(self, method):
        if method in self.fail_methods:
            raise RuntimeError(f"{self.source_id}: forced {method} failure")

    def _all_collections(self):
        self._maybe_fail("all_collections")
        self.calls.append(("all_collections", {}))
        return {
            "collections": list(self._collections.values()),
            "links": [],
        }

    def _get_collection(self, collection_id):
        self._maybe_fail("get_collection")
        self.calls.append(("get_collection", {"collection_id": collection_id}))
        if collection_id not in self._collections:
            from stac_fastapi.types.errors import NotFoundError

            raise NotFoundError(f"{collection_id} not on {self.source_id}")
        return copy.deepcopy(self._collections[collection_id])

    def _get_item(self, item_id, collection_id):
        self._maybe_fail("get_item")
        self.calls.append(
            ("get_item", {"item_id": item_id, "collection_id": collection_id})
        )
        items = self._items.get(collection_id, [])
        for item in items:
            if item["id"] == item_id:
                return copy.deepcopy(item)
        from stac_fastapi.types.errors import NotFoundError

        raise NotFoundError(
            f"item {item_id} not in {collection_id} on {self.source_id}"
        )

    def _item_collection(self, collection_id, **kwargs):
        self._maybe_fail("item_collection")
        self.calls.append(("item_collection", {"collection_id": collection_id, **kwargs}))
        if collection_id not in self._collections:
            from stac_fastapi.types.errors import NotFoundError

            raise NotFoundError(f"{collection_id} not on {self.source_id}")
        return {
            "type": "FeatureCollection",
            "features": copy.deepcopy(self._items.get(collection_id, [])),
            "links": [],
        }

    def _search(self, **kwargs):
        self._maybe_fail("get_search")
        self.calls.append(("get_search", dict(kwargs)))
        collection_filter = kwargs.get("collections")
        if collection_filter:
            unknown = [cid for cid in collection_filter if cid not in self._items]
            if unknown:
                from stac_fastapi.types.errors import NotFoundError

                raise NotFoundError(
                    f"collection(s) {unknown} not on {self.source_id}"
                )
        ids_filter = kwargs.get("ids")
        features = []
        for cid, items in self._items.items():
            if collection_filter and cid not in collection_filter:
                continue
            for item in items:
                if ids_filter and item["id"] not in ids_filter:
                    continue
                features.append(copy.deepcopy(item))
        return {"type": "FeatureCollection", "features": features, "links": []}


class SyncFakeBackend(BaseCoreClient, FakeBackend):
    def __init__(self, source_id, collections, items_by_collection, fail_methods=()):
        FakeBackend.__init__(
            self, source_id, collections, items_by_collection, fail_methods
        )

    def all_collections(self, **kwargs):
        return self._all_collections()

    def get_collection(self, collection_id, **kwargs):
        return self._get_collection(collection_id)

    def get_item(self, item_id, collection_id, **kwargs):
        return self._get_item(item_id, collection_id)

    def item_collection(self, collection_id, **kwargs):
        return self._item_collection(collection_id, **kwargs)

    def get_search(self, **kwargs):
        return self._search(**kwargs)

    def post_search(self, search_request: BaseSearchPostRequest, **kwargs):
        return self._search(**search_request.model_dump(exclude_none=True))


class AsyncFakeBackend(AsyncBaseCoreClient, FakeBackend):
    def __init__(self, source_id, collections, items_by_collection, fail_methods=()):
        FakeBackend.__init__(
            self, source_id, collections, items_by_collection, fail_methods
        )

    async def all_collections(self, **kwargs):
        return self._all_collections()

    async def get_collection(self, collection_id, **kwargs):
        return self._get_collection(collection_id)

    async def get_item(self, item_id, collection_id, **kwargs):
        return self._get_item(item_id, collection_id)

    async def item_collection(self, collection_id, **kwargs):
        return self._item_collection(collection_id, **kwargs)

    async def get_search(self, **kwargs):
        return self._search(**kwargs)

    async def post_search(self, search_request: BaseSearchPostRequest, **kwargs):
        return self._search(**search_request.model_dump(exclude_none=True))
