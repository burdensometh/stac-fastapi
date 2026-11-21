"""Transaction extension."""

import inspect
from collections.abc import Callable, Sequence
from enum import StrEnum
from typing import Annotated, Any

import attr
from fastapi import APIRouter, Body, FastAPI, Header, Query
from fastapi.params import Depends
from pydantic import TypeAdapter
from stac_pydantic import Collection, Item, ItemCollection
from stac_pydantic.shared import MimeTypes
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import NoMatchFound

from stac_fastapi.api.models import CollectionUri, ItemUri, JSONResponse
from stac_fastapi.api.routes import create_async_endpoint
from stac_fastapi.types.config import ApiSettings
from stac_fastapi.types.extension import ApiExtension

from .client import AsyncBaseTransactionsClient, BaseTransactionsClient
from .request import PartialCollection, PartialItem, PatchOperation


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
    """Patch Item.

    A conditional patch carries the version of the Item the client read: in the
    ``If-Match`` header, as a normal `ETag` read, or in the
    ``expected_version`` query parameter (``version`` is accepted as an
    alias).  A patch which carries none of them is applied as before.
    """

    patch: Annotated[
        PartialItem | list[PatchOperation],
        Body(),
    ] = attr.ib(default=None)
    if_match: Annotated[
        str | None,
        Header(
            alias="If-Match",
            description=(
                "Patch the item only if its current version is listed here. "
                "Read the item to get its version from the ETag response header."
            ),
        ),
    ] = attr.ib(default=None)
    expected_version: Annotated[
        str | None,
        Query(
            description=(
                "Patch the item only if its current version is this value. "
                "Same as sending the version in If-Match."
            ),
        ),
    ] = attr.ib(default=None)
    version: Annotated[
        str | None,
        Query(
            description=(
                "Alias of `expected_version`: patch the item only if its current "
                "version is this value."
            ),
        ),
    ] = attr.ib(default=None)

    def kwargs(self) -> dict:
        """Transform api request params into the endpoint's arguments.

        A version is forwarded only when the client sent one, so a request
        without a version calls `patch_item` exactly as before.
        """
        params: dict[str, Any] = {
            "collection_id": self.collection_id,
            "item_id": self.item_id,
            "patch": self.patch,
        }
        expected_version = self.expected_version or self.version or self.if_match
        if expected_version is not None:
            params["expected_version"] = expected_version
        return params


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


def _patch_item_endpoint(func: Callable) -> Callable:
    """Return a `patch_item` endpoint which forwards a version when it can.

    The version is a newer argument of `patch_item`.  A client written before
    it was added does not accept it, so it is forwarded only when the client's
    signature takes it (or `**kwargs`); such a client applies the patch without
    the version, as it did before.
    """
    try:
        parameters = inspect.signature(func).parameters
    except (TypeError, ValueError):
        # An uninspectable callable: assume it takes what it is given.
        return func
    takes_version = "expected_version" in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    if takes_version:
        return func

    def without_version(kwargs: dict) -> dict:
        """Drop the version a legacy client cannot accept."""
        kwargs.pop("expected_version", None)
        return kwargs

    def patch_without_version(
        collection_id: str,
        item_id: str,
        patch: PartialItem | list[PatchOperation],
        **kwargs,
    ):
        return func(
            collection_id=collection_id,
            item_id=item_id,
            patch=patch,
            **without_version(kwargs),
        )

    if inspect.iscoroutinefunction(func):

        async def patch_without_version_async(
            collection_id: str,
            item_id: str,
            patch: PartialItem | list[PatchOperation],
            **kwargs,
        ):
            return await func(
                collection_id=collection_id,
                item_id=item_id,
                patch=patch,
                **without_version(kwargs),
            )

        return patch_without_version_async
    return patch_without_version


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
            endpoint=create_async_endpoint(self.client.update_item, PutItem),
            dependencies=self.route_dependencies,
        )

    def register_patch_item(self):
        """Register patch item endpoint (PATCH
        /collections/{collection_id}/items/{item_id}).

        The endpoint passes the version the client sent -- in `If-Match` or in
        the `expected_version` query parameter -- to the client, which compares
        it against the stored item and applies the patch only if they match.
        A mismatch raises `VersionConflictError`, which the API answers with
        409 without writing anything.
        """
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
                    "headers": {
                        "ETag": {
                            "description": "The version of the patched Item.",
                            "schema": {"type": "string"},
                        }
                    },
                },
                409: {
                    "description": (
                        "Conflict: the Item changed since the expected version; "
                        "nothing was written."
                    ),
                    "content": {
                        MimeTypes.json.value: {},
                    },
                },
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
            endpoint=create_async_endpoint(
                _patch_item_endpoint(self.client.patch_item),
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
            endpoint=create_async_endpoint(self.client.delete_item, ItemUri),
            dependencies=self.route_dependencies,
        )

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
            endpoint=create_async_endpoint(self.client.update_collection, PutCollection),
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
            endpoint=create_async_endpoint(
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
