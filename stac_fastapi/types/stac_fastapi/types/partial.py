"""Partial pushdown search and pagination.

Some backends can only execute a subset of CQL2.  Rather than refusing every
filter (or -- worse -- silently ignoring the unsupported part), a backend can:

1. split the filter into a *pushdown* fragment it can execute and a *residual*
   fragment it cannot (:func:`stac_fastapi.types.cql2.split_filter`);
2. fetch candidate Items matching the pushdown and the request selectors
   (``collections``/``ids``/``bbox``/``datetime``);
3. decide the residual locally on those candidates.

:class:`PartialPushdownSearch` implements steps 2 and 3, including pagination.
Pagination applies only to Items that pass the residual filter: when a
candidate page does not yield enough matches, further candidate pages are read.
``numberMatched`` is reported only when it is consistent with the search
semantics; when a residual filter is present and the exact total is not
computed, it is ``None`` (unknown) instead of the number of candidates.

The engine is transport agnostic.  A backend supplies a callable returning one
page of candidates for a :class:`CandidateRequest`:

.. code-block:: python

    def fetch(
        request: CandidateRequest, limit: int, token: str | None
    ) -> CandidatePage: ...

Both a synchronous (:meth:`PartialPushdownSearch.page_sync`) and an
asynchronous (:meth:`PartialPushdownSearch.page_async`) driver are provided.
"""

import base64
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypedDict

from stac_fastapi.types import cql2

__all__ = [
    "CandidateFetcher",
    "CandidatePage",
    "CandidateRequest",
    "PartialPushdownSearch",
    "PreparedSearch",
    "SearchPage",
    "to_item_collection",
]

#: ``fetch(candidate_request, limit, token) -> CandidatePage``.
CandidateFetcher = Callable[[Any, int, "str | None"], "CandidatePage"]


@dataclass(frozen=True)
class CandidateRequest:
    """What a backend is asked to return candidates for.

    The request always carries the request selectors (``collections``,
    ``ids``, ``bbox``, ``datetime``), which most backends can execute even
    without CQL2 support.  ``pushdown`` is the part of the CQL2 filter the
    backend declared it can run, or ``None`` when every candidate matching the
    selectors has to be inspected.

    Attributes:
        pushdown: the CQL2-JSON fragment to execute, or ``None``.
        collections: collection ids to restrict candidates to.
        ids: item ids to restrict candidates to.
        bbox: bounding box to restrict candidates to.
        datetime: RFC 3339 instant or interval to restrict candidates to.
    """

    pushdown: dict[str, Any] | None = None
    collections: list[str] | None = None
    ids: list[str] | None = None
    bbox: Any | None = None
    datetime: str | None = None

    def is_unrestricted(self) -> bool:
        """Whether the backend has to return every Item as a candidate."""
        return (
            self.pushdown is None
            and not self.collections
            and not self.ids
            and self.bbox is None
            and not self.datetime
        )


