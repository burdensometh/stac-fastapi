"""Tests for CQL2 parsing, evaluation, splitting and partial pushdown search."""

import asyncio

import pytest

from stac_fastapi.types import cql2
from stac_fastapi.types.partial import (
    CandidatePage,
    PartialPushdownSearch,
    to_item_collection,
)

ALL_OPERATORS = cql2.LOCAL_OPERATORS | {"s_intersects"}

_SPATIAL = {
    "op": "s_intersects",
    "args": [
        {"property": "geometry"},
        {"type": "Point", "coordinates": [0, 0]},
    ],
}


class Backend:
    """A fake backend executing a CQL2 subset over an in-memory list.

    It honours the pushdown it is given (using :func:`cql2.evaluate` itself, so
    unsupported operators raise), paginates with opaque tokens and reports
    ``number_matched`` for its pushdown.
    """

    def __init__(self, items, page_size=None):
        self.items = list(items)
        self.calls = 0
        self.returned = []
        self.page_size = page_size

    def fetch(self, request, limit, token):
        self.calls += 1
        # The backend executes the CQL2 pushdown, plus the request selectors
        # (collections/id/bbox/datetime) it always understands.
        matching = []
        for item in self.items:
            if request.pushdown is not None and not cql2.evaluate(
                request.pushdown, item
            ):
                continue
            if request.collections and item.get("collection") not in request.collections:
                continue
            if request.ids and item.get("id") not in request.ids:
                continue
            if request.bbox is not None and not cql2.evaluate(
                cql2.selectors_to_filter(bbox=request.bbox), item
            ):
                continue
            if request.datetime and not cql2.evaluate(
                cql2.selectors_to_filter(datetime=request.datetime), item
            ):
                continue
            matching.append(item)
        start = int(token or 0)
        size = self.page_size or limit
        window = matching[start : start + size]
        self.returned.extend(window)
        end = start + size
        next_token = str(end) if end < len(matching) else None
        return CandidatePage(
            features=window,
            next_token=next_token,
            number_matched=len(matching),
        )


def make_items():
    items = []
    for n in range(25):
        items.append(
            {
                "id": f"item-{n}",
                "collection": "c1" if n % 2 == 0 else "c2",
                "type": "Feature",
                "properties": {"eo:cloud_cover": n, "platform": "a" if n < 10 else "b"},
            }
        )
    return items


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_cql2_json_passthrough():
    expr = {"op": "=", "args": [{"property": "id"}, "a"]}
    assert cql2.parse_cql2(expr) == expr
    assert cql2.parse_cql2(None) is None


def test_parse_cql2_json_string():
    import json

    expr = {"op": "=", "args": [{"property": "id"}, "a"]}
    assert cql2.parse_cql2(json.dumps(expr), "cql2-json") == expr


@pytest.mark.parametrize(
    "text,expected",
    [
        ("id='a'", {"op": "=", "args": [{"property": "id"}, "a"]}),
        (
            "a = 1 AND b <> 2",
            {
                "op": "and",
                "args": [
                    {"op": "=", "args": [{"property": "a"}, 1]},
                    {"op": "<>", "args": [{"property": "b"}, 2]},
                ],
            },
        ),
        (
            "a = 1 OR b = 2 AND c = 3",
            {
                "op": "or",
                "args": [
                    {"op": "=", "args": [{"property": "a"}, 1]},
                    {
                        "op": "and",
                        "args": [
                            {"op": "=", "args": [{"property": "b"}, 2]},
                            {"op": "=", "args": [{"property": "c"}, 3]},
                        ],
                    },
                ],
            },
        ),
        (
            "NOT a = 1",
            {"op": "not", "args": [{"op": "=", "args": [{"property": "a"}, 1]}]},
        ),
        (
            "a NOT LIKE 'x%'",
            {"op": "notLike", "args": [{"property": "a"}, "x%"]},
        ),
        (
            "a BETWEEN 1 AND 3",
            {"op": "between", "args": [{"property": "a"}, 1, 3]},
        ),
        (
            "a NOT IN ('x', 'y')",
            {"op": "notIn", "args": [{"property": "a"}, ["x", "y"]]},
        ),
        ("a IS NULL", {"op": "isNull", "args": [{"property": "a"}]}),
        ("a IS NOT NULL", {"op": "isNotNull", "args": [{"property": "a"}]}),
        (
            "datetime > TIMESTAMP('2020-01-01T00:00:00Z')",
            {
                "op": ">",
                "args": [
                    {"property": "datetime"},
                    {"timestamp": "2020-01-01T00:00:00Z"},
                ],
            },
        ),
        (
            "\"eo:cloud_cover\" = 10",
            {"op": "=", "args": [{"property": "eo:cloud_cover"}, 10]},
        ),
        ("a = 1 + 2 * 3", None),
    ],
)
def test_parse_cql2_text(text, expected):
    parsed = cql2.parse_cql2_text(text)
    if expected is not None:
        assert parsed == expected


