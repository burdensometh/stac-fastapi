"""Field selection and projection helpers for the Fields extension.

The STAC API *Fields* extension lets a client describe which parts of an Item
should be returned. The same selection has two wire formats:

* ``GET``: a single comma separated ``fields`` parameter where excluded fields
  are prefixed with ``-`` (``?fields=id,-geometry``).
* ``POST``: a ``fields`` object holding ``include`` and ``exclude`` arrays.

:class:`FieldsProjection` normalises both forms into a single object. It serves
three purposes:

1. It tells a backend which fields it actually needs to read. When the client
   asks for a projection (an ``include`` selection) the backend does not need
   to read the rest of the document, but it still needs the pagination identity
   and any field used by filtering or sorting (see
   :meth:`FieldsProjection.backend_fields`).
2. It trims the returned entities precisely, walking nested paths such as
   ``assets.preview.href`` instead of only handling the first level (see
   :func:`filter_fields` and :func:`trim_payload`).
3. It fixes the precedence rules when a parent and one of its children are both
   referenced: ``include`` wins and the most specific path is honored.

Backends that do not support column projection can keep working. They may
ignore :meth:`FieldsProjection.backend_fields` and trim the response
themselves, for instance by calling :func:`filter_fields`.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ALWAYS_INCLUDED_FIELDS",
    "PAGINATION_IDENTITY_FIELDS",
    "STRUCTURAL_FIELDS",
    "FieldsProjection",
    "extract_filter_fields",
    "extract_sort_fields",
    "filter_fields",
    "get_backend_projection",
    "get_fields_projection",
    "resolve_selection",
    "split_fields",
    "trim_payload",
]

#: Fields that carry the pagination identity. They are always kept, both when
#: a backend restricts which columns it reads and when the response is trimmed,
#: because a next-page cursor and the item link cannot be built without them.
PAGINATION_IDENTITY_FIELDS = frozenset({"id", "collection"})

#: Markers that keep an entity recognizable as a STAC object. They are tiny
#: and always kept so trimming cannot strip the structure a consumer relies on
#: to understand what it received.
STRUCTURAL_FIELDS = frozenset({"type", "stac_version"})

#: Fields that survive any selection, even if the client excluded them. These
#: are the pagination identity and the structural markers above. Everything
#: else, including ``geometry`` and ``bbox``, can be excluded on request: the
#: extension explicitly allows a server to return an invalid Item.
ALWAYS_INCLUDED_FIELDS = PAGINATION_IDENTITY_FIELDS | STRUCTURAL_FIELDS


def split_fields(selection: Iterable[str]) -> tuple[frozenset[str], frozenset[str]]:
    """Split raw selectors into an ``(include, exclude)`` pair.

    ``+field`` and bare names are includes, ``-field`` is an exclude. Empty
    names (for example an empty ``fields=`` parameter) are ignored.
    """
    include: set[str] = set()
    exclude: set[str] = set()
    for raw in selection:
        if not isinstance(raw, str):
            continue
        name = raw.strip()
        if not name:
            continue
        if name.startswith("-"):
            name = name[1:].strip()
            if name:
                exclude.add(name)
        else:
            include.add((name[1:].strip() if name.startswith("+") else name).strip())
    include.discard("")
    return frozenset(include), frozenset(exclude)


def _is_ancestor(ancestor: str, path: str) -> bool:
    """Whether ``ancestor`` is ``path`` or one of its dotted prefixes."""
    return path == ancestor or path.startswith(f"{ancestor}.")


def resolve_selection(
    include: Iterable[str], exclude: Iterable[str]
) -> tuple[frozenset[str], frozenset[str]]:
    """Normalise include/exclude sets following the extension precedence rules.

    * A field present in both sets is included.
    * Excluding a parent while including one of its children keeps only the
      children (the most specific path wins).
    * Including a parent while excluding one of its children keeps the parent
      but drops the excluded child.

    The result does not depend on the order in which selectors were provided.
    """
    include_set = frozenset(include)
    exclude_set = frozenset(exclude)

    # A field in both sets is included.
    exclude_set = exclude_set - include_set

    # An exclude that is an ancestor of an included field would remove that
    # field. The more specific include wins, so drop such excludes.
    exclude_set = frozenset(
        field
        for field in exclude_set
        if not any(_is_ancestor(field, included) for included in include_set)
    )
    return include_set, exclude_set


def _collapse_include(include: frozenset[str]) -> frozenset[str]:
    """Drop includes shadowed by an included ancestor.

    Including ``assets`` already selects everything below it, so keeping
    ``assets.preview.href`` alongside it would make the result depend on
    iteration order.
    """
    return frozenset(
        path
        for path in include
        if not any(other != path and _is_ancestor(other, path) for other in include)
    )


class FieldsProjection(BaseModel):
    """A resolved field selection plus the data a backend must read.

    Attributes:
        include: fields the client asked to include. Empty when the client only
            excluded fields, or asked for nothing at all.
        exclude: fields the client asked to omit, after precedence resolution.
        requested: whether the client actually sent a ``fields`` parameter.
        search_fields: fields used by filtering or sorting. They are read by a
            backend but are not part of the response unless also included.
    """

    model_config = ConfigDict(frozen=True)

    include: frozenset[str] = Field(default_factory=frozenset)
    exclude: frozenset[str] = Field(default_factory=frozenset)
    requested: bool = False
    search_fields: frozenset[str] = Field(default_factory=frozenset)

    @classmethod
    def from_sets(
        cls,
        include: Iterable[str] | None,
        exclude: Iterable[str] | None,
        *,
        requested: bool = True,
        search_fields: Iterable[str] | None = None,
    ) -> FieldsProjection:
        """Create a projection from POST style ``include``/``exclude`` sets."""
        include_set, exclude_set = resolve_selection(include or (), exclude or ())
        return cls(
            include=include_set,
            exclude=exclude_set,
            requested=requested,
            search_fields=search_fields or (),
        )

    @classmethod
    def from_param(
        cls,
        fields: str | Iterable[str] | None,
        *,
        search_fields: Iterable[str] | None = None,
    ) -> FieldsProjection:
        """Create a projection from a GET style ``fields`` parameter."""
        if fields is None:
            return cls(search_fields=search_fields or ())
        selection = fields.split(",") if isinstance(fields, str) else list(fields)
        include, exclude = split_fields(selection)
        return cls.from_sets(
            include,
            exclude,
            requested=bool(selection),
            search_fields=search_fields,
        )

    @property
    def empty(self) -> bool:
        """Whether this projection would leave the response untouched."""
        return not self.include and not self.exclude

    @property
    def is_projection(self) -> bool:
        """Whether a backend can restrict which columns it reads."""
        return bool(self.include)

    def with_search_fields(self, *fields: Iterable[str] | None) -> FieldsProjection:
        """Return a copy that also reads fields used by filter or sort."""
        extra: set[str] = set(self.search_fields)
        for values in fields:
            extra.update(values or ())
        if frozenset(extra) == self.search_fields:
            return self
        return self.model_copy(update={"search_fields": frozenset(extra)})

    def backend_fields(self) -> list[str] | None:
        """Fields to read from storage, or ``None`` when no projection applies.

        ``None`` is the signal for a backend to read the full document. It is
        returned whenever the client did not send an ``include`` selection, so
        ``exclude``-only and empty selections behave exactly as before.

        The returned names include the client selection, the pagination
        identity and structural markers, and any field used to filter or sort
        the search. Those extra fields are read but not returned.
        """
        if not self.is_projection:
            return None
        return sorted(set(self.include) | ALWAYS_INCLUDED_FIELDS | self.search_fields)

    def is_excluded(self, field: str) -> bool:
        """Whether ``field`` (a dotted path) is excluded from the response."""
        if field in ALWAYS_INCLUDED_FIELDS:
            return False
        if any(_is_ancestor(included, field) for included in self.include):
            return False
        return any(_is_ancestor(excluded, field) for excluded in self.exclude)

    def contains(self, field: str) -> bool:
        """Whether ``field`` (a dotted path) survives this projection."""
        if field in ALWAYS_INCLUDED_FIELDS:
            return True
        if any(_is_ancestor(included, field) for included in self.include):
            return True
        if self.is_projection:
            return False
        return not self.is_excluded(field)

    def apply(self, entity: Mapping[str, Any]) -> dict[str, Any]:
        """Return a deep-trimmed copy of ``entity``."""
        return filter_fields(entity, self)


def _filter_expr_of(obj: Any) -> Any:
    if isinstance(obj, Mapping):
        return obj.get("filter_expr") or obj.get("filter")
    return getattr(obj, "filter_expr", None)


def _sortby_of(obj: Any) -> Any:
    if isinstance(obj, Mapping):
        return obj.get("sortby")
    return getattr(obj, "sortby", None)


def get_fields_projection(obj: Any) -> FieldsProjection | None:
    """Extract a projection from a request model or a raw ``fields`` value.

    Accepts a :class:`FieldsProjection`, a ``fields`` parameter (``str`` or
    iterable), a POST ``fields`` object with ``include``/``exclude``, or a
    request model carrying one of those as a ``fields`` attribute. Returns
    ``None`` when the object does not carry any field selection.

    Filter and sort fields are recorded so a backend can keep reading them.
    """
    projection = _raw_projection(obj)
    if projection is None:
        return None
    return projection.with_search_fields(
        extract_filter_fields(_filter_expr_of(obj)),
        extract_sort_fields(_sortby_of(obj)),
    )


def _raw_projection(obj: Any) -> FieldsProjection | None:
    if obj is None:
        return None
    if isinstance(obj, FieldsProjection):
        return obj
    if isinstance(obj, str):
        return FieldsProjection.from_param(obj)
    if isinstance(obj, Mapping):
        if "include" in obj or "exclude" in obj:
            return FieldsProjection.from_sets(
                obj.get("include"), obj.get("exclude"), requested=True
            )
        if "fields" in obj:
            return _raw_projection(obj["fields"])
        return None
    if isinstance(obj, (list, tuple, set, frozenset)):
        return FieldsProjection.from_param(obj)

    # Prefer a ``fields`` attribute (request models, POST body object) so an
    # ``include``/``exclude`` attribute from an unrelated extension cannot be
    # mistaken for a field selection.
    fields = getattr(obj, "fields", None)
    if fields is not None and fields is not obj:
        return _raw_projection(fields)

    include = getattr(obj, "include", None)
    exclude = getattr(obj, "exclude", None)
    if include is not None or exclude is not None:
        return FieldsProjection.from_sets(include, exclude, requested=True)
    return None


def get_backend_projection(obj: Any) -> list[str] | None:
    """Return the exact columns a backend should read for this request.

    This is the helper a backend calls with its request object (a search
    request model for POST, the endpoint kwargs for GET). ``None`` means the
    backend should keep reading full documents.
    """
    projection = get_fields_projection(obj)
    if projection is None:
        return None
    return projection.backend_fields()


def _get_path(source: Any, path: str) -> tuple[bool, Any]:
    current = source
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _set_path(target: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    current = target
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = value


def _remove_path(target: dict[str, Any], path: str) -> None:
    parts = path.split(".")
    current: Any = target
    for part in parts[:-1]:
        if not isinstance(current, dict) or part not in current:
            return
        current = current[part]
    if isinstance(current, dict):
        current.pop(parts[-1], None)


def _select(
    source: Mapping[str, Any],
    include: frozenset[str],
    exclude: frozenset[str],
) -> dict[str, Any]:
    if include:
        effective = _collapse_include(include)
        result: dict[str, Any] = {}
        for path in sorted(effective):
            found, value = _get_path(source, path)
            if found:
                if isinstance(value, Mapping):
                    value = copy.deepcopy(dict(value))
                _set_path(result, path, value)
    else:
        effective = include
        result = copy.deepcopy(dict(source))

    for path in sorted(exclude):
        _remove_path(result, path)

    # Identity and structural fields are always returned so a caller can page
    # through the trimmed entities and recognize what it received.
    for path in ALWAYS_INCLUDED_FIELDS:
        if _get_path(result, path)[0]:
            continue
        found, value = _get_path(source, path)
        if found:
            _set_path(result, path, value)

    _prune_empty(result, effective, exclude)
    return result


def _prune_empty(
    node: dict[str, Any],
    include: frozenset[str],
    exclude: frozenset[str],
    prefix: str = "",
) -> None:
    """Drop containers left empty because all their children were removed.

    Only containers that an exclude reached into are pruned, so an empty
    object that the source already had (or that an include selected whole) is
    preserved. A container the client explicitly asked for, or an ancestor of
    an included path, is likewise kept.
    """
    for key in list(node):
        child = node[key]
        if not isinstance(child, dict):
            continue
        path = f"{prefix}{key}"
        _prune_empty(child, include, exclude, f"{path}.")
        if child:
            continue
        if path in ALWAYS_INCLUDED_FIELDS:
            continue
        if any(path == inc or _is_ancestor(path, inc) for inc in include):
            continue
        if not any(_is_ancestor(path, exc) for exc in exclude):
            continue
        del node[key]


def filter_fields(
    entity: Mapping[str, Any],
    projection: FieldsProjection | Iterable[str] | None,
    exclude: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Return a deep-trimmed copy of ``entity`` honoring ``projection``.

    ``projection`` may be a :class:`FieldsProjection`, or an ``include`` set
    when ``exclude`` is given, to match the historic backend helper signature.
    """
    if projection is None:
        return dict(entity)
    if not isinstance(projection, FieldsProjection):
        projection = FieldsProjection.from_sets(projection, exclude, requested=True)
    if projection.empty:
        return dict(entity)
    return _select(entity, projection.include, projection.exclude)


