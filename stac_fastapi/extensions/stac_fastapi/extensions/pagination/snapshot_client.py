"""Consistent pagination for core clients using a snapshot backend.

The :class:`SnapshotPagination` helper wraps a core client so that the first page of a
result set is materialized into a :class:`~.snapshot.SnapshotBackend` and subsequent
pages are served from that immutable version. This guarantees that a user browsing
``next`` links keeps seeing the same result set even if items are inserted or deleted
concurrently.
"""

from __future__ import annotations

import inspect
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from .snapshot import (
    PAGINATION_RELS,
    InvalidPaginationToken,
    PaginationCriteriaMismatch,
    ResultSetTooLarge,
    Snapshot,
    SnapshotBackend,
    SnapshotExpired,
    criteria_fingerprint,
    decode_token,
    encode_token,
    next_token_from_links,
    replace_token_in_url,
    strip_token,
)

__all__ = ["SnapshotPagination"]

#: Returned by :meth:`SnapshotPagination._resume` when the token was not issued by
#: this extension and should be handled by the backend client.
_UNHANDLED = object()

DEFAULT_LIMIT = 10

#: Upper bound of pages read while materializing a snapshot, as a safety net against
#: backends which keep returning a ``next`` link forever.
MAX_PAGES = 100_000


def _page_limit(params: Mapping[str, Any]) -> int:
    limit = params.get("limit")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        return DEFAULT_LIMIT
    return limit


def _item_key(response: Mapping[str, Any]) -> str:
    return "collections" if "collections" in response else "features"


def _base_links(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(link)
        for link in response.get("links") or []
        if isinstance(link, Mapping) and link.get("rel") not in PAGINATION_RELS
    ]


def _envelope(response: Mapping[str, Any], item_key: str) -> dict[str, Any]:
    return {
        key: value
        for key, value in response.items()
        if key not in (item_key, "links", "numberMatched", "numberReturned")
    }


def _model_dump(model: Any) -> dict[str, Any]:
    dump = getattr(model, "model_dump", None)
    if callable(dump):
        return dict(dump(mode="json"))
    return dict(model or {})


