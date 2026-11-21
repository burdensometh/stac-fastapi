"""Federated STAC client.

:class:`FederatedClient` implements the standard
:class:`~stac_fastapi.types.core.AsyncBaseCoreClient` interface on top of
multiple upstream backend clients, applying the federation rules described in
the package docstring:

* capability-aware fan-out (each source executes only supported conditions),
* deterministic, provenance-stamped merging with explicit conflict rules,
* stable source/collection routing shared by every route, and
* explicit coverage reporting (including partial and total failures).
"""

import asyncio
from typing import Any

import attr
from geojson_pydantic.geometries import Geometry
from stac_pydantic.shared import BBox

from stac_fastapi.types.core import AsyncBaseCoreClient
from stac_fastapi.types.requests import get_base_url
from stac_fastapi.types.search import BaseSearchPostRequest
from stac_fastapi.types.stac import Collection, Collections, Item, ItemCollection

from .dispatch import (
    SourceCallResult,
    call_collections,
    call_search_source,
    call_single,
    split_request_by_capabilities,
)
from .errors import FederationCoverageError
from .filters import apply_local_filters
from .merge import (
    SOURCE_HINT_HEADER,
    attach_coverage,
    build_coverage,
    build_variants,
    merge_collections,
    merge_item_collections,
    stamp_collection,
    stamp_item,
)
from .models import (
    ConflictResolution,
    FederatedSearchRequest,
    SourceConfig,
    SourceStatus,
)
from .routing import SourceRegistry, parse_source_hint


