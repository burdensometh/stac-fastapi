"""Record Item changes from the transaction endpoints into the revision stream.

The helpers in this module are used by the transaction and bulk-transaction
extensions when they are given a `revision_store`. Events are built from the
result of a successful client call and appended to the store as a single
batch; a client call that raises records nothing, so failed commits produce
no events.
"""

import functools
import inspect
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response

from stac_fastapi.api.routes import sync_to_async
from stac_fastapi.types.revisions import RevisionEvent, delete_event, upsert_event

from .store import AsyncRevisionStore, RevisionStore


def record_on_success(
    store: RevisionStore | AsyncRevisionStore,
    func: Callable,
    events_builder: Callable[..., list[RevisionEvent]],
) -> Callable:
    """Wrap a transactions client method to append revision events on success.

    The wrapped callable is always a coroutine function; synchronous client
    methods are run in a threadpool, mirroring `create_async_endpoint`. Events
    are appended only when the client call returns without raising, so failed
    commits record no events.

    Args:
        store: the revision store to append events to.
        func: the transactions client method to wrap.
        events_builder: builds the batch of events from the client call's
            return value and arguments.

    Returns:
        The wrapped client method.
    """
    async_func = func if inspect.iscoroutinefunction(func) else sync_to_async(func)

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        result = await async_func(*args, **kwargs)
        await append_events(store, events_builder(result, *args, **kwargs))
        return result

    return wrapper


async def append_events(
    store: RevisionStore | AsyncRevisionStore, events: list[RevisionEvent]
) -> None:
    """Append a batch of events to the store.

    Synchronous stores are run in a threadpool. An empty batch is a no-op, so
    sequence numbers are only consumed by actual Item changes.
    """
    if not events:
        return
    append = store.append_batch
    if inspect.iscoroutinefunction(append):
        await append(events)
    else:
        await run_in_threadpool(append, events)


def _failed(result: Any) -> bool:
    """Check whether a client return value is an error response."""
    return isinstance(result, Response) and result.status_code >= 400


def _dump(item: Any) -> dict:
    """Convert a pydantic model or mapping to a plain JSON-compatible dict."""
    if isinstance(item, BaseModel):
        return item.model_dump(mode="json", exclude_none=True)
    return dict(item)


def _result_items(result: Any) -> list[dict] | None:
    """Extract Item payloads from an Item or ItemCollection shaped value."""
    if isinstance(result, (BaseModel, Mapping)):
        payload = _dump(result)
        if payload.get("type") == "Feature":
            return [payload]
        if payload.get("type") == "FeatureCollection":
            return [dict(feature) for feature in payload.get("features", [])]
    return None


def create_item_events(result: Any, collection_id=None, item=None, **kwargs):
    """Build upsert events for `create_item` (single Item or ItemCollection)."""
    if _failed(result):
        return []
    payloads = _result_items(result)
    if payloads is None and item is not None:
        payloads = _result_items(item)
    return [
        upsert_event(
            collection_id=payload.get("collection") or collection_id,
            item_id=str(payload["id"]),
            item=payload,
        )
        for payload in payloads or []
        if payload.get("id") is not None
    ]


def update_item_events(
    result: Any, collection_id=None, item_id=None, item=None, **kwargs
):
    """Build an upsert event for `update_item`."""
    if _failed(result):
        return []
    payloads = _result_items(result)
    if payloads is None and item is not None:
        payloads = _result_items(item)
    payload = payloads[0] if payloads else None
    return [
        upsert_event(
            collection_id=collection_id,
            item_id=str(item_id),
            item=payload,
        )
    ]


def patch_item_events(result: Any, collection_id=None, item_id=None, **kwargs):
    """Build an upsert event for `patch_item`.

    The event carries the patched Item when the client returns it; otherwise
    only the identity is recorded and readers fetch the current state by
    `collection_id`/`item_id`.
    """
    if _failed(result):
        return []
    payloads = _result_items(result)
    payload = payloads[0] if payloads else None
    return [
        upsert_event(
            collection_id=collection_id,
            item_id=str(item_id),
            item=payload,
        )
    ]


def delete_item_events(result: Any, collection_id=None, item_id=None, **kwargs):
    """Build a delete event for `delete_item`, carrying the Item's identity."""
    if _failed(result):
        return []
    return [delete_event(collection_id=collection_id, item_id=str(item_id))]


def bulk_item_events(result: Any, items=None, request: Request | None = None, **kwargs):
    """Build upsert events for the successful items of a `bulk_item_insert`.

    Items reported in the `errors` list of the bulk transaction result are
    skipped, so partially failed bulk writes only record committed Items. A
    `Response` return value cannot be inspected and records nothing.
    """
    if not isinstance(result, Mapping) or items is None:
        return []
    failed = {
        error.get("id")
        for error in result.get("errors", [])
        if isinstance(error, Mapping)
    }
    collection_id = (
        request.path_params.get("collection_id") if request is not None else None
    )
    events = []
    for key, item in items.items.items():
        if not isinstance(item, Mapping):
            continue
        item_id = item.get("id", key)
        if item_id in failed or key in failed:
            continue
        events.append(
            upsert_event(
                collection_id=item.get("collection") or collection_id,
                item_id=str(item_id),
                item=dict(item),
            )
        )
    return events
