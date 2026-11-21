"""Revision stream extension."""

from collections.abc import Sequence
from enum import StrEnum
from typing import Annotated

import attr
from fastapi import APIRouter, FastAPI, Query
from fastapi.params import Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.requests import Request

from stac_fastapi.api.routes import create_async_endpoint
from stac_fastapi.types.extension import ApiExtension
from stac_fastapi.types.revisions import RevisionFeed
from stac_fastapi.types.search import APIRequest, Limit

from .client import AsyncBaseRevisionsClient, BaseRevisionsClient


class RevisionStreamConformanceClasses(StrEnum):
    """Conformance classes for the revision stream extension."""

    REVISIONS = "https://stac-utils.github.io/stac-fastapi/conformance/revisions"


@attr.s
class GetRevisions(APIRequest):
    """Get revision events."""

    cursor: Annotated[
        int | None,
        Query(
            ge=0,
            description="Sequence number of the last applied event; only events "
            "with a greater sequence number are returned. Omit to read from "
            "the oldest retained event.",
        ),
    ] = attr.ib(default=None)
    limit: Annotated[
        Limit,
        Query(description="Maximum number of events to return (capped to 10_000)."),
    ] = attr.ib(default=100)


@attr.s
class RevisionStreamExtension(ApiExtension):
    """Revision Stream Extension.

    The revision stream extension adds the `GET /revisions` endpoint to the
    application, exposing the revision events recorded for committed Item
    changes (creates, updates, patches, deletions and bulk writes) as a
    single ordered stream. Readers page through the stream with the `cursor`
    query parameter and apply events to their own catalog keyed by
    `(collection_id, item_id)`:

    - `upsert` events carry the committed Item when the payload is available;
    - `delete` events carry the identity (`collection_id`, `item_id`) of the
      removed Item;
    - a cursor outside the retained window is answered with `410 Gone`
      (`RebaselineRequiredError`), requiring the reader to re-baseline from a
      full catalog snapshot.

    The stream is optional and empty unless a `revision_store` is configured
    on the transaction and/or bulk-transaction extensions (or the backend
    records events itself).
    """

    client: AsyncBaseRevisionsClient | BaseRevisionsClient = attr.ib()
    conformance_classes: list[str] = attr.ib(
        factory=lambda: [RevisionStreamConformanceClasses.REVISIONS]
    )
    schema_href: str | None = attr.ib(default=None)
    route_dependencies: Sequence[Depends] | None = attr.ib(default=None)
    enable_response_models: bool = attr.ib(default=True)

    def register(self, app: FastAPI) -> None:
        """Register the extension with a FastAPI application.

        Args:
            app: target FastAPI application.

        Returns:
            None
        """
        endpoint = create_async_endpoint(self.client.get_revisions, GetRevisions)

        async def get_revisions(request: Request, request_data=Depends(GetRevisions)):
            """Get revisions endpoint.

            Pydantic results are serialized to plain dicts so the endpoint
            also works with `enable_direct_response`, which skips FastAPI
            serialization.
            """
            feed = await endpoint(request, request_data)
            if isinstance(feed, BaseModel):
                return feed.model_dump(mode="json")
            return feed

        router = APIRouter(prefix=app.state.router_prefix)
        router.add_api_route(
            name="Get Revisions",
            path="/revisions",
            response_model=RevisionFeed if self.enable_response_models else None,
            responses={
                200: {
                    "content": {
                        "application/json": {},
                    },
                    "model": RevisionFeed,
                },
            },
            response_class=JSONResponse,
            response_model_exclude_none=True,
            methods=["GET"],
            endpoint=get_revisions,
            dependencies=self.route_dependencies,
        )
        app.include_router(router, tags=["Revision Stream Extension"])
