"""Unit tests for deterministic routing and capability dispatch."""

import pytest

from stac_fastapi.api.federation import (
    ConflictResolution,
    SourceCapabilities,
    SourceConfig,
    SourceRegistry,
    encode_source_hint,
    parse_source_hint,
)
from stac_fastapi.types.errors import NotFoundError

from .fakes import AsyncFakeBackend, SyncFakeBackend, make_collection


def _source(source_id, priority=0, collections=None, capabilities=None):
    collections_doc = [make_collection(c, "MIT") for c in (collections or ["c"])]
    backend = SyncFakeBackend(source_id, collections_doc, {})
    return SourceConfig(
        source_id=source_id,
        client=backend,
        priority=priority,
        collections=collections,
        capabilities=capabilities or SourceCapabilities(),
    )


def test_source_id_validation():
    with pytest.raises(ValueError):
        SourceConfig(source_id="bad:id", client=SyncFakeBackend("x", [], {}))


def test_hint_roundtrip():
    assert parse_source_hint("alpha:shared") == ("alpha", "shared")
    assert parse_source_hint("shared") is None
    assert parse_source_hint(":nope") is None
    assert encode_source_hint("alpha", "shared") == "alpha:shared"


def test_duplicate_source_ids_rejected():
    with pytest.raises(ValueError):
        SourceRegistry([_source("a"), _source("a")])


def test_priority_order_is_deterministic():
    low = _source("low", priority=5)
    high = _source("high", priority=1)
    registry = SourceRegistry([low, high])
    assert [s.source_id for s in registry.sources] == ["high", "low"]


def test_registration_order_breaks_priority_ties():
    first = _source("first")
    second = _source("second")
    registry = SourceRegistry([first, second])
    assert [s.source_id for s in registry.sources] == ["first", "second"]
    # Rebuilding with the same configs in a different order changes the
    # tie-break, proving order comes from registration, not dict hashing.
    registry_reversed = SourceRegistry([second, first])
    assert [s.source_id for s in registry_reversed.sources] == ["second", "first"]


def test_plan_routes_with_static_hosting():
    alpha = _source("alpha", collections=["shared", "only_a"])
    bravo = _source("bravo", collections=["shared", "only_b"])
    registry = SourceRegistry([alpha, bravo])

    plan = registry.plan_collection_routes(["shared"])
    assert [s.source_id for s in plan["shared"]] == ["alpha", "bravo"]

    plan = registry.plan_collection_routes(["only_b"])
    assert [s.source_id for s in plan["only_b"]] == ["bravo"]

    assert registry.plan_collection_routes(None) == {"*": registry.sources}


def test_split_hint_pins_exact_source():
    alpha = _source("alpha", collections=["shared"])
    bravo = _source("bravo", collections=["shared"])
    registry = SourceRegistry(
        [alpha, bravo], resolution=ConflictResolution.SPLIT
    )
    plan = registry.plan_collection_routes(["bravo:shared"])
    assert list(plan.keys()) == ["shared"]
    assert [s.source_id for s in plan["shared"]] == ["bravo"]


def test_resolve_item_route_priority_picks_winner():
    alpha = _source("alpha", collections=["shared"])
    bravo = _source("bravo", collections=["shared"])
    registry = SourceRegistry([alpha, bravo])
    source, cid = registry.resolve_item_route("shared")
    assert source.source_id == "alpha"
    assert cid == "shared"


def test_resolve_item_route_split_requires_qualification():
    alpha = _source("alpha", collections=["shared"])
    bravo = _source("bravo", collections=["shared"])
    registry = SourceRegistry(
        [alpha, bravo], resolution=ConflictResolution.SPLIT
    )
    with pytest.raises(NotFoundError):
        registry.resolve_item_route("shared")
    source, cid = registry.resolve_item_route("bravo:shared")
    assert source.source_id == "bravo"
    assert cid == "shared"


def test_source_hint_overrides_priority():
    alpha = _source("alpha", collections=["shared"])
    bravo = _source("bravo", collections=["shared"])
    registry = SourceRegistry([alpha, bravo])
    source, cid = registry.resolve_item_route("shared", source_hint="bravo")
    assert source.source_id == "bravo"
    assert cid == "shared"


def test_unknown_source_hint_raises():
    registry = SourceRegistry([_source("alpha", collections=["shared"])])
    with pytest.raises(NotFoundError):
        registry.resolve_item_route("shared", source_hint="ghost")


def test_capability_split_strips_unsupported_conditions():
    from stac_fastapi.api.federation.dispatch import (
        split_request_by_capabilities,
    )

    limited = SourceCapabilities(bbox=True, intersects=False, datetime=False)
    request = {
        "collections": ["shared"],
        "bbox": (0, 0, 1, 1),
        "intersects": {"type": "Point", "coordinates": [0, 0]},
        "datetime": "2020-01-01/2020-12-31",
        "limit": 10,
    }
    forwarded, local = split_request_by_capabilities(request, limited)
    assert "bbox" in forwarded
    assert "intersects" not in forwarded
    assert "datetime" not in forwarded
    assert set(local.keys()) == {"intersects", "datetime"}
    # Original request must not be mutated.
    assert "intersects" in request and "datetime" in request


def test_fully_capable_source_forwards_everything():
    from stac_fastapi.api.federation.dispatch import (
        split_request_by_capabilities,
    )

    request = {"bbox": (0, 0, 1, 1), "datetime": "2020-01-01"}
    forwarded, local = split_request_by_capabilities(
        request, SourceCapabilities()
    )
    assert forwarded == request
    assert local == {}


@pytest.mark.asyncio
async def test_refresh_hosting_caches_and_survives_failure():
    good_backend = AsyncFakeBackend(
        "good", [make_collection("c1", "MIT")], {}
    )
    bad_backend = AsyncFakeBackend(
        "bad", [make_collection("c2", "MIT")], {}, fail_methods=["all_collections"]
    )
    registry = SourceRegistry(
        [
            SourceConfig("good", good_backend),
            SourceConfig("bad", bad_backend),
        ]
    )
    await registry.refresh_hosting()
    # Good source hosting is known; bad source degrades to "may host anything".
    assert registry._hosts(registry.get("good"), "c1")
    assert not registry._hosts(registry.get("good"), "c2")
    assert registry._hosts(registry.get("bad"), "anything")


@pytest.mark.asyncio
async def test_discovery_runs_at_most_once_per_source():
    backend = AsyncFakeBackend("a", [make_collection("c1", "MIT")], {})
    registry = SourceRegistry([SourceConfig("a", backend)])
    await registry.refresh_hosting()
    await registry.refresh_hosting()
    assert len(backend.calls) == 1


@pytest.mark.asyncio
async def test_discovery_disabled_keeps_permissive_hosting():
    backend = AsyncFakeBackend("a", [make_collection("c1", "MIT")], {})
    registry = SourceRegistry(
        [SourceConfig("a", backend)], discover=False
    )
    await registry.refresh_hosting()
    assert len(backend.calls) == 0
    # No discovery means unknown hosting: treated as potentially hosting it.
    assert registry._hosts(registry.get("a"), "anything")
