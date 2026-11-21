"""Tests for the explicit multi-key SortContract.

The contract is the backend-neutral answer to inconsistent null ordering and
non-deterministic pagination. These tests pin:

* GET (``+field``/``-field``) and POST (``field``/``direction``) parsing to
  structurally equal contracts;
* type and missing-value resolution from the Sortables JSON Schema;
* the synthesized ``collection``/``id`` stable tail;
* explicit errors for incomparable declared and runtime types;
* ordering that can be applied to the full result set before pagination.
"""

import pytest
from stac_pydantic.api.extensions.sort import SortExtension as PostSortModel

from stac_fastapi.api.models import create_get_request_model, create_post_request_model
from stac_fastapi.extensions.sort import (
    AmbiguousSortableTypeError,
    IncomparableSortValueError,
    NullPosition,
    SortContract,
    SortContractError,
    SortDirection,
    SortExtension,
    SortExtensionGetRequest,
    SortExtensionPostRequest,
    SortKey,
    SortValueType,
    UnknownSortableError,
    get_field_value,
    sort_contract_from_request,
    sort_items,
)

SORTABLES = {
    "properties": {
        "id": {"type": "string"},
        "collection": {"type": "string"},
        "name": {"type": "string"},
        "properties.gsd": {"type": ["number", "null"]},
        "properties.cloud_cover": {
            "anyOf": [{"type": "number"}, {"type": "null"}],
            "x-null-position": "first",
        },
        "properties.datetime": {"type": "string", "format": "date-time"},
        "properties.count": {"type": ["integer", "null"]},
        "properties.recent": {
            "type": "string",
            "format": "date-time",
            "x-default-direction": "desc",
        },
        "properties.mixed": {"type": ["string", "integer"]},
        "properties.blob": {"type": "object"},
        "properties.tags": {"type": "array", "items": {"type": "string"}},
    }
}


def _item(item_id, collection, **props):
    return {
        "id": item_id,
        "collection": collection,
        "properties": props,
    }


ITEMS = [
    _item("a", "c1", gsd=5.0, cloud_cover=None, count=1,
          datetime="2020-01-01T00:00:00Z", recent="2020-01-01T00:00:00Z"),
    _item("b", "c1", gsd=None, cloud_cover=2.0, count=None,
          datetime="2019-01-01T00:00:00Z", recent="2019-01-01T00:00:00Z"),
    _item("c", "c2", gsd=5.0, cloud_cover=1.0, count=3,
          datetime="2021-01-01T00:00:00Z", recent="2021-01-01T00:00:00Z"),
    _item("d", "c1", gsd=1.0, cloud_cover=None, count=2,
          datetime="2018-01-01T00:00:00Z", recent="2018-01-01T00:00:00Z"),
]


# ---------------------------------------------------------------------------
# Schema resolution
# ---------------------------------------------------------------------------


def test_resolves_type_and_nullability_from_type_array():
    contract = SortContract.from_get_sortby(["properties.gsd"], SORTABLES)
    key = contract.keys[0]
    assert key.value_type is SortValueType.NUMBER
    assert key.null_position is NullPosition.LAST


def test_resolves_type_and_nullability_from_anyof():
    contract = SortContract.from_get_sortby(["properties.cloud_cover"], SORTABLES)
    key = contract.keys[0]
    assert key.value_type is SortValueType.NUMBER
    # Annotated missing-value position wins over the default.
    assert key.null_position is NullPosition.FIRST
    assert key.nulls_first is True


def test_datetime_format_resolves_to_datetime_type():
    contract = SortContract.from_get_sortby(["properties.datetime"], SORTABLES)
    assert contract.keys[0].value_type is SortValueType.DATETIME


def test_integer_resolves_separately_from_number():
    contract = SortContract.from_get_sortby(["properties.count"], SORTABLES)
    assert contract.keys[0].value_type is SortValueType.INTEGER


def test_non_nullable_field_ignores_null_position_annotation():
    sortables = {
        "properties": {
            "id": {"type": "string"},
            "collection": {"type": "string"},
            "name": {"type": "string", "x-null-position": "first"},
        }
    }
    contract = SortContract.from_get_sortby(["name"], sortables)
    assert contract.keys[0].null_position is NullPosition.LAST


