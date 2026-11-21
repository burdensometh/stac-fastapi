"""Tests for the ETag middleware."""

import json

import pytest
from starlette.applications import Starlette
from starlette.responses import (
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)
from starlette.routing import Route
from starlette.testclient import TestClient

from stac_fastapi.api.etag import ETagMiddleware, set_etag
from stac_fastapi.types.etag import item_version, quote_etag

ITEM = {
    "type": "Feature",
    "stac_version": "1.0.0",
    "id": "test_item",
    "geometry": {"type": "Point", "coordinates": [-105, 40]},
    "bbox": [-105, 40, -105, 40],
    "properties": {"datetime": "2020-06-13T13:00:00Z"},
    "links": [],
    "assets": {},
    "collection": "test_collection",
}


def build_app() -> Starlette:
    async def get_item(request):
        return JSONResponse(ITEM, media_type="application/geo+json")

    async def get_collection(request):
        return JSONResponse({"type": "Collection", "id": "test"})

    async def get_items(request):
        return JSONResponse(
            {"type": "FeatureCollection", "features": [ITEM], "links": []},
            media_type="application/geo+json",
        )

    async def get_plain(request):
        return PlainTextResponse("hello")

    async def get_error(request):
        return JSONResponse({"detail": "nope"}, status_code=404)

    async def get_with_etag(request):
        return JSONResponse(ITEM, headers={"ETag": '"custom"'})

    async def get_recorded(request):
        set_etag(request, "the-version")
        return JSONResponse(ITEM)

    async def get_stream(request):
        async def body():
            yield json.dumps(ITEM).encode()

        return StreamingResponse(body(), media_type="application/geo+json")

    app = Starlette(
        routes=[
            Route("/collections/c/items/{item_id}", get_item, name="Get Item"),
            Route("/collections/{collection_id}", get_collection, name="Get Collection"),
            Route("/collections/c/items", get_items, name="Get ItemCollection"),
            Route("/plain", get_plain, name="Plain"),
            Route("/error", get_error, name="Error"),
            Route("/custom", get_with_etag, name="Custom"),
            Route("/recorded", get_recorded, name="Recorded"),
            Route("/stream", get_stream, name="Stream"),
        ]
    )
    return ETagMiddleware(app)


@pytest.fixture
def client() -> TestClient:
    return TestClient(build_app())


def test_item_response_gets_a_version(client: TestClient) -> None:
    response = client.get("/collections/c/items/i")
    assert response.headers["etag"] == quote_etag(item_version(ITEM))


def test_non_item_response_gets_no_version(client: TestClient) -> None:
    assert "etag" not in client.get("/collections/c").headers
    assert "etag" not in client.get("/collections/c/items").headers


def test_plain_text_response_gets_no_version(client: TestClient) -> None:
    assert "etag" not in client.get("/plain").headers


def test_error_response_gets_no_version(client: TestClient) -> None:
    assert "etag" not in client.get("/error").headers


def test_existing_etag_is_preserved(client: TestClient) -> None:
    assert client.get("/custom").headers["etag"] == '"custom"'


def test_recorded_version_is_used(client: TestClient) -> None:
    assert client.get("/recorded").headers["etag"] == '"the-version"'


def test_streaming_response_is_forwarded(client: TestClient) -> None:
    response = client.get("/stream")
    assert response.headers["content-type"].startswith("application/geo+json")


def test_body_is_unchanged(client: TestClient) -> None:
    response = client.get("/collections/c/items/i")
    assert response.json() == ITEM


def test_compressed_item_gets_no_version() -> None:
    """The body of a compressed response is not the Item document."""
    from starlette.middleware import Middleware
    from starlette.middleware.gzip import GZipMiddleware

    async def get_item(request):
        return JSONResponse(ITEM, media_type="application/geo+json")

    app = Starlette(
        routes=[Route("/items/{item_id}", get_item, name="Get Item")],
        middleware=[Middleware(GZipMiddleware, minimum_size=1)],
    )

    with TestClient(ETagMiddleware(app)) as client:
        response = client.get("/items/i", headers={"Accept-Encoding": "gzip"})
        assert response.headers.get("content-encoding") == "gzip"
        assert "etag" not in response.headers


def test_middleware_is_added_below_a_compression_middleware() -> None:
    """`StacApi` keeps the ETag middleware innermost, so it reads the raw body."""
    from starlette.middleware import Middleware
    from starlette.middleware.cors import CORSMiddleware

    from stac_fastapi.api.app import StacApi
    from stac_fastapi.types.config import ApiSettings
    from stac_fastapi.types.core import BaseCoreClient

    class ItemClient(BaseCoreClient):
        def get_item(self, item_id, collection_id, **kwargs):
            return ITEM

        def all_collections(self, *args, **kwargs):
            raise NotImplementedError

        def get_collection(self, *args, **kwargs):
            raise NotImplementedError

        def get_search(self, *args, **kwargs):
            raise NotImplementedError

        def post_search(self, *args, **kwargs):
            raise NotImplementedError

        def item_collection(self, *args, **kwargs):
            raise NotImplementedError

    api = StacApi(
        settings=ApiSettings(),
        client=ItemClient(),
        middlewares=[Middleware(CORSMiddleware, allow_origins=["*"])],
    )
    with TestClient(api.app) as client:
        response = client.get("/collections/c/items/i")

    assert response.headers["etag"] == quote_etag(item_version(ITEM))
