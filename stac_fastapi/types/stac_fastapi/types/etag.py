"""Item versions and entity tags.

A *version* is an opaque token which names a stored revision of a STAC Item.  A
read of an Item returns the version of the Item it read as the response's
``ETag`` header, and a conditional write sends that token back as its *expected
version*: the write is performed only if the stored Item is still that version,
so a write built from stale data cannot overwrite an update which landed first.

``item_version`` derives a version from the Item's content, so a backend which
keeps no version counter of its own can still offer usable versions: the
version of a read Item, and the version of the stored Item it would write over,
are computed with the same function and compared.  Backends with a native
version (a sequence number, a row version, ...) may use that instead; the
framework treats the version as an opaque token and only compares it for
equality (or matches it against the tags of an ``If-Match`` value).

The canonical form used by ``item_version`` is a SHA-256 digest of the Item
serialized as compact JSON with sorted object keys.  It is stable across
requests for the same Item content, and differs as soon as any value differs.
Two representations of the same value are canonicalized so that an Item a
backend stores and the Item the API returns for it get the same version even
when serialization is not byte-identical: datetimes are written in UTC with a
``Z`` suffix (the way STAC writes them, whether the value is a `datetime` or an
RFC 3339 string), and object members whose value is `None` are omitted (which
is what a response serialized with ``exclude_none`` does).
"""

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import date, datetime, timezone
from typing import Any

# An RFC 3339 datetime string, as the STAC spec writes it: an upper-case `T`
# separator, and either a `Z` or a numeric offset.  Lower-case forms and a
# space separator are left as they are, so they cannot alias a canonical value.
RFC3339 = re.compile(
    r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2}(?:\.\d+)?)"
    r"(?:Z|([+-])(\d{2}):(\d{2}))$"
)

# An entity tag from an `If-Match` value (RFC 9110, section 8.8.3).  Empty list
# elements are ignored, as RFC 9110, section 5.6.1.2 asks.
ENTITY_TAG = re.compile(
    r'[ \t,]*(W/)?("[\x21\x23-\x7e\x80-\xff]*"|[^ \t,]+)[ \t]*(?:,|$)'
)


def item_payload(item: Any) -> Any:
    """Return a plain-Python representation of an Item.

    Args:
        item: a mapping or a pydantic model describing a STAC Item.

    Returns:
        The Item as a plain Python object.

    Raises:
        TypeError: if the value is neither a mapping nor a pydantic model.
    """
    if isinstance(item, Mapping):
        return dict(item)

    model_dump = getattr(item, "model_dump", None)
    if callable(model_dump):
        try:
            return model_dump(mode="json")
        except TypeError:
            return model_dump()

    raise TypeError(f"Expected a mapping or a pydantic model, got {type(item)!r}")


def _normalize_datetime(value: datetime) -> str:
    """Write a datetime the way STAC writes it: UTC with a ``Z`` suffix."""
    if value.tzinfo is not None:
        try:
            value = value.astimezone(timezone.utc)
        except (ValueError, OSError):
            return value.isoformat()
        return value.replace(tzinfo=None).isoformat() + "Z"
    return value.isoformat()


def _normalize_string(value: str) -> str:
    """Normalize an RFC 3339 string to UTC with a ``Z`` suffix."""
    match = RFC3339.match(value)
    if match is None:
        return value
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return _normalize_datetime(parsed)


def canonical_item(item: Any) -> Any:
    """Return the canonical, JSON-serializable form of an Item.

    Args:
        item: a mapping or a pydantic model describing a STAC Item.

    Returns:
        The Item with datetimes in UTC ``Z`` form and `None` members omitted.
    """
    return _canonical(item_payload(item))


