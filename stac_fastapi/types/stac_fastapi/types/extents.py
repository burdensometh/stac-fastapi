"""Derive and maintain a Collection's extent from its Items.

A Collection's ``extent`` describes the space and time covered by the Items it
contains.  Backends that only compute it when the Collection is created keep a
stale value once Items are written: an Item can be fetched directly while a
search restricted to the Collection's extent misses it.

The helpers in this module recompute the extent from the Items currently stored
in a collection.  They have no backend or HTTP dependency, so every backend can
reuse them.

The extent is *recomputed*, not widened: when the Item that defined an extreme is
deleted, the extent shrinks again.  A recomputation therefore looks at every Item
of the collection.

Items without a temporal annotation do not contribute to the temporal extent and
are never assumed to have been observed "now".  A collection without any usable
Item has an explicit, deterministic extent:

* spatial: the whole world, ``[[-180.0, -90.0, 180.0, 90.0]]``
* temporal: open on both ends, ``[[None, None]]``

References:
* https://github.com/radiantearth/stac-spec/blob/v1.0.0/collection-spec/collection-spec.md#extent-object
"""  # noqa: E501

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

from stac_fastapi.types.rfc3339 import datetime_to_str, rfc3339_str_to_datetime

__all__ = [
    "DEFAULT_MAX_PAGES",
    "DEFAULT_PAGE_LIMIT",
    "DEFAULT_SPATIAL_BBOX",
    "DEFAULT_TEMPORAL_INTERVAL",
    "EMPTY_SPATIAL_BBOX",
    "EMPTY_TEMPORAL_INTERVAL",
    "ExtentReadError",
    "aget_collection_items",
    "apply_extent",
    "as_dict",
    "compute_collection_extent",
    "compute_extent",
    "compute_spatial_extent",
    "compute_temporal_extent",
    "extent_from_items",
    "extent_maintenance_scope",
    "get_collection_items",
    "in_extent_maintenance",
    "item_bbox",
    "item_temporal_interval",
    "maintain_collection_extent",
    "maintenance_enabled",
    "recompute_collection_extent",
    "refresh_collection_extent",
    "resolve_core_client",
    "run_sync",
    "spatial_extent_from_items",
    "temporal_extent_from_items",
    "update_collection_extent",
]

#: Spatial extent used when a collection has no Item with a usable geometry.
DEFAULT_SPATIAL_BBOX: list[list[float]] = [[-180.0, -90.0, 180.0, 90.0]]
EMPTY_SPATIAL_BBOX = DEFAULT_SPATIAL_BBOX

#: Temporal extent used when a collection has no Item with a temporal annotation.
DEFAULT_TEMPORAL_INTERVAL: list[list[None]] = [[None, None]]
EMPTY_TEMPORAL_INTERVAL = DEFAULT_TEMPORAL_INTERVAL

#: Maximum number of Items requested per page when reading a collection.
DEFAULT_PAGE_LIMIT = 10_000

#: Safety bound on the number of pages read for a single collection.
DEFAULT_MAX_PAGES = 1_000

#: Setting names which enable the maintenance, in addition to the canonical
#: ``maintain_collection_extents``.
MAINTENANCE_SETTING_NAMES = (
    "maintain_collection_extents",
    "update_collection_extents",
    "collection_extent_maintenance",
    "enable_collection_extent_maintenance",
    "enable_extent_maintenance",
    "extent_maintenance",
    "maintain_extents",
)

#: Depth of nested extent recomputations in the current task.
_MAINTENANCE_DEPTH: ContextVar[int] = ContextVar("extent_maintenance_depth", default=0)


@contextmanager
def extent_maintenance_scope() -> Iterator[None]:
    """Mark the current task as recomputing an extent.

    Backends commonly read a collection (to check that it exists) from inside
    ``item_collection``.  If reading a collection also recomputed its extent, the
    recomputation would recurse.  The scope lets the read paths skip the
    recomputation while an extent is being computed.
    """
    token = _MAINTENANCE_DEPTH.set(_MAINTENANCE_DEPTH.get() + 1)
    try:
        yield
    finally:
        _MAINTENANCE_DEPTH.reset(token)


