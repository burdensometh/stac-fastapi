"""Shared temporal contract for Item Search, Collection Search, and Aggregations.

This module is the single source of truth for how stac-fastapi interprets the
STAC ``datetime`` query parameter and an Item's temporal extent. Item Search
(GET/POST ``/search`` and ``GET /collections/{id}/items``), Collection Search
(GET/POST ``/collections``), and the Aggregation extension all normalize their
``datetime`` input through :func:`parse_datetime_interval`, so every endpoint
applies identical semantics.

The contract
------------

**Item temporal extent** — read from the Item's ``properties``:

- when ``datetime`` is set, the Item is an *instant* (e.g. a single
  acquisition) and its extent is the closed interval ``[datetime, datetime]``;
- when ``datetime`` is null, the Item is a *range* (e.g. a multi-day
  observation) and its extent is ``[start_datetime, end_datetime]``; a missing
  bound is treated as open (unbounded).

**Query window** — the ``datetime`` query parameter:

- a single RFC 3339 datetime ``t`` is the closed interval ``[t, t]``;
- ``start/end`` is the closed interval ``[start, end]`` where either side may
  be ``..`` (or empty), meaning open (unbounded);
- a double-open interval (``../..``) is rejected.

**Matching** — an Item matches when its temporal extent *intersects* the query
window. Intervals are closed, so an extent that touches a query boundary at
exactly one point (e.g. an Item ending exactly at the query start) *does*
match. Open bounds are unbounded (they never constrain the intersection).

**Time zones** — all datetimes are normalized to UTC before comparison, so the
same instant written with different UTC offsets (e.g.
``2021-01-01T08:00:00+08:00`` and ``2021-01-01T00:00:00Z``) yields the same
results.
"""

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any, TypeVar

from stac_fastapi.types.rfc3339 import (
    DateTimeType,
    rfc3339_str_to_datetime,
    str_to_interval,
)

# A normalized temporal interval: ``(start, end)`` as UTC datetimes, where
# ``None`` means the interval is open (unbounded) on that side.
DateTimeInterval = tuple[datetime | None, datetime | None]

T = TypeVar("T")


def normalize_to_utc(value: datetime) -> datetime:
    """Normalize a datetime to UTC.

    Aware datetimes are converted; naive datetimes are assumed to be UTC.
    Two datetimes representing the same instant with different UTC offsets
    normalize to the same value.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_interval(value: DateTimeType | None) -> DateTimeInterval | None:
    """Normalize a parsed ``datetime`` query value to a ``(start, end)`` interval.

    - ``None`` -> ``None`` (no temporal constraint)
    - a single datetime ``t`` -> ``(t, t)`` (an instant is a closed point interval)
    - ``(start, end)`` -> ``(start, end)``; either side may be ``None`` (open)

    All returned datetimes are normalized to UTC.
    """
    if value is None:
        return None

    if isinstance(value, datetime):
        point = normalize_to_utc(value)
        return (point, point)

    start, end = value
    return (
        normalize_to_utc(start) if start is not None else None,
        normalize_to_utc(end) if end is not None else None,
    )


def parse_datetime_interval(value: str | None) -> DateTimeInterval | None:
    """Parse a STAC ``datetime`` query parameter into a normalized interval.

    Accepts a single RFC 3339 datetime or a ``start/end`` interval with
    optional open (``..``) ends, and returns the ``(start, end)`` UTC interval
    described at the top of this module. Returns ``None`` when ``value`` is
    ``None``.

    Raises:
        fastapi.HTTPException: (400) if the value is not a valid RFC 3339
            datetime or interval.
    """
    return to_interval(str_to_interval(value))


def _coerce_bound(value: Any) -> datetime | None:
    """Coerce a temporal extent bound to a UTC datetime (``None`` stays open)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return normalize_to_utc(value)
    return normalize_to_utc(rfc3339_str_to_datetime(str(value)))


def item_temporal_extent(properties: Mapping[str, Any]) -> DateTimeInterval:
    """Read an Item's temporal extent from its properties.

    Uses ``datetime`` when it is set (an instant, returned as the closed
    interval ``(t, t)``); otherwise falls back to
    ``start_datetime``/``end_datetime`` (a range). A missing bound is treated
    as open (unbounded), per the STAC specification.

    Args:
        properties: the Item's ``properties`` mapping.

    Returns:
        The Item's temporal extent as a ``(start, end)`` UTC interval.
    """
    single = properties.get("datetime")
    if single is not None:
        point = _coerce_bound(single)
        return (point, point)

    return (
        _coerce_bound(properties.get("start_datetime")),
        _coerce_bound(properties.get("end_datetime")),
    )


def intervals_intersect(
    first: DateTimeInterval,
    second: DateTimeInterval,
) -> bool:
    """Return whether two normalized intervals intersect.

    Intervals are closed: touching at exactly one boundary point counts as
    intersecting. Open (``None``) bounds are unbounded and never constrain the
    intersection.
    """
    first_start, first_end = first
    second_start, second_end = second

    if first_start is not None and second_end is not None and first_start > second_end:
        return False
    if second_start is not None and first_end is not None and second_start > first_end:
        return False
    return True


def item_matches_interval(
    properties: Mapping[str, Any],
    interval: DateTimeInterval | None,
) -> bool:
    """Return whether an Item's temporal extent intersects the query interval.

    A ``None`` interval (no temporal constraint) matches every Item.
    """
    if interval is None:
        return True
    return intervals_intersect(item_temporal_extent(properties), interval)


def collection_temporal_extent(
    collection: Mapping[str, Any],
) -> DateTimeInterval:
    """Read a Collection's temporal extent.

    Uses the first entry of ``extent.temporal.interval`` and returns it as a
    ``(start, end)`` UTC interval; ``None`` bounds are open (unbounded). A
    Collection without a temporal extent is treated as unbounded.
    """
    try:
        start, end = collection["extent"]["temporal"]["interval"][0]
    except (KeyError, IndexError, TypeError):
        return (None, None)
    return (_coerce_bound(start), _coerce_bound(end))


def collection_matches_interval(
    collection: Mapping[str, Any],
    interval: DateTimeInterval | None,
) -> bool:
    """Return whether a Collection's temporal extent intersects the interval.

    A ``None`` interval (no temporal constraint) matches every Collection.
    """
    if interval is None:
        return True
    return intervals_intersect(collection_temporal_extent(collection), interval)


def _properties_of(item: Any) -> Mapping[str, Any]:
    """Extract the ``properties`` mapping from an Item-like object."""
    if isinstance(item, Mapping):
        properties = item.get("properties") or {}
    else:
        properties = getattr(item, "properties", None) or {}
    if not isinstance(properties, Mapping):
        model_dump = getattr(properties, "model_dump", None)
        if callable(model_dump):
            properties = model_dump()
    return properties


def filter_items_by_datetime(
    items: Iterable[T],
    interval: DateTimeInterval | None,
) -> list[T]:
    """Filter Items to those whose temporal extent intersects ``interval``.

    Each item may be a GeoJSON Feature mapping (with a ``properties`` key) or
    an object exposing ``properties``. This is a reference implementation of
    the shared temporal contract for backends that filter in memory; a
    ``None`` interval returns every item.
    """
    if interval is None:
        return list(items)
    return [
        item for item in items if item_matches_interval(_properties_of(item), interval)
    ]