def test_parse_cql2_text_arithmetic_precedence():
    parsed = cql2.parse_cql2_text("a = 1 + 2 * 3")
    assert parsed == {
        "op": "=",
        "args": [
            {"property": "a"},
            {
                "op": "+",
                "args": [1, {"op": "*", "args": [2, 3]}],
            },
        ],
    }


def test_parse_cql2_text_negative_number():
    assert cql2.parse_cql2_text("a = -5") == {
        "op": "=",
        "args": [{"property": "a"}, -5],
    }


@pytest.mark.parametrize(
    "text",
    ["a =", "a === 1", "a IN ()", "a = 'unterminated", "", "a AND (b"],
)
def test_parse_cql2_text_errors(text):
    with pytest.raises(cql2.InvalidCql2Expression):
        cql2.parse_cql2_text(text)


def test_parse_cql2_text_geometry_rejected():
    with pytest.raises(cql2.UnsupportedCql2Function):
        cql2.parse_cql2_text("s_intersects(geometry, POINT(0 0))")


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def test_evaluate_comparisons():
    item = {"id": "a", "properties": {"n": 5}}
    assert cql2.evaluate({"op": "=", "args": [{"property": "id"}, "a"]}, item)
    assert not cql2.evaluate({"op": "!=", "args": [{"property": "id"}, "a"]}, item)
    assert cql2.evaluate({"op": ">", "args": [{"property": "properties.n"}, 4]}, item)
    assert cql2.evaluate({"op": "<=", "args": [{"property": "n"}, 5]}, item)
    assert not cql2.evaluate({"op": ">", "args": [{"property": "n"}, 6]}, item)


def test_evaluate_missing_property_never_matches():
    item = {"id": "a", "properties": {}}
    assert not cql2.evaluate({"op": "=", "args": [{"property": "n"}, None]}, item)
    assert cql2.evaluate({"op": "isNull", "args": [{"property": "n"}]}, item)
    assert cql2.evaluate({"op": "isNotNull", "args": [{"property": "id"}]}, item)


def test_evaluate_like():
    item = {"properties": {"name": "Sentinel-2"}}
    assert cql2.evaluate({"op": "like", "args": [{"property": "name"}, "Sent%"]}, item)
    assert cql2.evaluate(
        {"op": "like", "args": [{"property": "name"}, "Sentinel-_"]}, item
    )
    assert not cql2.evaluate({"op": "like", "args": [{"property": "name"}, "Sent"]}, item)
    assert cql2.evaluate({"op": "notLike", "args": [{"property": "name"}, "Land%"]}, item)


def test_evaluate_between_and_in():
    item = {"properties": {"n": 5}}
    assert cql2.evaluate({"op": "between", "args": [{"property": "n"}, 1, 9]}, item)
    assert not cql2.evaluate({"op": "between", "args": [{"property": "n"}, 6, 9]}, item)
    assert cql2.evaluate({"op": "in", "args": [{"property": "n"}, [1, 5, 7]]}, item)
    assert cql2.evaluate({"op": "notIn", "args": [{"property": "n"}, [1, 2]]}, item)


def test_evaluate_arithmetic():
    item = {"properties": {"n": 5}}
    expr = {
        "op": "=",
        "args": [{"op": "+", "args": [{"property": "n"}, 1]}, 6],
    }
    assert cql2.evaluate(expr, item)


def test_evaluate_temporal():
    item = {
        "properties": {
            "datetime": None,
            "start_datetime": "2020-01-01T00:00:00Z",
            "end_datetime": "2020-01-02T00:00:00Z",
        }
    }
    after = {
        "op": "t_after",
        "args": [
            {"property": "properties"},
            {"interval": ["2019-01-01T00:00:00Z", "2019-12-31T00:00:00Z"]},
        ],
    }
    during = {
        "op": "t_during",
        "args": [
            {"property": "properties"},
            {"interval": ["2019-01-01T00:00:00Z", "2021-01-01T00:00:00Z"]},
        ],
    }
    disjoint = {
        "op": "t_during",
        "args": [
            {"property": "properties"},
            {"interval": ["2022-01-01T00:00:00Z", "2023-01-01T00:00:00Z"]},
        ],
    }
    assert cql2.evaluate(after, item)
    assert cql2.evaluate(during, item)
    assert not cql2.evaluate(disjoint, item)


