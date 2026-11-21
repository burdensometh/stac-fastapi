"""Bulk transactions extension."""

import abc
import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable, Sequence
from enum import StrEnum
from typing import Any, TypedDict

import attr
from fastapi import APIRouter, FastAPI, HTTPException, Response
from fastapi.params import Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.requests import Request

from stac_fastapi.api.models import create_request_model
from stac_fastapi.api.routes import create_async_endpoint, sync_to_async
from stac_fastapi.types.errors import ConflictError
from stac_fastapi.types.extension import ApiExtension


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


class BulkImportReceipt(TypedDict):
    """Receipt of a committed bulk import, as stored by the backend.

    Attributes:
        request_id: the client-supplied import request id.
        content_hash: fingerprint of the import, see `bulk_import_fingerprint`.
        result: the committed per-item result of the import.
    """

    request_id: str
    content_hash: str
    result: BulkTransaction


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


class BulkImportReceiptExistsError(ConflictError):
    """A receipt for this bulk import request already exists.

    Backends raise this from `bulk_item_insert` when the import receipt
    cannot be recorded because a receipt for the same collection and
    request id has already been committed by another (concurrent) request.
    The extension then replays the stored receipt instead of committing
    the items a second time. Implementations must make sure the existing
    receipt is visible to `get_bulk_import_receipt` once this is raised.
    """

    pass


def bulk_import_fingerprint(
    collection_id: str, method: BulkTransactionMethod | str, items: dict[str, Any]
) -> str:
    """Compute a stable fingerprint identifying a bulk import.

    An import is identified by the collection it targets, the write method
    ("insert" or "upsert") and the actual item content, so that a retried
    request can be matched against the import that produced a stored
    receipt. The fingerprint is independent of JSON object key order.
    """
    canonical = json.dumps(
        {"collection_id": collection_id, "method": str(method), "items": items},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Items(BaseModel):
    """A group of STAC Item objects, in the form of a dictionary from Item.id -> Item."""

    items: dict[str, Any]
    method: BulkTransactionMethod = BulkTransactionMethod.INSERT
    request_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        description=(
            "Optional client-supplied identifier for this import request. "
            "When set, and the backend supports idempotent imports, retrying "
            "the request (e.g. after a network timeout) returns the stored "
            "result of the original import instead of importing the items "
            "again. Reusing the same request id with different content is "
            "rejected with a 409 Conflict error."
        ),
    )

    def __iter__(self):
        """Return an iterable of STAC Item objects."""
        return iter(self.items.values())


def _receipt_result(
    receipt: BulkImportReceipt, content_hash: str, request_id: str
) -> BulkTransaction:
    """Return the stored result of a previous import.

    Raises a ConflictError if the stored import was committed with
    different content (collection, method or items) than the request
    being replayed.
    """
    if receipt["content_hash"] != content_hash:
        raise ConflictError(
            f"Bulk import request {request_id!r} was already committed with "
            "different content; use a new request id for a new import."
        )
    return receipt["result"]


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
class BaseIdempotentBulkTransactionsClient(BaseBulkTransactionsClient):
    """Bulk transactions client with idempotent import support (synchronous).

    Only implement this interface if the backend can commit the items and
    the import receipt in a single transaction, so that a bulk import is
    always recorded exactly once. When `Items.request_id` is set, the
    extension calls `bulk_item_insert` with an additional `content_hash`
    keyword argument (see `bulk_import_fingerprint`); the client must then
    atomically:

    1. store a `BulkImportReceipt` (request id, content hash and per-item
       result) keyed by the collection and request id, and
    2. commit the items,

    and raise `BulkImportReceiptExistsError` if a receipt for the same
    collection and request id already exists (e.g. through a unique
    constraint), rolling back the items as well.
    """

    @abc.abstractmethod
    def get_bulk_import_receipt(
        self, collection_id: str, request_id: str, **kwargs
    ) -> BulkImportReceipt | None:
        """Return the receipt of a previously committed import.

        Args:
            collection_id: collection the import targeted.
            request_id: client-supplied import request id.

        Returns:
            The stored receipt, or None if no import was committed for
            this collection and request id.
        """
        raise NotImplementedError


