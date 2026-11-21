"""Tests for import-time href rebasing."""

import copy

import pytest
from stac_pydantic import Item, ItemCollection

from stac_fastapi.types.errors import RebaseError, UnknownReferenceError
from stac_fastapi.types.links import is_absolute_href
from stac_fastapi.types.rebase import (
    HrefRebaser,
    PrefixMapping,
    rebase_item,
    rebase_item_collection,
)

OLD_PREFIX = "https://stac.internal/catalog"
NEW_PREFIX = "s3://catalog-bucket/catalog"
PREFIX_MAPPINGS = {OLD_PREFIX: NEW_PREFIX}


def make_item(self_href: str | None = f"{OLD_PREFIX}/collections/c/items/i") -> dict:
    links = []
    if self_href is not None:
        links.append({"rel": "self", "href": self_href})
    links.append({"rel": "via", "href": "data/via.json"})
    links.append({"rel": "license", "href": "https://example.com/license.pdf"})
    return {
        "type": "Feature",
        "stac_version": "1.0.0",
        "id": "i",
        "collection": "c",
        "geometry": {"type": "Point", "coordinates": [0, 0]},
        "bbox": [0, 0, 0, 0],
        "properties": {"datetime": "2020-01-01T00:00:00Z"},
        "links": links,
        "assets": {
            "surface": {"href": "data/surface.tif"},
            "external": {"href": "https://elsewhere.example.com/external.tif"},
        },
    }


def test_relative_asset_resolved_against_self_then_remapped():
    """A relative asset resolves against the original self link, then into storage."""
    rebased = rebase_item(make_item(), PREFIX_MAPPINGS)

    assert (
        rebased["assets"]["surface"]["href"]
        == f"{NEW_PREFIX}/collections/c/items/data/surface.tif"
    )


def test_external_absolute_asset_is_preserved():
    rebased = rebase_item(make_item(), PREFIX_MAPPINGS)
    assert (
        rebased["assets"]["external"]["href"]
        == "https://elsewhere.example.com/external.tif"
    )


def test_absolute_link_under_old_prefix_is_remapped():
    item = make_item()
    item["links"].append({"rel": "related", "href": f"{OLD_PREFIX}/collections/d"})

    rebased = rebase_item(item, PREFIX_MAPPINGS)
    related = next(link for link in rebased["links"] if link["rel"] == "related")
    assert related["href"] == f"{NEW_PREFIX}/collections/d"


def test_inferred_links_are_not_rewritten():
    """Generated-on-read links are left alone; only stored hrefs are rebased."""
    rebased = rebase_item(make_item(), PREFIX_MAPPINGS)
    self_link = next(link for link in rebased["links"] if link["rel"] == "self")
    assert self_link["href"] == f"{OLD_PREFIX}/collections/c/items/i"


def test_repeated_import_does_not_stack_prefixes():
    once = rebase_item(make_item(), PREFIX_MAPPINGS)
    twice = rebase_item(once, PREFIX_MAPPINGS)
    assert twice == once


def test_repeated_import_does_not_stack_prefixes_when_self_was_rebased():
    """Re-importing an Item whose self link was already moved stays stable."""
    mappings = {OLD_PREFIX: NEW_PREFIX, NEW_PREFIX: "https://served.example.com"}
    once = rebase_item(make_item(), mappings)
    twice = rebase_item(once, mappings)
    # `self` is inferred, so it is never rewritten; the asset must not gain a
    # second prefix either.
    assert once["assets"]["surface"]["href"].count("catalog-bucket") == 1
    assert twice["assets"]["surface"]["href"] == once["assets"]["surface"]["href"]


def test_prefix_matches_only_on_path_boundary():
    item = make_item()
    item["assets"]["lookalike"] = {"href": f"{OLD_PREFIX}e/asset.tif"}

    rebased = rebase_item(item, {OLD_PREFIX: NEW_PREFIX})
    assert rebased["assets"]["lookalike"]["href"] == f"{OLD_PREFIX}e/asset.tif"


def test_most_specific_prefix_wins_regardless_of_declaration_order():
    item = make_item()
    item["assets"]["deep"] = {"href": f"{OLD_PREFIX}/raw/deep.tif"}

    mappings = {
        OLD_PREFIX: "s3://broad",
        f"{OLD_PREFIX}/raw": "s3://specific",
    }
    rebased = rebase_item(item, mappings)
    assert rebased["assets"]["deep"]["href"] == "s3://specific/deep.tif"
    assert rebased["assets"]["surface"]["href"].startswith("s3://broad/")


def test_unknown_reference_is_not_guessed():
    """Relative hrefs without any self link cannot be resolved and are rejected."""
    with pytest.raises(UnknownReferenceError):
        rebase_item(make_item(self_href=None), PREFIX_MAPPINGS)