def test_incompatible_primitive_types_are_rejected():
    with pytest.raises(AmbiguousSortableTypeError, match="incomparable types"):
        SortContract.from_get_sortby(["properties.mixed"], SORTABLES)


def test_non_orderable_types_are_rejected_explicitly():
    with pytest.raises(AmbiguousSortableTypeError, match="non-orderable"):
        SortContract.from_get_sortby(["properties.blob"], SORTABLES)
    with pytest.raises(AmbiguousSortableTypeError, match="non-orderable"):
        SortContract.from_get_sortby(["properties.tags"], SORTABLES)


def test_unknown_field_is_rejected_unless_allowed():
    with pytest.raises(UnknownSortableError):
        SortContract.from_get_sortby(["properties.unknown"], SORTABLES)

    contract = SortContract.from_get_sortby(
        ["properties.unknown"], SORTABLES, allow_undeclared=True
    )
    assert contract.keys[0].field == "properties.unknown"
    assert contract.keys[0].value_type is SortValueType.STRING


def test_invalid_null_position_annotation_raises():
    sortables = {
        "properties": {
            "id": {"type": "string"},
            "collection": {"type": "string"},
            "x": {"type": ["number", "null"], "x-null-position": "middle"},
        }
    }
    with pytest.raises(SortContractError, match="x-null-position"):
        SortContract.from_get_sortby(["x"], sortables)


# ---------------------------------------------------------------------------
# GET / POST equivalence
# ---------------------------------------------------------------------------


def test_get_and_post_contracts_are_equal():
    get_request = SortExtensionGetRequest(
        sortby="-properties.gsd,+properties.datetime"
    )
    post_request = SortExtensionPostRequest(
        sortby=[
            PostSortModel(field="properties.gsd", direction="desc"),
            PostSortModel(field="properties.datetime", direction="asc"),
        ]
    )

    get_contract = get_request.to_sort_contract(SORTABLES)
    post_contract = post_request.to_sort_contract(SORTABLES)

    assert get_contract == post_contract
    assert get_contract.fields == post_contract.fields
    assert get_contract.keys == post_contract.keys


def test_combined_request_models_resolve_equivalent_contracts():
    """The dynamically generated search models keep GET/POST equivalence."""
    extension = SortExtension()
    combined_get = create_get_request_model([extension])(
        sortby="-properties.gsd,id"
    )
    combined_post = create_post_request_model([extension])(
        sortby=[
            {"field": "properties.gsd", "direction": "desc"},
            {"field": "id", "direction": "asc"},
        ]
    )

    # Combined POST models do not inherit mixin methods, so the module-level
    # helper is the stable entry point for both transports.
    get_contract = sort_contract_from_request(combined_get, SORTABLES)
    post_contract = sort_contract_from_request(combined_post, SORTABLES)

    assert get_contract == post_contract
    assert get_contract.fields == (
        "properties.gsd",
        "id",
        "collection",
    )


def test_none_sortby_resolves_to_stable_tail_contract():
    assert sort_contract_from_request(
        SortExtensionGetRequest(sortby=None), SORTABLES
    ).fields == ("collection", "id")
    assert sort_contract_from_request(
        SortExtensionPostRequest(), SORTABLES
    ).fields == ("collection", "id")


def test_bare_get_token_defaults_to_ascending():
    contract = SortContract.from_get_sortby(["name"], SORTABLES)
    assert contract.keys[0].direction is SortDirection.ASC


def test_schema_default_direction_hint_only_applies_to_bare_get_token():
    bare = SortContract.from_get_sortby(["properties.recent"], SORTABLES)
    assert bare.keys[0].direction is SortDirection.DESC

    explicit = SortContract.from_get_sortby(["+properties.recent"], SORTABLES)
    assert explicit.keys[0].direction is SortDirection.ASC

    post = SortContract.from_post_sortby(
        [{"field": "properties.recent", "direction": "asc"}], SORTABLES
    )
    assert post.keys[0].direction is SortDirection.ASC


def test_invalid_default_direction_hint_raises():
    sortables = {
        "properties": {
            "id": {"type": "string"},
            "collection": {"type": "string"},
            "x": {"type": "string", "x-default-direction": "sideways"},
        }
    }
    with pytest.raises(SortContractError, match="x-default-direction"):
        SortContract.from_get_sortby(["x"], sortables)


