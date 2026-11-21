"""Bulk transactions extension."""

import abc
from collections.abc import Sequence
from enum import StrEnum
from typing import Any, TypedDict

import attr
from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.params import Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from stac_fastapi.api.models import create_request_model
from stac_fastapi.api.routes import create_async_endpoint
from stac_fastapi.types.extension import ApiExtension

DEFAULT_STREAM_CHUNK_SIZE = 1000
"""Default number of items per committed batch of a streaming bulk request."""

DEFAULT_STREAM_MAX_IN_FLIGHT = 2
"""Default maximum number of uncommitted (in-flight) batches per stream."""

DEFAULT_STREAM_MAX_LINE_BYTES = 10 * 1024 * 1024
"""Default maximum size, in bytes, of a single NDJSON line."""

DEFAULT_STREAM_MAX_ERRORS = 1000
"""Default maximum number of error entries collected from a single stream."""


class TransactionError(TypedDict):
    """Transaction Error."""

    id: str
    msg: str


class BulkTransaction(TypedDict):
    """Bulk Transaction Response."""

    received: int
    success: int
    skipped: int
    errors: list[TransactionError]


class TransactionErrorModel(BaseModel):
    """Transaction Error."""

    id: str
    msg: str


class BulkTransactionModel(BaseModel):
    """Bulk Transaction Model."""

    received: int
    success: int
    skipped: int
    errors: list[TransactionErrorModel]


class BulkTransactionMethod(StrEnum):
    """Bulk Transaction Methods."""

    INSERT = "insert"
    UPSERT = "upsert"


class Items(BaseModel):
    """A group of STAC Item objects, in the form of a dictionary from Item.id -> Item."""

    items: dict[str, Any]
    method: BulkTransactionMethod = BulkTransactionMethod.INSERT

    def __iter__(self):
        """Return an iterable of STAC Item objects."""
        return iter(self.items.values())


@attr.s
class BaseBulkTransactionsClient(abc.ABC):
    """BulkTransactionsClient."""

    @staticmethod
    def _chunks(lst, n):
        """Yield successive n-sized chunks from list.

        https://stackoverflow.com/questions/312443/how-do-you-split-a-list-into-evenly-sized-chunks
        """
        for i in range(0, len(lst), n):
            yield lst[i : i + n]

    @abc.abstractmethod
    def bulk_item_insert(
        self,
        items: Items,
        chunk_size: int | None = None,
        **kwargs,
    ) -> BulkTransaction | Response:
        """Bulk creation of items.

        Args:
            items: list of items.
            chunk_size: number of items processed at a time.

        Returns:
            Message indicating the status of the insert.
        """
        raise NotImplementedError


@attr.s
class AsyncBaseBulkTransactionsClient(abc.ABC):
    """BulkTransactionsClient."""

    @abc.abstractmethod
    async def bulk_item_insert(
        self,
        items: Items,
        **kwargs,
    ) -> BulkTransaction | Response:
        """Bulk creation of items.

        Args:
            items: list of items.

        Returns:
            Message indicating the status of the insert.
        """
        raise NotImplementedError


