"""Behavioral tests for the FederatedClient merge/conflict/coverage rules."""

import pytest

from stac_fastapi.api.federation import (
    ConflictResolution,
    FederatedClient,
    FederationCoverageError,
    SourceCapabilities,
    SourceConfig,
)
from stac_fastapi.api.federation.merge import (
    PROVENANCE_COLLECTION_PROPERTY,
    PROVENANCE_SOURCE_PROPERTY,
    SOURCE_HINT_HEADER,
)

from .fakes import AsyncFakeBackend, SyncFakeBackend, make_collection, make_item


@pytest.mark.asyncio
async def test_all_collections_priority_merges_same_named(priority_client):
    response = await priority_client.all_collections()
    ids = [c["id"] for c in response["collections"]]
    # "shared" appears once (priority winner alpha), unique collections remain.
    assert sorted(ids) == ["only_a", "only_b", "shared"]

    shared = next(c for c in response["collections"] if c["id"] == "shared")
    provenance = shared["federation:provenance"]
    assert provenance[PROVENANCE_SOURCE_PROPERTY] == "alpha"
    assert provenance[PROVENANCE_COLLECTION_PROPERTY] == "shared"
    # The losing bravo variant is still visible, not silently dropped.
    variant_sources = {v["source"] for v in provenance["federation:variants"]}
    assert variant_sources == {"alpha", "bravo"}
    assert shared["license"] == "proprietary-a"

    coverage = response["federation:coverage"]
    assert coverage["partial"] is False
    assert {entry["source"]: entry["status"] for entry in coverage["coverage"]} == {
        "alpha": "ok",
        "bravo": "ok",
    }


@pytest.mark.asyncio
async def test_all_collections_split_exposes_every_variant(split_client):
    response = await split_client.all_collections()
    ids = sorted(c["id"] for c in response["collections"])
    assert ids == [
        "alpha:only_a",
        "alpha:shared",
        "bravo:only_b",
        "bravo:shared",
    ]
    shared_bravo = next(
        c for c in response["collections"] if c["id"] == "bravo:shared"
    )
    assert shared_bravo["license"] == "proprietary-b"
    assert (
        shared_bravo["federation:provenance"][PROVENANCE_COLLECTION_PROPERTY]
        == "shared"
    )
    assert (
        shared_bravo["federation:provenance"][PROVENANCE_SOURCE_PROPERTY]
        == "bravo"
    )


@pytest.mark.asyncio
async def test_priority_never_depends_on_speed(source_a, source_b, monkeypatch):
    """A slow high-priority source must still win over a fast low-priority one."""
    import time

    original = SyncFakeBackend.all_collections

    def slow_collections(self, **kwargs):
        time.sleep(0.05)
        return original(self, **kwargs)

    monkeypatch.setattr(
        SyncFakeBackend, "all_collections", slow_collections, raising=True
    )
    # source_a (alpha) is registered first and therefore higher priority.
    client = FederatedClient(
        sources=[source_a, source_b],
        conflict_resolution=ConflictResolution.PRIORITY,
        discover=False,
    )
    response = await client.all_collections()
    shared = next(c for c in response["collections"] if c["id"] == "shared")
    assert shared["federation:provenance"][PROVENANCE_SOURCE_PROPERTY] == "alpha"


@pytest.mark.asyncio
async def test_get_collection_priority_with_variants(priority_client):
    collection = await priority_client.get_collection("shared")
    assert collection["id"] == "shared"
    assert collection["federation:provenance"][PROVENANCE_SOURCE_PROPERTY] == "alpha"
    assert len(collection["federation:provenance"]["federation:variants"]) == 2


@pytest.mark.asyncio
async def test_get_collection_split_by_hint(split_client):
    collection = await split_client.get_collection("bravo:shared")
    assert collection["id"] == "bravo:shared"
    assert collection["license"] == "proprietary-b"