def _canonical(value: Any) -> Any:
    """Canonicalize a value for versioning."""
    if isinstance(value, Mapping):
        return {
            key: _canonical(member)
            for key, member in value.items()
            if member is not None
        }
    if isinstance(value, (list, tuple)):
        return [_canonical(member) for member in value]
    if isinstance(value, datetime):
        return _normalize_datetime(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        return _normalize_string(value)
    if isinstance(value, float) and value.is_integer():
        # A pydantic model may turn an integer coordinate into a float; the
        # two name the same value, so they get the same version.
        return int(value)
    return value


def canonical_item_json(item: Any) -> str:
    """Serialize an Item to the canonical JSON used to compute its version.

    Args:
        item: a mapping or a pydantic model describing a STAC Item.

    Returns:
        Compact JSON with sorted object keys.
    """
    return json.dumps(
        canonical_item(item),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def item_version(item: Any) -> str:
    """Return the content version of an Item.

    Args:
        item: a mapping or a pydantic model describing a STAC Item.

    Returns:
        A hexadecimal SHA-256 digest of the Item's canonical JSON.
    """
    canonical = canonical_item_json(item)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def quote_etag(version: str) -> str:
    """Return an ``ETag`` header value for a version.

    A strong entity tag is a quoted string (RFC 9110, section 8.8.3).  Values
    which already are entity tags are returned unchanged, so a backend which
    keeps its own tags can pass them through.

    Args:
        version: the version token.

    Returns:
        The version as an entity tag.
    """
    version = version.strip()
    if version.startswith("W/"):
        return version
    if len(version) >= 2 and version[0] == '"' and version[-1] == '"':
        return version
    return f'"{version}"'


def unquote_etag(etag: str) -> str:
    """Return the version token of an ``ETag`` or ``If-Match`` value.

    Args:
        etag: a strong or weak entity tag, quoted or not.

    Returns:
        The tag without its ``W/`` prefix and surrounding quotes.
    """
    etag = etag.strip()
    if etag.startswith("W/"):
        etag = etag[2:].strip()
    if len(etag) >= 2 and etag[0] == '"' and etag[-1] == '"':
        return etag[1:-1]
    return etag


def parse_etags(value: str) -> list[str]:
    """Return the version tokens listed in an ``If-Match`` value.

    Args:
        value: an ``If-Match`` field value, e.g. ``"a", "b"`` or ``*``.

    Returns:
        The unquoted tokens, in the order they were listed.
    """
    value = value.strip(" \t,")
    if value == "*":
        return ["*"]
    tokens: list[str] = []
    position = 0
    while position < len(value):
        match = ENTITY_TAG.match(value, position)
        if match is None:
            break
        # group(2) is the tag itself; group(0) also has the separator.
        tokens.append(unquote_etag(match.group(2)))
        position = match.end()
    return tokens


def version_matches(expected_version: str, current_version: str) -> bool:
    """Return whether an expected version names the current version.

    ``*`` matches any current version, and a list matches when one of its
    tokens does.  The comparison is case-sensitive; surrounding quotes and a
    weak-tag prefix are ignored on both sides, so a client may send back the
    ``ETag`` it read, verbatim.

    Args:
        expected_version: the ``If-Match`` value or expected version token.
        current_version: the version of the stored Item.

    Returns:
        True when the expected version names the current version.
    """
    tokens = parse_etags(expected_version) or [expected_version.strip()]
    current = unquote_etag(current_version)
    return any(token == "*" or token == current for token in tokens)


def item_etag(item: Any) -> str | None:
    """Return the quoted ``ETag`` of an Item value, or None.

    A mapping or a pydantic model describing a GeoJSON ``Feature`` has a
    version; anything else (an ItemCollection, a message, None) does not.

    Args:
        item: a mapping or a pydantic model, or any other value.

    Returns:
        The quoted ETag, or None.
    """
    if item is None or isinstance(item, (str, bytes)):
        return None
    if not isinstance(item, Mapping) and not hasattr(item, "model_dump"):
        return None
    try:
        document = item_payload(item)
    except TypeError:
        return None
    if not isinstance(document, dict) or document.get("type") != "Feature":
        return None
    return quote_etag(item_version(document))
