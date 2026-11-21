"""Tests for patch computation on STAC Items."""

import copy

import pytest

from stac_fastapi.types.errors import PatchError, VersionConflictError
from stac_fastapi.types.etag import item_version, quote_etag
from stac_fastapi.types.patch import apply_patch, conditional_patch


@pytest.fixture
def item() -> dict:
    return {
        "type": "Feature",
        "stac_version": "1.0.0",
        "id": "test_item",
        "geometry": {"type": "Point", "coordinates": [-105, 40]},
        "bbox": [-105, 40, -105, 40],
        "properties": {"datetime": "2020-06-13T13:00:00Z", "foo": "bar"},
        "links": [],
        "assets": {},
        "collection": "test_collection",
    }


def test_merge_patch_adds_and_removes() -> None:
    patched = apply_patch(item={}, patch={"properties": {"a": 1}})
    assert patched == {"properties": {"a": 1}}


def test_merge_patch_removes_null_members() -> None:
    patched = apply_patch(
        {"properties": {"keep": 1, "drop": 2}}, {"properties": {"drop": None}}
    )
    assert patched == {"properties": {"keep": 1}}


def test_merge_patch_does_not_modify_the_item(item: dict) -> None:
    before = copy.deepcopy(item)
    apply_patch(item, {"properties": {"foo": "changed"}})
    assert item == before


def test_json_patch_add_replace_remove(item: dict) -> None:
    patched = apply_patch(
        item,
        [
            {"op": "add", "path": "/properties/new", "value": 1},
            {"op": "replace", "path": "/properties/foo", "value": "changed"},
            {"op": "remove", "path": "/properties/new"},
        ],
    )
    assert patched["properties"] == {"datetime": "2020-06-13T13:00:00Z", "foo": "changed"}


def test_json_patch_copy_and_move(item: dict) -> None:
    patched = apply_patch(
        item,
        [
            {"op": "copy", "from": "/properties/foo", "path": "/properties/copied"},
            {"op": "move", "from": "/properties/copied", "path": "/properties/moved"},
        ],
    )
    assert patched["properties"]["moved"] == "bar"
    assert "copied" not in patched["properties"]


def test_json_patch_test_succeeds(item: dict) -> None:
    patched = apply_patch(
        item, [{"op": "test", "path": "/properties/foo", "value": "bar"}]
    )
    assert patched == item


def test_json_patch_failed_test_raises(item: dict) -> None:
    with pytest.raises(PatchError):
        apply_patch(item, [{"op": "test", "path": "/properties/foo", "value": "x"}])


def test_json_patch_escapes_pointer_tokens(item: dict) -> None:
    patched = apply_patch(
        item, [{"op": "add", "path": "/properties/a~1b", "value": 1}]
    )
    assert patched["properties"]["a/b"] == 1


def test_json_patch_unknown_operation_raises(item: dict) -> None:
    with pytest.raises(PatchError):
        apply_patch(item, [{"op": "increment", "path": "/properties/foo"}])


def test_json_patch_bad_operation_leaves_the_item_alone(item: dict) -> None:
    before = copy.deepcopy(item)
    with pytest.raises(PatchError):
        apply_patch(
            item,
            [
                {"op": "replace", "path": "/properties/foo", "value": "changed"},
                {"op": "remove", "path": "/properties/missing"},
            ],
        )
    assert item == before


def test_conditional_patch_applies_when_the_version_matches(item: dict) -> None:
    patched = conditional_patch(
        item,
        {"properties": {"foo": "changed"}},
        quote_etag(item_version(item)),
    )
    assert patched["properties"]["foo"] == "changed"


def test_conditional_patch_without_a_version_applies(item: dict) -> None:
    patched = conditional_patch(item, {"properties": {"foo": "changed"}}, None)
    assert patched["properties"]["foo"] == "changed"


def test_conditional_patch_raises_when_the_version_is_stale(item: dict) -> None:
    before = copy.deepcopy(item)
    with pytest.raises(VersionConflictError):
        conditional_patch(item, {"properties": {"foo": "changed"}}, '"stale"')
    assert item == before
