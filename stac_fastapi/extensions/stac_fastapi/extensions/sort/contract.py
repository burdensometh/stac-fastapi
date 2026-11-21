"""Explicit multi-key sort contract for the Sort extension.

The STAC Sort extension lets clients pass an ordered list of sort keys, but on
its own it says nothing about how values of a given field must be compared,
where missing values are placed, or how ties are broken. Backends therefore
disagree: PostgreSQL puts ``NULL`` values last in ascending order, SQLite puts
them first, MongoDB uses its own collation, and so on. When results are sorted
*inside the database* and only afterwards paginated, those differing null
orderings make the same request return different pages on different backends
(or skip/duplicate rows across pages when the tiebreak order is not stable).

This module turns a request's ``sortby`` parameter into an explicit
:class:`SortContract` that removes every backend-specific choice:

* the comparable type of every key (string, number, boolean, datetime,
  geometry, ...) and its ascending/descending direction are resolved from the
  Sortables JSON Schema definitions, not guessed from the data;
* the position of missing values (``NULL``/absent) is pinned per key, so every
  backend emits the same ordering;
* a stable tail of ``collection`` then ``id`` is always appended (unless the
  client already supplied those keys), making the total ordering a total,
  deterministic ordering that pagination tokens can safely be built from;
* values whose runtime type cannot be compared under the declared schema type
  raise :class:`IncomparableSortValueError` instead of falling through to a
  Python ``TypeError`` or a backend-specific error.

The contract is intentionally transport-neutral. A GET request (``+field`` /
``-field`` strings) and a POST request (``{"field", "direction"}`` objects)
parse to *equal* :class:`SortContract` instances, and backend clients are
expected to translate the whole contract into the data store's native
ordering clause **before** applying ``limit``/pagination. Sorting one page in
the service layer after the query is explicitly unsupported: it cannot fix
rows that the database never returned.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Sequence

from stac_fastapi.types.rfc3339 import rfc3339_str_to_datetime

__all__ = [
    "NullPosition",
    "SortDirection",
    "SortValueType",
    "SortKey",
    "SortContract",
    "SortContractError",
    "UnknownSortableError",
    "AmbiguousSortableTypeError",
    "IncomparableSortValueError",
    "get_field_value",
    "sort_items",
]

# Vendor annotation recognized inside a Sortables JSON Schema property to pin
# where missing values sort. The default (and the only portable behavior
# without a hint) is NullPosition.LAST.
NULL_POSITION_KEYWORD = "x-null-position"

# Vendor annotation for the direction assumed by a bare GET token (one with
# no +/- prefix). Defaults to ascending per the Sort extension spec.
DEFAULT_DIRECTION_KEYWORD = "x-default-direction"

# Stable tail keys, in order. ``collection`` first because an item id is only
# guaranteed unique within a collection, then ``id``.
STABLE_TAIL_FIELDS: tuple[str, ...] = ("collection", "id")


class SortContractError(ValueError):
    """Base class for errors raised while building or applying a sort contract."""


class UnknownSortableError(SortContractError):
    """A requested sort field is not declared by the Sortables schema."""


class AmbiguousSortableTypeError(SortContractError):
    """A Sortables definition does not resolve to a single comparable type.

    Raised when a property declares several non-null primitive types (for
    example ``{"type": ["string", "number"]}``), because ordering strings and
    numbers against each other is not defined, or when it only declares
    non-orderable types such as objects or arrays.
    """


class IncomparableSortValueError(SortContractError):
    """A runtime value cannot be compared under the field's declared type."""


class SortDirection(str, Enum):
    """Sort direction shared by GET and POST expressions."""

    ASC = "asc"
    DESC = "desc"


class NullPosition(str, Enum):
    """Where missing values are placed, independently of the direction."""

    FIRST = "first"
    LAST = "last"


class SortValueType(str, Enum):
    """Comparable type of a sort key, resolved from a Sortables JSON Schema.

    Only the primitive JSON schema types have a defined ordering. The
    non-scalar types are retained so that an *explicit* error can be raised
    instead of silently comparing dicts or lists by memory identity.
    """

    STRING = "string"
    NUMBER = "number"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    DATETIME = "date-time"
    OBJECT = "object"
    ARRAY = "array"

    @property
    def is_orderable(self) -> bool:
        """Whether values of this type have a well-defined total ordering."""
        return self in _ORDERABLE_TYPES

    @property
    def python_types(self) -> tuple[type[Any], ...]:
        """Python types accepted at runtime for this declared sort type."""
        return _PYTHON_TYPES[self]


