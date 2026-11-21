"""Bounding box validation and antimeridian-aware query planning.

A bbox whose west longitude is greater than its east longitude does not
describe an empty extent: per the STAC API / OGC API Features convention it
describes an extent that crosses the antimeridian (the 180th meridian). Such
an extent cannot be expressed as a single ordinary rectangular query window,
so it is split into two windows, one on each side of the antimeridian. The
two queries are then merged back together and their overlapping matches are
deduplicated before the limit and aggregations are applied.
"""

import copy
import math
from collections.abc import Awaitable, Callable
from typing import Any

from stac_pydantic.shared import BBox

__all__ = [
    "validate_bbox",
    "bbox_crosses_antimeridian",
    "split_antimeridian_bbox",
    "bbox_query_windows",
    "execute_bbox_query",
    "execute_bbox_query_async",
    "merge_search_results",
    "merge_aggregation_results",
    "execute_aggregation_query",
    "execute_aggregation_query_async",
    "ITEM_IDENTITY_KEYS",
    "COLLECTION_IDENTITY_KEYS",
]


# Feature (Item) identity is the (collection, id) pair; Collection identity
# is the collection id alone.
ITEM_IDENTITY_KEYS = ("collection", "id")
COLLECTION_IDENTITY_KEYS = ("id",)


def validate_bbox(bbox: BBox | None) -> BBox | None:
    """Validate a WGS84 bounding box.

    A reversed longitude order (``xmin > xmax``) is legal and denotes a bbox
    that crosses the antimeridian. Latitude bounds must always be ordered and
    within [-90, 90], longitudes within [-180, 180], and the upper elevation
    must not be below the lower elevation.
    """
    if bbox is None:
        return None

    if len(bbox) not in (4, 6):
        raise ValueError("Bounding box must have 4 or 6 coordinates")

    if len(bbox) == 4:
        xmin, ymin, xmax, ymax = bbox
        min_elev = max_elev = None
    else:
        xmin, ymin, min_elev, xmax, ymax, max_elev = bbox
        if max_elev < min_elev:
            raise ValueError(
                "Maximum elevation must be greater than minimum elevation"
            )

    if any(math.isnan(v) for v in bbox):
        raise ValueError("Bounding box coordinates must not be NaN")

    if xmin < -180 or xmax > 180:
        raise ValueError("Bounding box longitudes must be within [-180, 180]")

    if ymin < -90 or ymax > 90:
        raise ValueError("Bounding box latitudes must be within [-90, 90]")

    if ymax < ymin:
        raise ValueError(
            f"Maximum latitude ({ymax}) must be greater than minimum latitude ({ymin})"
        )

    return bbox


def bbox_crosses_antimeridian(bbox: BBox) -> bool:
    """Return True when the bbox crosses the 180th meridian.

    A bbox crosses the antimeridian when its west longitude is greater than
    its east longitude. Equal longitudes describe a zero-width ordinary
    window and are not treated as crossing.
    """
    xmin = bbox[0]
    xmax = bbox[2] if len(bbox) == 4 else bbox[3]
    return xmin > xmax


def split_antimeridian_bbox(bbox: BBox) -> tuple[BBox, BBox]:
    """Split an antimeridian-crossing bbox into two ordinary bboxes.

    The first window covers ``[xmin, 180]`` and the second covers
    ``[-180, xmax]``; both keep the latitude (and elevation) bounds of the
    original extent.
    """
    if len(bbox) == 4:
        xmin, ymin, xmax, ymax = bbox
        west: BBox = (xmin, ymin, 180.0, ymax)
        east: BBox = (-180.0, ymin, xmax, ymax)
    else:
        xmin, ymin, min_elev, xmax, ymax, max_elev = bbox
        west = (xmin, ymin, min_elev, 180.0, ymax, max_elev)
        east = (-180.0, ymin, min_elev, xmax, ymax, max_elev)
    return west, east


