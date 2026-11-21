"""Snapshot support for token based pagination.

The helpers in this module implement *consistent* (a.k.a. snapshot) pagination for the
:class:`~stac_fastapi.extensions.pagination.token_pagination.TokenPaginationExtension`.

When a snapshot backend is configured, the first request of a search materializes the
complete result set and stores it in the backend.  The pagination token returned to the
client binds

* the identifier of the stored result version (the snapshot), and
* a fingerprint of the request parameters which define the result set (collections,
  filters, bbox, datetime, ...).

Following the ``next`` links therefore always continues to browse the result set as it
existed for the first request, even if items were inserted or deleted in the meantime.
A token issued by ``GET /search`` can also be used with ``POST /search`` (and vice
versa) as long as the search parameters are equivalent.
"""

from __future__ import annotations

import abc
import base64
import binascii
import hmac
import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from stac_fastapi.types.errors import InvalidQueryParameter, StacApiError

logger = logging.getLogger(__name__)

__all__ = [
    "InvalidPaginationToken",
    "InMemorySnapshotBackend",
    "MemorySnapshotBackend",
    "PaginationCriteriaMismatch",
    "ResultSetTooLarge",
    "Snapshot",
    "SnapshotBackend",
    "SnapshotExpired",
    "SnapshotStore",
    "criteria_fingerprint",
    "decode_token",
    "encode_token",
    "normalize_criteria",
    "next_token_from_links",
    "replace_token_in_url",
]

#: Link relations which are regenerated for every page.
PAGINATION_RELS = frozenset({"next", "prev", "previous", "self"})

#: Request parameters which never take part in the result-set fingerprint.
CRITERIA_EXCLUDED = frozenset({"token", "request", "limit", "collection_id"})

TOKEN_VERSION = 1


class InvalidPaginationToken(InvalidQueryParameter):
    """The pagination token is malformed or has been tampered with.

    Attributes:
        ours: whether the token looks like one issued by this extension. Tokens
            which don't (for example opaque backend tokens) can be forwarded to the
            backend client for backwards compatibility.
    """

    def __init__(self, message: str = "Invalid pagination token.", ours: bool = True):
        """Initialize the error.

        Args:
            message: human readable description.
            ours: whether the token was issued by this extension.
        """
        super().__init__(message)
        self.ours = ours


class PaginationCriteriaMismatch(InvalidQueryParameter):
    """The token was issued for a different set of search parameters."""

    def __init__(self, message: str | None = None):
        """Initialize the error.

        Args:
            message: optional custom description.
        """
        super().__init__(
            message
            or (
                "Pagination token does not match the current search parameters "
                "(e.g. filter, collections or ids). Please start a new search."
            )
        )


class SnapshotExpired(StacApiError):
    """The snapshot referenced by the token is no longer available."""

    def __init__(self, snapshot_id: str | None = None):
        """Initialize the error.

        Args:
            snapshot_id: identifier of the missing snapshot.
        """
        identifier = f" '{snapshot_id}'" if snapshot_id else ""
        super().__init__(
            f"Pagination token is no longer valid: result snapshot{identifier} "
            "has expired or is unknown. Please re-run the search to start a new "
            "result set."
        )
        self.snapshot_id = snapshot_id


class ResultSetTooLarge(StacApiError):
    """The result set can not be materialized into a snapshot."""

    def __init__(self, max_items: int | None = None, max_pages: int | None = None):
        """Initialize the error.

        Args:
            max_items: configured maximum number of items.
            max_pages: configured maximum number of pages.
        """
        if max_items is not None:
            reason = f"more than {max_items} items"
        elif max_pages is not None:
            reason = f"more than {max_pages} pages"
        else:
            reason = "too many items"
        super().__init__(
            "The result set is too large to be paginated consistently "
            f"({reason}). Please narrow the search (e.g. add collections, "
            "datetime or a filter)."
        )