@attr.s
class BulkTransactionExtension(ApiExtension):
    """Bulk Transaction Extension.

    Bulk Transaction extension adds the `POST
    /collections/{collection_id}/bulk_items` endpoint to the application for
    efficient bulk insertion of items. The input to this is an object with an
    attribute  "items", that has a value that is an object with a group of
    attributes that are the ids of each Item, and the value is the Item entity.

    Optionally, clients can specify a "method" attribute that is either "insert"
    or "upsert". If "insert", then the items will be inserted if they do not
    exist, and an error will be returned if they do. If "upsert", then the items
    will be inserted if they do not exist, and updated if they do. This defaults
    to "insert".

        {
            "items": {
                "id1": { "type": "Feature", ... },
                "id2": { "type": "Feature", ... },
                "id3": { "type": "Feature", ... }
            },
            "method": "insert"
        }

    Unless disabled with `enable_streaming=False`, the extension also adds a
    `POST /collections/{collection_id}/bulk_items/stream` endpoint which
    consumes newline-delimited JSON (`application/x-ndjson`, one STAC Item per
    line) and commits items to the bulk client in bounded batches as the
    request body arrives, so arbitrarily large files can be imported without
    materializing the whole payload. The batch size (`stream_chunk_size`), the
    number of uncommitted batches in flight (`stream_max_in_flight`), the
    maximum line length (`stream_max_line_bytes`) and the number of collected
    errors (`stream_max_errors`) are all bounded by explicit limits.
    """

    client: AsyncBaseBulkTransactionsClient | BaseBulkTransactionsClient = attr.ib()
    conformance_classes: list[str] = attr.ib(default=list())
    schema_href: str | None = attr.ib(default=None)
    route_dependencies: Sequence[Depends] | None = attr.ib(default=None)
    enable_response_models: bool = attr.ib(default=True)
    enable_streaming: bool = attr.ib(default=True)
    stream_chunk_size: int = attr.ib(default=DEFAULT_STREAM_CHUNK_SIZE)
    stream_max_in_flight: int = attr.ib(default=DEFAULT_STREAM_MAX_IN_FLIGHT)
    stream_max_line_bytes: int = attr.ib(default=DEFAULT_STREAM_MAX_LINE_BYTES)
    stream_max_errors: int | None = attr.ib(default=DEFAULT_STREAM_MAX_ERRORS)

    def register(self, app: FastAPI) -> None:
        """Register the extension with a FastAPI application.

        Args:
            app: target FastAPI application.

        Returns:
            None
        """
        items_request_model = create_request_model("Items", base_model=Items)

        router = APIRouter(prefix=app.state.router_prefix)
        router.add_api_route(
            name="Bulk Create Item",
            path="/collections/{collection_id}/bulk_items",
            response_model=BulkTransactionModel,
            responses={
                200: {
                    "content": {
                        "application/json": {},
                    },
                    "model": BulkTransactionModel,
                },
            },
            response_class=JSONResponse,
            response_model_exclude_unset=False,
            methods=["POST"],
            endpoint=create_async_endpoint(
                self.client.bulk_item_insert, items_request_model
            ),
            dependencies=self.route_dependencies,
        )
        if self.enable_streaming:
            self._register_streaming_route(router)
        app.include_router(router, tags=["Bulk Transaction Extension"])

    def _register_streaming_route(self, router: APIRouter) -> None:
        """Add the streaming (NDJSON) bulk items endpoint to the router."""
        from stac_fastapi.extensions.bulk_transactions.streaming import (
            BulkStreamIngester,
        )

        async def stream_bulk_items(
            request: Request,
            collection_id: str,
            method: BulkTransactionMethod = BulkTransactionMethod.INSERT,
        ) -> BulkTransaction:
            """Ingest newline-delimited STAC Items as they arrive."""
            ingester = BulkStreamIngester(
                client=self.client,
                collection_id=collection_id,
                method=method,
                chunk_size=self.stream_chunk_size,
                max_in_flight=self.stream_max_in_flight,
                max_line_bytes=self.stream_max_line_bytes,
                max_errors=self.stream_max_errors,
            )
            return await ingester.ingest(request.stream(), request=request)

        router.add_api_route(
            name="Bulk Create Items Stream",
            path="/collections/{collection_id}/bulk_items/stream",
            response_model=BulkTransactionModel,
            responses={
                200: {
                    "content": {
                        "application/json": {},
                    },
                    "model": BulkTransactionModel,
                },
            },
            response_class=JSONResponse,
            response_model_exclude_unset=False,
            methods=["POST"],
            endpoint=stream_bulk_items,
            dependencies=self.route_dependencies,
            openapi_extra={
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/x-ndjson": {
                            "schema": {
                                "type": "string",
                                "description": "One STAC Item per line.",
                            },
                        },
                    },
                },
            },
        )
