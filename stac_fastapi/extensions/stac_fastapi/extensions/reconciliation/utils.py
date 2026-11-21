"""Snapshot normalization and diffing helpers.

The helpers in this module are deliberately backend agnostic: they operate on
plain JSON dictionaries and never mutate their inputs.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError
from stac_pydantic import Item

from stac_fastapi.types.errors import InvalidSnapshotError

#: Top-level Item fields that participate in change detection. ``links`` and
#: ``collection`` are deliberately excluded: both are navigation/context
#: metadata that a server owns and may rewrite or omit on read, so including
#: them would make every reconciliation an update. The collection of a snapshot
#: Item is validated separately against the request path.
COMPARABLE_FIELDS = (
    "type",
    "stac_version",
    "stac_extensions",
    "id",
    "geometry",
    "bbox",
    "properties",
    "assets",
)


def normalize_datetime_string(value: str) -> str:
    """Normalize an RFC 3339 datetime string to UTC, when it is one.

    Returns the input unchanged when it is not a parseable datetime. This keeps
    ``2020-01-01T00:00:00Z`` and ``2020-01-01T00:00:00+00:00`` equal.
    """
    if len(value) < 19 or "T" not in value:
        return value
    candidate = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        return value
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def canonicalize(value: Any) -> Any:
    """Return a deterministic, JSON-serializable representation of ``value``."""
    if isinstance(value, dict):
        return {key: canonicalize(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [canonicalize(item) for item in value]
    if isinstance(value, str):
        return normalize_datetime_string(value)
    return value


def comparable_item(item: dict[str, Any]) -> dict[str, Any]:
    """Extract the server-independent content of an Item for comparison."""
    comparable = {
        field: canonicalize(item[field])
        for field in COMPARABLE_FIELDS
        if field in item
    }
    if isinstance(comparable.get("stac_extensions"), list):
        comparable["stac_extensions"] = sorted(comparable["stac_extensions"])
    return comparable


def item_digest(item: dict[str, Any]) -> str:
    """Return the SHA-256 of an Item's comparable content."""
    payload = json.dumps(
        comparable_item(item), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def snapshot_digest(items: list[dict[str, Any]]) -> str:
    """Return the SHA-256 of a canonical, order-independent snapshot digest."""
    digests = sorted(item_digest(item) for item in items)
    return hashlib.sha256("\n".join(digests).encode("utf-8")).hexdigest()


def items_equivalent(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Return True when two Items have the same server-independent content."""
    return comparable_item(left) == comparable_item(right)


def parse_snapshot_item(raw: Any, collection_id: str) -> dict[str, Any]:
    """Validate one raw Item and return a payload safe to persist.

    The Item's own ``id`` is authoritative. The returned payload preserves all
    fields of the input (including extension fields), except that ``collection``
    is filled in from the request path when it is absent.

    Raises:
        InvalidSnapshotError: if the Item is not a valid STAC Item, has no id,
            or declares a different collection.
    """
    if not isinstance(raw, dict):
        raise InvalidSnapshotError(
            f"snapshot items must be JSON objects, got {type(raw).__name__}"
        )

    try:
        item = Item.model_validate(raw)
    except ValidationError as exc:
        raise InvalidSnapshotError(
            f"invalid STAC Item in snapshot: {exc.errors(include_url=False)[:1]}"
        ) from exc

    item_id = item.id
    if not item_id:
        raise InvalidSnapshotError("snapshot Item is missing an id")

    declared_collection = raw.get("collection")
    if declared_collection is not None and declared_collection != collection_id:
        raise InvalidSnapshotError(
            f"Item {item_id!r} belongs to collection {declared_collection!r}, "
            f"not {collection_id!r}"
        )

    payload = copy.deepcopy(raw)
    payload["id"] = item_id
    payload.setdefault("collection", collection_id)
    return payload


def raw_item(item: Any) -> dict[str, Any]:
    """Convert a stored Item (dict or pydantic model) to a JSON dictionary."""
    if isinstance(item, dict):
        return copy.deepcopy(item)
    if hasattr(item, "model_dump"):
        return item.model_dump(mode="json", by_alias=True, exclude_none=True)
    if hasattr(item, "to_dict"):
        return copy.deepcopy(item.to_dict())
    raise InvalidSnapshotError(
        f"cannot read persisted Item of type {type(item).__name__}"
    )