@dataclass(frozen=True)
class Snapshot:
    """An immutable version of a search result set.

    Attributes:
        id: opaque snapshot identifier.
        criteria: fingerprint of the request parameters the snapshot was built for.
        items: every matching item, in result order.
        links: links of the first response which are not pagination links.
        envelope: response members other than the items/links/counters.
        item_key: name of the response member containing the items
            (``features`` or ``collections``).
        created_at: unix timestamp of the snapshot creation.
        expires_at: unix timestamp after which the snapshot must not be used.
    """

    id: str
    criteria: str
    items: tuple[Any, ...] = ()
    links: tuple[dict[str, Any], ...] = ()
    envelope: dict[str, Any] = field(default_factory=dict)
    item_key: str = "features"
    created_at: float = 0.0
    expires_at: float | None = None

    def is_expired(self, now: float | None = None) -> bool:
        """Return ``True`` if the snapshot can no longer be served."""
        if self.expires_at is None:
            return False
        return (time.time() if now is None else now) >= self.expires_at


class SnapshotBackend(abc.ABC):
    """Storage backend for materialized search results.

    Implementations must be safe to use from the threadpool used by synchronous
    clients. The interface is deliberately small so that it can be backed by an
    in-process cache, Redis, a database, ...
    """

    @abc.abstractmethod
    def save(self, snapshot: Snapshot) -> None:
        """Store ``snapshot``. Existing entries with the same id are replaced."""

    @abc.abstractmethod
    def load(self, snapshot_id: str) -> Snapshot | None:
        """Return the snapshot or ``None`` if it is unknown or expired."""

    def delete(self, snapshot_id: str) -> None:
        """Remove the snapshot from the backend (best effort)."""
        raise NotImplementedError

    def clear(self) -> None:
        """Remove every snapshot from the backend (best effort)."""
        raise NotImplementedError


class InMemorySnapshotBackend(SnapshotBackend):
    """A small thread-safe in-process snapshot backend.

    Args:
        max_snapshots: maximum number of snapshots kept in memory. The oldest
            snapshots are evicted first.
    """

    def __init__(self, max_snapshots: int = 1024):
        """Initialize the backend.

        Args:
            max_snapshots: maximum number of snapshots kept in memory.
        """
        self.max_snapshots = max_snapshots
        self._lock = threading.RLock()
        self._snapshots: OrderedDict[str, Snapshot] = OrderedDict()

    def _evict_expired(self, now: float | None = None) -> None:
        expired = [
            key for key, snapshot in self._snapshots.items() if snapshot.is_expired(now)
        ]
        for key in expired:
            self._snapshots.pop(key, None)

    def save(self, snapshot: Snapshot) -> None:
        """Store ``snapshot``, evicting the oldest entries if needed."""
        with self._lock:
            self._evict_expired()
            self._snapshots[snapshot.id] = snapshot
            self._snapshots.move_to_end(snapshot.id)
            while len(self._snapshots) > self.max_snapshots:
                self._snapshots.popitem(last=False)

    def load(self, snapshot_id: str) -> Snapshot | None:
        """Return the snapshot, or ``None`` if it is unknown or expired."""
        with self._lock:
            snapshot = self._snapshots.get(snapshot_id)
            if snapshot is None:
                return None
            if snapshot.is_expired():
                self._snapshots.pop(snapshot_id, None)
                return None
            return snapshot

    def delete(self, snapshot_id: str) -> None:
        """Remove the snapshot from the backend."""
        with self._lock:
            self._snapshots.pop(snapshot_id, None)

    def clear(self) -> None:
        """Remove every snapshot from the backend."""
        with self._lock:
            self._snapshots.clear()

    def __len__(self) -> int:
        """Return the number of stored snapshots."""
        with self._lock:
            return len(self._snapshots)


#: Friendly aliases for the same implementation.
MemorySnapshotBackend = InMemorySnapshotBackend
SnapshotStore = SnapshotBackend


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except (binascii.Error, ValueError) as exc:
        raise InvalidPaginationToken() from exc