@pytest.mark.asyncio
async def test_search_fans_out_and_keeps_both_same_id_items(priority_client):
    response = await priority_client.get_search(collections=["shared"])
    provenance_pairs = sorted(
        (
            feature["properties"][PROVENANCE_SOURCE_PROPERTY],
            feature["id"],
        )
        for feature in response["features"]
    )
    # Item id "a1" exists on both sources with different geometry; both kept.
    assert ("alpha", "a1") in provenance_pairs
    assert ("bravo", "a1") in provenance_pairs
    assert ("alpha", "a2") in provenance_pairs
    assert ("bravo", "b1") in provenance_pairs

    # Deterministic ordering: source priority first (alpha before bravo).
    sources = [
        feature["properties"][PROVENANCE_SOURCE_PROPERTY]
        for feature in response["features"]
    ]
    assert sources == sorted(sources, key={"alpha": 0, "bravo": 1}.get)

    coverage = response["federation:coverage"]
    assert coverage["partial"] is False


@pytest.mark.asyncio
async def test_search_items_carry_stable_self_links(priority_client):
    response = await priority_client.get_search(collections=["shared"])
    by_source = {
        feature["properties"][PROVENANCE_SOURCE_PROPERTY]: feature
        for feature in response["features"]
        if feature["id"] == "a1"
    }
    bravo_item = by_source["bravo"]
    self_link = next(
        link for link in bravo_item["links"] if link["rel"] == "self"
    )
    assert SOURCE_HINT_HEADER in self_link
    assert self_link[SOURCE_HINT_HEADER] == "bravo"
    assert "x-stac-source=bravo" in self_link["href"]


@pytest.mark.asyncio
async def test_get_item_routes_to_the_same_real_source(priority_client):
    # Without a hint, PRIORITY resolves to alpha.
    item = await priority_client.get_item("a1", "shared")
    assert item["properties"][PROVENANCE_SOURCE_PROPERTY] == "alpha"
    assert item["geometry"]["coordinates"] == [0.0, 0.0]

    # The source hint routes the *same* logical request to bravo instead.
    item_b = await _get_with_hint(priority_client, "a1", "shared", "bravo")
    assert item_b["properties"][PROVENANCE_SOURCE_PROPERTY] == "bravo"
    assert item_b["geometry"]["coordinates"] == [30.0, 30.0]


class _HintedRequest:
    def __init__(self, source):
        self.headers = {SOURCE_HINT_HEADER: source} if source else {}
        self.query_params = {}
        self.base_url = "http://test/"


async def _get_with_hint(client, item_id, collection_id, source):
    return await client.get_item(
        item_id, collection_id, request=_HintedRequest(source)
    )


@pytest.mark.asyncio
async def test_get_item_unknown_collection_reports_no_coverage(priority_client):
    with pytest.raises(FederationCoverageError):
        await priority_client.get_item("x", "ghost", request=_HintedRequest(None))


@pytest.mark.asyncio
async def test_partial_failure_is_marked_not_silent(source_a):
    failing_b = AsyncFakeBackend(
        "bravo",
        [make_collection("shared", "proprietary-b")],
        {"shared": [make_item("b1", "shared")]},
        fail_methods=["get_search"],
    )
    client = FederatedClient(
        sources=[
            source_a,
            SourceConfig("bravo", failing_b),
        ],
        discover=False,
    )
    response = await client.get_search(collections=["shared"])
    # Alpha's data is still returned.
    assert len(response["features"]) == 2
    coverage = response["federation:coverage"]
    assert coverage["partial"] is True
    statuses = {entry["source"]: entry for entry in coverage["coverage"]}
    assert statuses["alpha"]["status"] == "ok"
    assert statuses["bravo"]["status"] == "error"
    assert "forced get_search failure" in statuses["bravo"]["detail"]


@pytest.mark.asyncio
async def test_total_failure_raises_coverage_error():
    bad_a = SyncFakeBackend(
        "alpha",
        [make_collection("shared", "MIT")],
        {"shared": [make_item("a1", "shared")]},
        fail_methods=["get_search"],
    )
    bad_b = AsyncFakeBackend(
        "bravo",
        [make_collection("shared", "MIT")],
        {"shared": [make_item("b1", "shared")]},
        fail_methods=["get_search"],
    )
    client = FederatedClient(
        sources=[
            SourceConfig("alpha", bad_a),
            SourceConfig("bravo", bad_b),
        ],
        discover=False,
    )
    with pytest.raises(FederationCoverageError) as exc_info:
        await client.get_search(collections=["shared"])
    statuses = exc_info.value.source_statuses
    assert all(status["status"] == "error" for status in statuses)


