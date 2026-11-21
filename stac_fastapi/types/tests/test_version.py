"""Tests for item versions and entity tags."""

import datetime

import pytest

from stac_fastapi.types.etag import (
    canonical_item,
    item_etag,
    item_version,
    parse_etags,
    quote_etag,
    unquote_etag,
    version_matches,
)


def item(**overrides) -> dict:
    base = {
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
    base.update(overrides)
    return base


def test_version_is_stable_for_the_same_item() -> None:
    assert item_version(item()) == item_version(item())


def test_version_ignores_member_order() -> None:
    first = {"type": "Feature", "id": "a", "properties": {"x": 1, "y": 2}}
    second = {"id": "a", "properties": {"y": 2, "x": 1}, "type": "Feature"}
    assert item_version(first) == item_version(second)


def test_version_changes_when_a_property_changes() -> None:
    changed = item(properties={"datetime": "2020-06-13T13:00:00Z", "foo": "bar"})
    assert item_version(item()) != item_version(changed)


def test_version_treats_datetime_and_its_string_alike() -> None:
    """A backend stores the string form; the API may still hold a datetime."""
    as_string = item(properties={"datetime": "2020-06-13T13:00:00Z"})
    as_datetime = item(
        properties={
            "datetime": datetime.datetime(
                2020, 6, 13, 13, 0, 0, tzinfo=datetime.timezone.utc
            )
        }
    )
    assert item_version(as_string) == item_version(as_datetime)


def test_version_treats_offsets_as_utc() -> None:
    utc = item(properties={"datetime": "2020-06-13T13:00:00Z"})
    offset = item(properties={"datetime": "2020-06-13T15:00:00+02:00"})
    assert item_version(utc) == item_version(offset)


def test_version_ignores_fractional_second_padding() -> None:
    plain = item(properties={"datetime": "2020-06-13T13:00:00Z"})
    padded = item(properties={"datetime": "2020-06-13T13:00:00.000000Z"})
    assert item_version(plain) == item_version(padded)


def test_version_does_not_conflate_other_strings() -> None:
    """Only RFC 3339 datetimes are normalized; a version string is not."""
    first = item(properties={"version": "1.0.0"})
    second = item(properties={"version": "1.0.0", "other": "1.0.0"})
    assert item_version(first) != item_version(second)


def test_version_treats_an_integer_float_alike() -> None:
    """A response model may turn an integer coordinate into a float."""
    as_int = item(bbox=[-105, 40, -105, 40])
    as_float = item(bbox=[-105.0, 40.0, -105.0, 40.0])
    assert item_version(as_int) == item_version(as_float)


def test_version_keeps_fractional_numbers_distinct() -> None:
    assert item_version({"v": 1.5}) != item_version({"v": 1})


def test_version_omits_null_members() -> None:
    """A response serialized with `exclude_none` drops nulls; the version agrees."""
    with_null = item(properties={"datetime": None}, extra=None)
    without_extra = item(properties={"datetime": None})
    assert item_version(with_null) == item_version(without_extra)


def test_canonical_item_does_not_modify_the_input() -> None:
    original = item(properties={"datetime": "2020-06-13T13:00:00Z"})
    snapshot = repr(original)
    canonical_item(original)
    assert repr(original) == snapshot


def test_quote_and_unquote_round_trip() -> None:
    assert quote_etag("abc") == '"abc"'
    assert unquote_etag('"abc"') == "abc"
    assert quote_etag('"abc"') == '"abc"'
    assert unquote_etag('W/"abc"') == "abc"


def test_parse_etags_lists_tokens() -> None:
    assert parse_etags('"a", "b"') == ["a", "b"]
    assert parse_etags("*") == ["*"]
    assert parse_etags('W/"weak"') == ["weak"]


def test_version_matches() -> None:
    current = item_version(item())
    assert version_matches(current, current)
    assert version_matches(quote_etag(current), current)
    assert version_matches("*", current)
    assert version_matches(f'"other", "{current}"', current)
    assert not version_matches('"other"', current)


def test_item_etag_only_for_a_single_item() -> None:
    assert item_etag(item()) == quote_etag(item_version(item()))
    assert item_etag(None) is None
    assert item_etag("not an item") is None
    assert (
        item_etag({"type": "FeatureCollection", "features": [], "links": []}) is None
    )


def test_item_version_accepts_a_pydantic_item() -> None:
    from stac_pydantic import Item

    model = Item(**item())
    assert item_version(model) == item_version(model.model_dump(mode="json"))


def test_item_version_rejects_other_values() -> None:
    with pytest.raises(TypeError):
        item_version(42)
