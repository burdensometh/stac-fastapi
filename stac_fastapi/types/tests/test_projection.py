"""Tests for the unified Fields projection helpers."""

import pytest

from stac_fastapi.types.fields import (
    ALWAYS_INCLUDED_FIELDS,
    FieldsProjection,
    extract_filter_fields,
    extract_sort_fields,
    filter_fields,
    get_backend_projection,
    get_fields_projection,
    resolve_selection,
    split_fields,
    trim_payload,
)

item = {
    "type": "Feature",
    "stac_version": "1.0.0",
    "stac_extensions": ["https://example.com/ext.json"],
    "id": "item-1",
    "collection": "collection-1",
    "geometry": {"type": "Point", "coordinates": [0, 0]},
    "bbox": [0, 0, 0, 0],
    "properties": {
        "datetime": "2020-01-01T00:00:00Z",
        "eo:cloud_cover": 12,
    },
    "links": [{"rel": "self", "href": "https://example.com/items/item-1"}],
    "assets": {
        "preview": {
            "href": "https://example.com/preview.png",
            "type": "image/png",
            "roles": ["thumbnail"],
        },
        "data": {"href": "https://example.com/data.tif", "type": "image/tiff"},
    },
}


def test_split_fields_get_param():
    include, exclude = split_fields(["id", "-geometry", "+assets.preview.href"])
    assert include == {"id", "assets.preview.href"}
    assert exclude == {"geometry"}


def test_split_fields_ignores_empty_names():
    include, exclude = split_fields(["", "-", "+", "  "])
    assert include == frozenset()
    assert exclude == frozenset()


def test_resolve_same_field_in_both_is_included():
    include, exclude = resolve_selection(["datetime"], ["datetime"])
    assert include == {"datetime"}
    assert exclude == frozenset()


def test_resolve_excluded_parent_with_included_child():
    include, exclude = resolve_selection(["properties.datetime"], ["properties"])
    assert include == {"properties.datetime"}
    # The ancestor exclude is dropped: the more specific include wins.
    assert exclude == frozenset()


def test_resolve_included_parent_with_excluded_child():
    include, exclude = resolve_selection(["properties"], ["properties.datetime"])
    assert include == {"properties"}
    assert exclude == {"properties.datetime"}


def test_resolve_is_order_independent():
    first = resolve_selection(["assets", "assets.preview.href"], [])
    second = resolve_selection(["assets.preview.href", "assets"], [])
    assert first == second


def test_projection_from_param_get():
    projection = FieldsProjection.from_param("id,-geometry")
    assert projection.include == {"id"}
    assert projection.exclude == {"geometry"}
    assert projection.requested
    assert projection.is_projection


def test_projection_from_sets_post():
    projection = FieldsProjection.from_sets(["id"], ["geometry"])
    assert projection.include == {"id"}
    assert projection.exclude == {"geometry"}


def test_exclude_only_has_no_backend_projection():
    projection = FieldsProjection.from_param("-geometry")
    assert projection.include == frozenset()
    assert projection.exclude == {"geometry"}
    assert not projection.is_projection
    # Exclude-only searches must keep reading full documents.
    assert projection.backend_fields() is None


def test_empty_selection_has_no_backend_projection():
    assert FieldsProjection.from_param(None).backend_fields() is None
    assert FieldsProjection.from_param("").backend_fields() is None
    assert FieldsProjection.from_sets([], []).backend_fields() is None


def test_backend_fields_keep_identity_and_structure():
    projection = FieldsProjection.from_param("assets.preview.href")
    backend = projection.backend_fields()
    assert backend is not None
    for field in ALWAYS_INCLUDED_FIELDS:
        assert field in backend
    assert "assets.preview.href" in backend


def test_backend_fields_keep_filter_and_sort_fields():
    projection = FieldsProjection.from_param("assets.preview.href").with_search_fields(
        {"properties.datetime"}, {"properties.eo:cloud_cover"}
    )
    backend = projection.backend_fields()
    assert "assets.preview.href" in backend
    # Needed to evaluate the query even though they are not returned.
    assert "properties.datetime" in backend
    assert "properties.eo:cloud_cover" in backend


