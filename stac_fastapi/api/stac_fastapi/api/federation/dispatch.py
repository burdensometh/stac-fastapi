"""Concurrent, capability-aware fan-out to federation sources.

Each source is invoked with a request that contains only the conditions it
declared support for; unsupported conditions are removed from the forwarded
request and applied locally afterward. Sources are queried concurrently with
:mod:`asyncio`, but results are always reassembled in the registry's
deterministic order — wall-clock timing never influences which record wins a
conflict.
"""

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any

from .filters import apply_local_filters
from .models import SourceCapabilities, SourceConfig, SourceStatus
from .routing import maybe_await


@dataclass
class SourceCallResult:
    """Outcome of calling one source for one logical operation."""

    source: SourceConfig
    status: SourceStatus
    payload: Any = None
    # Conditions that this source could not execute and were enforced locally.
    locally_filtered: list[str] = field(default_factory=list)


def _adapt_call(client, method_name: str):
    """Return an async callable bound to ``client.<method_name>``.

    Synchronous ``BaseCoreClient`` implementations are executed in a worker
    thread so a federation can mix sync and async backends without blocking the
    event loop.
    """
    method = getattr(client, method_name)

    if inspect.iscoroutinefunction(method):

        async def caller(*args, **kwargs):
            return await method(*args, **kwargs)

        return caller

    async def caller(*args, **kwargs):
        return await asyncio.to_thread(method, *args, **kwargs)

    return caller


def split_request_by_capabilities(
    request_kwargs: dict[str, Any], capabilities: SourceCapabilities
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split a search request into (forwarded, local) condition sets.

    Returns the kwargs safe to forward to this source and the conditions the
    federation must enforce locally on its response.
    """
    forwarded = dict(request_kwargs)
    local: dict[str, Any] = {}
    for condition in ("bbox", "intersects", "datetime", "ids"):
        value = forwarded.get(condition)
        if value is None:
            continue
        if not capabilities.supports(condition):
            local[condition] = forwarded.pop(condition)
    return forwarded, local


async def call_search_source(
    source: SourceConfig,
    *,
    method_name: str,
    request_kwargs: dict[str, Any],
    not_hosted: bool = False,
) -> SourceCallResult:
    """Execute one search-style call against one source.

    Args:
        method_name: ``get_search`` or ``item_collection``.
        request_kwargs: Full logical request; unsupported conditions are
            stripped before forwarding.
        not_hosted: When the registry already knows the source does not host
            the target collection, skip the network call and record
            ``not_hosted``.
    """
    if not_hosted:
        return SourceCallResult(
            source=source,
            status=SourceStatus(
                source=source.source_id, status="not_hosted"
            ),
            payload={"type": "FeatureCollection", "features": [], "links": []},
        )

    forwarded, local = split_request_by_capabilities(
        request_kwargs, source.capabilities
    )
    caller = _adapt_call(source.client, method_name)
    try:
        payload = await caller(**forwarded)
    except Exception as exc:  # noqa: BLE001 - recorded, never silently swallowed
        # A 404 means the source does not host this collection: record it as a
        # distinct state so coverage reports can say "missing" rather than
        # lumping it together with genuine server errors.
        if is_not_found(exc):
            return SourceCallResult(
                source=source,
                status=SourceStatus(
                    source=source.source_id,
                    status="not_hosted",
                    detail=f"{type(exc).__name__}: {exc}",
                ),
                payload={"type": "FeatureCollection", "features": [], "links": []},
            )
        return SourceCallResult(
            source=source,
            status=SourceStatus(
                source=source.source_id,
                status="error",
                detail=f"{type(exc).__name__}: {exc}",
            ),
        )

    if local and payload is not None:
        payload = dict(payload)
        payload["features"] = apply_local_filters(
            list(payload.get("features", [])),
            bbox=local.get("bbox"),
            intersects=local.get("intersects"),
            datetime_query=local.get("datetime"),
            ids=local.get("ids"),
        )

    status = SourceStatus(source=source.source_id, status="ok")
    if local:
        status["detail"] = (
            "locally applied unsupported conditions: "
            + ", ".join(sorted(local.keys()))
        )
    return SourceCallResult(
        source=source,
        status=status,
        payload=payload,
        locally_filtered=list(local.keys()),
    )


async def fan_out(
    calls: list[tuple[SourceConfig, dict[str, Any], str]],
) -> list[SourceCallResult]:
    """Run ``(source, kwargs, method_name)`` calls concurrently, ordered back.

    Results are returned in the same order as ``calls`` (which is registry
    priority order), regardless of completion order.
    """
    coroutines = [
        call_search_source(source, method_name=method_name, request_kwargs=kwargs)
        for source, kwargs, method_name in calls
    ]
    results = await asyncio.gather(*coroutines)
    return list(results)


async def call_single(
    source: SourceConfig, method_name: str, *args, **kwargs
) -> SourceCallResult:
    """Call one source for a single-record route (collection/item details)."""
    caller = _adapt_call(source.client, method_name)
    try:
        payload = await caller(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - recorded in coverage
        status = "not_hosted" if is_not_found(exc) else "error"
        return SourceCallResult(
            source=source,
            status=SourceStatus(
                source=source.source_id,
                status=status,
                detail=f"{type(exc).__name__}: {exc}",
            ),
        )
    return SourceCallResult(
        source=source,
        status=SourceStatus(source=source.source_id, status="ok"),
        payload=payload,
    )


async def call_collections(source: SourceConfig) -> SourceCallResult:
    """Fetch all collections from one source."""
    caller = _adapt_call(source.client, "all_collections")
    try:
        payload = await caller()
    except Exception as exc:  # noqa: BLE001 - recorded in coverage
        return SourceCallResult(
            source=source,
            status=SourceStatus(
                source=source.source_id,
                status="error",
                detail=f"{type(exc).__name__}: {exc}",
            ),
        )
    return SourceCallResult(
        source=source,
        status=SourceStatus(source=source.source_id, status="ok"),
        payload=payload,
    )


def is_not_found(exc: Exception) -> bool:
    """Best-effort recognition of upstream 404s without HTTP coupling."""
    name = type(exc).__name__.lower()
    return "notfound" in name or "not_found" in name or "404" in str(exc)


__all__ = [
    "SourceCallResult",
    "call_collections",
    "call_search_source",
    "call_single",
    "fan_out",
    "is_not_found",
    "maybe_await",
    "split_request_by_capabilities",
]