def bbox_query_windows(bbox: BBox | None) -> tuple[BBox, ...]:
    """Return the executable rectangular query windows for a bbox.

    Returns a single window for an ordinary bbox (or an empty tuple when no
    bbox is given) and two windows for an antimeridian-crossing bbox.
    """
    if bbox is None:
        return ()
    if bbox_crosses_antimeridian(bbox):
        return split_antimeridian_bbox(bbox)
    return (bbox,)


def _identity(record: dict[str, Any], keys: tuple[str, ...]) -> tuple[Any, ...]:
    return tuple(record.get(key) for key in keys)


def merge_search_results(
    responses: list[dict[str, Any]],
    *,
    result_key: str,
    identity_keys: tuple[str, ...],
    limit: int | None = None,
) -> dict[str, Any]:
    """Merge per-window search responses into one response.

    Results are deduplicated by their resource identity (Item identity is
    the ``(collection, id)`` pair; Collection identity is ``id``), keeping
    first-seen order. The limit is applied *after* deduplication so each
    resource is counted once against the real geographic extent.

    ``numberMatched`` reflects the union of the windows: per-window totals
    are summed and the duplicates observed in the returned pages are
    subtracted. ``numberReturned`` is set from the deduplicated result count
    whenever the responses carry the field. Pagination links are left to the
    caller, since tokens cannot be meaningfully merged across windows.
    """
    if not responses:
        raise ValueError("At least one search response is required to merge")

    merged = dict(responses[0])

    # First pass: deduplicate in first-seen order and remember, for every
    # duplicate copy, whether the window that produced it reported a total.
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    duplicates_in_matched_windows = 0
    any_returned = any("numberReturned" in response for response in responses)

    for response in responses:
        window_counts_matched = "numberMatched" in response
        for record in response.get(result_key, []):
            identity = _identity(record, identity_keys)
            if identity in seen:
                if window_counts_matched:
                    duplicates_in_matched_windows += 1
                continue
            seen.add(identity)
            deduped.append(record)

    if limit is not None:
        deduped = deduped[:limit]

    merged[result_key] = deduped

    # numberMatched is the union of the per-window totals. Only windows that
    # report a total participate, and only duplicates from those windows are
    # subtracted.
    matched_windows = [r for r in responses if "numberMatched" in r]
    if matched_windows:
        merged["numberMatched"] = (
            sum(r["numberMatched"] for r in matched_windows)
            - duplicates_in_matched_windows
        )
    if any_returned:
        merged["numberReturned"] = len(deduped)

    # Pagination tokens belong to a single backend query and cannot be
    # combined; drop next/prev links produced for individual windows.
    links = [
        link
        for link in merged.get("links", [])
        if link.get("rel") not in ("next", "prev", "previous")
    ]
    if links:
        merged["links"] = links
    else:
        merged.pop("links", None)

    return merged


def _single_window_response(
    response: dict[str, Any], limit: int | None
) -> dict[str, Any]:
    """Apply the limit to a single-window response without restructuring it."""
    if limit is None:
        return response
    result = copy.deepcopy(response)
    for key in ("features", "collections"):
        if key in result and isinstance(result[key], list):
            result[key] = result[key][:limit]
            if "numberReturned" in result:
                result["numberReturned"] = len(result[key])
            break
    return result


def execute_bbox_query(
    bbox: BBox | None,
    execute: Callable[[BBox], dict[str, Any]],
    *,
    result_key: str,
    identity_keys: tuple[str, ...],
    limit: int | None = None,
) -> dict[str, Any]:
    """Run a synchronous search over an antimeridian-aware query plan.

    ``execute`` is called once per rectangular query window with a bbox that
    never crosses the antimeridian. For ordinary bboxes the backend response
    is returned unchanged (apart from enforcing the limit), preserving
    existing single-window behavior.
    """
    windows = bbox_query_windows(bbox)
    if len(windows) <= 1:
        window = windows[0] if windows else bbox
        return _single_window_response(execute(window), limit)

    responses = [execute(window) for window in windows]
    return merge_search_results(
        responses,
        result_key=result_key,
        identity_keys=identity_keys,
        limit=limit,
    )