def test_filter_fields_nested_include_trim():
    projection = FieldsProjection.from_param("assets.preview.href")
    trimmed = filter_fields(item, projection)
    assert trimmed["assets"] == {"preview": {"href": "https://example.com/preview.png"}}
    assert trimmed["id"] == "item-1"
    assert trimmed["collection"] == "collection-1"
    assert "geometry" not in trimmed
    assert "properties" not in trimmed


def test_filter_fields_keeps_structural_fields():
    projection = FieldsProjection.from_param("assets")
    trimmed = filter_fields(item, projection)
    assert trimmed["type"] == "Feature"
    assert trimmed["stac_version"] == "1.0.0"


def test_filter_fields_exclude_deep_path():
    projection = FieldsProjection.from_param("-assets.preview.href")
    trimmed = filter_fields(item, projection)
    assert trimmed["assets"]["preview"] == {
        "type": "image/png",
        "roles": ["thumbnail"],
    }
    assert trimmed["assets"]["data"]["href"] == "https://example.com/data.tif"


def test_filter_fields_excluding_parent_keeps_included_child():
    projection = FieldsProjection.from_sets(
        include=["assets.preview.href"], exclude=["assets"]
    )
    trimmed = filter_fields(item, projection)
    assert trimmed["assets"] == {"preview": {"href": "https://example.com/preview.png"}}


def test_filter_fields_including_parent_excluding_child():
    projection = FieldsProjection.from_sets(
        include=["assets"], exclude=["assets.preview.href"]
    )
    trimmed = filter_fields(item, projection)
    assert trimmed["assets"]["data"]["href"] == "https://example.com/data.tif"
    assert trimmed["assets"]["preview"] == {
        "type": "image/png",
        "roles": ["thumbnail"],
    }


def test_filter_fields_identity_always_kept():
    projection = FieldsProjection.from_sets(include=["geometry"], exclude=["id"])
    trimmed = filter_fields(item, projection)
    assert trimmed["id"] == "item-1"
    assert trimmed["collection"] == "collection-1"


def test_filter_fields_empty_projection_returns_copy():
    trimmed = filter_fields(item, FieldsProjection())
    assert trimmed == item
    assert trimmed is not item


def test_filter_fields_keeps_preexisting_empty_containers():
    """An empty object in the source is not mistaken for an excluded one."""
    source = {**item, "properties": {**item["properties"], "extra": {}}}
    trimmed = filter_fields(source, FieldsProjection.from_param("-geometry"))
    assert trimmed["properties"]["extra"] == {}


def test_filter_fields_drops_container_emptied_by_exclude():
    projection = FieldsProjection.from_param("-assets.preview.href")
    trimmed = filter_fields({**item, "assets": {"preview": {"href": "x"}}}, projection)
    # Every child of ``assets`` was excluded, so the container is dropped.
    assert "assets" not in trimmed


def test_filter_fields_accepts_include_exclude_sets():
    trimmed = filter_fields(item, {"assets.preview.href"}, set())
    assert trimmed["assets"] == {"preview": {"href": "https://example.com/preview.png"}}


def test_filter_fields_parent_and_child_include_order_independent():
    one = filter_fields(item, {"assets", "assets.preview.href"})
    two = filter_fields(item, {"assets.preview.href", "assets"})
    assert one == two
    assert one["assets"]["data"]["href"] == "https://example.com/data.tif"


def test_trim_payload_trims_features_only():
    payload = {
        "type": "FeatureCollection",
        "features": [item, {**item, "id": "item-2"}],
        "links": [{"rel": "next", "href": "https://example.com/next"}],
        "numberReturned": 2,
    }
    trimmed = trim_payload(payload, FieldsProjection.from_param("id"))
    assert trimmed["type"] == "FeatureCollection"
    assert trimmed["numberReturned"] == 2
    assert trimmed["links"] == payload["links"]
    assert [f["id"] for f in trimmed["features"]] == ["item-1", "item-2"]
    assert "geometry" not in trimmed["features"][0]


