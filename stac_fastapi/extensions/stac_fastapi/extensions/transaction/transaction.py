"""Transaction extension."""

import inspect
import logging
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Annotated, Any

import attr
from fastapi import APIRouter, Body, FastAPI
from fastapi.params import Depends
from pydantic import TypeAdapter
from stac_pydantic import Collection, Item, ItemCollection
from stac_pydantic.shared import MimeTypes
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import NoMatchFound

from stac_fastapi.api.models import CollectionUri, ItemUri, JSONResponse
from stac_fastapi.api.routes import create_async_endpoint, sync_to_async
from stac_fastapi.types.config import ApiSettings
from stac_fastapi.types.extension import ApiExtension
from stac_fastapi.types.extents import (
    aget_collection_items,
    as_dict,
    extent_maintenance_scope,
    get_collection_items,
    maintenance_enabled,
    refresh_collection_extent,
    resolve_core_client,
)

from .client import AsyncBaseTransactionsClient, BaseTransactionsClient
from .request import PartialCollection, PartialItem, PatchOperation

logger = logging.getLogger(__name__)


class TransactionConformanceClasses(StrEnum):
    """Conformance classes for the Transaction extension.

    See https://github.com/stac-api-extensions/transaction

    """

    ITEMS = "https://api.stacspec.org/v1.0.0/ogcapi-features/extensions/transaction"
    COLLECTIONS = "https://api.stacspec.org/v1.0.0/collections/extensions/transaction"


@attr.s
class PostItem(CollectionUri):
    """Create Item."""

    item: Annotated[Item | ItemCollection, Body()] = attr.ib(default=None)


@attr.s
class PutItem(ItemUri):
    """Update Item."""

    item: Annotated[Item, Body()] = attr.ib(default=None)


@attr.s
class PatchItem(ItemUri):
    """Patch Item."""

    patch: Annotated[
        PartialItem | list[PatchOperation],
        Body(),
    ] = attr.ib(default=None)


@attr.s
class PutCollection(CollectionUri):
    """Update Collection."""

    collection: Annotated[Collection, Body()] = attr.ib(default=None)


@attr.s
class PatchCollection(CollectionUri):
    """Patch Collection."""

    patch: Annotated[
        PartialCollection | list[PatchOperation],
        Body(),
    ] = attr.ib(default=None)


_patch_item_schema = TypeAdapter(list[PatchOperation]).json_schema() | {
    "examples": [
        [
            {
                "op": "add",
                "path": "/properties/foo",
                "value": "bar",
            },
            {
                "op": "replace",
                "path": "/properties/foo",
                "value": "bar",
            },
            {
                "op": "test",
                "path": "/properties/foo",
                "value": "bar",
            },
            {
                "op": "copy",
                "path": "/properties/foo",
                "from": "/properties/bar",
            },
            {
                "op": "move",
                "path": "/properties/foo",
                "from": "/properties/bar",
            },
            {
                "op": "remove",
                "path": "/properties/foo",
            },
        ]
    ]
}
# ref: https://github.com/pydantic/pydantic/issues/889
_patch_item_schema_dict: dict[str, Any] = _patch_item_schema
_patch_item_schema_dict["items"]["anyOf"] = list(
    _patch_item_schema_dict["$defs"].values()
)

_patch_collection_schema = TypeAdapter(list[PatchOperation]).json_schema() | {
    "examples": [
        [
            {
                "op": "add",
                "path": "/summeries/foo",
                "value": "bar",
            },
            {
                "op": "replace",
                "path": "/summeries/foo",
                "value": "bar",
            },
            {
                "op": "test",
                "path": "/summeries/foo",
                "value": "bar",
            },
            {
                "op": "copy",
                "path": "/summeries/foo",
                "from": "/summeries/bar",
            },
            {
                "op": "move",
                "path": "/summeries/foo",
                "from": "/summeries/bar",
            },
            {
                "op": "remove",
                "path": "/summeries/foo",
            },
        ]
    ]
}
# ref: https://github.com/pydantic/pydantic/issues/889
_patch_collection_schema_dict: dict[str, Any] = _patch_collection_schema
_patch_collection_schema_dict["items"]["anyOf"] = list(
    _patch_collection_schema_dict["$defs"].values()
)


