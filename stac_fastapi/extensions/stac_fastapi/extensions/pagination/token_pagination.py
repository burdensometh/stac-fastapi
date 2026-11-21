"""Token pagination API extension."""

import attr
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette import status

from stac_fastapi.types.extension import ApiExtension
from stac_fastapi.types.search import APIRequest

from .request import GETTokenPagination, POSTTokenPagination
from .snapshot import ResultSetTooLarge, SnapshotBackend, SnapshotExpired
from .snapshot_client import SnapshotPagination


def _snapshot_error_handler(status_code: int):
    def handler(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            content={
                "code": exc.__class__.__name__,
                "description": str(exc),
            },
            status_code=status_code,
        )

    return handler


_snapshot_expired_handler = _snapshot_error_handler(status.HTTP_410_GONE)
_result_set_too_large_handler = _snapshot_error_handler(status.HTTP_413_CONTENT_TOO_LARGE)


@attr.s
class TokenPaginationExtension(ApiExtension):
    """Token Pagination.

    Though not strictly an extension, the chosen pagination will modify the form of the
    request object. By making pagination an extension class, we can use
    create_request_model to dynamically add the correct pagination parameter to the
    request model for OpenAPI generation.

    Attributes:
        snapshot_backend: optional storage for materialized result sets. When set,
            the first page of a search is stored and the pagination token continues
            to browse that immutable version, even if items are inserted or deleted
            concurrently. Without a backend, tokens are simply forwarded to the
            client (the historical behaviour).
        snapshot_secret: optional secret used to sign pagination tokens.
        snapshot_ttl: optional lifetime (in seconds) of a snapshot. Expired tokens
            respond with ``410 Gone`` and ask the client to re-run the search.
        snapshot_max_items: optional maximum size of a result set which may be
            materialized. Larger result sets respond with ``413`` instead of being
            buffered.
    """

    GET: type[APIRequest] = GETTokenPagination
    POST: type[BaseModel] = POSTTokenPagination

    conformance_classes: list[str] = attr.ib(factory=list)
    schema_href: str | None = attr.ib(default=None)

    snapshot_backend: SnapshotBackend | None = attr.ib(default=None)
    snapshot_secret: str | None = attr.ib(default=None)
    snapshot_ttl: float | None = attr.ib(default=None)
    snapshot_max_items: int | None = attr.ib(default=None)

    pagination: SnapshotPagination | None = attr.ib(default=None, init=False, eq=False)

    @property
    def snapshot_enabled(self) -> bool:
        """Whether results are served from a snapshot backend."""
        return self.snapshot_backend is not None

    def bind_client(self, client: object) -> object:
        """Install the snapshot pagination handlers on ``client``.

        Called by :class:`stac_fastapi.api.app.StacApi` before the routes are
        registered, so that the endpoints pick up the wrapped methods.

        Args:
            client: core client implementing the search/collection endpoints.

        Returns:
            The client to use (usually the same object, possibly a proxy).
        """
        if self.snapshot_backend is None:
            return client

        self.pagination = SnapshotPagination(
            self.snapshot_backend,
            secret=self.snapshot_secret,
            max_items=self.snapshot_max_items,
            ttl=self.snapshot_ttl,
        )
        return self.pagination.apply(client)

    def register(self, app: FastAPI) -> None:
        """Register the extension with a FastAPI application.

        Args:
            app: target FastAPI application.

        Returns:
            None
        """
        if self.snapshot_backend is not None:
            app.add_exception_handler(SnapshotExpired, _snapshot_expired_handler)
            app.add_exception_handler(ResultSetTooLarge, _result_set_too_large_handler)
