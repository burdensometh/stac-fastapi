"""Additional edge-case tests for federation semantics."""

import asyncio

import pytest

from stac_fastapi.api.federation import (
    ConflictResolution,
    FederatedClient,
    SourceCapabilities,
    SourceConfig,
)
from stac_fastapi.api.federation.merge import (
    PROVENANCE_COLLECTION_PROPERTY,
    PROVENANCE_SOURCE_PROPERTY,
)

from .fakes import (
    AsyncFakeBackend,
    FakeBackend,
    SyncFakeBackend,
    make_collection,
    make_item,
)


def _config(source_id, backend, *, collections=None, capabilities=None, priority=0):
    return SourceConfig(
        source_id=source_id,
        client=backend,
        collections=collections,
        capabilities=capabilities or SourceCapabilities(),
        priority=priority,
    )


@pytest.mark.asyncio
async def test_static_hosting_skips_sources_that_do_not_host():
    alpha = SyncFakeBackend(
        "alpha",
        [make_collection("c_a", "MIT")],
        {"c_a": [make_item("a1", "c_a")]},
    )
    bravo = AsyncFakeBackend(
        "bravo",
        [make_collection("c_b", "MIT")],
        {"c_b": [make_item("b1", "c_b")]},
    )
    client = FederatedClient(
        sources=[
            _config("alpha", alpha, collections=["c_a"]),
            _config("bravo", bravo, collections=["c_b"]),
        ],
        discover=False,
    )
    response = await client.get_search(collections=["c_a"])
    # Only alpha was queried; bravo is not in the coverage at all (not planned).
    queried = response["federation:coverage"]["queried_sources"]
    assert queried == ["alpha"]
    assert response["features"][0]["id"] == "a1"


@pytest.mark.asyncio
async def test_not_hosted_404_recorded_distinctly_from_errors():
    alpha = SyncFakeBackend(
        "alpha",
        [make_collection("shared", "MIT")],
        {"shared": [make_item("a1", "shared")]},
    )
    # Bravo raises NotFoundError for unknown collections via the fake's search.
    bravo = AsyncFakeBackend("bravo", [], {})
    client = FederatedClient(
        sources=[
            _config("alpha", alpha),
            _config("bravo", bravo),
        ],
        discover=False,
    )
    response = await client.get_search(collections=["shared"])
    statuses = {
        entry["source"]: entry["status"]
        for entry in response["federation:coverage"]["coverage"]
    }
    assert statuses == {"alpha": "ok", "bravo": "not_hosted"}
    # Partial coverage is flagged because one source lacked coverage.
    assert response["federation:coverage"]["partial"] is True
    # Alpha's data is still served.
    assert [f["id"] for f in response["features"]] == ["a1"]


@pytest.mark.asyncio
async def test_result_order_is_independent_of_completion_order(monkeypatch):
    """Concurrent completion order must not change merged output order."""
    alpha = SyncFakeBackend(
        "alpha",
        [make_collection("shared", "MIT")],
        {"shared": [make_item("a1", "shared", lon=1, lat=1)]},
    )
    bravo = AsyncFakeBackend(
        "bravo",
        [make_collection("shared", "MIT")],
        {"shared": [make_item("b1", "shared", lon=2, lat=2)]},
    )

    async def slow_search(self, **kwargs):
        await asyncio.sleep(0.05)
        return FakeBackend._search(self, **kwargs)

    monkeypatch.setattr(AsyncFakeBackend, "get_search", slow_search)

    client = FederatedClient(
        sources=[_config("alpha", alpha), _config("bravo", bravo)],
        discover=False,
    )
    # Run twice; merged source ordering must be alpha-first every time.
    for _ in range(2):
        response = await client.get_search(collections=["shared"])
        sources = [
            feature["properties"][PROVENANCE_SOURCE_PROPERTY]
            for feature in response["features"]
        ]
        assert sources == ["alpha", "bravo"]


@pytest.mark.asyncio
async def test_unsupported_ids_filter_applied_locally():
    limited = AsyncFakeBackend(
        "bravo",
        [make_collection("shared", "MIT")],
        {
            "shared": [
                make_item("keep", "shared"),
                make_item("drop", "shared"),
            ]
        },
    )
    client = FederatedClient(
        sources=[
            _config(
                "bravo",
                limited,
                capabilities=SourceCapabilities(ids=False),
            )
        ],
        discover=False,
    )
    response = await client.get_search(
        collections=["shared"], ids=["keep"]
    )
    assert [f["id"] for f in response["features"]] == ["keep"]
    forwarded = [
        kwargs
        for method, kwargs in limited.calls
        if method == "get_search"
    ][0]
    assert "ids" not in forwarded