_ORDERABLE_TYPES: frozenset[SortValueType] = frozenset(
    {
        SortValueType.STRING,
        SortValueType.NUMBER,
        SortValueType.INTEGER,
        SortValueType.BOOLEAN,
        SortValueType.DATETIME,
    }
)

_PYTHON_TYPES: dict[SortValueType, tuple[type[Any], ...]] = {
    SortValueType.STRING: (str,),
    # bool is a subclass of int; numeric columns must not accept booleans, so
    # it is filtered explicitly in _coerce_comparable.
    SortValueType.NUMBER: (int, float),
    SortValueType.INTEGER: (int,),
    SortValueType.BOOLEAN: (bool,),
    SortValueType.DATETIME: (str, datetime),
    SortValueType.OBJECT: (Mapping,),
    SortValueType.ARRAY: (list, tuple),
}

# JSON Schema ``type`` -> sort value type. JSON Schema's "number" covers both
# ints and floats; "integer" stays distinct for schema fidelity.
_JSON_TYPE_MAP: dict[str, SortValueType] = {
    "string": SortValueType.STRING,
    "number": SortValueType.NUMBER,
    "integer": SortValueType.INTEGER,
    "boolean": SortValueType.BOOLEAN,
    "object": SortValueType.OBJECT,
    "array": SortValueType.ARRAY,
}


@dataclass(frozen=True)
class SortKey:
    """One fully resolved element of a multi-key sort contract."""

    field: str
    direction: SortDirection = SortDirection.ASC
    null_position: NullPosition = NullPosition.LAST
    value_type: SortValueType = SortValueType.STRING

    @property
    def is_ascending(self) -> bool:
        """Whether the key sorts ascending."""
        return self.direction is SortDirection.ASC

    @property
    def nulls_first(self) -> bool:
        """Whether missing values sort before present values.

        The position is pinned independently of the direction, matching the
        SQL ``NULLS FIRST`` / ``NULLS LAST`` semantics that backends translate
        to their own native mechanism.
        """
        return self.null_position is NullPosition.FIRST


@dataclass(frozen=True)
class _SortExpression:
    """One client-supplied sort key before schema resolution."""

    field: str
    direction: SortDirection
    explicit_direction: bool


@dataclass(frozen=True)
class SortContract:
    """An explicit, backend-neutral multi-key ordering.

    Instances are immutable and compared structurally, so a contract parsed
    from a GET request is ``==`` to one parsed from the equivalent POST
    request. Backends translate :attr:`keys` into their native ordering clause
    (e.g. SQL ``ORDER BY ... NULLS FIRST/LAST``) and must apply it to the full
    result set before pagination.
    """

    keys: tuple[SortKey, ...] = field(default_factory=tuple)

    # -- factories -----------------------------------------------------------

    @classmethod
    def from_get_sortby(
        cls,
        sortby: Sequence[str] | None,
        sortables: Mapping[str, Any] | None = None,
        *,
        allow_undeclared: bool = False,
    ) -> SortContract:
        """Build a contract from GET ``sortby`` tokens.

        Tokens use the STAC Sort syntax: a field name optionally prefixed by
        ``+`` (ascending) or ``-`` (descending). A bare field name carries no
        explicit direction; the schema's ``x-default-direction`` hint (or
        ascending, per the spec) then applies.
        """
        expressions = [_parse_get_token(token) for token in (sortby or [])]
        return cls._from_expressions(
            expressions,
            sortables,
            allow_undeclared=allow_undeclared,
        )

    @classmethod
    def from_post_sortby(
        cls,
        sortby: Sequence[Any] | None,
        sortables: Mapping[str, Any] | None = None,
        *,
        allow_undeclared: bool = False,
    ) -> SortContract:
        """Build a contract from POST ``sortby`` objects.

        Each object exposes ``field`` and ``direction`` attributes (the
        ``stac_pydantic`` ``SortExtension`` model) or mapping keys. POST
        directions are always explicit.
        """
        expressions = [_parse_post_expression(item) for item in (sortby or [])]
        return cls._from_expressions(
            expressions,
            sortables,
            allow_undeclared=allow_undeclared,
        )

    @classmethod
    def _from_expressions(
        cls,
        expressions: Sequence[_SortExpression],
        sortables: Mapping[str, Any] | None,
        *,
        allow_undeclared: bool,
    ) -> SortContract:
        """Resolve parsed wire expressions against the Sortables schema."""
        properties = _sortable_properties(sortables)
        keys: list[SortKey] = []
        seen: set[str] = set()

        for expression in expressions:
            field_name = expression.field
            if not field_name:
                raise SortContractError("Sort field names must be non-empty strings.")
            if field_name in seen:
                raise SortContractError(
                    f"Duplicate sort field '{field_name}' in sortby."
                )
            seen.add(field_name)

            definition = properties.get(field_name)
            if definition is None and not allow_undeclared:
                raise UnknownSortableError(
                    f"Field '{field_name}' is not declared in the Sortables schema."
                )
            keys.append(
                _resolve_sort_key(expression, definition),
            )

        _append_stable_tail(keys, seen, properties)
        return cls(keys=tuple(keys))

    # -- helpers -------------------------------------------------------------

    @property
    def fields(self) -> tuple[str, ...]:
        """Ordered field names of the contract, including the stable tail."""
        return tuple(key.field for key in self.keys)

    def without_stable_tail(self) -> SortContract:
        """Return the client-requested keys, without the synthesized tail."""
        tail = set(STABLE_TAIL_FIELDS)
        return SortContract(
            keys=tuple(key for key in self.keys if key.field not in tail)
        )

    def compare(self, left: Mapping[str, Any], right: Mapping[str, Any]) -> int:
        """Compare two STAC objects under this contract.

        Returns a negative/zero/positive integer following the comparator
        protocol. Missing values follow each key's pinned
        :class:`NullPosition`, independently of direction. Values whose type
        is not comparable under the declared schema type raise
        :class:`IncomparableSortValueError`.

        This reference implementation exists for in-memory backends and for
        tests; database backends should translate the contract into a native
        ordering clause rather than fetching every row to compare in Python.
        """
        for key in self.keys:
            result = _compare_key(key, left, right)
            if result != 0:
                return result
        return 0