def _location_url(request: Request, route_name: str, **path_params: str) -> str:
    """Build the canonical URL of a resource for the `Location` header."""
    try:
        return str(request.url_for(route_name, **path_params))
    except NoMatchFound:
        *_, resource_id = path_params.values()
        return f"{request.base_url}{request.url.path.lstrip('/')}/{resource_id}"


def _accepts_argument(method: Any, name: str) -> bool:
    """Return whether ``method`` accepts a keyword argument called ``name``."""
    try:
        parameters = inspect.signature(method).parameters
    except (TypeError, ValueError):
        return True
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return True
    return name in parameters


def _coerce_collection(collection: Any, parameter: Any) -> Any:
    """Convert ``collection`` to the type expected by ``parameter``.

    Transactions clients in the wild accept either a ``Collection`` model or a
    plain mapping.  When the annotation is known, mirror it; otherwise pass the
    value through unchanged.
    """
    annotation = getattr(parameter, "annotation", None)
    if annotation is Collection and not isinstance(collection, Collection):
        return Collection.model_validate(collection)
    return collection


@attr.s
class TransactionExtension(ApiExtension):
    """Transaction Extension.

    The transaction extension adds several endpoints which allow the creation,
    deletion, and updating of items and collections:
        POST /collections
        PUT /collections/{collection_id}
        DELETE /collections/{collection_id}
        POST /collections/{collection_id}/items
        PUT /collections/{collection_id}/items
        DELETE /collections/{collection_id}/items

    https://github.com/stac-api-extensions/transaction
    https://github.com/stac-api-extensions/collection-transaction

    Attributes:
        client: CRUD application logic

    """

    client: AsyncBaseTransactionsClient | BaseTransactionsClient = attr.ib()
    settings: ApiSettings = attr.ib()
    core_client: Any = attr.ib(default=None)
    conformance_classes: list[str] = attr.ib(
        factory=lambda: [
            TransactionConformanceClasses.ITEMS,
            TransactionConformanceClasses.COLLECTIONS,
        ]
    )
    schema_href: str | None = attr.ib(default=None)
    router: APIRouter = attr.ib(factory=APIRouter)
    response_class: type[Response] = attr.ib(default=JSONResponse)
    route_dependencies: Sequence[Depends] | None = attr.ib(default=None)

    # ------------------------------------------------------------------
    # Collection extent maintenance (opt-in)
    # ------------------------------------------------------------------

    def _extent_maintenance_enabled(self) -> bool:
        return maintenance_enabled(self.settings)

    def _resolve_core_client(self, request: Request | None) -> Any:
        """Find the client used to read the Items of a collection."""
        if self.core_client is not None:
            return self.core_client
        for source in (request, self.client):
            resolved = resolve_core_client(source)
            if resolved is not None:
                return resolved
        return None

    def _is_client_async(self) -> bool:
        return inspect.iscoroutinefunction(self.client.create_item)

    @staticmethod
    def _call_read(
        client: Any, method_name: str, collection_id: str, request: Request | None
    ) -> Any:
        """Call a read method, forwarding ``request`` only when accepted."""
        method = getattr(client, method_name)
        if request is not None and _accepts_argument(method, "request"):
            return method(collection_id, request=request)
        return method(collection_id)

    def _persist_collection(
        self, collection: Any, collection_id: str, request: Request | None
    ) -> Any:
        """Store an updated collection using the client's calling convention.

        Backends accept either ``update_collection(collection_id, collection)``
        or ``update_collection(collection)``, and may expect a pydantic model
        rather than a mapping.  The signature of the actual method decides how
        the collection is passed.
        """
        method = self.client.update_collection
        try:
            parameters: Mapping[str, inspect.Parameter] = inspect.signature(
                method
            ).parameters
        except (TypeError, ValueError):
            parameters = {}

        accepts_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )

        collection_parameter = parameters.get("collection")
        payload = _coerce_collection(collection, collection_parameter)

        args: list[Any] = []
        if "collection_id" in parameters:
            args.append(collection_id)
        args.append(payload)

        kwargs: dict[str, Any] = {}
        if request is not None and (accepts_kwargs or "request" in parameters):
            kwargs["request"] = request
        return method(*args, **kwargs)

    def _refresh_extent_sync(self, request: Request | None, collection_id: str) -> None:
        """Recompute the extent of ``collection_id`` from its stored Items."""
        core_client = self._resolve_core_client(request)
        if core_client is None:
            return
        with extent_maintenance_scope():
            items = get_collection_items(core_client, collection_id, request=request)
            collection = self._call_read(
                core_client, "get_collection", collection_id, request
            )
            if collection is None:
                return
            updated = refresh_collection_extent(collection, items)
            self._persist_collection(updated, collection_id, request)

    async def _refresh_extent_async(
        self, request: Request | None, collection_id: str
    ) -> None:
        """Asynchronous variant of :meth:`_refresh_extent_sync`."""
        core_client = self._resolve_core_client(request)
        if core_client is None:
            return
        with extent_maintenance_scope():
            items = await aget_collection_items(
                core_client, collection_id, request=request
            )
            collection = self._call_read(
                core_client, "get_collection", collection_id, request
            )
            if inspect.isawaitable(collection):
                collection = await collection
            if collection is None:
                return
            updated = refresh_collection_extent(collection, items)
            result = self._persist_collection(updated, collection_id, request)
            if inspect.isawaitable(result):
                await result

    async def _maintain_extent(
        self, request: Request | None, collection_id: str | None
    ) -> None:
        """Recompute a collection's extent after an Item write, if enabled.

        Failures are logged and never fail the write: the Item itself has been
        committed and must keep its response.
        """
        if not collection_id or not self._extent_maintenance_enabled():
            return
        try:
            if self._is_client_async():
                await self._refresh_extent_async(request, collection_id)
            else:
                await run_in_threadpool(self._refresh_extent_sync, request, collection_id)
        except Exception:  # noqa: BLE001 - a write must not fail on maintenance
            logger.exception(
                "Could not update the extent of collection %s", collection_id
            )

    def _item_endpoint(self, method: Any, request_model: type):
        """Wrap an Item write so the collection extent follows the write.

        The endpoint only recomputes the extent when the write succeeded and
        maintenance is enabled.  Request parsing, response wrapping and
        ``enable_direct_response`` handling are unchanged: the wrapper is built
        with ``create_async_endpoint``, exactly like the unwrapped endpoint.
        """
        call = method if inspect.iscoroutinefunction(method) else sync_to_async(method)

        async def endpoint(*args: Any, **kwargs: Any) -> Any:
            request = kwargs.get("request")
            result = await call(*args, **kwargs)
            status = result.status_code if isinstance(result, Response) else 200
            if status < 400:
                await self._maintain_extent(
                    request, self._collection_id_from_call(args, kwargs)
                )
            return result

        return create_async_endpoint(endpoint, request_model)

    @staticmethod
    def _collection_id_from_call(args: tuple, kwargs: dict) -> str | None:
        """Extract ``collection_id`` from how the endpoint received its request."""
        for value in args:
            collection_id = getattr(value, "collection_id", None)
            if collection_id:
                return str(collection_id)
        collection_id = kwargs.get("collection_id")
        return str(collection_id) if collection_id else None

    def register_create_item(self):
        """Register create item endpoint (POST /collections/{collection_id}/items)."""
        endpoint = create_async_endpoint(self.client.create_item, PostItem)

        async def create_item(
            request: Request,
            response: Response,
            request_data=Depends(PostItem),
        ):
            """Create item endpoint with Location header.

            The Transaction extension requires a `Location` header with the URI
            of the newly created Item for single-Item payloads.
            """
            item = await endpoint(request, request_data)
            if request_data.item.type == "Feature":
                item_id = (
                    item.get("id")
                    if isinstance(item, dict)
                    else getattr(item, "id", None)
                ) or request_data.item.id
                target = item if isinstance(item, Response) else response
                if (
                    item_id
                    and (target.status_code or 200) < 400
                    and "location" not in target.headers
                ):
                    target.headers["Location"] = _location_url(
                        request,
                        "Get Item",
                        collection_id=request_data.collection_id,
                        item_id=item_id,
                    )
            target = item if isinstance(item, Response) else response
            if (target.status_code or 200) < 400:
                await self._maintain_extent(request, request_data.collection_id)
            return item

        self.router.add_api_route(
            name="Create Item",
            path="/collections/{collection_id}/items",
            status_code=201,
            response_model=(Item | ItemCollection)
            if self.settings.enable_response_models
            else None,
            responses={
                201: {
                    "content": {
                        MimeTypes.geojson.value: {},
                    },
                    "model": Item | ItemCollection,
                }
            },
            response_class=self.response_class,
            response_model_exclude_unset=True,
            response_model_exclude_none=True,
            methods=["POST"],
            endpoint=create_item,
            dependencies=self.route_dependencies,
        )

    def register_update_item(self):
        """Register update item endpoint (PUT
        /collections/{collection_id}/items/{item_id})."""
        self.router.add_api_route(
            name="Update Item",
            path="/collections/{collection_id}/items/{item_id}",
            response_model=Item if self.settings.enable_response_models else None,
            responses={
                200: {
                    "content": {
                        MimeTypes.geojson.value: {},
                    },
                    "model": Item,
                }
            },
            response_class=self.response_class,
            response_model_exclude_unset=True,
            response_model_exclude_none=True,
            methods=["PUT"],
            endpoint=self._item_endpoint(self.client.update_item, PutItem),
            dependencies=self.route_dependencies,
        )

    def register_patch_item(self):
        """Register patch item endpoint (PATCH
        /collections/{collection_id}/items/{item_id})."""
        self.router.add_api_route(
            name="Patch Item",
            path="/collections/{collection_id}/items/{item_id}",
            response_model=Item if self.settings.enable_response_models else None,
            responses={
                200: {
                    "content": {
                        MimeTypes.geojson.value: {},
                    },
                    "model": Item,
                }
            },
            openapi_extra={
                "requestBody": {
                    "content": {
                        "application/json-patch+json": {
                            "schema": _patch_item_schema,
                        },
                        "application/merge-patch+json": {
                            "schema": PartialItem.model_json_schema(),
                        },
                        "application/json": {
                            "schema": PartialItem.model_json_schema(),
                        },
                    },
                    "required": True,
                },
            },
            response_class=self.response_class,
            response_model_exclude_unset=True,
            response_model_exclude_none=True,
            methods=["PATCH"],
            endpoint=self._item_endpoint(
                self.client.patch_item,
                PatchItem,
            ),
            dependencies=self.route_dependencies,
        )

    def register_delete_item(self):
        """Register delete item endpoint (DELETE
        /collections/{collection_id}/items/{item_id})."""
        self.router.add_api_route(
            name="Delete Item",
            path="/collections/{collection_id}/items/{item_id}",
            response_model=Item if self.settings.enable_response_models else None,
            responses={
                200: {
                    "content": {
                        MimeTypes.geojson.value: {},
                    },
                    "model": Item,
                }
            },
            response_class=self.response_class,
            response_model_exclude_unset=True,
            response_model_exclude_none=True,
            methods=["DELETE"],
            endpoint=self._item_endpoint(self.client.delete_item, ItemUri),
            dependencies=self.route_dependencies,
        )

    def _collection_endpoint(self, method: Any, request_model: type):
        """Wrap a Collection write to recompute its extent afterwards.

        When maintenance is enabled the extent is recomputed from the Items the
        collection contains, so that an update submitted by a client cannot
        reintroduce a stale extent.
        """
        endpoint = create_async_endpoint(method, request_model)

        async def wrapped(
            request: Request,
            request_data=Depends(request_model),
        ):
            result = await endpoint(request, request_data)
            status = result.status_code if isinstance(result, Response) else 200
            if status < 400:
                collection_id = self._collection_id(request_data, result)
                await self._maintain_extent(request, collection_id)
            return result

        return wrapped

    @staticmethod
    def _collection_id(request_data: Any, result: Any) -> str | None:
        values = [result, request_data]
        collection = getattr(request_data, "collection", None)
        if collection is not None:
            values.append(collection)
        collection_id = getattr(request_data, "collection_id", None)
        if collection_id:
            values.append({"id": collection_id})
        for value in values:
            if value is None:
                continue
            data = as_dict(value)
            if isinstance(data, Mapping) and data.get("id"):
                return str(data["id"])
        return None

    def register_create_collection(self):
        """Register create collection endpoint (POST /collections)."""
        endpoint = create_async_endpoint(self.client.create_collection, Collection)

        async def create_collection(
            request: Request,
            response: Response,
            request_data: Collection,
        ):
            """Create collection endpoint with Location header."""
            collection = await endpoint(request, request_data)
            collection_id = (
                collection.get("id")
                if isinstance(collection, dict)
                else getattr(collection, "id", None)
            ) or request_data.id
            target = collection if isinstance(collection, Response) else response
            if (
                collection_id
                and (target.status_code or 200) < 400
                and "location" not in target.headers
            ):
                target.headers["Location"] = _location_url(
                    request, "Get Collection", collection_id=collection_id
                )
            return collection

        self.router.add_api_route(
            name="Create Collection",
            path="/collections",
            status_code=201,
            response_model=Collection if self.settings.enable_response_models else None,
            responses={
                201: {
                    "content": {
                        MimeTypes.json.value: {},
                    },
                    "model": Collection,
                }
            },
            response_class=self.response_class,
            response_model_exclude_unset=True,
            response_model_exclude_none=True,
            methods=["POST"],
            endpoint=create_collection,
            dependencies=self.route_dependencies,
        )

    def register_update_collection(self):
        """Register update collection endpoint (PUT /collections/{collection_id})."""
        self.router.add_api_route(
            name="Update Collection",
            path="/collections/{collection_id}",
            response_model=Collection if self.settings.enable_response_models else None,
            responses={
                200: {
                    "content": {
                        MimeTypes.json.value: {},
                    },
                    "model": Collection,
                }
            },
            response_class=self.response_class,
            response_model_exclude_unset=True,
            response_model_exclude_none=True,
            methods=["PUT"],
            endpoint=self._collection_endpoint(
                self.client.update_collection, PutCollection
            ),
            dependencies=self.route_dependencies,
        )

    def register_patch_collection(self):
        """Register patch collection endpoint (PATCH /collections/{collection_id})."""
        self.router.add_api_route(
            name="Patch Collection",
            path="/collections/{collection_id}",
            response_model=Collection if self.settings.enable_response_models else None,
            responses={
                200: {
                    "content": {
                        MimeTypes.geojson.value: {},
                    },
                    "model": Collection,
                }
            },
            openapi_extra={
                "requestBody": {
                    "content": {
                        "application/json-patch+json": {
                            "schema": _patch_collection_schema,
                        },
                        "application/merge-patch+json": {
                            "schema": PartialCollection.model_json_schema(),
                        },
                        "application/json": {
                            "schema": PartialCollection.model_json_schema(),
                        },
                    },
                    "required": True,
                },
            },
            response_class=self.response_class,
            response_model_exclude_unset=True,
            response_model_exclude_none=True,
            methods=["PATCH"],
            endpoint=self._collection_endpoint(
                self.client.patch_collection,
                PatchCollection,
            ),
            dependencies=self.route_dependencies,
        )

    def register_delete_collection(self):
        """Register delete collection endpoint (DELETE /collections/{collection_id})."""
        self.router.add_api_route(
            name="Delete Collection",
            path="/collections/{collection_id}",
            response_model=Collection if self.settings.enable_response_models else None,
            responses={
                200: {
                    "content": {
                        MimeTypes.json.value: {},
                    },
                    "model": Collection,
                }
            },
            response_class=self.response_class,
            response_model_exclude_unset=True,
            response_model_exclude_none=True,
            methods=["DELETE"],
            endpoint=create_async_endpoint(self.client.delete_collection, CollectionUri),
            dependencies=self.route_dependencies,
        )

    def register(self, app: FastAPI) -> None:
        """Register the extension with a FastAPI application.

        Args:
            app: target FastAPI application.

        Returns:
            None
        """
        self.router.prefix = app.state.router_prefix
        self.register_create_item()
        self.register_update_item()
        self.register_patch_item()
        self.register_delete_item()
        self.register_create_collection()
        self.register_update_collection()
        self.register_patch_collection()
        self.register_delete_collection()
        app.include_router(self.router, tags=["Transaction Extension"])
