"""Route factories."""

import copy
import functools
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

from fastapi import Depends, FastAPI, params
from fastapi.datastructures import DefaultPlaceholder
from fastapi.dependencies.utils import get_dependant, get_parameterless_sub_dependant
from fastapi.routing import APIRoute, request_response
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Match
from starlette.status import HTTP_204_NO_CONTENT

from stac_fastapi.types.config import Settings
from stac_fastapi.types.fields import get_fields_projection, trim_payload
from stac_fastapi.types.search import APIRequest


def _wrap_response(resp: Any) -> Any:
    if resp is not None:
        return resp
    else:  # None is returned as 204 No Content
        return Response(status_code=HTTP_204_NO_CONTENT)


def _response_models_enabled() -> bool:
    """Whether response-model validation is on for this application."""
    try:
        return Settings.get().enable_response_models
    except ValueError:  # Settings not initialised (e.g. a bare FastAPI app)
        return False


def _apply_fields(request_data: Any, resp: Any) -> Any:
    """Trim a dict payload to the fields the client asked for.

    Backends that support column projection already return only the requested
    fields, and trimming is then a no-op. This is what keeps backends that do
    not support projection working: they may return full documents and the API
    layer trims them precisely, including nested paths such as
    ``assets.preview.href``.

    ``Response`` objects are returned untouched: the backend has serialized
    them itself, usually to bypass response-model validation for a projection
    that produces a non STAC-compliant item. When response-model validation is
    enabled, trimming is left to the backend for the same reason (a trimmed
    Item may legitimately not validate), matching the documented contract.
    """
    if resp is None or isinstance(resp, Response):
        return resp
    projection = get_fields_projection(request_data)
    if projection is None:
        return resp
    if _response_models_enabled():
        return resp
    return trim_payload(resp, projection)


def _accepts_fields_projection(func: Callable) -> bool:
    """Whether ``func`` can receive the ``fields_projection`` keyword."""
    try:
        parameters = inspect.signature(func).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters)


def _fields_kwargs(request_data: Any, func: Callable) -> dict[str, Any]:
    """Keyword arguments carrying the resolved field selection to a backend.

    Both ``GET`` and ``POST`` searches reach their client through this helper,
    so a backend sees the same ``fields_projection`` no matter the verb. The
    projection exposes :meth:`FieldsProjection.backend_fields`, the exact set
    of columns to read, which already accounts for the pagination identity and
    for the fields used by filtering and sorting.

    Nothing is injected when the request carries no field selection, when the
    selection is empty, or when the client does not accept extra keyword
    arguments, so backends that never use the Fields extension see an
    unchanged call.
    """
    projection = get_fields_projection(request_data)
    if projection is None or projection.empty:
        return {}
    if not _accepts_fields_projection(func):
        return {}
    return {"fields_projection": projection}


def sync_to_async(func):
    """Run synchronous function asynchronously in a background thread."""

    @functools.wraps(func)
    async def run(*args, **kwargs):
        return await run_in_threadpool(func, *args, **kwargs)

    return run


def create_async_endpoint(
    func: Callable,
    request_model: type[APIRequest] | type[BaseModel] | dict,
) -> Callable[[Any, Any], Awaitable[Any]]:
    """Wrap a function in a coroutine which may be used to create a FastAPI endpoint.

    Synchronous functions are executed asynchronously using a background thread.
    """

    if not inspect.iscoroutinefunction(func):
        func = sync_to_async(func)

    _endpoint: Callable[[Any, Any], Awaitable[Any]]

    if isinstance(request_model, dict):

        async def _endpoint(request: Request, request_data: dict[str, Any]):
            """Endpoint."""
            resp = await func(
                request_data, request=request, **_fields_kwargs(request_data, func)
            )
            return _wrap_response(_apply_fields(request_data, resp))

    elif issubclass(request_model, APIRequest):

        async def _endpoint(request: Request, request_data=Depends(request_model)):  # type: ignore
            """Endpoint."""
            kwargs = request_data.kwargs()
            # Never shadow a value the request model already produced.
            fields_kwargs = {
                key: value
                for key, value in _fields_kwargs(request_data, func).items()
                if key not in kwargs
            }
            resp = await func(request=request, **kwargs, **fields_kwargs)
            return _wrap_response(_apply_fields(request_data, resp))

    elif issubclass(request_model, BaseModel):

        async def _endpoint(request: Request, request_data: request_model):  # type: ignore
            """Endpoint."""
            resp = await func(
                request_data, request=request, **_fields_kwargs(request_data, func)
            )
            return _wrap_response(_apply_fields(request_data, resp))

    else:
        raise ValueError(f"Unsupported type for request model {type(request_model)}")

    return _endpoint