def _as_mapping(response: Any) -> Mapping[str, Any] | None:
    """Return a response as a mapping, accepting pydantic models."""
    if isinstance(response, Mapping):
        return response
    dump = getattr(response, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    return None


class SnapshotPagination:
    """Serve paginated results from immutable snapshots.

    Args:
        backend: storage for materialized result sets.
        secret: optional secret used to sign pagination tokens.
        max_items: maximum number of items a single snapshot may hold. When a result
            set is larger, the response is passed through unchanged instead of being
            materialized (so that huge result sets are not buffered in memory).
        ttl: optional lifetime (in seconds) of a snapshot. Expired snapshots respond
            with ``410 Gone`` and ask the client to re-run the search.
    """

    ENDPOINTS = ("get_search", "post_search", "item_collection", "all_collections")

    def __init__(
        self,
        backend: SnapshotBackend,
        *,
        secret: str | None = None,
        max_items: int | None = None,
        ttl: float | None = None,
    ):
        """Initialize the handler.

        Args:
            backend: storage for materialized result sets.
            secret: optional secret used to sign pagination tokens.
            max_items: optional maximum number of items per snapshot.
            ttl: optional snapshot lifetime, in seconds.
        """
        self.backend = backend
        self.secret = secret
        self.max_items = max_items
        self.ttl = ttl

    # ------------------------------------------------------------------ setup
    def apply(self, client: Any) -> Any:
        """Wrap the paginated methods of ``client``.

        Returns:
            The same client, or a delegating proxy if the client does not allow
            attribute replacement.
        """
        handlers: dict[str, Callable[..., Any]] = {}
        for name in self.ENDPOINTS:
            method = getattr(client, name, None)
            if method is None:
                continue
            handlers[name] = self._make_handler(name, method)

        if not handlers:
            return client

        try:
            for name, handler in handlers.items():
                setattr(client, name, handler)
        except (AttributeError, TypeError):  # frozen or slotted clients
            return _SnapshotClientProxy(client, handlers)
        return client

    def _make_handler(self, name: str, method: Callable[..., Any]) -> Callable:
        is_async = inspect.iscoroutinefunction(method)

        if name == "post_search":
            if is_async:

                async def post_search_async(search_request, **kwargs):
                    return await self._serve_post_async(search_request, method, kwargs)

                return post_search_async

            def post_search_sync(search_request, **kwargs):
                return self._serve_post(search_request, method, kwargs)

            return post_search_sync

        endpoint = "search"
        if name == "item_collection":
            endpoint = "items"
        elif name == "all_collections":
            endpoint = "collections"

        if is_async:

            async def get_async(**kwargs):
                return await self._serve_get_async(method, kwargs, endpoint)

            return get_async

        def get_sync(**kwargs):
            return self._serve_get(method, kwargs, endpoint)

        return get_sync

    # -------------------------------------------------------------- backend io
    def _load(self, snapshot_id: str) -> Snapshot | None:
        load = getattr(self.backend, "load", None) or getattr(self.backend, "get", None)
        if load is None:
            raise TypeError(
                "Snapshot backend must implement `load(snapshot_id)` (or `get`)."
            )
        return load(snapshot_id)

    def _save(self, snapshot: Snapshot) -> None:
        save = getattr(self.backend, "save", None) or getattr(self.backend, "put", None)
        if save is None:
            raise TypeError(
                "Snapshot backend must implement `save(snapshot)` (or `put`)."
            )
        save(snapshot)

    # ------------------------------------------------------------- token load
    def _resume(self, token: str, criteria: str) -> tuple[Snapshot, int] | object:
        try:
            payload = decode_token(token, self.secret)
        except InvalidPaginationToken as exc:
            if not exc.ours:
                # Not a token we issued (e.g. an opaque backend token): let the
                # client handle it, preserving existing behaviour.
                return _UNHANDLED
            raise

        if payload["criteria"] != criteria:
            raise PaginationCriteriaMismatch()

        snapshot = self._load(payload["snapshot"])
        if snapshot is None or snapshot.is_expired():
            raise SnapshotExpired(payload["snapshot"])
        if snapshot.criteria != criteria:
            raise PaginationCriteriaMismatch()

        return snapshot, payload["offset"]

    # ------------------------------------------------------------ GET & /items
    def _serve_get(self, method, kwargs, endpoint):
        params = dict(kwargs)
        request = params.get("request")
        limit = _page_limit(params)
        collection_id = params.get("collection_id") if endpoint == "items" else None
        criteria = criteria_fingerprint(params, endpoint, collection_id)

        token = params.get("token")
        if token:
            resumed = self._resume(token, criteria)
            if resumed is not _UNHANDLED:
                snapshot, offset = resumed  # type: ignore[misc]
                return self._render(snapshot, offset, limit, request)
            return method(**params)

        first = method(**params)

        def fetch(next_token: str):
            return method(**{**params, "token": next_token})

        return self._start(first, fetch, criteria, limit, request)

    async def _serve_get_async(self, method, kwargs, endpoint):
        params = dict(kwargs)
        request = params.get("request")
        limit = _page_limit(params)
        collection_id = params.get("collection_id") if endpoint == "items" else None
        criteria = criteria_fingerprint(params, endpoint, collection_id)

        token = params.get("token")
        if token:
            resumed = self._resume(token, criteria)
            if resumed is not _UNHANDLED:
                snapshot, offset = resumed  # type: ignore[misc]
                return self._render(snapshot, offset, limit, request)
            return await method(**params)

        first = await method(**params)

        async def fetch(next_token: str):
            return await method(**{**params, "token": next_token})

        return await self._start_async(first, fetch, criteria, limit, request)

    # ------------------------------------------------------------------- POST
    def _serve_post(self, search_request, method, kwargs):
        params = dict(kwargs)
        request = params.get("request")
        body = self._post_body(search_request)
        criteria = criteria_fingerprint(body, "search")
        limit = _page_limit(body)

        token = self._post_token(search_request)
        if token:
            resumed = self._resume(token, criteria)
            if resumed is not _UNHANDLED:
                snapshot, offset = resumed  # type: ignore[misc]
                return self._render(snapshot, offset, limit, request, body=body)
            return method(search_request, **params)

        first = method(search_request, **params)

        def fetch(next_token: str):
            return method(
                search_request.model_copy(update={"token": next_token}), **params
            )

        return self._start(first, fetch, criteria, limit, request, body=body)

    async def _serve_post_async(self, search_request, method, kwargs):
        params = dict(kwargs)
        request = params.get("request")
        body = self._post_body(search_request)
        criteria = criteria_fingerprint(body, "search")
        limit = _page_limit(body)

        token = self._post_token(search_request)
        if token:
            resumed = self._resume(token, criteria)
            if resumed is not _UNHANDLED:
                snapshot, offset = resumed  # type: ignore[misc]
                return self._render(snapshot, offset, limit, request, body=body)
            return await method(search_request, **params)

        first = await method(search_request, **params)

        async def fetch(next_token: str):
            return await method(
                search_request.model_copy(update={"token": next_token}), **params
            )

        return await self._start_async(first, fetch, criteria, limit, request, body=body)

    @staticmethod
    def _post_body(search_request: Any) -> dict[str, Any]:
        """Search parameters of a POST request, without the pagination token."""
        body = _model_dump(search_request)
        body.pop("token", None)
        return body

    @staticmethod
    def _post_token(search_request: Any) -> str | None:
        token = getattr(search_request, "token", None)
        return token if isinstance(token, str) and token else None

    # ------------------------------------------------------------- materialize
    def _start(self, first, fetch, criteria, limit, request, body=None):
        snapshot = self._collect_sync(first, fetch, criteria)
        if snapshot is None:
            return first
        return self._render(snapshot, 0, limit, request, body=body)

    async def _start_async(self, first, fetch, criteria, limit, request, body=None):
        snapshot = await self._collect_async(first, fetch, criteria)
        if snapshot is None:
            return first
        return self._render(snapshot, 0, limit, request, body=body)

    def _new_snapshot(self, first, items, criteria) -> Snapshot:
        item_key = _item_key(first)
        now = time.time()
        return Snapshot(
            id=uuid.uuid4().hex,
            criteria=criteria,
            items=tuple(items),
            links=tuple(_base_links(first)),
            envelope=_envelope(first, item_key),
            item_key=item_key,
            created_at=now,
            expires_at=None if self.ttl is None else now + self.ttl,
        )

    def _collect_sync(self, first, fetch, criteria) -> Snapshot | None:
        first = _as_mapping(first)
        if first is None:
            return None

        item_key = _item_key(first)
        next_token = next_token_from_links(first.get("links") or [])
        if not next_token:
            # A single page: no snapshot needed, keep the response untouched.
            return None

        items = list(first.get(item_key) or [])
        seen: set[str] = set()
        while next_token and next_token not in seen:
            seen.add(next_token)
            if len(seen) > MAX_PAGES:
                raise ResultSetTooLarge(max_pages=MAX_PAGES)
            if self.max_items is not None and len(items) > self.max_items:
                raise ResultSetTooLarge(max_items=self.max_items)

            response = _as_mapping(fetch(next_token))
            if response is None:
                return None
            items.extend(response.get(item_key) or [])
            next_token = next_token_from_links(response.get("links") or [])

        if self.max_items is not None and len(items) > self.max_items:
            raise ResultSetTooLarge(max_items=self.max_items)

        snapshot = self._new_snapshot(first, items, criteria)
        self._save(snapshot)
        return snapshot

    async def _collect_async(self, first, fetch, criteria) -> Snapshot | None:
        first = _as_mapping(first)
        if first is None:
            return None

        item_key = _item_key(first)
        next_token = next_token_from_links(first.get("links") or [])
        if not next_token:
            return None

        items = list(first.get(item_key) or [])
        seen: set[str] = set()
        while next_token and next_token not in seen:
            seen.add(next_token)
            if len(seen) > MAX_PAGES:
                raise ResultSetTooLarge(max_pages=MAX_PAGES)
            if self.max_items is not None and len(items) > self.max_items:
                raise ResultSetTooLarge(max_items=self.max_items)

            response = _as_mapping(await fetch(next_token))
            if response is None:
                return None
            items.extend(response.get(item_key) or [])
            next_token = next_token_from_links(response.get("links") or [])

        if self.max_items is not None and len(items) > self.max_items:
            raise ResultSetTooLarge(max_items=self.max_items)

        snapshot = self._new_snapshot(first, items, criteria)
        self._save(snapshot)
        return snapshot

    # ---------------------------------------------------------------- render
    def _render(
        self,
        snapshot: Snapshot,
        offset: int,
        limit: int,
        request,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        total = len(snapshot.items)
        offset = max(0, min(offset, total))
        page = list(snapshot.items[offset : offset + limit])

        response = dict(snapshot.envelope)
        response[snapshot.item_key] = page
        response["numberMatched"] = total
        response["numberReturned"] = len(page)

        links = [dict(link) for link in snapshot.links]
        if request is not None:
            links.extend(
                self._pagination_links(snapshot, offset, len(page), limit, request, body)
            )
        response["links"] = links
        return response

    def _pagination_links(
        self,
        snapshot: Snapshot,
        offset: int,
        returned: int,
        limit: int,
        request,
        body: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        total = len(snapshot.items)
        is_post = body is not None and getattr(request, "method", "GET") == "POST"

        def link(rel: str, position: int, with_token: bool = True) -> dict[str, Any]:
            token = (
                encode_token(snapshot.id, snapshot.criteria, position, secret=self.secret)
                if with_token
                else None
            )

            if is_post and body is not None:
                page_body = dict(body)
                if token is not None:
                    page_body["token"] = token
                else:
                    page_body.pop("token", None)
                return {
                    "rel": rel,
                    "href": str(request.url),
                    "method": "POST",
                    "body": page_body,
                }

            href = str(request.url)
            if token is None:
                href = strip_token(href)
            else:
                href = replace_token_in_url(href, token)
            return {"rel": rel, "href": href}

        links = [link("self", offset, with_token=offset != 0)]
        if offset + returned < total:
            links.append(link("next", offset + returned))
        if offset > 0:
            links.append(link("prev", max(0, offset - limit)))
        return links


class _SnapshotClientProxy:
    """Delegating proxy used when the client does not accept attribute updates."""

    def __init__(self, client: Any, handlers: dict[str, Callable[..., Any]]):
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_handlers", handlers)

    def __getattr__(self, name: str) -> Any:
        handlers = object.__getattribute__(self, "_handlers")
        if name in handlers:
            return handlers[name]
        return getattr(object.__getattribute__(self, "_client"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(object.__getattribute__(self, "_client"), name, value)