def encode_token(
    snapshot_id: str,
    criteria: str,
    offset: int = 0,
    secret: str | None = None,
) -> str:
    """Encode a pagination token.

    Args:
        snapshot_id: identifier of the result snapshot.
        criteria: fingerprint of the search parameters.
        offset: index of the first item of the page.
        secret: optional key used to sign the token.

    Returns:
        An opaque, URL safe token.
    """
    payload = {
        "v": TOKEN_VERSION,
        "snapshot": snapshot_id,
        "criteria": criteria,
        "offset": int(offset),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    token = _b64encode(raw)
    if secret:
        signature = hmac.new(secret.encode("utf-8"), raw, sha256).digest()[:16]
        token = f"{token}.{_b64encode(signature)}"
    return token


def _verify_signature(raw: bytes, encoded_signature: str, secret: str) -> None:
    """Raise if the token signature does not match ``raw``."""
    if not encoded_signature:
        raise InvalidPaginationToken("Invalid pagination token signature.")
    expected = hmac.new(secret.encode("utf-8"), raw, sha256).digest()[:16]
    try:
        provided = _b64decode(encoded_signature)
    except InvalidPaginationToken as exc:
        raise InvalidPaginationToken("Invalid pagination token signature.") from exc
    if not hmac.compare_digest(expected, provided):
        raise InvalidPaginationToken("Invalid pagination token signature.")


def _validate_payload(decoded: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the token payload and return it as a plain dict."""
    if not isinstance(decoded, Mapping) or "v" not in decoded:
        raise InvalidPaginationToken(ours=False)
    if decoded.get("v") != TOKEN_VERSION:
        raise InvalidPaginationToken("Unsupported pagination token version.")
    if not isinstance(decoded.get("snapshot"), str) or not decoded["snapshot"]:
        raise InvalidPaginationToken()
    if not isinstance(decoded.get("criteria"), str) or not decoded["criteria"]:
        raise InvalidPaginationToken()
    offset = decoded.get("offset")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise InvalidPaginationToken()
    return dict(decoded)


def decode_token(token: str, secret: str | None = None) -> dict[str, Any]:
    """Decode a pagination token.

    Raises:
        InvalidPaginationToken: if the token is malformed or its signature is
            invalid.
    """
    if not isinstance(token, str) or not token:
        raise InvalidPaginationToken(ours=False)

    main, _, encoded_signature = token.partition(".")

    try:
        raw = _b64decode(main)
        decoded = json.loads(raw.decode("utf-8"))
    except (InvalidPaginationToken, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidPaginationToken(ours=False) from exc

    payload = _validate_payload(decoded)

    if secret:
        _verify_signature(raw, encoded_signature, secret)

    return payload


def _maybe_json(value: str) -> Any:
    stripped = value.strip()
    if stripped[:1] in ("{", "["):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return value
    return value


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return _jsonable(dump(mode="json"))
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    return str(value)


def _normalize_sortby(value: Any) -> list[list[str]]:
    normalized: list[list[str]] = []
    if isinstance(value, Mapping) or isinstance(value, str):
        value = [value]
    for entry in value:
        if isinstance(entry, str):
            direction = "desc" if entry.startswith("-") else "asc"
            normalized.append([entry.lstrip("+-"), direction])
        elif isinstance(entry, Mapping):
            direction = str(entry.get("direction", "asc")).lower()
            normalized.append([str(entry.get("field")), direction])
        else:
            normalized.append([str(entry), "asc"])
    return normalized


def _normalize_fields(value: Any) -> dict[str, list[str]]:
    include: list[str] = []
    exclude: list[str] = []
    if isinstance(value, Mapping):
        include = [str(item) for item in value.get("include") or []]
        exclude = [str(item) for item in value.get("exclude") or []]
    elif isinstance(value, (list, tuple)):
        for item in value:
            item = str(item)
            if item.startswith("-"):
                exclude.append(item[1:])
            else:
                include.append(item.lstrip("+"))
    return {"include": sorted(include), "exclude": sorted(exclude)}


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (str, bytes, list, tuple, set, dict)) and len(value) == 0:
        return True
    return False


def _normalize_datetime(value: Any) -> Any:
    """Normalize a datetime interval to its RFC 3339 string form."""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        parts = [_normalize_datetime(item) for item in value]
        return "/".join(str(part) for part in parts)
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    return _jsonable(value)


def _normalize_value(key: str, value: Any) -> Any:
    """Normalize a single request parameter."""
    if key == "datetime":
        return _normalize_datetime(value)
    if key in ("collections", "ids"):
        return sorted({str(item) for item in value})
    if key == "bbox":
        return [float(item) for item in value]
    if key == "intersects":
        return _jsonable(_maybe_json(value) if isinstance(value, str) else value)
    if key == "sortby":
        return _normalize_sortby(value)
    if key == "fields":
        fields = _normalize_fields(value)
        return fields if fields["include"] or fields["exclude"] else None
    if key in ("filter_expr", "query") and isinstance(value, str):
        return _jsonable(_maybe_json(value))
    return _jsonable(value)


def normalize_criteria(params: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize request parameters into a verb independent representation.

    ``GET`` and ``POST`` requests encode the same search slightly differently
    (``bbox`` as a tuple vs a list, ``intersects`` as a JSON string vs an object,
    ``sortby`` as ``"-field"`` vs ``{"field": ..., "direction": ...}``, ...).
    Normalizing both forms to the same structure allows a token issued by one verb
    to be used with the other.
    """
    normalized: dict[str, Any] = {}
    for key, value in params.items():
        if key in CRITERIA_EXCLUDED or _is_empty(value):
            continue
        normalized_value = _normalize_value(key, value)
        if normalized_value is not None:
            normalized[key] = normalized_value

    # ``filter-lang``/``filter-crs`` are only meaningful together with a filter.
    if not normalized.get("filter_expr"):
        normalized.pop("filter_crs", None)
        normalized.pop("filter_lang", None)

    return normalized


def criteria_fingerprint(
    params: Mapping[str, Any],
    endpoint: str,
    collection_id: str | None = None,
) -> str:
    """Return a stable fingerprint of the parameters defining a result set.

    Args:
        params: request parameters (``token`` and ``limit`` are ignored).
        endpoint: logical endpoint name, so that tokens cannot be replayed on
            another route.
        collection_id: collection of the ``/items`` endpoint, if any.

    Returns:
        A hexadecimal SHA-256 digest.
    """
    payload: dict[str, Any] = {
        "endpoint": endpoint,
        "params": normalize_criteria(params),
    }
    if collection_id is not None:
        payload["collection_id"] = str(collection_id)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()


def _without_token(url: str) -> str:
    parts = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != "token"
    ]
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def replace_token_in_url(url: str, token: str) -> str:
    """Return ``url`` with its ``token`` query parameter replaced."""
    parts = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != "token"
    ]
    query.append(("token", token))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def next_token_from_links(links: Sequence[Mapping[str, Any]]) -> str | None:
    """Extract the client pagination token from a ``next`` link."""
    for link in links:
        if not isinstance(link, Mapping) or link.get("rel") != "next":
            continue
        token = link.get("token")
        if isinstance(token, str) and token:
            return token
        href = link.get("href")
        if isinstance(href, str):
            values = dict(parse_qsl(urlsplit(href).query, keep_blank_values=True))
            if values.get("token"):
                return values["token"]
        body = link.get("body")
        if isinstance(body, Mapping) and isinstance(body.get("token"), str):
            return body["token"]
    return None


def strip_token(url: str) -> str:
    """Return ``url`` without its ``token`` query parameter."""
    return _without_token(url)
