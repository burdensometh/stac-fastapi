"""Pytest fixtures for the federation tests."""

import pytest

from stac_fastapi.api.federation import (
    ConflictResolution,
    FederatedClient,
    SourceCapabilities,
    SourceConfig,
)

from .fakes import (
    AsyncFakeBackend,
    SyncFakeBackend,
    make_collection,
    make_item,
)


# Two catalogs hosting a same-named collection ("shared") with *different*
# extent/license, plus one collection unique to each source — the motivating
# scenario from the federation requirements.
def _catalog_a():
    collections = [
        make_collection(
            "shared",
            license_="proprietary-a",
            bbox=[-10, -10, 10, 10],
            interval=["2019-01-01T00:00:00Z", "2021-01-01T00:00:00Z"],
        ),
        make_collection("only_a", license_="MIT"),
    ]
    items = {
        "shared": [
            make_item("a1", "shared", lon=0.0, lat=0.0, when="2020-06-01T00:00:00Z"),
            make_item("a2", "shared", lon=5.0, lat=5.0, when="2020-07-01T00:00:00Z"),
        ],
        "only_a": [make_item("a3", "only_a", lon=1.0, lat=1.0)],
    }
    return collections, items


def _catalog_b():
    collections = [
        make_collection(
            "shared",
            license_="proprietary-b",
            bbox=[20, 20, 40, 40],
            interval=["2010-01-01T00:00:00Z", "2012-01-01T00:00:00Z"],
        ),
        make_collection("only_b", license_="Apache-2.0"),
    ]
    items = {
        "shared": [
            # Same item id as catalog A's a1 but a different real record —
            # provenance must keep both.
            make_item("a1", "shared", lon=30.0, lat=30.0, when="2011-06-01T00:00:00Z"),
            make_item("b1", "shared", lon=25.0, lat=25.0, when="2011-05-01T00:00:00Z"),
        ],
        "only_b": [make_item("b2", "only_b", lon=35.0, lat=35.0)],
    }
    return collections, items


@pytest.fixture
def backend_a():
    collections, items = _catalog_a()
    return SyncFakeBackend("alpha", collections, items)


@pytest.fixture
def backend_b():
    collections, items = _catalog_b()
    return AsyncFakeBackend("bravo", collections, items)


@pytest.fixture
def source_a(backend_a):
    return SourceConfig(
        source_id="alpha",
        client=backend_a,
        title="Alpha Catalog",
        capabilities=SourceCapabilities(),
    )


@pytest.fixture
def source_b(backend_b):
    return SourceConfig(
        source_id="bravo",
        client=backend_b,
        title="Bravo Catalog",
        capabilities=SourceCapabilities(),
    )


@pytest.fixture
def priority_client(source_a, source_b):
    return FederatedClient(
        sources=[source_a, source_b],
        conflict_resolution=ConflictResolution.PRIORITY,
        discover=False,
    )


@pytest.fixture
def split_client(source_a, source_b):
    return FederatedClient(
        sources=[source_b, source_a],  # reversed registration on purpose
        conflict_resolution=ConflictResolution.SPLIT,
        discover=False,
    )