class Scope(TypedDict, total=False):
    """More strict version of Starlette's Scope."""

    # https://github.com/encode/starlette/blob/6af5c515e0a896cbf3f86ee043b88f6c24200bcf/starlette/types.py#L3
    path: str
    method: str
    type: str | None


def _is_endpoint_route(route: BaseRoute) -> bool:
    """Check if route is a standard endpoint (not a mount or sub-router)."""
    return (
        hasattr(route, "path") and hasattr(route, "methods") and route.methods is not None
    )


def _apply_dependencies_to_route(
    route: BaseRoute, scopes: list[Scope], dependencies: list[params.Depends]
) -> None:
    """Apply dependencies to a single route matching the given scopes."""
    for scope in scopes:
        _scope = copy.deepcopy(scope)

        if scope["path"] == "*":
            _scope["path"] = route.path  # type: ignore

        if scope["method"] == "*":
            _scope["method"] = list(route.methods)[0]  # type: ignore

        match, _ = route.matches({"type": "http", **_scope})
        if match != Match.FULL:
            continue

        if not hasattr(route, "dependant"):
            continue

        for depends in dependencies[::-1]:
            route.dependant.dependencies.insert(
                0,
                get_parameterless_sub_dependant(
                    depends=depends,
                    path=route.path_format,  # type: ignore
                ),
            )

        route.dependencies.extend(dependencies)  # type: ignore


def add_route_dependencies(
    routes: list[BaseRoute], scopes: list[Scope], dependencies: list[params.Depends]
) -> None:
    """Add dependencies to routes.

    Allows a developer to add dependencies to a route after the route has been
    defined.

    "*" can be used for path or method to match all allowed routes.

    Returns:
        None
    """
    for route in routes:
        if hasattr(route, "original_router"):
            add_route_dependencies(route.original_router.routes, scopes, dependencies)
            continue

        if hasattr(route, "routes") and route.routes:
            add_route_dependencies(route.routes, scopes, dependencies)
            continue

        if not _is_endpoint_route(route):
            continue

        _apply_dependencies_to_route(route, scopes, dependencies)


def add_direct_response(app: FastAPI) -> None:
    """
    Setup FastAPI application's endpoints to return Response Object directly, avoiding
    Pydantic validation and FastAPI (slow) serialization.

    ref: https://gist.github.com/Zaczero/00f3a2679ebc0a25eb938ed82bc63553
    """

    def wrap_endpoint(endpoint: Callable, cls: type[Response]):
        @functools.wraps(endpoint)
        async def wrapper(*args, **kwargs):
            content = await endpoint(*args, **kwargs)
            return content if isinstance(content, Response) else cls(content)

        return wrapper

    def wrap_routes(routes: list[BaseRoute]) -> None:
        for route in routes:
            # Since FastAPI 0.137, `include_router` keeps the included router
            # instead of copying its routes into `app.routes`.
            if hasattr(route, "original_router"):
                wrap_routes(route.original_router.routes)
                continue

            if not isinstance(route, APIRoute):
                continue

            response_class = route.response_class
            if isinstance(response_class, DefaultPlaceholder):
                response_class = response_class.value

            if issubclass(response_class, Response):
                route.endpoint = wrap_endpoint(route.endpoint, response_class)
                route.dependant = get_dependant(
                    path=route.path_format, call=route.endpoint
                )
                for depends in route.dependencies[::-1]:
                    route.dependant.dependencies.insert(
                        0,
                        get_parameterless_sub_dependant(
                            depends=depends, path=route.path_format
                        ),
                    )
                route.app = request_response(route.get_route_handler())

    wrap_routes(app.routes)
