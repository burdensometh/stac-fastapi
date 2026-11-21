"""Errors raised by the federation layer."""

from collections.abc import Iterable

from stac_fastapi.types.errors import NotFoundError, StacApiError


class FederationError(StacApiError):
    """Generic error raised while federating a request across sources."""


class SourceError(FederationError):
    """A single upstream source failed while answering a federated request.

    The original exception is preserved as ``__cause__`` (raised ``from`` the
    upstream error) and the offending source is named, so callers and logs can
    attribute the failure instead of guessing which backend broke.
    """

    def __init__(self, source_id: str, original: BaseException) -> None:
        self.source_id = source_id
        self.original = original
        super().__init__(
            f"source {source_id!r} failed: {type(original).__name__}: {original}"
        )


class FederationCoverageError(NotFoundError):
    """Raised when no source can provide any coverage for a request.

    This covers two cases: no configured source hosts the requested
    collection/record, or every source that does host it failed. The per-source
    status report is attached so the HTTP layer can describe exactly which
    sources were missing or failing.
    """

    def __init__(self, message: str, source_statuses: Iterable | None = None) -> None:
        super().__init__(message)
        # Imported lazily to avoid a circular import at module import time;
        # SourceStatus is a lightweight dataclass-like attrs model.
        from .models import SourceStatus

        self.source_statuses: list[SourceStatus] = list(source_statuses or [])