@attr.s
class FederatedClient(AsyncBaseCoreClient):
    """STAC client that federates a set of named upstream sources.

    Args:
        sources: Configured backends, in registration order.
        conflict_resolution: Strategy for same-named collections.
        discover: Allow dynamic collection discovery for sources without a
            static collection allow-list.
        fail_fast: When ``True`` (default ``False``) any single source error
            aborts the whole request instead of being reported as partial
            coverage. Even with ``False``, requests with *no* successful
            coverage still raise :class:`FederationCoverageError`.
    """

    sources: list[SourceConfig] = attr.ib(kw_only=True)
    conflict_resolution: ConflictResolution = attr.ib(
        default=ConflictResolution.PRIORITY, kw_only=True
    )
    discover: bool = attr.ib(default=True, kw_only=True)
    fail_fast: bool = attr.ib(default=False, kw_only=True)
    registry: SourceRegistry = attr.ib(init=False)

    @sources.validator
    def _validate_sources(self, attribute, value):
        if not value:
            raise ValueError("FederatedClient requires at least one source")

    def __attrs_post_init__(self) -> None:
        self.registry = SourceRegistry(
            self.sources,
            resolution=self.conflict_resolution,
            discover=self.discover,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _check_fail_fast(self, results: list[SourceCallResult]) -> None:
        if not self.fail_fast:
            return
        for result in results:
            if result.status["status"] == "error":
                raise FederationCoverageError(
                    "fail_fast enabled and source "
                    f"{result.source.source_id!r} returned an error: "
                    f"{result.status.get('detail', '')}",
                    source_statuses=[r.status for r in results],
                )

    def _require_coverage(
        self,
        results: list[SourceCallResult],
        message: str,
    ) -> list[SourceCallResult]:
        """Raise when every queried source failed or was unhosted."""
        ok = [r for r in results if r.status["status"] == "ok"]
        if ok:
            return results
        raise FederationCoverageError(
            message,
            source_statuses=[r.status for r in results],
        )

    async def _ensure_hosting(self) -> None:
        if self.discover:
            await self.registry.refresh_hosting()

    # ------------------------------------------------------------------
    # Collections
    # ------------------------------------------------------------------
    async def all_collections(self, **kwargs) -> Collections:
        """Merge every source's catalog under the configured conflict rule."""
        await self._ensure_hosting()
        results = list(
            await asyncio.gather(
                *[call_collections(s) for s in self.registry.sources]
            )
        )

        self._check_fail_fast(results)
        self._require_coverage(
            results, "no federated source returned any collections"
        )

        per_source = [
            (
                result.source,
                list((result.payload or {}).get("collections", [])),
            )
            for result in results
            if result.status["status"] == "ok"
        ]
        base_url = _base_url(kwargs.get("request"))
        merged = merge_collections(per_source, self.registry, base_url=base_url)
        payload: Collections = {
            "collections": merged,
            "links": [
                {"rel": "self", "type": "application/json",
                 "href": f"{base_url or ''}/collections"},
                {"rel": "root", "type": "application/json",
                 "href": f"{base_url or ''}/"},
            ],
            "numberReturned": len(merged),
            "numberMatched": len(merged),
        }
        coverage = build_coverage(
            [r.status for r in results],
            self.conflict_resolution,
            [s.source_id for s in self.registry.sources],
        )
        attach_coverage(payload, coverage)
        return payload

    async def get_collection(self, collection_id: str, **kwargs) -> Collection:
        """Return the federation view of one collection.

        PRIORITY returns the winning variant and lists the others in
        ``federation:variants``; SPLIT requires a ``source:collection`` id and
        returns that exact source's document.
        """
        await self._ensure_hosting()
        candidates = self.registry.resolve_collection(collection_id)
        if not candidates:
            raise FederationCoverageError(
                f"no federated source hosts collection {collection_id!r}"
            )

        results = list(
            await asyncio.gather(
                *[
                    call_single(source, "get_collection", original_id)
                    for source, original_id in candidates
                ]
            )
        )
        self._check_fail_fast(results)
        ok_results = [r for r in results if r.status["status"] == "ok"]
        if not ok_results:
            raise FederationCoverageError(
                f"collection {collection_id!r} has no available source coverage",
                source_statuses=[r.status for r in results],
            )

        hint = parse_source_hint(collection_id)
        base_url = _base_url(kwargs.get("request"))
        if hint is not None:
            source_id, original_id = hint
            result = ok_results[0]
            document = dict(result.payload)
            document["id"] = collection_id
            return stamp_collection(
                document,
                result.source,
                self.registry,
                original_collection_id=original_id,
                base_url=base_url,
            )

        # Bare id: PRIORITY winner (results preserve registry order). The
        # losing variants remain visible in the provenance block.
        winner = ok_results[0]
        document = dict(winner.payload)
        return stamp_collection(
            document,
            winner.source,
            self.registry,
            variants=build_variants(
                [(r.source, r.payload["id"]) for r in ok_results]
            ),
            original_collection_id=document["id"],
            base_url=base_url,
        )

    # ------------------------------------------------------------------
    # Items
    # ------------------------------------------------------------------
    async def get_item(
        self, item_id: str, collection_id: str, **kwargs
    ) -> Item:
        """Fetch one item from the exact source its collection belongs to."""
        await self._ensure_hosting()
        request = kwargs.get("request")
        source_hint = None
        if request is not None:
            source_hint = request.headers.get(SOURCE_HINT_HEADER)
        # Link-level query hint (?x-stac-source=...) takes over when no header.
        if source_hint is None and request is not None:
            source_hint = request.query_params.get(SOURCE_HINT_HEADER)

        source, original_collection_id = self.registry.resolve_item_route(
            collection_id, source_hint=source_hint
        )
        result = await call_single(
            source, "get_item", item_id, original_collection_id
        )
        if result.status["status"] != "ok":
            raise FederationCoverageError(
                f"item {item_id!r} in collection {collection_id!r} is "
                f"unavailable from source {source.source_id!r}: "
                f"{result.status.get('detail', '')}",
                source_statuses=[result.status],
            )
        return stamp_item(
            result.payload,
            source,
            original_collection_id,
            self.registry,
            base_url=_base_url(request),
        )

    async def item_collection(
        self,
        collection_id: str,
        bbox: BBox | None = None,
        datetime: str | None = None,
        limit: int = 10,
        token: str | None = None,
        **kwargs,
    ) -> ItemCollection:
        """List items of a collection from its resolved real source(s)."""
        await self._ensure_hosting()
        request = kwargs.get("request")
        source_hint = None
        if request is not None:
            source_hint = request.headers.get(
                SOURCE_HINT_HEADER
            ) or request.query_params.get(SOURCE_HINT_HEADER)

        if source_hint is not None or parse_source_hint(collection_id) is not None:
            source, original_id = self.registry.resolve_item_route(
                collection_id, source_hint=source_hint
            )
            targets = [(source, original_id)]
        else:
            candidates = self.registry.resolve_collection(collection_id)
            if not candidates:
                raise FederationCoverageError(
                    f"no federated source hosts collection {collection_id!r}"
                )
            if self.conflict_resolution == ConflictResolution.PRIORITY:
                targets = [candidates[0]]
            else:
                raise FederationCoverageError(
                    f"collection {collection_id!r} is split across sources; "
                    "qualify it with a source:collection id",
                    source_statuses=[
                        SourceStatus(source=s.source_id, status="skipped")
                        for s, _ in candidates
                    ],
                )

        logical_kwargs: dict[str, Any] = {
            "bbox": bbox,
            "datetime": datetime,
            "limit": limit,
            "token": token,
        }
        # One call per resolved (source, original id); unsupported conditions
        # are stripped per source and applied locally after the response.
        planned = []
        for source, original_id in targets:
            forwarded, local = split_request_by_capabilities(
                logical_kwargs, source.capabilities
            )
            planned.append((source, original_id, forwarded, local))

        results = list(
            await asyncio.gather(
                *[
                    call_search_source(
                        source,
                        method_name="item_collection",
                        request_kwargs={
                            "collection_id": original_id,
                            **forwarded,
                        },
                    )
                    for source, original_id, forwarded, _ in planned
                ]
            )
        )
        self._check_fail_fast(results)
        self._require_coverage(
            results,
            f"no source could list items for collection {collection_id!r}",
        )

        per_source = []
        statuses: list[SourceStatus] = []
        for result, (source, original_id, _forwarded, local) in zip(
            results, planned
        ):
            statuses.append(result.status)
            if result.status["status"] != "ok":
                continue
            payload = dict(result.payload or {})
            features = list(payload.get("features", []))
            if local:
                features = apply_local_filters(
                    features,
                    bbox=local.get("bbox"),
                    intersects=local.get("intersects"),
                    datetime_query=local.get("datetime"),
                )
            per_source.append((source, features, original_id))

        merged = merge_item_collections(
            per_source, self.registry, base_url=_base_url(request)
        )
        coverage = build_coverage(
            statuses,
            self.conflict_resolution,
            [s.source_id for s, _ in targets],
        )
        attach_coverage(merged, coverage)
        return merged

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------
    async def get_search(
        self,
        collections: list[str] | None = None,
        ids: list[str] | None = None,
        bbox: BBox | None = None,
        intersects: Geometry | str | None = None,
        datetime: str | None = None,
        limit: int | None = 10,
        **kwargs,
    ) -> ItemCollection:
        """Federate a GET item search across every relevant source."""
        normalized = FederatedSearchRequest(
            collections=collections,
            ids=ids,
            bbox=tuple(bbox) if bbox else None,
            intersects=_coerce_geometry(intersects),
            datetime=datetime,
            limit=limit or 10,
            extra={
                k: v
                for k, v in kwargs.items()
                if k not in ("request",)
            },
        )
        return await self._search(normalized, kwargs.get("request"))

    async def post_search(
        self, search_request: BaseSearchPostRequest, **kwargs
    ) -> ItemCollection:
        """Federate a POST item search across every relevant source."""
        data = search_request.model_dump(exclude_none=True, by_alias=True)
        normalized = FederatedSearchRequest(
            collections=data.get("collections"),
            ids=data.get("ids"),
            bbox=tuple(data["bbox"]) if data.get("bbox") else None,
            intersects=_coerce_geometry(data.get("intersects")),
            datetime=data.get("datetime"),
            limit=data.get("limit", 10),
            extra={
                k: v
                for k, v in data.items()
                if k
                not in {
                    "collections",
                    "ids",
                    "bbox",
                    "intersects",
                    "datetime",
                    "limit",
                }
            },
        )
        return await self._search(normalized, kwargs.get("request"))

    async def _search(
        self,
        normalized: FederatedSearchRequest,
        request: Any,
    ) -> ItemCollection:
        await self._ensure_hosting()
        plan = self.registry.plan_collection_routes(normalized.get("collections"))

        # Build one call per (source, original collection id) pair; a source
        # without a collection filter is queried once with its own kwargs.
        # Each tuple also remembers the original collection id for provenance.
        planned: list[tuple[SourceConfig, str, dict[str, Any]]] = []
        if "*" in plan:
            for source in plan["*"]:
                planned.append(
                    (source, "*", self._search_kwargs(normalized))
                )
        else:
            for original_id, sources in plan.items():
                for source in sources:
                    planned.append(
                        (
                            source,
                            original_id,
                            self._search_kwargs(
                                normalized, collection_override=[original_id]
                            ),
                        )
                    )

        if not planned:
            raise FederationCoverageError(
                "no federated source hosts the requested collections: "
                f"{normalized.get('collections')}",
            )

        results = list(
            await asyncio.gather(
                *[
                    call_search_source(
                        source,
                        method_name="get_search",
                        request_kwargs=search_kwargs,
                    )
                    for source, _, search_kwargs in planned
                ]
            )
        )
        self._check_fail_fast(results)
        self._require_coverage(results, "all federated sources failed the search")

        per_source = []
        for (source, original_id, _search_kwargs), result in zip(planned, results):
            if result.status["status"] != "ok":
                continue
            payload = result.payload or {}
            features = list(payload.get("features", []))
            # Unfiltered searches may span several collections; tag each item
            # with its own real collection id when the call wasn't pinned.
            effective_id = original_id
            for feature in features:
                if original_id == "*":
                    effective_id = feature.get("collection", "*")
                per_source.append((source, [feature], effective_id))

        merged = merge_item_collections(
            per_source, self.registry, base_url=_base_url(request)
        )
        coverage = build_coverage(
            [r.status for r in results],
            self.conflict_resolution,
            [source.source_id for source, _, _ in planned],
        )
        attach_coverage(merged, coverage)

        limit = normalized.get("limit") or 10
        if len(merged["features"]) > limit:
            merged["features"] = merged["features"][:limit]
            merged["numberReturned"] = len(merged["features"])
        return merged

    def _search_kwargs(
        self,
        normalized: FederatedSearchRequest,
        collection_override: list[str] | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "collections": collection_override or normalized.get("collections"),
            "ids": normalized.get("ids"),
            "bbox": normalized.get("bbox"),
            "intersects": normalized.get("intersects"),
            "datetime": normalized.get("datetime"),
            "limit": normalized.get("limit", 10),
        }
        kwargs.update(normalized.get("extra") or {})
        return {k: v for k, v in kwargs.items() if v is not None}


def _coerce_geometry(intersects: Geometry | str | dict | None) -> dict | None:
    """Normalize an intersects value to a GeoJSON geometry mapping.

    GET search delivers ``intersects`` as a URL-encoded GeoJSON *string*,
    while POST delivers a parsed object; sources and the local filters both
    expect a plain mapping.
    """
    if intersects is None:
        return None
    if isinstance(intersects, str):
        import json

        return json.loads(intersects)
    if hasattr(intersects, "model_dump"):
        return intersects.model_dump()
    return dict(intersects)


def _base_url(request: Any) -> str | None:
    if request is None:
        return None
    try:
        return get_base_url(request)
    except Exception:  # pragma: no cover - defensive
        return str(request.base_url).rstrip("/")


__all__ = ["FederatedClient"]