class CandidatePage:
    """One page of candidates returned by a backend.

    Attributes:
        features: the candidate Items.
        next_token: an opaque token for the next candidate page, or ``None``
            when the backend has no more candidates.
        number_matched: the backend's total number of candidates matching the
            pushdown filter, when it knows it.  It counts *candidates*; it is a
            valid ``numberMatched`` only when no residual filter is applied.
        number_returned: the number of candidates in this page.
    """

    __slots__ = ("features", "next_token", "number_matched", "number_returned")

    def __init__(
        self,
        features: list[Mapping[str, Any]],
        next_token: str | None = None,
        number_matched: int | None = None,
        number_returned: int | None = None,
    ) -> None:
        """Create a candidate page."""
        self.features = features
        self.next_token = next_token
        self.number_matched = number_matched
        self.number_returned = (
            len(features) if number_returned is None else number_returned
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        """Return a readable representation."""
        return (
            f"CandidatePage(n={len(self.features)}, "
            f"next_token={self.next_token!r}, "
            f"number_matched={self.number_matched!r})"
        )


class SearchPage(TypedDict):
    """One page of search results.

    ``next_token`` is ``None`` on the last page.  ``number_matched`` is
    ``None`` when the total cannot be stated consistently with the filter; it
    is then omitted from responses rather than reporting a misleading count.
    """

    features: list[Mapping[str, Any]]
    next_token: str | None
    number_matched: int | None
    number_returned: int


def to_item_collection(
    page: SearchPage,
    links: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Turn a :class:`SearchPage` into a STAC ItemCollection.

    ``numberMatched`` is only included when it is known; when the count would
    be inconsistent with the applied filter it is omitted instead of reporting
    the number of candidates.  ``numberReturned`` is always included.

    Args:
        page: a page returned by :class:`PartialPushdownSearch`.
        links: links to attach to the ItemCollection.

    Returns:
        A GeoJSON FeatureCollection dictionary.
    """
    collection: dict[str, Any] = {
        "type": "FeatureCollection",
        "features": [dict(feature) for feature in page["features"]],
        "links": list(links or []),
        "numberReturned": page["number_returned"],
    }
    if page["number_matched"] is not None:
        collection["numberMatched"] = page["number_matched"]
    return collection


@dataclass(frozen=True)
class PreparedSearch:
    """A parsed and split filter, ready to be executed.

    Attributes:
        pushdown: the fragment for the backend, or ``None`` when every
            candidate has to be inspected.
        residual: the fragment to evaluate locally, or ``None`` when the
            pushdown is exact.
        is_exact: ``True`` when there is no residual filter.
    """

    pushdown: dict[str, Any] | None
    residual: dict[str, Any] | None
    is_exact: bool


# ---------------------------------------------------------------------------
# Pagination state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _PageToken:
    """The resumable state of a search.

    ``candidate_token`` identifies the candidate page still being consumed
    (``None`` for the first one).  ``skip`` counts matches on that page already
    consumed.  ``base`` counts matches on all *earlier* candidate pages, so
    ``base + skip`` is the number of matches passed over so far.  ``page_size``
    is kept in the token so a resumed search reads the same candidate page.
    """

    candidate_token: str | None = None
    skip: int = 0
    page_size: int = 100
    base: int = 0
    exhausted: bool = False
    count: int | None = None

    def encode(self) -> str:
        """Encode the state as a URL-safe token."""
        payload = json.dumps(
            {
                "t": self.candidate_token,
                "s": self.skip,
                "n": self.page_size,
                "b": self.base,
                "x": self.exhausted,
                "c": self.count,
            },
            separators=(",", ":"),
        ).encode()
        return base64.urlsafe_b64encode(payload).decode().rstrip("=")

    @classmethod
    def decode(cls, token: str) -> "_PageToken":
        """Decode a token produced by :meth:`encode`."""
        padded = token + "=" * (-len(token) % 4)
        try:
            payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
            count = payload.get("c")
            state = cls(
                candidate_token=payload["t"],
                skip=int(payload["s"]),
                page_size=int(payload["n"]),
                base=int(payload["b"]),
                exhausted=bool(payload["x"]),
                count=None if count is None else int(count),
            )
        except (ValueError, KeyError, TypeError) as e:
            raise cql2.Cql2Error(f"Invalid pagination token: {token!r}") from e
        if state.skip < 0 or state.base < 0 or state.page_size < 1:
            raise cql2.Cql2Error(f"Invalid pagination token: {token!r}")
        return state


def _passes(expr: Mapping[str, Any] | None, item: Mapping[str, Any]) -> bool:
    return expr is None or cql2.evaluate(expr, item)


class _Cursor:
    """Collects matches for one client page from a stream of candidate pages.

    Each call to :meth:`feed` consumes one candidate page.  It updates
    ``features`` with the matches to return, and either sets :attr:`next_state`
    (the page is complete) or leaves it ``None`` (another candidate page is
    needed).  ``matched`` is the number of matches passed over in the stream,
    which equals the exact total once the stream is exhausted.
    """

    def __init__(self, prepared: PreparedSearch, token: _PageToken, limit: int) -> None:
        """Start a page from the given continuation token."""
        self._prepared = prepared
        self._limit = limit
        self._candidate_token = token.candidate_token
        self._skip = token.skip
        self._page_size = token.page_size
        self._base = token.base
        self.exhausted = token.exhausted
        self.features: list[Mapping[str, Any]] = []
        self.next_state: _PageToken | None = None
        #: ``numberMatched`` supplied by the backend, if any.  Carried in the
        #: token so a later page does not need to ask for the first page again.
        self.backend_count: int | None = token.count

    @property
    def matched(self) -> int:
        """Number of matches passed over so far."""
        return self._base + self._skip

    @property
    def done(self) -> bool:
        """Whether this client page is complete."""
        return self.next_state is not None or self.exhausted

    def next_request(self) -> tuple[str | None, int] | None:
        """Return ``(candidate_token, page_size)`` or ``None`` when finished."""
        if self.done:
            return None
        return self._candidate_token, self._page_size

    def feed(self, page: CandidatePage) -> None:
        """Consume one candidate page."""
        if page.number_matched is not None:
            self.backend_count = page.number_matched

        matches = [
            item for item in page.features if _passes(self._prepared.residual, item)
        ]
        new = matches[self._skip :]

        room = self._limit - len(self.features)
        self.features.extend(new[:room])
        consumed = self._skip + min(room, len(new))

        if len(self.features) >= self._limit and self._limit > 0:
            if consumed < len(matches):
                # More matches on this candidate page: resume here.
                self.next_state = self._state(consumed)
            elif page.next_token is not None:
                self.next_state = self._state_after_advance(page.next_token, matches)
            else:
                self._exhaust(matches)
            return

        if page.next_token is None:
            self._exhaust(matches)
            return

        # Need another candidate page.
        self._base += len(matches)
        self._skip = 0
        self._candidate_token = page.next_token

    def drain(self, page: CandidatePage) -> None:
        """Account for a page's matches without collecting them."""
        matches = [
            item for item in page.features if _passes(self._prepared.residual, item)
        ]
        if page.next_token is None:
            self._exhaust(matches)
            return
        self._base += len(matches)
        self._skip = 0
        self._candidate_token = page.next_token

    # -- internals -------------------------------------------------------

    def _state(self, skip: int) -> _PageToken:
        return _PageToken(
            candidate_token=self._candidate_token,
            skip=skip,
            page_size=self._page_size,
            base=self._base,
            count=self.backend_count,
        )

    def _state_after_advance(self, next_token: str, matches: list[Any]) -> _PageToken:
        return _PageToken(
            candidate_token=next_token,
            skip=0,
            page_size=self._page_size,
            base=self._base + len(matches),
            count=self.backend_count,
        )

    def _exhaust(self, matches: list[Any]) -> None:
        self._base += len(matches)
        self._skip = 0
        self._candidate_token = None
        self.exhausted = True
        self.next_state = None


def _page(
    features: list[Mapping[str, Any]],
    token: _PageToken | None,
    number_matched: int | None,
) -> SearchPage:
    return SearchPage(
        features=features,
        next_token=token.encode() if token is not None else None,
        number_matched=number_matched,
        number_returned=len(features),
    )


class PartialPushdownSearch:
    """Execute a CQL2 filter with partial pushdown and correct pagination.

    The search may also carry *candidate selectors* -- ``collections``,
    ``ids``, ``bbox`` and ``datetime`` -- which nearly every backend can execute
    even when it cannot run CQL2.  They are passed to ``fetch`` untouched so a
    backend can use them to narrow the candidate set; they are *not* re-applied
    locally, so they must be honoured by ``fetch``.  The CQL2 filter is split on
    :attr:`capabilities`.
    """

    def __init__(
        self,
        filter_expr: Any | None = None,
        capabilities: cql2.PushdownCapabilities | None = None,
        filter_lang: str | None = None,
        candidate_page_size: int = 100,
        count_all: bool = False,
        collections: list[str] | None = None,
        ids: list[str] | None = None,
        bbox: Any | None = None,
        datetime: str | None = None,
        candidate_selectors_are_exact: bool = True,
    ) -> None:
        """Prepare a search.

        Args:
            filter_expr: a CQL2 filter in ``cql2-json`` (mapping) or
                ``cql2-text`` (string) form.
            capabilities: what the backend can execute.  Defaults to nothing,
                so every candidate is inspected locally.
            filter_lang: ``"cql2-json"`` or ``"cql2-text"``; inferred when
                omitted.
            candidate_page_size: how many candidates to request at a time.  It
                only controls batching of candidates, not the page size returned
                to the client.
            count_all: read every candidate page to compute an exact
                ``numberMatched`` rather than relying on the backend's count.
                By default the count comes from the backend when the pushdown is
                exact (no residual filter); otherwise it is ``None`` (unknown),
                because the number of candidates is not the number of matches.
                Set this when an exact total is required but the backend does
                not report one, or when a residual filter is present.
            collections: restrict candidates to these collection ids; passed to
                ``fetch``.
            ids: restrict candidates to these item ids; passed to ``fetch``.
            bbox: restrict candidates to this bounding box; passed to ``fetch``.
            datetime: restrict candidates to this RFC 3339 interval; passed to
                ``fetch``.
            candidate_selectors_are_exact: when ``True`` (the default), the
                backend applies ``collections``/``ids``/``bbox``/``datetime``
                exactly, so they do not count as a residual filter.
                ``numberMatched`` from the backend is then admissible.  Set to
                ``False`` if ``fetch`` only *narrows* candidates (for example a
                coarse index lookup); the selectors are then re-evaluated
                locally and the count is treated as a candidate count.

        Raises:
            UnsupportedCql2Function: if the residual filter cannot be evaluated
                locally.  This is a 400-class error, never a silent partial
                match.
            InvalidCql2Expression: if the filter is malformed.
        """
        if candidate_page_size < 1:
            raise ValueError("candidate_page_size must be at least 1")
        expr = cql2.parse_cql2(filter_expr, filter_lang)
        split = cql2.split_filter(expr, capabilities or cql2.PushdownCapabilities())
        residual = split.residual
        if not candidate_selectors_are_exact:
            selector = cql2.selectors_to_filter(
                collections=collections, ids=ids, bbox=bbox, datetime=datetime
            )
            residual = cql2.conjoin(residual, selector)
        self.prepared = PreparedSearch(
            pushdown=split.pushdown,
            residual=residual,
            is_exact=residual is None,
        )
        self.selectors = {
            "collections": list(collections) if collections is not None else None,
            "ids": list(ids) if ids is not None else None,
            "bbox": list(bbox) if isinstance(bbox, (list, tuple)) else bbox,
            "datetime": datetime,
        }
        self._page_size = candidate_page_size
        self._count_all = count_all

    @classmethod
    def from_search_request(
        cls,
        request: Any,
        capabilities: cql2.PushdownCapabilities | None = None,
        candidate_page_size: int = 100,
        count_all: bool = False,
        candidate_selectors_are_exact: bool = True,
    ) -> "PartialPushdownSearch":
        """Build a search from a STAC search request.

        Reads ``collections``, ``ids``, ``bbox``, ``datetime``, the CQL2 filter
        (``filter_expr``/``filter``) and ``filter_lang`` from a
        :class:`stac_fastapi.types.search.BaseSearchPostRequest` (or the
        equivalent GET request object), ignoring the ones it does not have.

        Args:
            request: a search request model instance or a mapping.
            capabilities: what the backend can execute.
            candidate_page_size: see :meth:`__init__`.
            count_all: see :meth:`__init__`.
            candidate_selectors_are_exact: see :meth:`__init__`.

        Returns:
            A prepared :class:`PartialPushdownSearch`.
        """
        get = request.get if isinstance(request, Mapping) else lambda k, d=None: getattr(
            request, k, d
        )

        filter_expr = get("filter_expr", None)
        if filter_expr is None:
            filter_expr = get("filter", None)

        return cls(
            filter_expr=filter_expr,
            capabilities=capabilities,
            filter_lang=get("filter_lang", None),
            candidate_page_size=candidate_page_size,
            count_all=count_all,
            collections=_as_list(get("collections", None)),
            ids=_as_list(get("ids", None)),
            bbox=get("bbox", None),
            datetime=get("datetime", None),
            candidate_selectors_are_exact=candidate_selectors_are_exact,
        )

    @property
    def pushdown(self) -> dict[str, Any] | None:
        """The fragment handed to the backend."""
        return self.prepared.pushdown

    @property
    def residual(self) -> dict[str, Any] | None:
        """The fragment evaluated locally."""
        return self.prepared.residual

    @property
    def is_exact(self) -> bool:
        """Whether the backend answers the whole filter."""
        return self.prepared.is_exact

    # -- drivers ---------------------------------------------------------

    def page_sync(
        self,
        fetch: CandidateFetcher,
        limit: int,
        token: str | None = None,
    ) -> SearchPage:
        """Return one page of matching Items (synchronous backend).

        Args:
            fetch: ``fetch(request, limit, token) -> CandidatePage``, where
                ``request`` is a :class:`CandidateRequest` carrying the
                pushdown and the request selectors.  It must return only
                candidates matching ``request``.
            limit: maximum number of matching Items to return.
            token: continuation token from a previous page.

        Returns:
            A :class:`SearchPage`.
        """
        _check_limit(limit)
        cursor = _Cursor(self.prepared, self._parse_token(token), limit)
        while True:
            request = cursor.next_request()
            if request is None:
                break
            cursor.feed(self.fetch_one(fetch, request))
        return self._finish(cursor, fetch)

    def fetch_one(
        self,
        fetch: CandidateFetcher,
        request: tuple[str | None, int],
    ) -> CandidatePage:
        """Call ``fetch`` with the candidate request and page token."""
        candidate_token, page_size = request
        return fetch(self.candidate_request, page_size, candidate_token)

    @property
    def candidate_request(self) -> "CandidateRequest":
        """The request handed to the backend for each candidate page."""
        return CandidateRequest(
            pushdown=self.prepared.pushdown,
            collections=self.selectors["collections"],
            ids=self.selectors["ids"],
            bbox=self.selectors["bbox"],
            datetime=self.selectors["datetime"],
        )

    async def page_async(
        self,
        fetch: Callable[[Any, int, "str | None"], Awaitable[CandidatePage]],
        limit: int,
        token: str | None = None,
    ) -> SearchPage:
        """Return one page of matching Items (asynchronous backend).

        See :meth:`page_sync`; ``fetch`` returns an awaitable.
        """
        _check_limit(limit)
        cursor = _Cursor(self.prepared, self._parse_token(token), limit)
        while True:
            request = cursor.next_request()
            if request is None:
                break
            candidate_token, page_size = request
            cursor.feed(
                await fetch(self.candidate_request, page_size, candidate_token)
            )
        return await self._finish_async(cursor, fetch)

    # -- counting --------------------------------------------------------

    def _count(self, cursor: _Cursor) -> int | None:
        """Return the exact ``numberMatched``, ``None`` if unknown.

        Returns ``-1`` as a sentinel when the caller must drain the remaining
        candidate pages to obtain the count.
        """
        if cursor.exhausted:
            return cursor.matched
        if self.prepared.is_exact and cursor.backend_count is not None:
            # Without a residual filter, the backend's candidate count is exact.
            return cursor.backend_count
        if self._count_all:
            # Read the rest of the candidates to count matches exactly.
            return -1
        # Unknown, not zero: callers omit numberMatched.
        return None

    def _finish(self, cursor: _Cursor, fetch: CandidateFetcher) -> SearchPage:
        count = self._count(cursor)
        if count == -1:
            count = self._drain_sync(cursor.next_state, fetch)
        return _page(cursor.features, cursor.next_state, count)

    def _drain_sync(self, state: _PageToken | None, fetch: CandidateFetcher) -> int:
        """Count matches from ``state`` to the end of the candidate stream."""
        if state is None:
            return 0
        cursor = _Cursor(self.prepared, state, limit=0)
        while True:
            request = cursor.next_request()
            if request is None:
                return cursor.matched
            candidate_token, page_size = request
            cursor.drain(fetch(self.candidate_request, page_size, candidate_token))

    async def _finish_async(
        self,
        cursor: _Cursor,
        fetch: Callable[[Any, int, "str | None"], Awaitable[CandidatePage]],
    ) -> SearchPage:
        count = self._count(cursor)
        if count == -1:
            count = await self._drain_async(cursor.next_state, fetch)
        return _page(cursor.features, cursor.next_state, count)

    async def _drain_async(
        self,
        state: _PageToken | None,
        fetch: Callable[[Any, int, "str | None"], Awaitable[CandidatePage]],
    ) -> int:
        """Count matches from ``state`` to the end of the candidate stream."""
        if state is None:
            return 0
        cursor = _Cursor(self.prepared, state, limit=0)
        while True:
            request = cursor.next_request()
            if request is None:
                return cursor.matched
            candidate_token, page_size = request
            cursor.drain(
                await fetch(self.candidate_request, page_size, candidate_token)
            )

    # -- helpers ---------------------------------------------------------

    def _parse_token(self, token: str | None) -> _PageToken:
        if token is None:
            return _PageToken(page_size=self._page_size)
        return _PageToken.decode(token)


def _check_limit(limit: int) -> None:
    if limit < 1:
        raise ValueError("limit must be at least 1")


def _as_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    return list(value)