def test_malformed_get_tokens_are_rejected():
    for token in ("", "   ", "-", "+"):
        with pytest.raises(SortContractError):
            SortContract.from_get_sortby([token], SORTABLES)


def test_duplicate_fields_are_rejected():
    with pytest.raises(SortContractError, match="Duplicate"):
        SortContract.from_get_sortby(["name", "+name"], SORTABLES)


def test_invalid_post_direction_raises_contract_error():
    with pytest.raises(SortContractError, match="Invalid sort direction"):
        SortContract.from_post_sortby(
            [{"field": "name", "direction": "sideways"}], SORTABLES
        )


# ---------------------------------------------------------------------------
# Stable tail
# ---------------------------------------------------------------------------


def test_stable_tail_is_collection_then_id():
    contract = SortContract.from_get_sortby(["name"], SORTABLES)
    assert contract.fields == ("name", "collection", "id")
    tail = contract.keys[-2:]
    assert [key.field for key in tail] == ["collection", "id"]
    assert all(key.direction is SortDirection.ASC for key in tail)
    assert all(key.value_type is SortValueType.STRING for key in tail)


def test_empty_sortby_still_produces_stable_tail():
    assert SortContract.from_get_sortby(None, SORTABLES).fields == (
        "collection",
        "id",
    )
    assert SortContract.from_post_sortby(None, SORTABLES).fields == (
        "collection",
        "id",
    )


def test_client_supplied_tail_fields_are_not_duplicated():
    contract = SortContract.from_get_sortby(["id", "name"], SORTABLES)
    assert contract.fields == ("id", "name", "collection")

    contract = SortContract.from_get_sortby(
        ["collection", "id"], SORTABLES
    )
    assert contract.fields == ("collection", "id")


def test_without_stable_tail_returns_only_client_keys():
    contract = SortContract.from_get_sortby(["name"], SORTABLES)
    assert contract.without_stable_tail().fields == ("name",)


# ---------------------------------------------------------------------------
# Ordering semantics
# ---------------------------------------------------------------------------


def test_ascending_nulls_last_by_default():
    contract = SortContract.from_get_sortby(["properties.gsd"], SORTABLES)
    ordered = [item["id"] for item in sort_items(ITEMS, contract)]
    # 1.0 (d), then the 5.0 tie broken by collection then id (a < c),
    # then the missing value last (b).
    assert ordered == ["d", "a", "c", "b"]


def test_descending_keeps_nulls_last():
    contract = SortContract.from_get_sortby(["-properties.gsd"], SORTABLES)
    ordered = [item["id"] for item in sort_items(ITEMS, contract)]
    # 5.0 tie (a, c), then 1.0 (d), missing still last (b).
    assert ordered == ["a", "c", "d", "b"]


def test_nulls_first_annotation_is_direction_independent():
    asc = SortContract.from_get_sortby(
        ["properties.cloud_cover"], SORTABLES
    )
    desc = SortContract.from_get_sortby(
        ["-properties.cloud_cover"], SORTABLES
    )
    asc_ids = [item["id"] for item in sort_items(ITEMS, asc)]
    desc_ids = [item["id"] for item in sort_items(ITEMS, desc)]

    # Missing cloud_cover on a and d sort first in both directions.
    assert asc_ids[:2] == ["a", "d"]
    assert desc_ids[:2] == ["a", "d"]
    assert asc_ids[-2:] == ["c", "b"]
    assert desc_ids[-2:] == ["b", "c"]


def test_datetime_values_order_chronologically():
    contract = SortContract.from_get_sortby(
        ["properties.datetime"], SORTABLES
    )
    ordered = [item["id"] for item in sort_items(ITEMS, contract)]
    assert ordered == ["d", "b", "a", "c"]


def test_tie_is_broken_by_collection_then_id():
    contract = SortContract.from_get_sortby(["properties.gsd"], SORTABLES)
    ordered = [item["id"] for item in sort_items(ITEMS, contract)]
    # a (c1) and c (c2) share gsd 5.0; collection decides.
    assert ordered.index("a") < ordered.index("c")


def test_compare_returns_zero_for_fully_tied_objects():
    contract = SortContract.from_get_sortby(["name"], SORTABLES)
    left = _item("x", "c", name="n")
    right = _item("x", "c", name="n")
    assert contract.compare(left, right) == 0


