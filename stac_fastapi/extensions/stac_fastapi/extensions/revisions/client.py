"""Revision stream clients."""

import abc

import attr
from starlette.responses import Response

from stac_fastapi.types.revisions import RevisionFeed


@attr.s  # type:ignore
class BaseRevisionsClient(abc.ABC):
    """Defines a pattern for implementing the read side of the revision stream."""

    @abc.abstractmethod
    def get_revisions(
        self, cursor: int | None = None, limit: int = 100, **kwargs
    ) -> RevisionFeed | Response:
        """Return the page of revision events that follows `cursor`.

        Called with `GET /revisions`.

        Args:
            cursor: sequence number of the last event the reader applied.
                Only events with a greater sequence number are returned.
                `None` reads from the oldest retained event.
            limit: maximum number of events to return.

        Returns:
            The feed page, with `next_cursor` to resume the stream.

        Raises:
            stac_fastapi.types.errors.RebaselineRequiredError: if `cursor` is
                older than the oldest retained event or ahead of the stream
                head, requiring the reader to re-baseline.
        """
        ...


@attr.s  # type:ignore
class AsyncBaseRevisionsClient(abc.ABC):
    """Defines a pattern for implementing the read side of the revision stream."""

    @abc.abstractmethod
    async def get_revisions(
        self, cursor: int | None = None, limit: int = 100, **kwargs
    ) -> RevisionFeed | Response:
        """Return the page of revision events that follows `cursor`.

        Called with `GET /revisions`.

        Args:
            cursor: sequence number of the last event the reader applied.
                Only events with a greater sequence number are returned.
                `None` reads from the oldest retained event.
            limit: maximum number of events to return.

        Returns:
            The feed page, with `next_cursor` to resume the stream.

        Raises:
            stac_fastapi.types.errors.RebaselineRequiredError: if `cursor` is
                older than the oldest retained event or ahead of the stream
                head, requiring the reader to re-baseline.
        """
        ...