# ---------------------------------------------------------------------------
# Wire parsing
# ---------------------------------------------------------------------------


def _parse_get_token(token: str) -> _SortExpression:
    """Parse one GET ``+field``/``-field``/``field`` token."""
    if not isinstance(token, str):
        raise SortContractError(
            f"GET sortby entries must be strings, got {type(token).__name__}."
        )
    token = token.strip()
    if not token:
        raise SortContractError("Sort field names must be non-empty strings.")
    if token.startswith("-"):
        return _SortExpression(
            field=token[1:].strip(),
            direction=SortDirection.DESC,
            explicit_direction=True,
        )
    if token.startswith("+"):
        return _SortExpression(
            field=token[1:].strip(),
            direction=SortDirection.ASC,
            explicit_direction=True,
        )
    return _SortExpression(
        field=token,
        direction=SortDirection.ASC,
        explicit_direction=False,
    )


def _parse_post_expression(item: Any) -> _SortExpression:
    """Parse one POST ``{"field", "direction"}`` expression (model or dict)."""
    if isinstance(item, Mapping):
        field_name = item.get("field")
        direction = item.get("direction", SortDirection.ASC.value)
    else:
        field_name = getattr(item, "field", None)
        direction = getattr(item, "direction", SortDirection.ASC.value)
        if isinstance(direction, Enum):
            direction = direction.value

    if not isinstance(field_name, str) or not field_name.strip():
        raise SortContractError("Sort field names must be non-empty strings.")
    try:
        resolved_direction = SortDirection(str(direction))
    except ValueError as exc:
        raise SortContractError(
            f"Invalid sort direction '{direction}' for field '{field_name}'."
        ) from exc
    return _SortExpression(
        field=field_name.strip(),
        direction=resolved_direction,
        explicit_direction=True,
    )