def test_sort_items_does_not_mutate_input_order():
    original = [item["id"] for item in ITEMS]
    contract = SortContract.from_get_sortby(["properties.gsd"], SORTABLES)
    sort_items(ITEMS, contract)
    assert [item["id"] for item in ITEMS] == original


def test_pagination_is_stable_regardless_of_input_ordering():
    """Different physical row orders must produce the same pages.

    Simulates two backends that store the same rows differently. The
    contract is applied to the full result set before slicing pages, so the
    null-position and tiebreak rules guarantee identical pages — this is the
    guarantee a service-layer re-sort of one fetched page cannot provide.
    """
    contract = SortContract.from_get_sortby(["-properties.gsd"], SORTABLES)

    shuffled_one = list(ITEMS)
    shuffled_two = list(reversed(ITEMS))

    pages_one = [
        [item["id"] for item in sort_items(shuffled_one, contract)[0:2]],
        [item["id"] for item in sort_items(shuffled_one, contract)[2:4]],
    ]
    pages_two = [
        [item["id"] for item in sort_items(shuffled_two, contract)[0:2]],
        [item["id"] for item in sort_items(shuffled_two, contract)[2:4]],
    ]

    assert pages_one == pages_two
    # Every id appears exactly once across the pages.
    assert sorted(sum(pages_one, [])) == ["a", "b", "c", "d"]


def test_get_field_value_walks_dotted_paths_and_treats_missing_as_none():
    item = _item("a", "c", gsd=2.0)
    assert get_field_value(item, "properties.gsd") == 2.0
    assert get_field_value(item, "properties.missing.deep") is None
    assert get_field_value(item, "missing") is None


# ---------------------------------------------------------------------------
# Incomparable runtime types
# ---------------------------------------------------------------------------


def test_runtime_value_of_wrong_type_raises_explicitly():
    contract = SortContract.from_get_sortby(["properties.gsd"], SORTABLES)
    bad = [_item("a", "c", gsd="high"), _item("b", "c", gsd=1.0)]
    with pytest.raises(IncomparableSortValueError, match="not comparable"):
        sort_items(bad, contract)


def test_boolean_is_not_accepted_as_number():
    contract = SortContract.from_get_sortby(["properties.gsd"], SORTABLES)
    bad = [_item("a", "c", gsd=True), _item("b", "c", gsd=1.0)]
    with pytest.raises(IncomparableSortValueError, match="boolean"):
        sort_items(bad, contract)


def test_invalid_rfc3339_datetime_raises_explicitly():
    contract = SortContract.from_get_sortby(
        ["properties.datetime"], SORTABLES
    )
    bad = [
        _item("a", "c", datetime="not-a-date"),
        _item("b", "c", datetime="2020-01-01T00:00:00Z"),
    ]
    with pytest.raises(IncomparableSortValueError, match="RFC 3339"):
        sort_items(bad, contract)


def test_absent_and_explicit_null_are_both_missing():
    contract = SortContract.from_get_sortby(
        ["properties.cloud_cover"], SORTABLES
    )
    items = [
        {"id": "a", "collection": "c", "properties": {}},
        _item("b", "c", cloud_cover=None),
        _item("c", "c", cloud_cover=1.0),
    ]
    ordered = [item["id"] for item in sort_items(items, contract)]
    # cloud_cover is nulls-first; the two missing values tie and break on id.
    assert ordered == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Contract shape
# ---------------------------------------------------------------------------


def test_contract_and_keys_are_frozen():
    contract = SortContract.from_get_sortby(["name"], SORTABLES)
    with pytest.raises(Exception):  # noqa: PT011 - frozen dataclass raises
        contract.keys = ()  # type: ignore[misc]
    with pytest.raises(Exception):  # noqa: PT011
        contract.keys[0].field = "other"  # type: ignore[misc]


def test_sort_key_properties():
    key = SortKey(
        field="x",
        direction=SortDirection.DESC,
        null_position=NullPosition.FIRST,
        value_type=SortValueType.NUMBER,
    )
    assert key.is_ascending is False
    assert key.nulls_first is True
    assert SortValueType.STRING.is_orderable is True
    assert SortValueType.OBJECT.is_orderable is False