def test_relative_self_link_is_rejected():
    with pytest.raises(UnknownReferenceError):
        rebase_item(make_item(self_href="/collections/c/items/i"), PREFIX_MAPPINGS)


def test_conflicting_self_links_are_rejected():
    item = make_item()
    item["links"].append({"rel": "self", "href": "https://other.example.com/items/i"})
    with pytest.raises(RebaseError):
        rebase_item(item, PREFIX_MAPPINGS)


def test_item_without_relative_hrefs_needs_no_self_link():
    """An already-absolute Item imports without guessing anything."""
    item = make_item(self_href=None)
    item["assets"] = {"surface": {"href": "s3://bucket/already.tif"}}
    item["links"] = [{"rel": "license", "href": "https://example.com/l"}]

    rebased = rebase_item(item, PREFIX_MAPPINGS)
    assert rebased["assets"]["surface"]["href"] == "s3://bucket/already.tif"


def test_original_item_is_not_mutated():
    item = make_item()
    snapshot = copy.deepcopy(item)
    rebase_item(item, PREFIX_MAPPINGS)
    assert item == snapshot


def test_accepts_pydantic_item():
    rebased = rebase_item(Item.model_validate(make_item()), PREFIX_MAPPINGS)
    assert rebased["assets"]["surface"]["href"].startswith(NEW_PREFIX)


def test_asset_extra_fields_are_preserved():
    item = make_item()
    item["assets"]["surface"].update({"file:size": 12, "x:meta": {"k": "v"}})

    rebased = rebase_item(item, PREFIX_MAPPINGS)
    asset = rebased["assets"]["surface"]
    assert asset["file:size"] == 12
    assert asset["x:meta"] == {"k": "v"}


def test_item_collection_rebases_every_feature():
    collection = ItemCollection.model_validate(
        {
            "type": "FeatureCollection",
            "features": [make_item(), {**make_item(), "id": "j"}],
            "links": [],
        }
    )

    rebased = rebase_item_collection(collection, PREFIX_MAPPINGS)
    assert [f["id"] for f in rebased["features"]] == ["i", "j"]
    for feature in rebased["features"]:
        assert feature["assets"]["surface"]["href"].startswith(NEW_PREFIX)


def test_no_mappings_still_resolves_relative_hrefs_against_self():
    rebased = HrefRebaser().rebase_item(make_item())
    assert (
        rebased["assets"]["surface"]["href"]
        == f"{OLD_PREFIX}/collections/c/items/data/surface.tif"
    )


def test_prefix_mapping_rejects_empty_prefix():
    with pytest.raises(ValueError):
        PrefixMapping(source="", target="s3://bucket")


def test_conflicting_prefix_declarations_are_rejected():
    with pytest.raises(ValueError):
        HrefRebaser(prefix_mappings=[(OLD_PREFIX, "s3://a"), (OLD_PREFIX, "s3://b")])


def test_stored_item_has_no_relative_hrefs_left_to_resolve():
    """An imported record is fully absolute, so both read paths agree.

    Rebasing happens before storage: the item endpoint and the search endpoints
    both return the stored href, so neither has to resolve anything against the
    request URL and the two cannot diverge.
    """
    stored = rebase_item(make_item(), PREFIX_MAPPINGS)

    stored_hrefs = [
        *(asset["href"] for asset in stored["assets"].values()),
        *(
            link["href"]
            for link in stored["links"]
            if link["rel"] not in {"self", "parent", "collection", "root"}
        ),
    ]

    assert all(is_absolute_href(href) for href in stored_hrefs)
    assert (
        stored["assets"]["surface"]["href"]
        == f"{NEW_PREFIX}/collections/c/items/data/surface.tif"
    )


def test_relative_href_resolves_under_object_storage_self_link():
    """A relative href resolves even when the original reference uses s3://."""
    item = make_item(self_href="s3://catalog-bucket/catalog/collections/c/items/i")
    item["assets"]["surface"] = {"href": "data/surface.tif"}

    rebased = rebase_item(item, {})
    assert (
        rebased["assets"]["surface"]["href"]
        == "s3://catalog-bucket/catalog/collections/c/items/data/surface.tif"
    )


@pytest.mark.parametrize(
    ("base", "href", "expected"),
    [
        (
            "s3://bucket/a/b/c",
            "../d.tif",
            "s3://bucket/a/d.tif",
        ),
        (
            "s3://bucket/a/b/c",
            "/root/d.tif",
            "s3://bucket/root/d.tif",
        ),
        (
            "https://host/a/b/c",
            "d/e.tif",
            "https://host/a/b/d/e.tif",
        ),
        (
            "file:///data/a/b/c",
            "d.tif",
            "file:///data/a/b/d.tif",
        ),
    ],
)
def test_resolution_supports_multiple_schemes(base, href, expected):
    rebased = HrefRebaser().rebase_href(href, base)
    assert rebased == expected