def test_evaluate_timestamp_string_coercion():
    item = {"properties": {"datetime": "2020-05-01T00:00:00Z"}}
    expr = {
        "op": ">",
        "args": [
            {"property": "datetime"},
            {"timestamp": "2020-01-01T00:00:00Z"},
        ],
    }
    assert cql2.evaluate(expr, item)


def test_evaluate_rejects_unsupported():
    with pytest.raises(cql2.UnsupportedCql2Function):
        cql2.evaluate(_SPATIAL, {"geometry": {"type": "Point", "coordinates": [0, 0]}})


def test_evaluate_unknown_operator_rejected():
    with pytest.raises(cql2.UnsupportedCql2Function):
        cql2.evaluate({"op": "frobnicate", "args": []}, {})


# ---------------------------------------------------------------------------
# Splitting
# ---------------------------------------------------------------------------


FULL = cql2.PushdownCapabilities(operators=ALL_OPERATORS)


def test_split_everything_pushable():
    expr = {"op": "=", "args": [{"property": "id"}, "a"]}
    result = cql2.split_filter(expr, FULL)
    assert result.pushdown == expr
    assert result.residual is None
    assert result.is_complete


def test_split_nothing_pushable():
    expr = {"op": "=", "args": [{"property": "id"}, "a"]}
    result = cql2.split_filter(expr, cql2.PushdownCapabilities())
    assert result.pushdown is None
    assert result.residual == expr
    assert not result.is_complete
    assert result.matches_all


def test_split_and_keeps_every_term():
    spatial = {
        "op": "s_intersects",
        "args": [{"property": "geometry"}, {"type": "Point", "coordinates": [0, 0]}],
    }
    expr = {
        "op": "and",
        "args": [
            {"op": "=", "args": [{"property": "collection"}, "c1"]},
            {"op": ">", "args": [{"property": "eo:cloud_cover"}, 10]},
            spatial,
        ],
    }
    caps = cql2.PushdownCapabilities(operators={"=", ">", "s_intersects"})
    result = cql2.split_filter(expr, caps)
    # Every term is pushed; none is dropped or duplicated.
    assert result.pushdown == expr
    assert result.residual is None


def test_split_and_unsplittable_branch_rejects():
    spatial = {
        "op": "s_intersects",
        "args": [{"property": "geometry"}, {"type": "Point", "coordinates": [0, 0]}],
    }
    expr = {
        "op": "and",
        "args": [
            {"op": "=", "args": [{"property": "id"}, "a"]},
            spatial,
        ],
    }
    with pytest.raises(cql2.UnsupportedCql2Function):
        cql2.split_filter(expr, cql2.PushdownCapabilities(operators={"="}))


def test_split_and_pushes_supported_and_keeps_residual():
    expr = {
        "op": "and",
        "args": [
            {"op": "=", "args": [{"property": "collection"}, "c1"]},
            {"op": ">", "args": [{"property": "eo:cloud_cover"}, 10]},
        ],
    }
    caps = cql2.PushdownCapabilities(operators={"="}, properties={"collection"})
    result = cql2.split_filter(expr, caps)
    assert result.pushdown == {"op": "=", "args": [{"property": "collection"}, "c1"]}
    assert result.residual == {"op": ">", "args": [{"property": "eo:cloud_cover"}, 10]}
    assert not result.is_complete


def test_split_and_all_terms_pushed():
    expr = {
        "op": "and",
        "args": [
            {"op": "=", "args": [{"property": "collection"}, "c1"]},
            {"op": ">", "args": [{"property": "eo:cloud_cover"}, 10]},
        ],
    }
    result = cql2.split_filter(expr, FULL)
    assert result.pushdown == expr
    assert result.residual is None


def test_split_or_all_branches_pushable():
    expr = {
        "op": "or",
        "args": [
            {"op": "=", "args": [{"property": "a"}, 1]},
            {"op": "=", "args": [{"property": "b"}, 2]},
        ],
    }
    result = cql2.split_filter(expr, FULL)
    assert result.pushdown == expr
    assert result.residual is None