@pytest.mark.asyncio
async def test_fail_fast_aborts_on_first_error():
    bad_b = AsyncFakeBackend(
        "bravo",
        [make_collection("shared", "MIT")],
        {"shared": []},
        fail_methods=["get_search"],
    )
    good_a = SyncFakeBackend(
        "alpha",
        [make_collection("shared", "MIT")],
        {"shared": [make_item("a1", "shared")]},
    )
    client = FederatedClient(
        sources=[
            SourceConfig("alpha", good_a),
            SourceConfig("bravo", bad_b),
        ],
        discover=False,
        fail_fast=True,
    )
    with pytest.raises(FederationCoverageError):
        await client.get_search(collections=["shared"])


@pytest.mark.asyncio
async def test_unsupported_condition_is_locally_applied(source_a, backend_a):
    # Bravo cannot execute bbox or datetime server-side.
    limited_b = AsyncFakeBackend(
        "bravo",
        [make_collection("shared", "proprietary-b")],
        {
            "shared": [
                make_item(
                    "b_in", "shared", lon=0.0, lat=0.0, when="2020-06-01T00:00:00Z"
                ),
                make_item(
                    "b_far", "shared", lon=50.0, lat=50.0, when="2020-06-01T00:00:00Z"
                ),
                make_item(
                    "b_old", "shared", lon=0.0, lat=0.0, when="2005-06-01T00:00:00Z"
                ),
            ]
        },
    )
    client = FederatedClient(
        sources=[
            source_a,
            SourceConfig(
                "bravo",
                limited_b,
                capabilities=SourceCapabilities(
                    bbox=False, intersects=False, datetime=False
                ),
            ),
        ],
        discover=False,
    )
    response = await client.get_search(
        collections=["shared"],
        bbox=(-10, -10, 10, 10),
        datetime="2019-01-01T00:00:00Z/2021-01-01T00:00:00Z",
    )
    bravo_items = [
        feature
        for feature in response["features"]
        if feature["properties"][PROVENANCE_SOURCE_PROPERTY] == "bravo"
    ]
    assert [item["id"] for item in bravo_items] == ["b_in"]

    # The forwarded call must not contain the unsupported conditions.
    forwarded = [
        kwargs
        for method, kwargs in limited_b.calls
        if method == "get_search"
    ][0]
    assert "bbox" not in forwarded
    assert "datetime" not in forwarded

    # And the coverage block explains the local enforcement.
    bravo_status = next(
        entry
        for entry in response["federation:coverage"]["coverage"]
        if entry["source"] == "bravo"
    )
    assert "locally applied" in bravo_status["detail"]


@pytest.mark.asyncio
async def test_item_collection_uses_resolved_source(priority_client):
    response = await priority_client.item_collection("shared", limit=10)
    # PRIORITY routes to alpha only.
    assert all(
        feature["properties"][PROVENANCE_SOURCE_PROPERTY] == "alpha"
        for feature in response["features"]
    )
    assert {feature["id"] for feature in response["features"]} == {"a1", "a2"}


@pytest.mark.asyncio
async def test_item_collection_split_requires_qualified_id(split_client):
    with pytest.raises(FederationCoverageError):
        await split_client.item_collection("shared", limit=10)
    response = await split_client.item_collection("bravo:shared", limit=10)
    assert all(
        feature["properties"][PROVENANCE_SOURCE_PROPERTY] == "bravo"
        for feature in response["features"]
    )


@pytest.mark.asyncio
async def test_post_search_equivalent_to_get(priority_client):
    from stac_fastapi.types.search import BaseSearchPostRequest

    request = BaseSearchPostRequest(collections=["only_a"])
    response = await priority_client.post_search(request)
    assert len(response["features"]) == 1
    assert response["features"][0]["id"] == "a3"


@pytest.mark.asyncio
async def test_search_unknown_collection_has_no_coverage(priority_client):
    with pytest.raises(FederationCoverageError):
        await priority_client.get_search(collections=["does_not_exist"])


@pytest.mark.asyncio
async def test_mixed_sync_and_async_backends(source_a, source_b):
    client = FederatedClient(sources=[source_a, source_b], discover=False)
    response = await client.all_collections()
    assert response["numberReturned"] == 3