@pytest.mark.asyncio
async def test_provenance_preserved_through_item_collection():
    alpha = SyncFakeBackend(
        "alpha",
        [make_collection("shared", "MIT")],
        {"shared": [make_item("a1", "shared")]},
    )
    client = FederatedClient(sources=[_config("alpha", alpha)], discover=False)
    response = await client.item_collection("shared", limit=10)
    feature = response["features"][0]
    assert feature["properties"][PROVENANCE_SOURCE_PROPERTY] == "alpha"
    assert (
        feature["properties"][PROVENANCE_COLLECTION_PROPERTY] == "shared"
    )
    self_link = next(link for link in feature["links"] if link["rel"] == "self")
    assert self_link["x-stac-source"] == "alpha"


@pytest.mark.asyncio
async def test_explicit_priority_overrides_registration_order():
    low_first = SyncFakeBackend(
        "first",
        [make_collection("shared", "license-first")],
        {"shared": []},
    )
    high_second = AsyncFakeBackend(
        "second",
        [make_collection("shared", "license-second")],
        {"shared": []},
    )
    client = FederatedClient(
        sources=[
            _config("first", low_first, priority=10),
            _config("second", high_second, priority=1),
        ],
        conflict_resolution=ConflictResolution.PRIORITY,
        discover=False,
    )
    collection = await client.get_collection("shared")
    assert collection["federation:provenance"][PROVENANCE_SOURCE_PROPERTY] == "second"
    assert collection["license"] == "license-second"


@pytest.mark.asyncio
async def test_unfiltered_search_spans_all_sources():
    alpha = SyncFakeBackend(
        "alpha",
        [make_collection("c_a", "MIT")],
        {"c_a": [make_item("a1", "c_a")]},
    )
    bravo = AsyncFakeBackend(
        "bravo",
        [make_collection("c_b", "MIT")],
        {"c_b": [make_item("b1", "c_b")]},
    )
    client = FederatedClient(
        sources=[_config("alpha", alpha), _config("bravo", bravo)],
        discover=False,
    )
    response = await client.get_search()
    ids = sorted(feature["id"] for feature in response["features"])
    assert ids == ["a1", "b1"]
    # Each item keeps the collection id it had on its real source.
    provenance = {
        feature["id"]: feature["properties"][PROVENANCE_COLLECTION_PROPERTY]
        for feature in response["features"]
    }
    assert provenance == {"a1": "c_a", "b1": "c_b"}


@pytest.mark.asyncio
async def test_limit_is_applied_after_merge():
    alpha = SyncFakeBackend(
        "alpha",
        [make_collection("shared", "MIT")],
        {"shared": [make_item(f"a{i}", "shared") for i in range(5)]},
    )
    bravo = AsyncFakeBackend(
        "bravo",
        [make_collection("shared", "MIT")],
        {"shared": [make_item(f"b{i}", "shared") for i in range(5)]},
    )
    client = FederatedClient(
        sources=[_config("alpha", alpha), _config("bravo", bravo)],
        discover=False,
    )
    response = await client.get_search(collections=["shared"], limit=3)
    assert len(response["features"]) == 3
    assert response["numberReturned"] == 3


@pytest.mark.asyncio
async def test_single_source_config_rejected():
    with pytest.raises(ValueError):
        FederatedClient(sources=[])


@pytest.mark.asyncio
async def test_get_search_parses_string_intersects():
    """GET search delivers intersects as a URL-encoded GeoJSON string."""
    import json

    bravo = AsyncFakeBackend(
        "bravo",
        [make_collection("shared", "MIT")],
        {
            "shared": [
                make_item("in", "shared", lon=0.0, lat=0.0),
                make_item("far", "shared", lon=50.0, lat=50.0),
            ]
        },
    )
    client = FederatedClient(
        sources=[
            _config(
                "bravo",
                bravo,
                # Force local application so the parser output is exercised.
                capabilities=SourceCapabilities(intersects=False),
            )
        ],
        discover=False,
    )
    polygon = {
        "type": "Polygon",
        "coordinates": [
            [
                [-1, -1],
                [-1, 1],
                [1, 1],
                [1, -1],
                [-1, -1],
            ]
        ],
    }
    response = await client.get_search(
        collections=["shared"], intersects=json.dumps(polygon)
    )
    assert [f["id"] for f in response["features"]] == ["in"]