def test_split_or_partial_branch_not_pushed():
    expr = {
        "op": "or",
        "args": [
            {"op": "=", "args": [{"property": "a"}, 1]},
            {"op": "=", "args": [{"property": "not_indexed"}, 2]},
        ],
    }
    caps = cql2.PushdownCapabilities(operators={"="}, properties={"a"})
    result = cql2.split_filter(expr, caps)
    # Pushing only the first branch would drop items matching only the second.
    assert result.pushdown is None
    assert result.residual == expr


def test_split_or_contains_unpushable_predicate_rejects():
    expr = {
        "op": "or",
        "args": [
            {"op": "=", "args": [{"property": "a"}, 1]},
            _SPATIAL,
        ],
    }
    with pytest.raises(cql2.UnsupportedCql2Function):
        cql2.split_filter(expr, cql2.PushdownCapabilities(operators={"="}))


def test_split_not_whole_pushdown():
    expr = {"op": "not", "args": [{"op": "=", "args": [{"property": "a"}, 1]}]}
    result = cql2.split_filter(expr, FULL)
    assert result.pushdown == expr
    assert result.residual is None


def test_split_not_partial_stays_local():
    expr = {
        "op": "not",
        "args": [{"op": ">", "args": [{"property": "not_indexed"}, 1]}],
    }
    caps = cql2.PushdownCapabilities(operators={">"}, properties={"a"})
    result = cql2.split_filter(expr, caps)
    assert result.pushdown is None
    assert result.residual == expr


def test_split_spatial_pushed_when_advertised():
    result = cql2.split_filter(_SPATIAL, FULL)
    assert result.pushdown == _SPATIAL
    assert result.residual is None


def test_split_spatial_rejected_when_not_advertised():
    with pytest.raises(cql2.UnsupportedCql2Function):
        cql2.split_filter(_SPATIAL, cql2.PushdownCapabilities(operators={"="}))


def test_split_none_is_complete():
    result = cql2.split_filter(None, cql2.PushdownCapabilities())
    assert result.pushdown is None
    assert result.residual is None
    assert result.is_complete


def test_split_does_not_mutate_input():
    expr = {"op": "and", "args": [{"op": "=", "args": [{"property": "a"}, 1]}]}
    snapshot = repr(expr)
    cql2.split_filter(expr, FULL)
    assert repr(expr) == snapshot


def test_split_unknown_function_pushed_only_if_advertised():
    expr = {"op": "lower", "args": [{"property": "name"}]}
    with pytest.raises(cql2.UnsupportedCql2Function):
        cql2.split_filter(expr, cql2.PushdownCapabilities())
    result = cql2.split_filter(
        expr, cql2.PushdownCapabilities(functions=frozenset({"lower"}))
    )
    assert result.pushdown == expr


def test_referenced_properties():
    expr = {
        "op": "and",
        "args": [
            {"op": "=", "args": [{"property": "a"}, {"property": "b"}]},
            {"op": ">", "args": [{"property": "properties.c"}, 1]},
        ],
    }
    assert cql2.referenced_properties(expr) == {"a", "b", "properties.c"}


# ---------------------------------------------------------------------------
# Search & pagination
# ---------------------------------------------------------------------------


def test_search_exact_filter_full_page():
    backend = Backend(make_items())
    search = PartialPushdownSearch(
        {"op": "=", "args": [{"property": "collection"}, "c1"]},
        capabilities=cql2.PushdownCapabilities(operators={"="}),
        candidate_page_size=10,
    )
    page = search.page_sync(backend.fetch, limit=100)
    assert [i["id"] for i in page["features"]] == [f"item-{n}" for n in range(0, 25, 2)]
    assert page["number_matched"] == 13
    assert page["next_token"] is None
    assert search.is_exact


def test_search_residual_filters_candidates():
    backend = Backend(make_items())
    # The backend cannot filter on cloud cover; it must be filtered locally.
    search = PartialPushdownSearch(
        {
            "op": "and",
            "args": [
                {"op": "=", "args": [{"property": "collection"}, "c1"]},
                {"op": "<", "args": [{"property": "eo:cloud_cover"}, 8]},
            ],
        },
        capabilities=cql2.PushdownCapabilities(operators={"="}),
        candidate_page_size=3,
    )
    expected = [f"item-{n}" for n in range(0, 8, 2)]
    seen = []
    token = None
    while True:
        page = search.page_sync(backend.fetch, limit=2, token=token)
        seen.extend(i["id"] for i in page["features"])
        token = page["next_token"]
        if token is None:
            break
    assert seen == expected


