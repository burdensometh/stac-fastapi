"""End-to-end tests through a real FastAPI application.

These tests exercise the full request path (StacApi + TestClient) to prove the
federation works over HTTP and that the links emitted on records route the
details/items endpoints back to the same real source.
"""

import pytest
from fastapi.testclient import TestClient

from stac_fastapi.api.app import StacApi
from stac_fastapi.api.federation import (
    ConflictResolution,
    FederatedClient,
    add_federation_exception_handlers,
)
from stac_fastapi.api.federation.merge import SOURCE_HINT_HEADER
from stac_fastapi.types.config import ApiSettings

from .fakes import AsyncFakeBackend, SyncFakeBackend, make_collection, make_item


def _build_app(resolution):
    alpha_collections = [
        make_collection("shared", "proprietary-a", bbox=[-10, -10, 10, 10]),
        make_collection("only_a", "MIT"),
    ]
    alpha_items = {
        "shared": [
            make_item("a1", "shared", lon=0.0, lat=0.0),
            make_item("a2", "shared", lon=5.0, lat=5.0),
        ],
        "only_a": [make_item("a3", "only_a")],
    }
    bravo_collections = [
        make_collection("shared", "proprietary-b", bbox=[20, 20, 40, 40]),
        make_collection("only_b", "Apache-2.0"),
    ]
    bravo_items = {
        "shared": [
            make_item("a1", "shared", lon=30.0, lat=30.0, when="2011-06-01T00:00:00Z"),
            make_item("b1", "shared", lon=25.0, lat=25.0, when="2011-05-01T00:00:00Z"),
        ],
        "only_b": [make_item("b2", "only_b", lon=35.0, lat=35.0)],
    }
    client = FederatedClient(
        sources=[
            _source("alpha", SyncFakeBackend("alpha", alpha_collections, alpha_items)),
            _source("bravo", AsyncFakeBackend("bravo", bravo_collections, bravo_items)),
        ],
        conflict_resolution=resolution,
        discover=False,
    )
    api = StacApi(
        settings=ApiSettings(enable_response_models=False),
        client=client,
    )
    add_federation_exception_handlers(api.app)
    return api, client


def _source(source_id, backend):
    from stac_fastapi.api.federation import SourceConfig

    return SourceConfig(source_id=source_id, client=backend)


@pytest.fixture
def priority_app():
    api, _ = _build_app(ConflictResolution.PRIORITY)
    return api


@pytest.fixture
def split_app():
    api, _ = _build_app(ConflictResolution.SPLIT)
    return api


def test_collections_endpoint_priority(priority_app):
    with TestClient(priority_app.app) as client:
        response = client.get("/collections")
    assert response.status_code == 200
    body = response.json()
    ids = sorted(collection["id"] for collection in body["collections"])
    assert ids == ["only_a", "only_b", "shared"]
    coverage = body["federation:coverage"]
    assert coverage["partial"] is False
    assert coverage["resolution"] == "priority"


def test_collection_details_carries_variants(priority_app):
    with TestClient(priority_app.app) as client:
        response = client.get("/collections/shared")
    assert response.status_code == 200
    body = response.json()
    provenance = body["federation:provenance"]
    assert provenance["federation:source"] == "alpha"
    assert {v["source"] for v in provenance["federation:variants"]} == {
        "alpha",
        "bravo",
    }
    # The federation extension is declared on the document.
    assert any(
        ext.endswith("/federation/v1") for ext in body["stac_extensions"]
    )


def test_search_endpoint_merges_and_provenance(priority_app):
    with TestClient(priority_app.app) as client:
        response = client.get("/search", params={"collections": "shared"})
    assert response.status_code == 200
    body = response.json()
    sources = sorted(
        feature["properties"]["federation:source"] for feature in body["features"]
    )
    assert sources.count("alpha") == 2
    assert sources.count("bravo") == 2
    # Same item id from two sources kept distinct.
    a1_sources = {
        feature["properties"]["federation:source"]
        for feature in body["features"]
        if feature["id"] == "a1"
    }
    assert a1_sources == {"alpha", "bravo"}


