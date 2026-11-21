"""Revision event stores."""

import abc
import threading
from uuid import uuid4

import attr
from attrs.validators import gt

from stac_fastapi.types.errors import RebaselineRequiredError
from stac_fastapi.types.revisions import RevisionEvent, RevisionFeed

from .client import BaseRevisionsClient


@attr.s  # type:ignore
class RevisionStore(abc.ABC):
    """Synchronous, append-only store for revision events.

    Backends persisting events inside their own transactions client should
    append the events in the same database transaction as the Item changes
    they describe, so a failed commit records no events.
    """

    @abc.abstractmethod
    def append_batch(self, events: list[RevisionEvent]) -> list[RevisionEvent]:
        """Atomically append one committed batch of events to the stream.

        Implementations must assign each event its stream-wide sequence
        number and the batch identifier shared by the whole batch.

        Args:
            events: the events of a single committed write, in order.

        Returns:
            The stored events, with `seq` and `batch` assigned.
        """
        ...


@attr.s  # type:ignore
class AsyncRevisionStore(abc.ABC):
    """Asynchronous, append-only store for revision events.

    Backends persisting events inside their own transactions client should
    append the events in the same database transaction as the Item changes
    they describe, so a failed commit records no events.
    """

    @abc.abstractmethod
    async def append_batch(self, events: list[RevisionEvent]) -> list[RevisionEvent]:
        """Atomically append one committed batch of events to the stream.

        Implementations must assign each event its stream-wide sequence
        number and the batch identifier shared by the whole batch.

        Args:
            events: the events of a single committed write, in order.

        Returns:
            The stored events, with `seq` and `batch` assigned.
        """
        ...


@attr.s  # type:ignore
class InMemoryRevisionStore(RevisionStore, BaseRevisionsClient):
    """In-memory revision store with count-based retention.

    Reference implementation of the revision stream, suitable for tests,
    demos and single-process deployments. Events are kept in memory and lost
    on restart; production deployments should persist events in their
    database, in the same transaction as the Item changes.

    The retained events always form a gapless run of sequence numbers ending
    at the stream head, which makes cursor validation and paging exact.

    Attributes:
        retention: maximum number of events retained. Older events are
            trimmed; readers resuming from a trimmed cursor get a
            `RebaselineRequiredError`.
    """

    retention: int = attr.ib(default=10_000, validator=gt(0))
    _events: list[RevisionEvent] = attr.ib(init=False, factory=list)
    _seq: int = attr.ib(init=False, default=0)
    _lock: threading.Lock = attr.ib(init=False, factory=threading.Lock)

    @property
    def latest_seq(self) -> int:
        """Sequence number of the newest event (0 when the stream is empty)."""
        return self._seq

    @property
    def oldest_seq(self) -> int:
        """Sequence number of the oldest retained event (0 when empty)."""
        return self._events[0].seq if self._events else 0

    def append_batch(self, events: list[RevisionEvent]) -> list[RevisionEvent]:
        """Append one committed batch of events, trimming to the retention."""
        if not events:
            return []
        with self._lock:
            batch = uuid4().hex
            stored = [
                event.model_copy(update={"seq": self._seq + i + 1, "batch": batch})
                for i, event in enumerate(events)
            ]
            self._seq += len(stored)
            self._events.extend(stored)
            overflow = len(self._events) - self.retention
            if overflow > 0:
                del self._events[:overflow]
            return stored

    def get_revisions(
        self, cursor: int | None = None, limit: int = 100, **kwargs
    ) -> RevisionFeed:
        """Return the page of events after `cursor`.

        Raises:
            RebaselineRequiredError: if `cursor` is older than the oldest
                retained event or ahead of the stream head.
        """
        with self._lock:
            # greatest trimmed sequence number; valid cursors are
            # [horizon, self._seq]
            horizon = self._events[0].seq - 1 if self._events else 0
            if cursor is None:
                start = horizon
            elif cursor < horizon or cursor > self._seq:
                raise RebaselineRequiredError(
                    f"cursor {cursor} is outside the valid cursor range "
                    f"[{horizon}, {self._seq}] (oldest retained event: "
                    f"{horizon + 1 if self._events else 0}); re-baseline from a "
                    "full catalog snapshot before resuming the revision stream"
                )
            else:
                start = cursor
            # retained sequence numbers are the gapless run
            # [horizon + 1, self._seq], so paging is a plain slice
            page = self._events[start - horizon : start - horizon + limit]
            return RevisionFeed(
                events=list(page),
                next_cursor=page[-1].seq if page else start,
                oldest_cursor=horizon + 1 if self._events else 0,
                latest_cursor=self._seq,
            )