def test_search_pagination_never_splits_matches_across_limits():
    backend = Backend(make_items())
    search = PartialPushdownSearch(
        {"op": "<", "args": [{"property": "eo:cloud_cover"}, 10]},
        capabilities=cql2.PushdownCapabilities(),
        candidate_page_size=2,
    )
    seen = []
    token = None
    while True:
        page = search.page_sync(backend.fetch, limit=3, token=token)
        assert page["number_returned"] <= 3
        seen.extend(i["id"] for i in page["features"])
        token = page["next_token"]
        if token is None:
            break
    assert seen == [f"item-{n}" for n in range(10)]


def test_search_pagination_skips_items_within_candidate_page():
    # Both matches and non-matches interleave inside every candidate page.
    backend = Backend(make_items())
    search = PartialPushdownSearch(
        {"op": "=", "args": [{"property": "properties.platform"}, "a"]},
        capabilities=cql2.PushdownCapabilities(),
        candidate_page_size=4,
    )
    seen = []
    token = None
    while True:
        page = search.page_sync(backend.fetch, limit=2, token=token)
        seen.extend(i["id"] for i in page["features"])
        token = page["next_token"]
        if token is None:
            break
    assert seen == [f"item-{n}" for n in range(10)]
    assert len(seen) == len(set(seen))


def test_search_number_matched_unknown_with_residual():
    backend = Backend(make_items())
    search = PartialPushdownSearch(
        {"op": "<", "args": [{"property": "eo:cloud_cover"}, 10]},
        capabilities=cql2.PushdownCapabilities(),
        candidate_page_size=5,
    )
    page = search.page_sync(backend.fetch, limit=3)
    assert page["number_matched"] is None


def test_search_number_matched_exact_when_drained():
    backend = Backend(make_items())
    search = PartialPushdownSearch(
        {"op": "<", "args": [{"property": "eo:cloud_cover"}, 10]},
        capabilities=cql2.PushdownCapabilities(),
        candidate_page_size=5,
    )
    seen = []
    token = None
    while True:
        page = search.page_sync(backend.fetch, limit=3, token=token)
        seen.extend(page["features"])
        token = page["next_token"]
        if token is None:
            break
    assert len(seen) == 10
    assert page["number_matched"] == 10


def test_search_count_all_reports_exact():
    backend = Backend(make_items())
    search = PartialPushdownSearch(
        {"op": "<", "args": [{"property": "eo:cloud_cover"}, 10]},
        capabilities=cql2.PushdownCapabilities(),
        candidate_page_size=5,
        count_all=True,
    )
    page = search.page_sync(backend.fetch, limit=3)
    assert page["number_matched"] == 10


def test_search_empty_result():
    backend = Backend(make_items())
    search = PartialPushdownSearch(
        {"op": "=", "args": [{"property": "id"}, "nope"]},
        capabilities=cql2.PushdownCapabilities(operators={"="}),
    )
    page = search.page_sync(backend.fetch, limit=10)
    assert page["features"] == []
    assert page["next_token"] is None
    assert page["number_matched"] == 0


def test_search_resumes_same_candidate_page_token_is_valid():
    backend = Backend(make_items())
    search = PartialPushdownSearch(
        {"op": "=", "args": [{"property": "properties.platform"}, "a"]},
        capabilities=cql2.PushdownCapabilities(),
        candidate_page_size=4,
    )
    first = search.page_sync(backend.fetch, limit=2)
    assert first["next_token"] is not None
    # The token round-trips through a fresh search object (page size restored).
    resumed = PartialPushdownSearch(
        {"op": "=", "args": [{"property": "properties.platform"}, "a"]},
        capabilities=cql2.PushdownCapabilities(),
        candidate_page_size=4,
    )
    second = resumed.page_sync(backend.fetch, limit=2, token=first["next_token"])
    assert [i["id"] for i in second["features"]] == ["item-2", "item-3"]


def test_search_rejects_unsupported_residual():
    with pytest.raises(cql2.UnsupportedCql2Function):
        # Construction rejects; it never silently returns all items.
        PartialPushdownSearch(
            _SPATIAL,
            capabilities=cql2.PushdownCapabilities(),
        )