def trim_payload(
    payload: Any,
    projection: FieldsProjection | None,
) -> Any:
    """Trim the entities of an ItemCollection/Collections payload.

    The envelope is left untouched: its ``type``, ``links`` and ``number*``
    members are needed to page the result and to describe the response, and
    only the ``features`` or ``collections`` entries are trimmed. Returns the
    original object when there is nothing to trim or when the payload is not a
    recognised container (for example a pre-built ``Response``).
    """
    if projection is None or projection.empty:
        return payload

    def _trim(entity: Any) -> Any:
        if isinstance(entity, Mapping):
            return filter_fields(entity, projection)
        return entity

    if isinstance(payload, Mapping):
        if isinstance(payload.get("features"), list):
            trimmed = dict(payload)
            trimmed["features"] = [_trim(item) for item in payload["features"]]
            return trimmed
        if isinstance(payload.get("collections"), list):
            trimmed = dict(payload)
            trimmed["collections"] = [
                _trim(collection) for collection in payload["collections"]
            ]
            return trimmed
    return payload


_CQL2_TEXT_PROPERTY = re.compile(r'"([^"]+)"|([A-Za-z_][\w:.]*)')
_CQL2_TEXT_STRING = re.compile(r"'(?:[^'\\]|\\.)*'")
_CQL2_KEYWORDS = {
    "and",
    "or",
    "not",
    "like",
    "between",
    "in",
    "is",
    "null",
    "true",
    "false",
    "date",
    "timestamp",
    "interval",
    "t_intersects",
    "t_before",
    "t_after",
    "t_during",
    "s_intersects",
    "s_within",
    "s_contains",
    "s_disjoint",
}