def in_extent_maintenance() -> bool:
    """Return whether the current task is computing an extent."""
    return _MAINTENANCE_DEPTH.get() > 0


# ---------------------------------------------------------------------------
# Item inspection
# ---------------------------------------------------------------------------


def as_dict(value: Any) -> Any:
    """Return a plain ``dict`` for a STAC object.

    Accepts mappings, pydantic models and any object exposing ``model_dump``.
    Values that cannot be converted are returned unchanged.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _parse_datetime(value: Any) -> datetime | None:
    """Parse a STAC datetime value, returning ``None`` when it is not usable.

    Unparseable values are ignored on purpose: a missing timestamp must not be
    replaced by the current date.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value)
        try:
            parsed = rfc3339_str_to_datetime(text)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _format_datetime(value: Any) -> str | None:
    """Return the RFC 3339 representation of a datetime value.

    Aware ``datetime`` objects are normalized to UTC so that the extent is
    independent of the offset used by the Item.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(UTC)
        return datetime_to_str(value)
    return str(value)


def _iter_positions(coordinates: Any) -> Iterable[tuple[float, float]]:
    """Yield the ``(longitude, latitude)`` positions of GeoJSON coordinates."""
    if isinstance(coordinates, Mapping):
        inner = coordinates.get("coordinates")
        if inner is not None:
            yield from _iter_positions(inner)
        for child in coordinates.get("geometries") or ():
            yield from _iter_positions(child)
        return

    if not isinstance(coordinates, (list, tuple)):
        return

    if len(coordinates) >= 2 and all(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        for value in coordinates[:2]
    ):
        yield float(coordinates[0]), float(coordinates[1])
        return

    for child in coordinates:
        yield from _iter_positions(child)


def _bbox_from_geometry(geometry: Any) -> list[float] | None:
    """Compute a 2D bbox from a GeoJSON geometry."""
    xs: list[float] = []
    ys: list[float] = []
    for x, y in _iter_positions(geometry):
        xs.append(x)
        ys.append(y)
    if not xs or not ys:
        return None
    return [min(xs), min(ys), max(xs), max(ys)]


def _numeric_bbox(bbox: Any) -> list[float] | None:
    """Return a bbox as a list of floats, or ``None`` when it is unusable."""
    if not isinstance(bbox, (list, tuple)) or len(bbox) not in (4, 6):
        return None
    values: list[float] = []
    for value in bbox:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        values.append(float(value))
    # 4D: xmin, ymin, xmax, ymax.  6D: xmin, ymin, zmin, xmax, ymax, zmax.
    if values[0] > _xmax(values) or values[1] > _ymax(values):
        # A box crossing the antimeridian cannot be represented by a plain
        # min/max union.  Fall back to the geometry or to the world extent.
        return None
    return values


def item_bbox(item: Any) -> list[float] | None:
    """Return the spatial bbox of an Item.

    The Item's ``bbox`` is used when present; otherwise it is derived from the
    Item's geometry.  ``None`` is returned when neither is usable.

    Args:
        item: a STAC Item (mapping or pydantic model).

    Returns:
        A 4- or 6-element list of floats, or ``None``.
    """
    item_dict = as_dict(item)
    if not isinstance(item_dict, Mapping):
        return None
    bbox = _numeric_bbox(item_dict.get("bbox"))
    if bbox is not None:
        return bbox

    geometry = item_dict.get("geometry")
    if geometry:
        derived = _bbox_from_geometry(geometry)
        if derived is not None and derived[0] <= derived[2] and derived[1] <= derived[3]:
            return derived
    return None


def item_temporal_interval(item: Any) -> tuple[str | None, str | None] | None:
    """Return the temporal annotation of an Item.

    ``properties.datetime`` takes precedence over the
    ``properties.start_datetime`` / ``properties.end_datetime`` pair.  ``None``
    is returned when the Item carries no usable temporal annotation; it is never
    interpreted as "now".

    Args:
        item: a STAC Item (mapping or pydantic model).

    Returns:
        A ``(start, end)`` tuple of RFC 3339 strings, or ``None``.  Either bound
        may be ``None`` for an open interval.
    """
    item_dict = as_dict(item)
    if not isinstance(item_dict, Mapping):
        return None
    properties = item_dict.get("properties")
    if not isinstance(properties, Mapping):
        return None

    value = properties.get("datetime")
    if value is not None:
        if _parse_datetime(value) is None:
            return None
        text = _format_datetime(value)
        return text, text

    start_value = properties.get("start_datetime")
    end_value = properties.get("end_datetime")
    start = _parse_datetime(start_value)
    end = _parse_datetime(end_value)
    if start is None and end is None:
        return None

    return (
        _format_datetime(start_value) if start is not None else None,
        _format_datetime(end_value) if end is not None else None,
    )


# ---------------------------------------------------------------------------
# Extent computation
# ---------------------------------------------------------------------------


def _xmax(box: list[float]) -> float:
    return box[3] if len(box) == 6 else box[2]


def _ymax(box: list[float]) -> float:
    return box[4] if len(box) == 6 else box[3]


def compute_spatial_extent(items: Iterable[Any]) -> dict[str, list[list[float]]]:
    """Compute the spatial extent of a group of Items.

    Args:
        items: the Items of a collection.

    Returns:
        The STAC ``spatial`` extent object (``{"bbox": [...]}``).  A group
        without any usable geometry yields the whole world.
    """
    boxes = [bbox for bbox in (item_bbox(item) for item in items) if bbox is not None]
    if not boxes:
        return {"bbox": [list(DEFAULT_SPATIAL_BBOX[0])]}

    if all(len(box) == 6 for box in boxes):
        return {
            "bbox": [
                [
                    min(box[0] for box in boxes),
                    min(box[1] for box in boxes),
                    min(box[2] for box in boxes),
                    max(_xmax(box) for box in boxes),
                    max(_ymax(box) for box in boxes),
                    max(box[5] for box in boxes),
                ]
            ]
        }

    xmin = min(box[0] for box in boxes)
    ymin = min(box[1] for box in boxes)
    xmax = max(_xmax(box) for box in boxes)
    ymax = max(_ymax(box) for box in boxes)
    if xmin > xmax or ymin > ymax:
        return {"bbox": [list(DEFAULT_SPATIAL_BBOX[0])]}
    return {"bbox": [[xmin, ymin, xmax, ymax]]}


def compute_temporal_extent(items: Iterable[Any]) -> dict[str, list[list[Any]]]:
    """Compute the temporal extent of a group of Items.

    Items without a usable temporal annotation are skipped; they are not
    assigned the current date.

    Args:
        items: the Items of a collection.

    Returns:
        The STAC ``temporal`` extent object (``{"interval": [[start, end]]}``).
        A group without any temporal annotation yields an open interval.
    """
    starts: list[tuple[datetime, str]] = []
    ends: list[tuple[datetime, str]] = []

    for item in items:
        interval = item_temporal_interval(item)
        if interval is None:
            continue
        start_text, end_text = interval
        if start_text is not None:
            start = _parse_datetime(start_text)
            if start is not None:
                starts.append((start, start_text))
        if end_text is not None:
            end = _parse_datetime(end_text)
            if end is not None:
                ends.append((end, end_text))

    if not starts and not ends:
        return {"interval": [list(DEFAULT_TEMPORAL_INTERVAL[0])]}

    start_text = min(starts, key=lambda value: value[0])[1] if starts else None
    end_text = max(ends, key=lambda value: value[0])[1] if ends else None

    if start_text is not None and end_text is not None:
        start = _parse_datetime(start_text)
        end = _parse_datetime(end_text)
        if start is not None and end is not None and start > end:
            return {"interval": [list(DEFAULT_TEMPORAL_INTERVAL[0])]}

    return {"interval": [[start_text, end_text]]}


def compute_collection_extent(items: Iterable[Any]) -> dict[str, Any]:
    """Compute the STAC ``extent`` object for the Items of a collection.

    Args:
        items: all Items currently stored in the collection.

    Returns:
        A mapping with ``spatial`` and ``temporal`` members, suitable for the
        ``extent`` field of a Collection.
    """
    item_list = list(items)
    return {
        "spatial": compute_spatial_extent(item_list),
        "temporal": compute_temporal_extent(item_list),
    }


#: Short aliases for the extent object.
compute_extent = compute_collection_extent
extent_from_items = compute_collection_extent
temporal_extent_from_items = compute_temporal_extent
spatial_extent_from_items = compute_spatial_extent


def apply_extent(collection: Any, items: Iterable[Any]) -> Any:
    """Return a copy of ``collection`` whose extent is computed from ``items``.

    The copy has the same type as the input when it is a mapping or a pydantic
    model; this keeps the value usable by backends that persist models.
    """
    extent = compute_collection_extent(items)

    if isinstance(collection, Mapping):
        updated = dict(collection)
        updated["extent"] = extent
        return updated

    if hasattr(collection, "model_dump") and hasattr(type(collection), "model_validate"):
        try:
            data = collection.model_dump(mode="json")
            data["extent"] = extent
            return type(collection).model_validate(data)
        except Exception:  # noqa: BLE001 - fall back to a plain copy
            pass

    if hasattr(collection, "model_copy"):
        try:
            return collection.model_copy(update={"extent": extent})
        except Exception:  # noqa: BLE001 - fall back to a plain dict
            pass

    updated = as_dict(collection)
    if isinstance(updated, Mapping):
        updated = dict(updated)
        updated["extent"] = extent
        return updated
    return collection


#: Aliases emphasizing that the extent is replaced rather than merged.
update_collection_extent = apply_extent
recompute_collection_extent = apply_extent
maintain_collection_extent = apply_extent


def refresh_collection_extent(
    collection: Any, items: Iterable[Any], **kwargs: Any
) -> Any:
    """Return ``collection`` with its extent recomputed from ``items``.

    Write and read paths both go through this function so that they agree on the
    resulting collection.
    """
    return apply_extent(collection, items)


# ---------------------------------------------------------------------------
# Reading the Items of a collection
# ---------------------------------------------------------------------------


class ExtentReadError(RuntimeError):
    """Raised when the Items of a collection cannot be read."""


def _result_features(result: Any) -> list[Any]:
    """Return the features of an ItemCollection result.

    Raises:
        ExtentReadError: if the result is not an ItemCollection.  Treating such
            a result as empty would wrongly reset the extent to an empty one.
    """
    data = as_dict(result)
    if not isinstance(data, Mapping) or "features" not in data:
        raise ExtentReadError(
            f"item_collection did not return an ItemCollection: {type(result)!r}"
        )
    return list(data.get("features") or ())


def _result_next_token(result: Any) -> str | None:
    """Return the pagination token of the ``next`` link of an ItemCollection."""
    data = as_dict(result)
    if not isinstance(data, Mapping):
        return None

    for link in data.get("links") or ():
        if not isinstance(link, Mapping) or link.get("rel") != "next":
            continue
        query = parse_qs(urlparse(str(link.get("href") or "")).query)
        for key in ("token", "next", "offset"):
            values = query.get(key)
            if values:
                return values[0]
    return None


def _call_item_collection(
    client: Any, collection_id: str, request: Any, token: str | None, limit: int
) -> Any:
    """Call ``item_collection`` with the arguments accepted by the client.

    Pagination and request arguments are only passed when the callable declares
    them, so backends with narrower signatures keep working.
    """
    method = client.item_collection
    try:
        parameters: Mapping[str, inspect.Parameter] = inspect.signature(method).parameters
    except (TypeError, ValueError):
        parameters = {}

    accepts_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )

    def _accepts(name: str) -> bool:
        return accepts_kwargs or name in parameters

    kwargs: dict[str, Any] = {}
    if limit is not None and _accepts("limit"):
        kwargs["limit"] = limit
    if token is not None and _accepts("token"):
        kwargs["token"] = token
    if request is not None and _accepts("request"):
        kwargs["request"] = request
    return method(collection_id, **kwargs)


def get_collection_items_sync(
    client: Any,
    collection_id: str,
    request: Any = None,
    limit: int = DEFAULT_PAGE_LIMIT,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> list[Any]:
    """Synchronously read every Item of a collection, following pagination."""
    if client is None or not hasattr(client, "item_collection"):
        return []

    items: list[Any] = []
    token: str | None = None
    for _ in range(max_pages):
        with extent_maintenance_scope():
            result = _call_item_collection(client, collection_id, request, token, limit)
        if inspect.isawaitable(result):
            result = run_sync(result)
        page = _result_features(result)
        items.extend(page)
        next_token = _result_next_token(result)
        if not page or next_token is None or next_token == token:
            break
        token = next_token
    return items


async def aget_collection_items(
    client: Any,
    collection_id: str,
    request: Any = None,
    limit: int = DEFAULT_PAGE_LIMIT,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> list[Any]:
    """Asynchronously read every Item of a collection, following pagination."""
    if client is None or not hasattr(client, "item_collection"):
        return []

    items: list[Any] = []
    token: str | None = None
    for _ in range(max_pages):
        with extent_maintenance_scope():
            result = _call_item_collection(client, collection_id, request, token, limit)
            if inspect.isawaitable(result):
                result = await result
        page = _result_features(result)
        items.extend(page)
        next_token = _result_next_token(result)
        if not page or next_token is None or next_token == token:
            break
        token = next_token
    return items


def get_collection_items(
    client: Any,
    collection_id: str,
    request: Any = None,
    limit: int = DEFAULT_PAGE_LIMIT,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> list[Any]:
    """Read every Item of a collection, working with sync and async clients.

    Must not be called from a running event loop when the client is
    asynchronous; use :func:`aget_collection_items` there instead.
    """
    return get_collection_items_sync(
        client, collection_id, request=request, limit=limit, max_pages=max_pages
    )


def run_sync(awaitable: Any) -> Any:
    """Run a coroutine to completion from synchronous code."""
    if not inspect.isawaitable(awaitable):
        return awaitable
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)  # type: ignore[arg-type]
    if inspect.iscoroutine(awaitable):
        awaitable.close()
    raise RuntimeError(
        "An event loop is already running; use the asynchronous extent helpers."
    )


# ---------------------------------------------------------------------------
# Enabling the maintenance
# ---------------------------------------------------------------------------


def maintenance_enabled(settings: Any = None) -> bool:
    """Return whether collection-extent maintenance is enabled.

    Args:
        settings: optional settings object.  When omitted, the configured
            global API settings are used.

    Returns:
        ``True`` when extents must be recomputed from Items.
    """
    if settings is None:
        from stac_fastapi.types.config import Settings

        try:
            settings = Settings.get()
        except ValueError:
            return False

    for name in MAINTENANCE_SETTING_NAMES:
        if bool(getattr(settings, name, False)):
            return True
    return False


def resolve_core_client(source: Any) -> Any:
    """Find a client able to read the Items of a collection.

    Transactions clients do not implement collection reads, so they need a core
    client.  Backends normally expose it as ``request.app.state.client``; a
    client may also carry a ``core_client`` attribute or implement
    ``item_collection`` itself.

    Args:
        source: a request, a client, or a settings object.

    Returns:
        A client exposing ``item_collection``, or ``None``.
    """
    if source is None:
        return None

    candidates: list[Any] = []

    requests: list[Any] = []
    for value in (source, getattr(source, "request", None)):
        if isinstance(value, Mapping):
            value = value.get("request", value)
        if value is not None:
            requests.append(value)

    for request in requests:
        app = getattr(request, "app", None)
        state = getattr(app, "state", None)
        if state is not None:
            candidates.extend(
                [getattr(state, "client", None), getattr(state, "core_client", None)]
            )

    candidates.extend(
        [
            getattr(source, "core_client", None),
            getattr(source, "client", None),
            source,
        ]
    )

    for candidate in candidates:
        if candidate is not None and hasattr(candidate, "item_collection"):
            return candidate
    return None