def test_trim_payload_trims_collections_only():
    payload = {
        "collections": [{"id": "c", "title": "t", "description": "d"}],
        "links": [],
    }
    trimmed = trim_payload(payload, FieldsProjection.from_param("title"))
    assert trimmed["collections"][0]["title"] == "t"
    assert trimmed["collections"][0]["id"] == "c"
    assert "description" not in trimmed["collections"][0]


def test_trim_payload_passthrough_when_empty():
    payload = {"features": [item], "links": []}
    assert trim_payload(payload, FieldsProjection()) is payload


def test_get_fields_projection_from_request_model():
    class Request:
        fields = type("Fields", (), {"include": {"id"}, "exclude": set()})()

    projection = get_fields_projection(Request())
    assert projection is not None
    assert projection.include == {"id"}


def test_get_fields_projection_none():
    assert get_fields_projection(None) is None

    class Request:
        fields = None

    assert get_fields_projection(Request()) is None


def test_get_fields_projection_records_filter_and_sort():
    class Request:
        fields = type("Fields", (), {"include": {"id"}, "exclude": set()})()
        filter_expr = {
            "op": "=",
            "args": [{"property": "eo:cloud_cover"}, 10],
        }
        sortby = [{"field": "properties.datetime", "direction": "asc"}]

    projection = get_fields_projection(Request())
    assert projection is not None
    assert "eo:cloud_cover" in projection.search_fields
    assert "properties.datetime" in projection.search_fields


def test_get_backend_projection():
    class Request:
        fields = type(
            "Fields", (), {"include": {"assets.preview.href"}, "exclude": set()}
        )()

    backend = get_backend_projection(Request())
    assert backend is not None
    assert "assets.preview.href" in backend
    assert "id" in backend

    class NoProjection:
        fields = None

    assert get_backend_projection(NoProjection()) is None


def test_extract_filter_fields_cql2_json():
    filt = {
        "op": "and",
        "args": [
            {"op": "=", "args": [{"property": "eo:cloud_cover"}, 10]},
            {"op": ">=", "args": [{"property": "properties.datetime"}, "2020-01-01"]},
        ],
    }
    assert extract_filter_fields(filt) == {"eo:cloud_cover", "properties.datetime"}


def test_extract_filter_fields_cql2_text():
    fields = extract_filter_fields(
        "eo:cloud_cover > 10 AND \"properties.datetime\" > 'date'"
    )
    assert fields == {"eo:cloud_cover", "properties.datetime"}


def test_extract_filter_fields_ignores_keywords_and_literals():
    assert extract_filter_fields("cloud_cover BETWEEN 1 AND 2") == {"cloud_cover"}


def test_extract_sort_fields_forms():
    assert extract_sort_fields("+properties.datetime") == {"properties.datetime"}
    assert extract_sort_fields(["-id", "+gsd"]) == {"id", "gsd"}
    assert extract_sort_fields([{"field": "properties.created"}]) == {
        "properties.created"
    }

    class Sort:
        def __init__(self, field):
            self.field = field

    assert extract_sort_fields([Sort("gsd")]) == {"gsd"}


@pytest.mark.parametrize(
    "include,exclude,contained,not_contained",
    [
        # include only
        (["id"], [], {"id"}, {"geometry"}),
        # exclude only
        ([], ["geometry"], {"id"}, {"geometry"}),
        # both, same field
        (["id"], ["id"], {"id"}, {"geometry"}),
        # parent excluded, child included
        (["properties.datetime"], ["properties"], {"properties.datetime"}, set()),
        # parent included, child excluded
        (["properties"], ["properties.datetime"], {"properties"}, set()),
    ],
)
def test_projection_contains(include, exclude, contained, not_contained):
    projection = FieldsProjection.from_sets(include, exclude)
    for field in contained:
        assert projection.contains(field), field
    for field in not_contained:
        assert not projection.contains(field), field
