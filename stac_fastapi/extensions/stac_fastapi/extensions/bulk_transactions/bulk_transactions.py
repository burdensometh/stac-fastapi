"""Bulk transactions extension."""

import abc
import inspect
from collections.abc import Sequence
from enum import StrEnum
from typing import Any, TypedDict

import attr
from fastapi import APIRouter, Depends, FastAPI, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from stac_fastapi.api.models import create_request_model
from stac_fastapi.api.routes import create_async_endpoint
from stac_fastapi.types.extension import ApiExtension
from stac_fastapi.types.rebase import HrefRebaser


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
    """

    client: AsyncBaseBulkTransactionsClient | BaseBulkTransactionsClient = attr.ib()
    conformance_classes: list[str] = attr.ib(default=list())
    schema_href: str | None = attr.ib(default=None)
    route_dependencies: Sequence[Depends] | None = attr.ib(default=None)
    enable_response_models: bool = attr.ib(default=True)
    prefix_mappings: dict[str, str] | None = attr.ib(default=None)

    def _rebase_items(self, items: Items) -> Items:
        """Rebase relative hrefs of every Item before the bulk insert is stored.

        The same rules as the Transaction extension's single-Item import apply,
        so both import paths store identical records. See
        :class:`stac_fastapi.types.rebase.HrefRebaser`.
        """
        if not self.prefix_mappings:
            return items

        rebaser = HrefRebaser(prefix_mappings=self.prefix_mappings)
        return items.model_copy(
            update={
                "items": {
                    item_id: rebaser.rebase_item(item)
                    for item_id, item in items.items.items()
                }
            }
        )

    def register(self, app: FastAPI) -> None:
        """Register the extension with a FastAPI application.

        Args:
            app: target FastAPI application.

        Returns:
            None
        """
        items_request_model = create_request_model("Items", base_model=Items)

        async def bulk_item_insert(items, **kwargs):
            """Rebase imported Items, then hand them to the client."""
            result = self.client.bulk_item_insert(self._rebase_items(items), **kwargs)
            if inspect.isawaitable(result):
                result = await result
            return result

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
            endpoint=create_async_endpoint(bulk_item_insert, items_request_model),
            dependencies=self.route_dependencies,
        )
        app.include_router(router, tags=["Bulk Transaction Extension"])