def test_item_self_link_routes_back_to_same_source(priority_app):
    with TestClient(priority_app.app) as client:
        search = client.get("/search", params={"collections": "shared"})
        bravo_item = next(
            feature
            for feature in search.json()["features"]
            if feature["id"] == "a1"
            and feature["properties"]["federation:source"] == "bravo"
        )
        self_link = next(
            link for link in bravo_item["links"] if link["rel"] == "self"
        )
        # Follow the emitted self link (includes the source hint query param).
        hinted = client.get(self_link["href"])
        assert hinted.status_code == 200
        assert hinted.json()["properties"]["federation:source"] == "bravo"

        # Without the hint, PRIORITY routes to alpha — proving the hint changes
        # routing rather than being decorative.
        plain = client.get("/collections/shared/items/a1")
        assert plain.json()["properties"]["federation:source"] == "alpha"


def test_source_hint_header_routes_item(priority_app):
    with TestClient(priority_app.app) as client:
        response = client.get(
            "/collections/shared/items/a1",
            headers={SOURCE_HINT_HEADER: "bravo"},
        )
    assert response.status_code == 200
    assert response.json()["properties"]["federation:source"] == "bravo"


def test_split_mode_collection_ids_are_qualified(split_app):
    with TestClient(split_app.app) as client:
        collections = client.get("/collections")
        ids = sorted(c["id"] for c in collections.json()["collections"])
        assert ids == [
            "alpha:only_a",
            "alpha:shared",
            "bravo:only_b",
            "bravo:shared",
        ]

        # Qualified item route works.
        items = client.get("/collections/bravo:shared/items")
        assert items.status_code == 200
        assert all(
            feature["properties"]["federation:source"] == "bravo"
            for feature in items.json()["features"]
        )

        # Bare ambiguous id is rejected with an explicit coverage error.
        ambiguous = client.get("/collections/shared/items")
        assert ambiguous.status_code == 503
        assert ambiguous.json()["code"] == "FederationCoverageError"


def test_partial_source_failure_marks_coverage():
    bravo = AsyncFakeBackend(
        "bravo",
        [make_collection("shared", "MIT")],
        {"shared": [make_item("b1", "shared")]},
        fail_methods=["get_search"],
    )
    alpha = SyncFakeBackend(
        "alpha",
        [make_collection("shared", "MIT")],
        {"shared": [make_item("a1", "shared")]},
    )
    federated = FederatedClient(
        sources=[
            _source("alpha", alpha),
            _source("bravo", bravo),
        ],
        discover=False,
    )
    api = StacApi(settings=ApiSettings(enable_response_models=False), client=federated)
    add_federation_exception_handlers(api.app)
    with TestClient(api.app) as client:
        response = client.get("/search", params={"collections": "shared"})
    assert response.status_code == 200
    body = response.json()
    assert len(body["features"]) == 1
    coverage = body["federation:coverage"]
    assert coverage["partial"] is True
    bravo_status = next(
        entry for entry in coverage["coverage"] if entry["source"] == "bravo"
    )
    assert bravo_status["status"] == "error"


def test_strict_response_models_remain_valid():
    """Federation documents must pass strict stac-pydantic validation."""
    api, _ = _build_app(ConflictResolution.PRIORITY)
    # Rebuild with response validation enabled.
    strict_api = StacApi(
        settings=ApiSettings(enable_response_models=True),
        client=api.client,
    )
    add_federation_exception_handlers(strict_api.app)
    with TestClient(strict_api.app) as client:
        for path in (
            "/collections",
            "/collections/shared",
            "/search?collections=shared",
            "/collections/shared/items",
        ):
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)


def test_all_sources_failing_returns_error_not_empty_success():
    alpha = SyncFakeBackend(
        "alpha",
        [make_collection("shared", "MIT")],
        {"shared": []},
        fail_methods=["get_search"],
    )
    bravo = AsyncFakeBackend(
        "bravo",
        [make_collection("shared", "MIT")],
        {"shared": []},
        fail_methods=["get_search"],
    )
    federated = FederatedClient(
        sources=[_source("alpha", alpha), _source("bravo", bravo)],
        discover=False,
    )
    api = StacApi(settings=ApiSettings(enable_response_models=False), client=federated)
    add_federation_exception_handlers(api.app)
    with TestClient(api.app) as client:
        response = client.get("/search", params={"collections": "shared"})
    # Must not masquerade as a successful empty result set; 503 carries the
    # explicit per-source coverage report.
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "FederationCoverageError"
    assert body["federation:coverage"]["coverage"]