def test_search_invalid_token():
    backend = Backend(make_items())
    search = PartialPushdownSearch(
        None, capabilities=cql2.PushdownCapabilities()
    )
    with pytest.raises(cql2.Cql2Error):
        search.page_sync(backend.fetch, limit=1, token="not-a-token")


def test_search_empty_filter_uses_backend_count():
    backend = Backend(make_items())
    search = PartialPushdownSearch(None, capabilities=cql2.PushdownCapabilities())
    assert search.is_exact
    page = search.page_sync(backend.fetch, limit=5)
    assert page["number_matched"] == 25
    assert page["number_returned"] == 5


def test_search_async_matches_sync():
    backend = Backend(make_items())
    sync_search = PartialPushdownSearch(
        {"op": "<", "args": [{"property": "eo:cloud_cover"}, 12]},
        capabilities=cql2.PushdownCapabilities(),
        candidate_page_size=4,
    )
    async_search = PartialPushdownSearch(
        {"op": "<", "args": [{"property": "eo:cloud_cover"}, 12]},
        capabilities=cql2.PushdownCapabilities(),
        candidate_page_size=4,
    )

    async def fetch(pushdown, limit, token):
        return backend.fetch(pushdown, limit, token)

    sync_ids = []
    token = None
    while True:
        page = sync_search.page_sync(backend.fetch, limit=3, token=token)
        sync_ids.extend(i["id"] for i in page["features"])
        token = page["next_token"]
        if token is None:
            break

    async def run():
        ids = []
        token = None
        while True:
            page = await async_search.page_async(fetch, limit=3, token=token)
            ids.extend(i["id"] for i in page["features"])
            token = page["next_token"]
            if token is None:
                return ids

    assert asyncio.run(run()) == sync_ids == [f"item-{n}" for n in range(12)]


def test_search_text_filter():
    backend = Backend(make_items())
    search = PartialPushdownSearch(
        "eo:cloud_cover < 5 AND collection = 'c1'",
        capabilities=cql2.PushdownCapabilities(
            operators={"="}, properties={"collection"}
        ),
        filter_lang="cql2-text",
        candidate_page_size=2,
    )
    assert search.pushdown == {
        "op": "=",
        "args": [{"property": "collection"}, "c1"],
    }
    page = search.page_sync(backend.fetch, limit=100)
    assert [i["id"] for i in page["features"]] == ["item-0", "item-2", "item-4"]


# ---------------------------------------------------------------------------
# Request selectors
# ---------------------------------------------------------------------------


def test_selectors_to_filter_none():
    assert cql2.selectors_to_filter() is None


def test_selectors_to_filter_combines():
    expr = cql2.selectors_to_filter(collections=["c1"], ids=["i1"])
    assert expr == {
        "op": "and",
        "args": [
            {"op": "in", "args": [{"property": "collection"}, ["c1"]]},
            {"op": "in", "args": [{"property": "id"}, ["i1"]]},
        ],
    }


@pytest.mark.parametrize(
    "datetime,item_dt,expected",
    [
        ("2020-06-01T00:00:00Z", "2020-06-01T00:00:00Z", True),
        ("2020-06-01T00:00:00Z", "2020-06-02T00:00:00Z", False),
        ("2020-01-01T00:00:00Z/..", "2023-01-01T00:00:00Z", True),
        ("../2020-01-01T00:00:00Z", "2023-01-01T00:00:00Z", False),
        ("2021-01-01T00:00:00Z/2021-12-31T00:00:00Z", "2020-01-01T00:00:00Z", False),
    ],
)
def test_selectors_datetime(datetime, item_dt, expected):
    item = {"properties": {"datetime": item_dt}}
    expr = cql2.selectors_to_filter(datetime=datetime)
    assert cql2.evaluate(expr, item) is expected


def test_selectors_datetime_invalid():
    with pytest.raises(cql2.InvalidCql2Expression):
        cql2.selectors_to_filter(datetime="not-a-datetime")


def test_selectors_bbox_2d_and_3d():
    item = {"bbox": [0, 0, 10, 10]}
    assert cql2.evaluate(cql2.selectors_to_filter(bbox=[-1, -1, 1, 1]), item)
    assert not cql2.evaluate(cql2.selectors_to_filter(bbox=[20, 20, 30, 30]), item)
    # 3D bbox uses the horizontal extent.
    assert cql2.evaluate(cql2.selectors_to_filter(bbox=[-1, -1, 0, 1, 1, 100]), item)


