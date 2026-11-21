"""The transaction extension adapts to backend calling conventions."""

from typing import Any

import pytest
from stac_pydantic import Collection

from stac_fastapi.extensions.transaction.transaction import (
    TransactionExtension,
    _accepts_argument,
    _coerce_collection,
)
from stac_fastapi.types.config import ApiSettings
from stac_fastapi.types.extents import compute_collection_extent

COLLECTION = {
    "type": "Collection",
    "stac_version": "1.0.0",
    "stac_extensions": [],
    "id": "c",
    "description": "d",
    "license": "proprietary",
    "extent": {
        "spatial": {"bbox": [[-180.0, -90.0, 180.0, 90.0]]},
        "temporal": {"interval": [[None, None]]},
    },
    "links": [],
    "assets": {},
}


class ClientAcceptingModel:
    """`update_collection(collection)` expecting a pydantic model."""

    def __init__(self, **kwargs: Any) -> None:
        self.received: Any = None

    def update_collection(self, collection: Collection, **kwargs: Any) -> None:
        self.received = collection


class ClientAcceptingIdAndModel:
    """`update_collection(collection_id, collection)` expecting a model."""

    def __init__(self, **kwargs: Any) -> None:
        self.received: Any = None

    def update_collection(
        self, collection_id: str, collection: Collection, **kwargs: Any
    ) -> None:
        self.received = (collection_id, collection)


class ClientAcceptingMapping:
    """`update_collection(collection)` expecting a mapping."""

    def __init__(self, **kwargs: Any) -> None:
        self.received: Any = None

    def update_collection(self, collection: dict, **kwargs: Any) -> None:
        self.received = collection


@pytest.mark.parametrize(
    "client_class",
    [ClientAcceptingModel, ClientAcceptingIdAndModel, ClientAcceptingMapping],
)
def test_persist_collection_adapts_to_signature(client_class: type) -> None:
    client = client_class()
    extension = TransactionExtension(client=client, settings=ApiSettings())

    extension._persist_collection(dict(COLLECTION), "c", request=None)

    received = client.received
    if isinstance(received, tuple):
        collection_id, collection = received
        assert collection_id == "c"
    else:
        collection = received
    if isinstance(collection, Collection):
        assert collection.id == "c"
    else:
        assert collection["id"] == "c"


def test_coerce_collection_leaves_mapping_when_annotation_unknown() -> None:
    assert _coerce_collection(dict(COLLECTION), None) == COLLECTION


def test_accepts_argument_detects_kwargs() -> None:
    def with_request(collection_id, request=None): ...

    def without_request(collection_id): ...

    def var_kwargs(collection_id, **kwargs): ...

    assert _accepts_argument(with_request, "request") is True
    assert _accepts_argument(without_request, "request") is False
    assert _accepts_argument(var_kwargs, "request") is True


def test_compute_extent_matches_recompute() -> None:
    """The helper used by write and read paths returns the same extent."""
    items = [
        {"bbox": [0, 0, 1, 1], "properties": {"datetime": "2020-01-01T00:00:00Z"}},
        {"bbox": [2, 2, 3, 3], "properties": {"datetime": "2021-01-01T00:00:00Z"}},
    ]
    assert compute_collection_extent(items) == {
        "spatial": {"bbox": [[0.0, 0.0, 3.0, 3.0]]},
        "temporal": {"interval": [["2020-01-01T00:00:00Z", "2021-01-01T00:00:00Z"]]},
    }