def _sortable_properties(
    sortables: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    """Extract the ``properties`` mapping from a Sortables response."""
    if sortables is None:
        return {}
    properties = sortables.get("properties", {})
    if not isinstance(properties, Mapping):
        raise SortContractError(
            "Sortables schema 'properties' must be a JSON Schema object mapping "
            "field names to definitions."
        )
    return properties


# ---------------------------------------------------------------------------
# Schema resolution
# ---------------------------------------------------------------------------


def _resolve_sort_key(
    expression: _SortExpression,
    definition: Mapping[str, Any] | None,
) -> SortKey:
    """Resolve one expression against its JSON Schema definition."""
    field_name = expression.field
    direction = expression.direction
    null_position = NullPosition.LAST
    value_type = SortValueType.STRING

    if definition is not None:
        value_type, nullable = _resolve_value_type(field_name, definition)
        null_position = _resolve_null_position(field_name, definition, nullable)
        if not expression.explicit_direction:
            direction = _resolve_default_direction(field_name, definition)

    if not value_type.is_orderable:
        raise AmbiguousSortableTypeError(
            f"Field '{field_name}' is declared with non-orderable type "
            f"'{value_type.value}' and cannot be used as a sort key."
        )

    return SortKey(
        field=field_name,
        direction=direction,
        null_position=null_position,
        value_type=value_type,
    )


def _resolve_default_direction(
    field_name: str, definition: Mapping[str, Any]
) -> SortDirection:
    """Read the ``x-default-direction`` hint, defaulting to ascending."""
    hinted = definition.get(DEFAULT_DIRECTION_KEYWORD)
    if hinted is None:
        return SortDirection.ASC
    try:
        return SortDirection(str(hinted))
    except ValueError as exc:
        raise SortContractError(
            f"Sortables field '{field_name}' declares an invalid "
            f"{DEFAULT_DIRECTION_KEYWORD} '{hinted}'."
        ) from exc


def _resolve_value_type(
    field_name: str, definition: Mapping[str, Any]
) -> tuple[SortValueType, bool]:
    """Resolve a JSON Schema definition to one comparable type.

    Returns the type and whether the definition explicitly allows null. A
    schema may spread its alternatives across ``type`` arrays or
    ``anyOf``/``oneOf``; both are flattened first.
    """
    nullable = _allows_null(definition)
    non_null = _primitive_types(definition)
    if not non_null:
        raise AmbiguousSortableTypeError(
            f"Sortables field '{field_name}' has no non-null type definition."
        )

    # int and float together still form one numeric ordering.
    if set(non_null) <= {SortValueType.INTEGER, SortValueType.NUMBER}:
        chosen = (
            SortValueType.NUMBER
            if SortValueType.NUMBER in non_null
            else SortValueType.INTEGER
        )
        return chosen, nullable

    if len(set(non_null)) > 1:
        names = ", ".join(sorted(candidate.value for candidate in non_null))
        raise AmbiguousSortableTypeError(
            f"Sortables field '{field_name}' declares incomparable types "
            f"({names}); a sort key must resolve to a single comparable type."
        )

    return non_null[0], nullable


def _primitive_types(definition: Mapping[str, Any]) -> list[SortValueType]:
    """Flatten ``type`` arrays and ``anyOf``/``oneOf`` alternatives."""
    candidates: list[SortValueType] = []

    raw_type = definition.get("type")
    if isinstance(raw_type, str):
        candidates.append(_map_json_type(definition, raw_type))
    elif isinstance(raw_type, list):
        for entry in raw_type:
            if isinstance(entry, str) and entry != "null":
                candidates.append(_map_json_type(definition, entry))

    for combinator in ("anyOf", "oneOf"):
        for alternative in definition.get(combinator, []) or []:
            if not isinstance(alternative, Mapping):
                continue
            sub_type = alternative.get("type")
            if isinstance(sub_type, str) and sub_type != "null":
                candidates.append(_map_json_type(alternative, sub_type))

    return candidates


def _map_json_type(definition: Mapping[str, Any], json_type: str) -> SortValueType:
    """Map a JSON Schema primitive type to a :class:`SortValueType`."""
    if json_type == "string" and definition.get("format") == "date-time":
        return SortValueType.DATETIME
    try:
        return _JSON_TYPE_MAP[json_type]
    except KeyError as exc:
        raise AmbiguousSortableTypeError(
            f"Unsupported JSON Schema type '{json_type}' in Sortables definition."
        ) from exc


def _allows_null(definition: Mapping[str, Any]) -> bool:
    """Whether a schema explicitly permits null via type arrays or combinators."""
    raw_type = definition.get("type")
    if isinstance(raw_type, list) and "null" in raw_type:
        return True
    for combinator in ("anyOf", "oneOf"):
        for alternative in definition.get(combinator, []) or []:
            if isinstance(alternative, Mapping) and alternative.get("type") == "null":
                return True
    return False


def _resolve_null_position(
    field_name: str,
    definition: Mapping[str, Any],
    nullable: bool,
) -> NullPosition:
    """Resolve the pinned missing-value position for a nullable field."""
    if not nullable:
        # Non-nullable fields have no missing values; keep the portable
        # default rather than trusting an irrelevant annotation.
        return NullPosition.LAST

    raw = definition.get(NULL_POSITION_KEYWORD, NullPosition.LAST.value)
    try:
        return NullPosition(str(raw))
    except ValueError as exc:
        raise SortContractError(
            f"Sortables field '{field_name}' declares an invalid "
            f"{NULL_POSITION_KEYWORD} '{raw}'; expected 'first' or 'last'."
        ) from exc


def _append_stable_tail(
    keys: list[SortKey],
    seen: set[str],
    properties: Mapping[str, Any],
) -> None:
    """Append collection/id so the whole ordering is deterministic.

    Client-supplied keys on these fields are kept where the client placed
    them; only the missing tail entries are synthesized.
    """
    for tail_field in STABLE_TAIL_FIELDS:
        if tail_field in seen:
            continue
        definition = properties.get(tail_field)
        if definition is not None:
            keys.append(
                _resolve_sort_key(
                    _SortExpression(
                        field=tail_field,
                        direction=SortDirection.ASC,
                        explicit_direction=True,
                    ),
                    definition,
                )
            )
        else:
            keys.append(
                SortKey(
                    field=tail_field,
                    direction=SortDirection.ASC,
                    null_position=NullPosition.LAST,
                    value_type=SortValueType.STRING,
                )
            )
        seen.add(tail_field)


# ---------------------------------------------------------------------------
# In-memory application
# ---------------------------------------------------------------------------


def get_field_value(item: Mapping[str, Any], field_path: str) -> Any:
    """Read a dotted STAC field path (e.g. ``properties.gsd``) from an item.

    Returns ``None`` when any segment is missing, treating absent values and
    explicit nulls uniformly as missing for ordering purposes.
    """
    current: Any = item
    for segment in field_path.split("."):
        if isinstance(current, Mapping) and segment in current:
            current = current[segment]
        else:
            return None
    return current


def _coerce_comparable(key: SortKey, value: Any) -> Any:
    """Validate a present value against the key's declared sort type."""
    # Reject booleans masquerading as numbers (bool subclasses int).
    if key.value_type in (
        SortValueType.NUMBER,
        SortValueType.INTEGER,
    ) and isinstance(value, bool):
        raise IncomparableSortValueError(
            f"Value for sort field '{key.field}' is boolean, but the field is "
            f"declared as {key.value_type.value}."
        )
    if key.value_type is SortValueType.DATETIME and isinstance(value, str):
        try:
            return rfc3339_str_to_datetime(value)
        except Exception as exc:  # noqa: BLE001 - normalize to contract error
            raise IncomparableSortValueError(
                f"Value '{value}' for sort field '{key.field}' is not a valid "
                "RFC 3339 date-time."
            ) from exc
    expected = key.value_type.python_types
    if not isinstance(value, expected):
        raise IncomparableSortValueError(
            f"Value for sort field '{key.field}' has type "
            f"{type(value).__name__}, which is not comparable under the declared "
            f"{key.value_type.value} type."
        )
    return value


def _compare_key(
    key: SortKey, left: Mapping[str, Any], right: Mapping[str, Any]
) -> int:
    """Compare two objects under one sort key, honoring null position."""
    left_raw = get_field_value(left, key.field)
    right_raw = get_field_value(right, key.field)
    left_missing = left_raw is None
    right_missing = right_raw is None

    if left_missing and right_missing:
        return 0
    if left_missing:
        return -1 if key.nulls_first else 1
    if right_missing:
        return 1 if key.nulls_first else -1

    left_value = _coerce_comparable(key, left_raw)
    right_value = _coerce_comparable(key, right_raw)

    if left_value < right_value:
        result = -1
    elif left_value > right_value:
        result = 1
    else:
        result = 0

    if key.direction is SortDirection.DESC:
        result = -result
    return result


def sort_items(
    items: Sequence[Mapping[str, Any]], contract: SortContract
) -> list[Mapping[str, Any]]:
    """Sort in-memory items under the full contract.

    This operates on the complete result set and is meant for in-memory
    backends and tests. Database backends must push the contract into the
    query's ordering clause so that ordering happens before pagination;
    re-sorting a single fetched page cannot produce correct pages.
    """
    return sorted(items, key=functools.cmp_to_key(contract.compare))