def extract_filter_fields(filter_expr: Any) -> set[str]:
    """Collect the property names referenced by a CQL2 filter.

    Supports CQL2-JSON (mappings with ``{"property": name}`` nodes, which is how
    the filter extension delivers filters) and a conservative subset of
    CQL2-text. Unknown tokens are ignored: callers only use the result to keep
    extra fields readable, so a superset is harmless.
    """
    fields: set[str] = set()
    if filter_expr is None:
        return fields
    if isinstance(filter_expr, str):
        fields.update(_cql2_text_fields(filter_expr))
        return fields

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            property_name = node.get("property")
            if isinstance(property_name, str):
                fields.add(property_name)
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(filter_expr)
    return fields


def _cql2_text_fields(expr: str) -> set[str]:
    # Single quoted literals are values, not properties.
    without_literals = _CQL2_TEXT_STRING.sub(" ", expr)
    fields: set[str] = set()
    for quoted, bare in _CQL2_TEXT_PROPERTY.findall(without_literals):
        name = quoted or bare
        if not name or name.lower() in _CQL2_KEYWORDS:
            continue
        fields.add(name)
    return fields


def extract_sort_fields(sortby: Any) -> set[str]:
    """Collect the field names used by a sort clause.

    Accepts GET style strings (``+properties.datetime``, ``-id``), POST style
    mappings (``{"field": ..., "direction": ...}``) and objects exposing a
    ``field`` attribute (as ``stac_pydantic``'s sort model does).
    """
    fields: set[str] = set()
    if sortby is None:
        return fields
    if isinstance(sortby, str):
        sortby = [sortby]
    for item in sortby:
        if isinstance(item, str):
            name = item.strip()
            name = name[1:].strip() if name[:1] in "+-" else name
            if name:
                fields.add(name)
        elif isinstance(item, Mapping):
            name = item.get("field")
            if isinstance(name, str) and name:
                fields.add(name)
        else:
            name = getattr(item, "field", None)
            if isinstance(name, str) and name:
                fields.add(name)
    return fields