def test_selectors_bbox_from_geometry():
    item = {"geometry": {"type": "Point", "coordinates": [5, 5]}}
    assert cql2.evaluate(cql2.selectors_to_filter(bbox=[4, 4, 6, 6]), item)
    assert not cql2.evaluate(cql2.selectors_to_filter(bbox=[0, 0, 1, 1]), item)


def test_search_collections_selector_narrows_candidates():
    backend = Backend(make_items())
    search = PartialPushdownSearch(
        {"op": ">", "args": [{"property": "properties.eo:cloud_cover"}, 3]},
        capabilities=cql2.PushdownCapabilities(),
        collections=["c1"],
        candidate_page_size=3,
    )
    page = search.page_sync(backend.fetch, limit=100)
    # Only even-numbered (c1) items with cloud cover > 3.
    assert [i["id"] for i in page["features"]] == [
        f"item-{n}" for n in range(4, 25, 2)
    ]
    assert search.candidate_request.collections == ["c1"]


def test_search_selectors_not_exact_are_rechecked_locally():
    # A backend that only narrows on collections (e.g. via an index) must have
    # its selectors re-checked; here the "backend" ignores collections, and the
    # local residual must still enforce them.
    backend = Backend(make_items())

    def loose_fetch(request, limit, token):
        # Deliberately ignore request.collections.
        clone = request.__class__(pushdown=request.pushdown)
        return backend.fetch(clone, limit, token)

    search = PartialPushdownSearch(
        None,
        capabilities=cql2.PushdownCapabilities(),
        collections=["c1"],
        candidate_selectors_are_exact=False,
        candidate_page_size=4,
    )
    page = search.page_sync(loose_fetch, limit=100)
    assert [i["id"] for i in page["features"]] == [f"item-{n}" for n in range(0, 25, 2)]
    assert page["number_matched"] == 13


def test_candidate_request_is_unrestricted():
    from stac_fastapi.types.partial import CandidateRequest

    assert CandidateRequest().is_unrestricted()
    assert not CandidateRequest(collections=["a"]).is_unrestricted()


def test_search_selector_only_exact_count():
    # No CQL2 filter, only selectors: the backend can count exactly.
    backend = Backend(make_items())
    search = PartialPushdownSearch(
        None,
        capabilities=cql2.PushdownCapabilities(),
        collections=["c1"],
    )
    assert search.is_exact
    page = search.page_sync(backend.fetch, limit=5)
    assert page["number_matched"] == 13


# ---------------------------------------------------------------------------
# STAC response adapter and request construction
# ---------------------------------------------------------------------------


def test_to_item_collection_includes_known_count():
    from stac_fastapi.types.partial import to_item_collection

    page = {
        "features": [{"id": "a"}],
        "next_token": None,
        "number_matched": 3,
        "number_returned": 1,
    }
    collection = to_item_collection(page, links=[{"rel": "self"}])
    assert collection["numberMatched"] == 3
    assert collection["numberReturned"] == 1
    assert collection["type"] == "FeatureCollection"
    assert collection["links"] == [{"rel": "self"}]


def test_to_item_collection_omits_unknown_count():
    from stac_fastapi.types.partial import to_item_collection

    page = {
        "features": [],
        "next_token": None,
        "number_matched": None,
        "number_returned": 0,
    }
    collection = to_item_collection(page)
    assert "numberMatched" not in collection
    assert collection["numberReturned"] == 0


def test_from_search_request_post_model():
    from stac_fastapi.api.models import create_post_request_model
    from stac_fastapi.extensions import FilterExtension
    from stac_fastapi.types.search import BaseSearchPostRequest

    model = create_post_request_model(
        [FilterExtension()], base_model=BaseSearchPostRequest
    )
    request = model(
        collections=["c1"],
        filter={
            "op": "and",
            "args": [
                {"op": "=", "args": [{"property": "collection"}, "c1"]},
                {"op": ">", "args": [{"property": "eo:cloud_cover"}, 5]},
            ],
        },
    )
    search = PartialPushdownSearch.from_search_request(
        request,
        capabilities=cql2.PushdownCapabilities(
            operators={"="}, properties={"collection"}
        ),
    )
    assert search.pushdown == {
        "op": "=",
        "args": [{"property": "collection"}, "c1"],
    }
    assert search.residual == {
        "op": ">",
        "args": [{"property": "eo:cloud_cover"}, 5],
    }
    assert search.selectors["collections"] == ["c1"]


