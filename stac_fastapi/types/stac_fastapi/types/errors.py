"""stac_fastapi.types.errors module."""


class StacApiError(Exception):
    """Generic API error."""

    pass


class ConflictError(StacApiError):
    """Database conflict."""

    pass


class NotFoundError(StacApiError):
    """Resource not found."""

    pass


class ForeignKeyError(StacApiError):
    """Foreign key error (collection does not exist)."""

    pass


class DatabaseError(StacApiError):
    """Generic database errors."""

    pass


class InvalidQueryParameter(StacApiError):
    """Error for unknown or invalid query parameters.

    Used to capture errors that should respond according to
    http://docs.opengeospatial.org/is/17-069r3/17-069r3.html#query_parameters
    """

    pass


class RebaselineRequiredError(StacApiError):
    """The revision stream cursor is outside the retained window.

    Raised when a reader of the revision stream resumes from a cursor that is
    older than the oldest retained event, or ahead of the stream head (e.g.
    after the stream was reset). The reader must re-baseline from a full
    catalog snapshot before resuming the stream.
    """

    pass
