"""Local application of search conditions.

When a source does not support a condition server-side (see
:class:`SourceCapabilities`) the federation layer must enforce it locally on
the records the source returned. This module contains those pure filters plus
the helpers that strip unsupported conditions from a request before it is
forwarded.

Only the core STAC spatial (``bbox`` / ``intersects``) and temporal
(``datetime``) conditions are federation concerns; extension parameters are
passed through untouched to sources that accept them.
"""

from datetime import datetime
from typing import Any

from stac_fastapi.types.rfc3339 import str_to_interval


def item_datetime(item: dict[str, Any]) -> datetime | tuple[datetime, datetime] | None:
    """Extract the temporal value of an Item as datetime object(s).

    Mirrors STAC semantics: ``properties.datetime`` for instants, and the
    ``start_datetime``/``end_datetime`` pair for ranges.
    """
    properties = item.get("properties", {})
    instant = properties.get("datetime")
    if instant is not None:
        return _parse(instant)
    start = properties.get("start_datetime")
    end = properties.get("end_datetime")
    if start is not None and end is not None:
        return (_parse(start), _parse(end))
    return None


def _parse(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    parsed = str_to_interval(value)
    if isinstance(parsed, tuple):
        return parsed[0]  # pragma: no cover - defensive, single RFC3339 instant
    return parsed


def matches_datetime(item: dict[str, Any], datetime_query: str | None) -> bool:
    """Return whether ``item``'s time intersects the RFC3339 query interval.

    ``None``/empty query matches everything. Open intervals (``..`` bounds)
    are handled by :func:`str_to_interval`.
    """
    if not datetime_query:
        return True
    interval = str_to_interval(datetime_query)
    if interval is None:
        return True
    if isinstance(interval, tuple):
        q_start, q_end = interval
    else:
        q_start = q_end = interval

    value = item_datetime(item)
    if value is None:
        # Items without a temporal property cannot be proven to match a time
        # filter, so exclude them — matching conservative catalog behavior.
        return False
    item_start, item_end = value if isinstance(value, tuple) else (value, value)

    if q_start is not None and item_end is not None and item_end < q_start:
        return False
    if q_end is not None and item_start is not None and item_start > q_end:
        return False
    return True


def _bbox_ranges(bbox: tuple[float, ...]) -> tuple[tuple[float, float], ...]:
    if len(bbox) == 4:
        minx, miny, maxx, maxy = bbox
        return ((minx, maxx), (miny, maxy))
    # 3D bbox: minx, miny, minz, maxx, maxy, maxz
    minx, miny, _minz, maxx, maxy, _maxz = bbox
    return ((minx, maxx), (miny, maxy))


def _point_in_bbox(
    coords: tuple[float, ...], ranges: tuple[tuple[float, float], ...]
) -> bool:
    (minx, maxx), (miny, maxy) = ranges
    x, y = coords[0], coords[1]
    return minx <= x <= maxx and miny <= y <= maxy


def geometry_intersects_bbox(
    geometry: dict[str, Any] | None, bbox: tuple[float, ...]
) -> bool:
    """Cheap bbox-vs-geometry intersection test on vertex bounding boxes.

    The federation layer cannot run a full GEOS engine; it conservatively
    intersects the query bbox with the geometry's own bbox derived from its
    vertices. Item bboxes supplied by the source are used directly when
    present. This may admit edge-adjacent false positives only when a source
    omits item bboxes, never drop true positives.
    """
    if geometry is None:
        return False
    ranges = _bbox_ranges(bbox)
    item_bbox = geometry.get("bbox") if geometry.get("type") == "Feature" else None
    if item_bbox:
        (iminx, imaxx), (iminy, imaxy) = _bbox_ranges(tuple(item_bbox))
        (qminx, qmaxx), (qminy, qmaxy) = ranges
        return not (imaxx < qminx or iminx > qmaxx or imaxy < qminy or iminy > qmaxy)

    coords = _extract_coordinates(geometry)
    if not coords:
        return False
    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    (qminx, qmaxx), (qminy, qmaxy) = ranges
    return not (
        max(xs) < qminx or min(xs) > qmaxx or max(ys) < qminy or min(ys) > qmaxy
    )


def _extract_coordinates(geometry: dict[str, Any]) -> list[tuple[float, ...]]:
    """Flatten every position out of a GeoJSON geometry."""
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if coords is None:
        return []
    points: list[tuple[float, ...]] = []

    def walk(node):
        if not node:
            return
        if isinstance(node[0], (int, float)):
            points.append(tuple(node))
        else:
            for child in node:
                walk(child)

    if gtype == "GeometryCollection":
        for sub in geometry.get("geometries", []):
            points.extend(_extract_coordinates(sub))
        return points
    walk(coords)
    return points


def geometries_intersect(
    geometry: dict[str, Any] | None, intersects: dict[str, Any] | None
) -> bool:
    """Local ``intersects`` check via vertex-bbox overlap.

    Same conservative strategy as :func:`geometry_intersects_bbox`: compare
    bounding boxes derived from the two geometries' vertices.
    """
    if not intersects or geometry is None:
        return intersects is None or geometry is not None
    q_coords = _extract_coordinates(intersects)
    g_coords = _extract_coordinates(geometry)
    if not q_coords or not g_coords:
        return False
    qx = [c[0] for c in q_coords]
    qy = [c[1] for c in q_coords]
    gx = [c[0] for c in g_coords]
    gy = [c[1] for c in g_coords]
    return not (
        max(gx) < min(qx)
        or min(gx) > max(qx)
        or max(gy) < min(qy)
        or min(gy) > max(qy)
    )


def apply_local_filters(
    items: list[dict[str, Any]],
    *,
    bbox: tuple[float, ...] | None,
    intersects: dict[str, Any] | None,
    datetime_query: str | None,
    ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Enforce every condition the source could not execute server-side."""
    result = items
    if ids is not None:
        wanted = set(ids)
        result = [item for item in result if item.get("id") in wanted]
    if bbox is not None:
        result = [
            item
            for item in result
            if geometry_intersects_bbox(item.get("geometry"), bbox)
        ]
    if intersects is not None:
        result = [
            item
            for item in result
            if geometries_intersect(item.get("geometry"), intersects)
        ]
    if datetime_query is not None:
        result = [item for item in result if matches_datetime(item, datetime_query)]
    return result
