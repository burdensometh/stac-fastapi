"""FastAPI exception handlers for federation-specific errors.

The default stac-fastapi handler would render :class:`FederationCoverageError`
as a plain 404 with only a message. These handlers preserve the per-source
status report in the response body so clients can tell *which* sources were
missing, failing or skipped instead of guessing from a generic error.
"""

from fastapi import FastAPI

from stac_fastapi.api.models import JSONResponse

from .errors import FederationCoverageError, SourceError


def add_federation_exception_handlers(app: FastAPI) -> None:
    """Register federation exception handlers on a FastAPI application.

    * :class:`FederationCoverageError` → 503 when at least one source was
      attempted but none could serve the request (missing/failed coverage),
      carrying the per-source status report.
    * :class:`SourceError` → 502 Bad Gateway, naming the offending upstream.
    """

    @app.exception_handler(FederationCoverageError)
    async def coverage_handler(request, exc: FederationCoverageError):
        return JSONResponse(
            content={
                "code": "FederationCoverageError",
                "description": str(exc),
                "federation:coverage": {
                    "coverage": [dict(status) for status in exc.source_statuses],
                    "partial": True,
                },
            },
            status_code=503,
        )

    @app.exception_handler(SourceError)
    async def source_error_handler(request, exc: SourceError):
        return JSONResponse(
            content={
                "code": "SourceError",
                "description": str(exc),
                "source": exc.source_id,
            },
            status_code=502,
        )
