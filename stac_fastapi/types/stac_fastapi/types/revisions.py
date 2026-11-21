"""Types for the optional Item revision stream (change feed).

The revision stream lets a downstream catalog synchronize Item creations,
updates and deletions: every committed Item change is recorded as a
:class:`RevisionEvent` carrying a stream-wide, monotonically increasing
sequence number. Readers resume from a cursor (the sequence number of the
last event they applied) and apply events to their own catalog keyed by
``(collection_id, item_id)``.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class RevisionOperation(StrEnum):
    """Type of change described by a :class:`RevisionEvent`."""

    UPSERT = "upsert"
    DELETE = "delete"


class RevisionEvent(BaseModel):
    """A single committed change to a STAC Item.

    Attributes:
        seq: stream-wide, gapless, monotonically increasing sequence number.
            Assigned by the revision store when the event is appended; the
            value set on a new event is ignored.
        batch: identifier shared by all events recorded by the same committed
            write (e.g. one bulk insert or one ItemCollection create).
            Assigned by the revision store.
        op: ``upsert`` for creations and updates (including patches and bulk
            writes), ``delete`` for deletions.
        collection_id: id of the collection the Item belongs to.
        item_id: id of the changed Item.
        item: the committed Item, for ``upsert`` events when the payload is
            available. Always ``None`` for ``delete`` events; when absent,
            readers fetch the current state by ``collection_id``/``item_id``.
        ts: time the event was recorded.
    """

    seq: int = 0
    batch: str = ""
    op: RevisionOperation
    collection_id: str
    item_id: str
    item: dict[str, Any] | None = None
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RevisionFeed(BaseModel):
    """A page of revision events, as returned by ``GET /revisions``.

    Attributes:
        events: events with ``seq`` greater than the requested cursor, in
            ascending ``seq`` order.
        next_cursor: cursor to pass as ``cursor`` to resume the stream.
        oldest_cursor: smallest sequence number still retained by the stream.
            A reader holding a cursor smaller than ``oldest_cursor - 1`` has
            missed events and must re-baseline from a full catalog snapshot.
        latest_cursor: greatest sequence number assigned so far (stream head).
    """

    events: list[RevisionEvent]
    next_cursor: int
    oldest_cursor: int
    latest_cursor: int


def upsert_event(
    collection_id: str, item_id: str, item: dict[str, Any] | None = None
) -> RevisionEvent:
    """Build an ``upsert`` event for a created or updated Item.

    Args:
        collection_id: id of the collection the Item belongs to.
        item_id: id of the changed Item.
        item: the committed Item, when the payload is available.

    Returns:
        The event, ready to be appended to a revision store.
    """
    return RevisionEvent(
        op=RevisionOperation.UPSERT,
        collection_id=collection_id,
        item_id=item_id,
        item=item,
    )


def delete_event(collection_id: str, item_id: str) -> RevisionEvent:
    """Build a ``delete`` event for a removed Item.

    The event carries the full identity of the deleted Item
    (``collection_id`` and ``item_id``) so a downstream catalog can remove
    it, and no payload.

    Args:
        collection_id: id of the collection the Item belonged to.
        item_id: id of the deleted Item.

    Returns:
        The event, ready to be appended to a revision store.
    """
    return RevisionEvent(
        op=RevisionOperation.DELETE,
        collection_id=collection_id,
        item_id=item_id,
    )