@attr.s
class AsyncBaseIdempotentBulkTransactionsClient(AsyncBaseBulkTransactionsClient):
    """Bulk transactions client with idempotent import support (asynchronous).

    Only implement this interface if the backend can commit the items and
    the import receipt in a single transaction, so that a bulk import is
    always recorded exactly once. When `Items.request_id` is set, the
    extension calls `bulk_item_insert` with an additional `content_hash`
    keyword argument (see `bulk_import_fingerprint`); the client must then
    atomically:

    1. store a `BulkImportReceipt` (request id, content hash and per-item
       result) keyed by the collection and request id, and
    2. commit the items,

    and raise `BulkImportReceiptExistsError` if a receipt for the same
    collection and request id already exists (e.g. through a unique
    constraint), rolling back the items as well.
    """

    @abc.abstractmethod
    async def get_bulk_import_receipt(
        self, collection_id: str, request_id: str, **kwargs
    ) -> BulkImportReceipt | None:
        """Return the receipt of a previously committed import.

        Args:
            collection_id: collection the import targeted.
            request_id: client-supplied import request id.

        Returns:
            The stored receipt, or None if no import was committed for
            this collection and request id.
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

    Clients may also supply a "request_id" attribute identifying the import
    request, so that a retry after a network failure returns the result of
    the original import instead of importing the items again:

        {
            "items": { ... },
            "method": "upsert",
            "request_id": "import-2024-05-01-batch-1"
        }

    An import is identified by the collection, the write method and the
    actual item content (see `bulk_import_fingerprint`). When "request_id"
    is set and the client is an (Async)BaseIdempotentBulkTransactionsClient:

    - a retry of an already committed import returns the stored per-item
      result without committing the items again, even if the original
      response was lost because the client disconnected;
    - two identical concurrent requests commit only once: the backend
      records the receipt atomically with the items and raises
      `BulkImportReceiptExistsError` on the losing request, which then
      returns the stored receipt;
    - reusing the same request id with different content (collection,
      method or items) is rejected with a 409 Conflict error.

    On clients that do not support idempotent imports, requests carrying a
    "request_id" are rejected with a 400 Bad Request error.
    """

    client: AsyncBaseBulkTransactionsClient | BaseBulkTransactionsClient = attr.ib()
    conformance_classes: list[str] = attr.ib(default=list())
    schema_href: str | None = attr.ib(default=None)
    route_dependencies: Sequence[Depends] | None = attr.ib(default=None)
    enable_response_models: bool = attr.ib(default=True)

    def _bulk_item_insert_endpoint(
        self,
    ) -> Callable[..., Awaitable[BulkTransaction | Response]]:
        """Build the endpoint function for the bulk_items route.

        Wraps the client's `bulk_item_insert` with the idempotent import
        flow when the request carries a `request_id`.
        """
        insert: Callable[..., Awaitable[Any]] = self.client.bulk_item_insert
        if not inspect.iscoroutinefunction(insert):
            insert = sync_to_async(insert)

        get_receipt: Callable[..., Awaitable[BulkImportReceipt | None]] | None = None
        if isinstance(
            self.client,
            (
                BaseIdempotentBulkTransactionsClient,
                AsyncBaseIdempotentBulkTransactionsClient,
            ),
        ):
            get_receipt = self.client.get_bulk_import_receipt
            if not inspect.iscoroutinefunction(get_receipt):
                get_receipt = sync_to_async(get_receipt)

        async def bulk_item_insert(items: Items, request: Request) -> Any:
            request_id = items.request_id
            if request_id is None:
                return await insert(items, request=request)

            if get_receipt is None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "This backend does not support bulk import request "
                        "ids; remove 'request_id' from the request."
                    ),
                )

            collection_id = request.path_params["collection_id"]
            content_hash = bulk_import_fingerprint(
                collection_id, items.method, items.items
            )

            receipt = await get_receipt(
                collection_id=collection_id, request_id=request_id, request=request
            )
            if receipt is not None:
                return _receipt_result(receipt, content_hash, request_id)

            try:
                return await insert(
                    items, request=request, content_hash=content_hash
                )
            except BulkImportReceiptExistsError:
                # A concurrent identical request committed first; replay its
                # receipt instead of committing the items a second time.
                receipt = await get_receipt(
                    collection_id=collection_id,
                    request_id=request_id,
                    request=request,
                )
                if receipt is None:
                    raise
                return _receipt_result(receipt, content_hash, request_id)

        return bulk_item_insert

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
                self._bulk_item_insert_endpoint(), items_request_model
            ),
            dependencies=self.route_dependencies,
        )
        app.include_router(router, tags=["Bulk Transaction Extension"])