async def execute_bbox_query_async(
    bbox: BBox | None,
    execute: Callable[[BBox], Awaitable[dict[str, Any]]],
    *,
    result_key: str,
    identity_keys: tuple[str, ...],
    limit: int | None = None,
) -> dict[str, Any]:
    """Async counterpart of :func:`execute_bbox_query`."""
    windows = bbox_query_windows(bbox)
    if len(windows) <= 1:
        window = windows[0] if windows else bbox
        return _single_window_response(await execute(window), limit)

    responses = [await execute(window) for window in windows]
    return merge_search_results(
        responses,
        result_key=result_key,
        identity_keys=identity_keys,
        limit=limit,
    )


def _bucket_key(bucket: dict[str, Any]) -> Any:
    """Identity of an aggregation bucket: its key or numeric interval."""
    if "key" in bucket:
        return bucket["key"]
    return (bucket.get("from"), bucket.get("to"))


def _merge_buckets(
    left: list[dict[str, Any]], right: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Merge two bucket lists, adding frequencies of matching buckets."""
    merged: dict[Any, dict[str, Any]] = {}
    order: list[Any] = []
    for bucket in [*left, *right]:
        key = _bucket_key(bucket)
        if key not in merged:
            merged[key] = copy.deepcopy(bucket)
            order.append(key)
            continue
        target = merged[key]
        if "frequency" in bucket or "frequency" in target:
            target_freq = target.setdefault("frequency", {})
            for metric, value in bucket.get("frequency", {}).items():
                target_freq[metric] = target_freq.get(metric, 0) + value
        for scalar in ("overflow", "value"):
            if scalar in bucket:
                target[scalar] = target.get(scalar, 0) + bucket[scalar]
    return [merged[key] for key in order]


def merge_aggregation_results(
    responses: list[dict[str, Any]],
) -> dict[str, Any]:
    """Merge per-window aggregation responses.

    Aggregations are matched by name; their buckets are merged by bucket key
    (or numeric interval), summing frequencies so each geographic region is
    counted once toward the totals of the combined antimeridian extent.
    """
    if not responses:
        raise ValueError("At least one aggregation response is required to merge")

    merged = dict(responses[0])
    by_name: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for response in responses:
        for aggregation in response.get("aggregations", []):
            name = aggregation.get("name")
            if name not in by_name:
                by_name[name] = copy.deepcopy(aggregation)
                order.append(name)
                continue
            target = by_name[name]
            if "buckets" in aggregation or "buckets" in target:
                target["buckets"] = _merge_buckets(
                    target.get("buckets", []), aggregation.get("buckets", [])
                )
            for scalar in ("overflow", "value"):
                if scalar in aggregation:
                    target[scalar] = target.get(scalar, 0) + aggregation[scalar]

    merged["aggregations"] = [by_name[name] for name in order]
    links = [
        link
        for link in merged.get("links", [])
        if link.get("rel") not in ("next", "prev", "previous")
    ]
    if links:
        merged["links"] = links
    else:
        merged.pop("links", None)
    return merged


def execute_aggregation_query(
    bbox: BBox | None,
    execute: Callable[[BBox], dict[str, Any]],
) -> dict[str, Any]:
    """Run a synchronous aggregation over an antimeridian-aware query plan."""
    windows = bbox_query_windows(bbox)
    if len(windows) <= 1:
        window = windows[0] if windows else bbox
        return execute(window)
    return merge_aggregation_results([execute(window) for window in windows])


async def execute_aggregation_query_async(
    bbox: BBox | None,
    execute: Callable[[BBox], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """Async counterpart of :func:`execute_aggregation_query`."""
    windows = bbox_query_windows(bbox)
    if len(windows) <= 1:
        window = windows[0] if windows else bbox
        return await execute(window)
    responses = [await execute(window) for window in windows]
    return merge_aggregation_results(responses)
