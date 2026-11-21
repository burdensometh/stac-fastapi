# Tips and Tricks

This page contains a few 'tips and tricks' for getting **stac-fastapi** working in various situations.

## Avoid FastAPI (slow) serialization

When not using Pydantic validation for responses, FastAPI will still use a complex (slow) [serialization process](https://github.com/fastapi/fastapi/discussions/8165).

Starting with stac-fastapi `5.2.0`, we've added `ENABLE_DIRECT_RESPONSE` option to by-pass the default FastAPI serialization by wrapping the endpoint responses into `starlette.Response` classes.

Ref: https://github.com/stac-utils/stac-fastapi-elasticsearch-opensearch/issues/347

## Application Middlewares

By default the `StacApi` class will enable 3 Middlewares (`BrotliMiddleware`, `CORSMiddleware` and `ProxyHeaderMiddleware`). You may want to overwrite the defaults configuration by editing your backend's `app.py`:

```python
from starlette.middleware import Middleware

from stac_fastapi.api.app import StacApi
from starlette.middleware.cors import CORSMiddleware


api = StacApi(
    ...
    middlewares=[
        Middleware(CORSMiddleware, allow_origins=["https://myendpoints.io"])
    ],
    ...
)
```

## Default `includes` in Fields extension (POST request)

The [**Fields** API extension](https://github.com/stac-api-extensions/fields) enables to filter in/out STAC Items keys (e.g `geometry`). The default behavior is to not filter out anything, but this can be overridden by providing a custom `FieldsExtensionPostRequest` class:

```python
from typing import Optional, Set

import attr
from stac_fastapi.extensions import FieldsExtension as FieldsExtensionBase
from stac_fastapi.extensions.fields import request
from pydantic import BaseModel, Field


class PostFieldsExtension(requests.PostFieldsExtension):
    include: Optional[Set[str]] = Field(
        default_factory=lambda: {
            "id",
            "type",
            "stac_version",
            "geometry",
            "bbox",
            "links",
            "assets",
            "properties.datetime",
            "collection",
        }
    )
    exclude: Optional[Set[str]] = set()


class FieldsExtensionPostRequest(BaseModel):
    """Additional fields and schema for the POST request."""

    fields: Optional[PostFieldsExtension] = Field(PostFieldsExtension())


class FieldsExtension(FieldsExtensionBase):
    """Override the POST model"""

    POST = FieldsExtensionPostRequest


from stac_fastapi.api.app import StacApi

stac = StacApi(
    extensions=[
        FieldsExtension()
    ]
)
```

## Sort Extension (v1.1.0)

The Sort extension adds sorting capabilities to the STAC API. As of v1.1.0, it also introduces `/sortables` endpoints that allow clients to discover which fields can be used for sorting.

To fully support the v1.1.0 specification, you must implement a "Sortables Client" that returns the JSON schema defining your sortable fields, and pass it to the extension.

### 1. Implement the Client

Create a class that inherits from `AsyncBaseSortablesClient` (or `BaseSortablesClient` for synchronous backends) and implement the abstract methods for the endpoints you wish to support.

```python
from typing import Any
from starlette.requests import Request
from stac_fastapi.extensions.sort import AsyncBaseSortablesClient


class MySortablesClient(AsyncBaseSortablesClient):
    
    async def get_sortables(self, request: Request | None = None, **kwargs: Any) -> dict[str, Any]:
        """Return sortables for the /search endpoint."""
        return {
            "$id": "https://example.com/sortables",
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "Item Search Sortables",
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "properties.datetime": {"type": "string", "format": "date-time"},
                "properties.cloud_cover": {"type": "number", "minimum": 0, "maximum": 100}
            }
        }

    async def get_collection_sortables(self, collection_id: str, request: Request | None = None, **kwargs: Any) -> dict[str, Any]:
        """Return sortables for the /collections/{collection_id}/items endpoint."""
        # You can dynamically return schemas based on the collection_id
        return {
            "$id": f"https://example.com/collections/{collection_id}/sortables",
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "properties.datetime": {"type": "string", "format": "date-time"}
            }
        }

    async def get_collections_sortables(self, request: Request | None = None, **kwargs: Any) -> dict[str, Any]:
        """Return sortables for the /collections endpoint."""
        return {
            "$id": "https://example.com/collections-sortables",
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "title": {"type": "string"}
            }
        }
```

### 2. Register the Extension

You can register the standard `SortExtension` to apply sorting and sortables to all endpoints (Item Search, OGC API Features, and Collection Search):

```python
from stac_fastapi.api.app import StacApi
from stac_fastapi.extensions.sort import SortExtension

sortables_client = MySortablesClient()

api = StacApi(
    # ... other configurations ...
    extensions=[
        SortExtension(client=sortables_client)
    ]
)
```

(Note: If you omit the `client` argument, the extension will gracefully degrade to the older specification, providing `?sortby` support without the `/sortables` endpoints.)

### 3. Granular Extensions

If your backend only supports sorting on specific endpoints, you can use the granular extensions instead of the global `SortExtension`. These will only mount the routes and advertise the conformance classes for the specific endpoints they target:

- `SearchSortExtension`: Applies to `/search` (and `/sortables`).
- `ItemCollectionSortExtension`: Applies to `/collections/{id}/items` (and `/collections/{id}/sortables`).
- `CollectionSearchSortExtension`: Applies to `/collections` (and `/collections-sortables`).

```python
from stac_fastapi.extensions.sort import (
    SearchSortExtension,
    ItemCollectionSortExtension
)

api = StacApi(
    # ... other configurations ...
    extensions=[
        # Only support sorting on /search and /collections/{id}/items
        SearchSortExtension(client=sortables_client),
        ItemCollectionSortExtension(client=sortables_client),
    ]
)
```

## Prometheus metrics

Install the optional dependency and enable metrics on `StacApi` to expose
`{router_prefix}/_mgmt/metrics` with low-cardinality STAC `operation` labels
(`search`, `get_item`, …):

```bash
pip install "stac-fastapi-api[metrics]"
```

```python
from stac_fastapi.api.app import StacApi

api = StacApi(
    settings=settings,
    client=client,
    add_metrics=True,
)
```

`/_mgmt/*` routes (ping, health, metrics) are excluded from instrumentation.

Unmatched `/catalogs*` paths fall back to `operation="catalog"`. Backends with extra routes (for example multi-tenant catalogs) should register explicit labels with `register_operations` before or at app construction:

```python
from stac_fastapi.api.metrics import register_operations

register_operations({
    ("GET", "/viewer"): "viewer",
    ("GET", "/catalogs/{catalog_id}/collections"): "list_collections",
})
```

## Partial pushdown for backends without full CQL2

Some backends can fetch candidates by collection, id, bounding box or time, but
cannot execute the full CQL2 filter language. Refusing such filters loses a lot
of functionality; ignoring the unsupported part silently returns Items the
client did not ask for. `stac_fastapi.types.cql2` and
`stac_fastapi.types.partial` let a backend do *partial pushdown* instead:

1. split the filter into a *pushdown* fragment the backend can execute and a
   *residual* fragment it cannot;
2. fetch candidates for the pushdown (and the request selectors);
3. evaluate the residual locally on the candidates.

The split never drops a predicate. `AND` pushes every term it can and keeps the
rest as a local condition; `OR` is only pushed when *every* branch is pushable
(a partial `OR` would wrongly restrict results); `NOT` is only pushed when its
operand is pushable in full. A predicate that cannot be evaluated locally and
cannot be pushed is rejected with `UnsupportedCql2Function` (a `400`), never
silently dropped.

Declare what the backend can execute and let the engine handle pagination. Only
Items that pass the residual filter count towards `limit`, and extra candidate
pages are read as needed:

```python
from stac_fastapi.types.cql2 import PushdownCapabilities
from stac_fastapi.types.partial import (
    CandidatePage,
    PartialPushdownSearch,
    to_item_collection,
)

CAPABILITIES = PushdownCapabilities(
    operators={"=", "<", ">", "<=", ">="},
    properties={"collection", "id", "datetime"},
)


def fetch(request, limit, token):
    # ``request`` carries ``pushdown`` plus ``collections``/``ids``/``bbox``/
    # ``datetime``; run the pushdown however the backend likes.
    features, next_token, total = my_db.search(
        collections=request.collections,
        datetime=request.datetime,
        filter=request.pushdown,
        limit=limit,
        token=token,
    )
    return CandidatePage(
        features=features,
        next_token=next_token,
        number_matched=total,
    )


def post_search(search_request, **kwargs):
    search = PartialPushdownSearch.from_search_request(
        search_request, capabilities=CAPABILITIES
    )
    page = search.page_sync(
        fetch,
        limit=search_request.limit or 10,
        token=getattr(search_request, "token", None),
    )
    return to_item_collection(page, links=[])
```

`numberMatched` is reported only when it is consistent with the search: for an
exact pushdown it comes from the backend, and when the candidates are drained
the local match count is exact. If a residual filter is present but not every
candidate was read, the count is reported as unknown (`None`, omitted from
`to_item_collection`) rather than as the number of candidates. Pass
`count_all=True` to read every candidate and report an exact count instead.