def test_from_search_request_mapping():
    request = {
        "filter_expr": "eo:cloud_cover < 10",
        "filter_lang": "cql2-text",
        "ids": ["i1", "i2"],
    }
    search = PartialPushdownSearch.from_search_request(request)
    assert search.residual == {
        "op": "<",
        "args": [{"property": "eo:cloud_cover"}, 10],
    }
    assert search.selectors["ids"] == ["i1", "i2"]


def test_end_to_end_collection_and_time_candidates():
    """Backend can only select candidates by collection and time."""
    items = [
        {
            "type": "Feature",
            "id": f"i{n}",
            "collection": "landsat" if n % 2 == 0 else "sentinel",
            "bbox": [n, n, n + 1, n + 1],
            "properties": {
                "datetime": f"2020-0{1 + (n % 9)}-01T00:00:00Z",
                "eo:cloud_cover": n * 3 % 100,
            },
        }
        for n in range(40)
    ]

    def fetch(request, limit, token):
        candidates = [
            item
            for item in items
            if (not request.collections or item["collection"] in request.collections)
            and (
                not request.datetime
                or cql2.evaluate(
                    cql2.selectors_to_filter(datetime=request.datetime), item
                )
            )
        ]
        start = int(token or 0)
        window = candidates[start : start + limit]
        next_token = (
            str(start + limit) if start + limit < len(candidates) else None
        )
        return CandidatePage(
            features=window, next_token=next_token, number_matched=len(candidates)
        )

    search = PartialPushdownSearch.from_search_request(
        {
            "collections": ["landsat"],
            "datetime": "2020-01-01T00:00:00Z/2020-12-31T00:00:00Z",
            "filter_expr": {"op": "<", "args": [{"property": "eo:cloud_cover"}, 50]},
            "filter_lang": "cql2-json",
        },
        capabilities=cql2.PushdownCapabilities(),
    )
    assert search.pushdown is None
    assert not search.is_exact

    seen = []
    token = None
    while True:
        page = search.page_sync(fetch, limit=7, token=token)
        seen.extend(item["id"] for item in page["features"])
        token = page["next_token"]
        if token is None:
            break

    expected = [
        item["id"]
        for item in items
        if item["collection"] == "landsat"
        and item["properties"]["eo:cloud_cover"] < 50
    ]
    assert seen == expected

    first = search.page_sync(fetch, limit=7)
    collection = to_item_collection(first)
    # Candidate count is 26 but matches are 12; the count must not be reported.
    assert "numberMatched" not in collection
    assert collection["numberReturned"] == 7


def test_split_operand_function_must_be_advertised():
    # ``lower(name) = 'x'`` is not executable if the backend only knows ``=``.
    expr = {
        "op": "=",
        "args": [{"op": "lower", "args": [{"property": "name"}]}, "x"],
    }
    with pytest.raises(cql2.UnsupportedCql2Function):
        cql2.split_filter(expr, cql2.PushdownCapabilities(operators={"="}))

    result = cql2.split_filter(
        expr,
        cql2.PushdownCapabilities(operators={"="}, functions=frozenset({"lower"})),
    )
    assert result.pushdown == expr
    assert result.residual is None


def test_selector_filter_is_locally_validated():
    expr = cql2.selectors_to_filter(
        collections=["c1"],
        bbox=[0, 0, 1, 1],
        datetime="2020-01-01T00:00:00Z/2020-12-31T00:00:00Z",
    )
    cql2.validate_expression(expr, local=True)
    result = cql2.split_filter(expr, cql2.PushdownCapabilities())
    assert result.pushdown is None
    assert result.residual == expr


def test_exact_without_backend_count_is_unknown_unless_count_all():
    items = [{"id": f"i{n}", "properties": {}} for n in range(30)]

    def fetch(request, limit, token):
        start = int(token or 0)
        window = items[start : start + limit]
        next_token = str(start + limit) if start + limit < len(items) else None
        return CandidatePage(features=window, next_token=next_token, number_matched=None)

    search = PartialPushdownSearch(None, candidate_page_size=5)
    assert search.is_exact
    page = search.page_sync(fetch, limit=3)
    assert page["number_matched"] is None

    counting = PartialPushdownSearch(None, candidate_page_size=5, count_all=True)
    counted = counting.page_sync(fetch, limit=3)
    assert counted["number_matched"] == 30
