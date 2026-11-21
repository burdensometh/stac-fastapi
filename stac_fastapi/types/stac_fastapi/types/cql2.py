"""CQL2 expression parsing, inspection, splitting and evaluation.

This module provides the CQL2 machinery a backend needs to do *partial
pushdown*: hand the provably safe part of a filter to a database and evaluate
the rest locally on the returned candidate Items.

Implemented are the logical operators (``and``, ``or``, ``not``), the
comparison operators, ``like``, ``between``, ``in``, ``isNull``, arithmetic and
the CQL2 temporal functions.  Spatial predicates cannot be evaluated here (no
geometry engine is assumed); they are pushed down when the backend declares
support for them and rejected otherwise, rather than being silently dropped.

Expressions may be supplied either as ``cql2-json`` (a ``dict``) or as
``cql2-text`` (a ``str``).  The text parser understands the same subset of the
language.  Anything outside of it raises :class:`UnsupportedCql2Function`,
which is an :class:`~stac_fastapi.types.errors.InvalidQueryParameter` and so is
answered with ``400 Bad Request`` by an application using the default
exception handlers.
"""

import copy
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from stac_fastapi.types.errors import InvalidQueryParameter
from stac_fastapi.types.rfc3339 import (
    datetime_to_str,
    rfc3339_str_to_datetime,
    str_to_interval,
)

__all__ = [
    "Cql2Error",
    "InvalidCql2Expression",
    "PushdownCapabilities",
    "SplitResult",
    "UnsupportedCql2Function",
    "canonical_operator",
    "conjoin",
    "evaluate",
    "is_locally_evaluable",
    "parse_cql2",
    "parse_cql2_text",
    "referenced_properties",
    "selectors_to_filter",
    "split_filter",
    "validate_expression",
]


class Cql2Error(InvalidQueryParameter):
    """A CQL2 expression is malformed or cannot be evaluated locally."""


class InvalidCql2Expression(Cql2Error):
    """A CQL2 expression is syntactically invalid."""


class UnsupportedCql2Function(Cql2Error):
    """A CQL2 operator or function is not supported.

    Raised instead of silently ignoring a predicate: ignoring part of a filter
    would return items the client did not ask for.
    """


#: Internal, locally-evaluable bounding-box intersection predicate.  Request
#: selectors (``bbox``) are translated to it so they can be evaluated locally
#: without pulling in a geometry engine.
BBOX_INTERSECTS = "bbox_intersects"

#: Operator aliases, keyed by their lowercase spelling.
_OPERATOR_ALIASES = {
    "=": "=",
    "eq": "=",
    "==": "=",
    "<>": "<>",
    "!=": "<>",
    "neq": "<>",
    "<": "<",
    "lt": "<",
    "<=": "<=",
    "lte": "<=",
    ">": ">",
    "gt": ">",
    ">=": ">=",
    "gte": ">=",
    "and": "and",
    "or": "or",
    "not": "not",
    "like": "like",
    "notlike": "notLike",
    "between": "between",
    "notbetween": "notBetween",
    "in": "in",
    "notin": "notIn",
    "isnull": "isNull",
    "isnotnull": "isNotNull",
    "+": "+",
    "add": "+",
    "-": "-",
    "sub": "-",
    "*": "*",
    "mul": "*",
    "/": "/",
    "div": "/",
    "%": "%",
    "mod": "%",
    "t_after": "t_after",
    "after": "t_after",
    "t_before": "t_before",
    "before": "t_before",
    "t_during": "t_during",
    "during": "t_during",
    "t_intersects": "t_intersects",
    "t_anyinteracts": "t_intersects",
    "anyinteracts": "t_intersects",
    "s_intersects": "s_intersects",
    "s_contains": "s_contains",
    "s_within": "s_within",
    "s_disjoint": "s_disjoint",
    "s_equals": "s_equals",
    "s_touches": "s_touches",
    "s_crosses": "s_crosses",
    "s_overlaps": "s_overlaps",
    "a_contains": "a_contains",
    "a_equals": "a_equals",
    "a_overlaps": "a_overlaps",
    "a_containedby": "a_containedBy",
    BBOX_INTERSECTS: BBOX_INTERSECTS,
}

_LOGICAL = frozenset({"and", "or", "not"})
_COMPARISON = frozenset({"=", "<>", "<", "<=", ">", ">="})
_LIKE = frozenset({"like", "notLike"})
_MEMBERSHIP = frozenset({"in", "notIn"})
_ARITHMETIC = frozenset({"+", "-", "*", "/", "%"})
_TEMPORAL = frozenset({"t_after", "t_before", "t_during", "t_intersects"})
_NULL = frozenset({"isNull", "isNotNull"})
_SPATIAL = frozenset(
    {
        "s_intersects",
        "s_contains",
        "s_within",
        "s_disjoint",
        "s_equals",
        "s_touches",
        "s_crosses",
        "s_overlaps",
    }
)
_ARRAY = frozenset({"a_contains", "a_equals", "a_overlaps", "a_containedBy"})

#: Operators the local evaluator understands.
LOCAL_OPERATORS = (
    _LOGICAL | _COMPARISON | _LIKE | _MEMBERSHIP | _ARITHMETIC | _TEMPORAL | _NULL
    | frozenset({"between", "notBetween", BBOX_INTERSECTS})
)

_ARITY: dict[str, int | tuple[int, ...]] = {
    "and": (1,),
    "or": (1,),
    "not": 1,
    "=": 2,
    "<>": 2,
    "<": 2,
    "<=": 2,
    ">": 2,
    ">=": 2,
    "like": 2,
    "notLike": 2,
    "between": 3,
    "notBetween": 3,
    "in": 2,
    "notIn": 2,
    "isNull": 1,
    "isNotNull": 1,
    "+": 2,
    "-": 2,
    "*": 2,
    "/": 2,
    "%": 2,
    "t_after": 2,
    "t_before": 2,
    "t_during": 2,
    "t_intersects": 2,
    BBOX_INTERSECTS: 2,
}


def canonical_operator(operator: str) -> str:
    """Return the canonical spelling of a CQL2 operator.

    Args:
        operator: an operator as it appears in a CQL2 expression.

    Returns:
        The canonical (case-insensitive) operator name.

    Raises:
        UnsupportedCql2Function: if the operator is unknown.
    """
    canonical = _try_canonical(operator)
    if canonical is None:
        raise UnsupportedCql2Function(f"Unsupported CQL2 operator: {operator!r}")
    return canonical


def _try_canonical(operator: str) -> str | None:
    """Return the canonical operator name, or ``None`` if unknown."""
    return _OPERATOR_ALIASES.get(operator.lower())


class _Missing:
    """Sentinel for a property that is not present on an Item."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        """Return a readable representation."""
        return "MISSING"


MISSING = _Missing()


# ---------------------------------------------------------------------------
# JSON expression helpers
# ---------------------------------------------------------------------------


def _is_typed_literal(expr: Any) -> bool:
    return isinstance(expr, Mapping) and len(expr) == 1 and next(iter(expr)) in {
        "timestamp",
        "date",
        "interval",
    }


def _parse_interval(value: Any) -> tuple[datetime | None, datetime | None]:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        start, end = value
    else:
        raise InvalidCql2Expression(f"Invalid CQL2 interval: {value!r}")

    def _one(item: Any) -> datetime | None:
        if item in ("..", None):
            return None
        return _to_datetime(item)

    return _one(start), _one(end)


def _to_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            return rfc3339_str_to_datetime(value)
        except ValueError as e:
            raise InvalidCql2Expression(f"Invalid CQL2 timestamp: {value!r}") from e
    raise InvalidCql2Expression(f"Invalid CQL2 timestamp: {value!r}")


def _literal_value(expr: Any) -> Any:
    """Resolve a CQL2 literal node to a Python value.

    Property references are resolved by the evaluator, which is why this only
    handles literals.  Timestamps and dates become ``datetime`` objects and
    intervals become ``(start, end)`` tuples.
    """
    if _is_typed_literal(expr):
        key, value = next(iter(expr.items()))
        if key == "timestamp":
            if value == "..":
                return None
            return _to_datetime(value)
        if key == "date":
            return _to_datetime(f"{value}T00:00:00Z")
        if key == "interval":
            return _parse_interval(value)

    if isinstance(expr, list):
        return [_literal_value(item) for item in expr]

    return expr


_ITEM_LEVEL_PROPERTIES = frozenset(
    {
        "id",
        "collection",
        "geometry",
        "bbox",
        "type",
        "assets",
        "links",
        "stac_version",
        "stac_extensions",
    }
)


def _resolve_property(item: Mapping[str, Any], name: str) -> Any:
    """Resolve a CQL2 property name against a STAC Item."""
    properties = item.get("properties") or {}

    if name.startswith("properties."):
        return _dig(properties, name[len("properties.") :])

    if name in _ITEM_LEVEL_PROPERTIES:
        return item.get(name, MISSING)

    value = _dig(properties, name)
    if value is not MISSING:
        return value

    # Fall back to a top-level field, so custom Item-like documents work too.
    return item.get(name, MISSING)


def _dig(container: Any, path: str) -> Any:
    current: Any = container
    for part in path.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        else:
            return MISSING
    return current


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _coerce_datetimes(left: Any, right: Any) -> tuple[Any, Any]:
    """Coerce one side to a datetime when the other side is one.

    STAC Items store timestamps as RFC 3339 strings, while CQL2 literals written
    with ``timestamp`` are parsed into ``datetime`` objects.  Comparing the two
    only makes sense after normalisation, so do it here.
    """
    if isinstance(left, datetime) and isinstance(right, str):
        try:
            return left, rfc3339_str_to_datetime(right)
        except ValueError:
            return left, right
    if isinstance(right, datetime) and isinstance(left, str):
        try:
            return rfc3339_str_to_datetime(left), right
        except ValueError:
            return left, right
    return left, right


def _compare(left: Any, right: Any, operator: str) -> bool:
    if left is MISSING or right is MISSING:
        return False
    if isinstance(left, tuple) or isinstance(right, tuple):
        return False
    left, right = _coerce_datetimes(left, right)
    try:
        if operator == "=":
            return bool(left == right)
        if operator == "<>":
            return bool(left != right)
        if operator == "<":
            return bool(left < right)
        if operator == "<=":
            return bool(left <= right)
        if operator == ">":
            return bool(left > right)
        if operator == ">=":
            return bool(left >= right)
    except TypeError:
        # Incomparable types never match rather than raising a 500.
        return False
    raise UnsupportedCql2Function(f"Unsupported CQL2 operator: {operator!r}")


def _like(value: Any, pattern: Any) -> bool:
    if not isinstance(value, str) or not isinstance(pattern, str):
        return False
    regex = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\" and index + 1 < len(pattern):
            regex.append(re.escape(pattern[index + 1]))
            index += 2
            continue
        if char == "%":
            regex.append(".*")
        elif char == "_":
            regex.append(".")
        else:
            regex.append(re.escape(char))
        index += 1
    return re.fullmatch("".join(regex), value, re.DOTALL) is not None


def _interval_of(value: Any) -> tuple[datetime | None, datetime | None] | None:
    if isinstance(value, tuple) and len(value) == 2:
        return value
    if isinstance(value, str):
        try:
            value = rfc3339_str_to_datetime(value)
        except ValueError:
            return None
    if isinstance(value, datetime):
        return (value, value)
    if isinstance(value, Mapping):
        # A STAC Item's temporal extent: datetime, or start/end datetime.
        single = value.get("datetime")
        if isinstance(single, str):
            try:
                single = rfc3339_str_to_datetime(single)
            except ValueError:
                return None
        if isinstance(single, datetime):
            return (single, single)
        start = value.get("start_datetime")
        end = value.get("end_datetime")
        try:
            return (
                rfc3339_str_to_datetime(start) if isinstance(start, str) else None,
                rfc3339_str_to_datetime(end) if isinstance(end, str) else None,
            )
        except ValueError:
            return None
    return None


def _temporal(left: Any, right: Any, operator: str) -> bool:
    a = _interval_of(left)
    b = _interval_of(right)
    if a is None or b is None:
        return False
    a_start, a_end = a
    b_start, b_end = b
    # ``None`` bounds are open: treat them as +/- infinity on either side.
    if a_start is None and a_end is None:
        return False
    if b_start is None and b_end is None:
        return False

    if operator == "t_after":
        # The whole of ``a`` starts after ``b`` ends.
        return b_end is not None and a_start is not None and a_start > b_end
    if operator == "t_before":
        return b_start is not None and a_end is not None and a_end < b_start
    if operator == "t_during":
        # ``a`` lies within ``b``.
        starts_ok = b_start is None or (a_start is not None and a_start >= b_start)
        ends_ok = b_end is None or (a_end is not None and a_end <= b_end)
        return starts_ok and ends_ok
    if operator == "t_intersects":
        # Intervals overlap when neither ends strictly before the other starts.
        no_gap_left = b_end is None or a_start is None or a_start <= b_end
        no_gap_right = a_end is None or b_start is None or b_start <= a_end
        return no_gap_left and no_gap_right
    raise UnsupportedCql2Function(f"Unsupported temporal operator: {operator!r}")


def _item_bbox(item: Mapping[str, Any]) -> list[float] | None:
    bbox = item.get("bbox")
    if isinstance(bbox, (list, tuple)) and len(bbox) in (4, 6):
        values = [float(v) for v in bbox]
        if len(values) == 6:
            # 3D bbox: [w, s, minz, e, n, maxz]
            return [values[0], values[1], values[3], values[4]]
        return values
    geometry = item.get("geometry")
    if isinstance(geometry, Mapping):
        return _geometry_bbox(geometry)
    return None


def _geometry_bbox(geometry: Mapping[str, Any]) -> list[float] | None:
    coords: list[tuple[float, float]] = []

    def collect(value: Any) -> None:
        if (
            isinstance(value, (list, tuple))
            and len(value) >= 2
            and all(isinstance(v, (int, float)) for v in value[:2])
        ):
            coords.append((float(value[0]), float(value[1])))
            return
        if isinstance(value, (list, tuple)):
            for entry in value:
                collect(entry)

    collect(geometry.get("coordinates"))
    if not coords:
        return None
    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    return [min(xs), min(ys), max(xs), max(ys)]


def _bbox_intersects(item: Mapping[str, Any], bbox: Any) -> bool:
    if not isinstance(bbox, (list, tuple)):
        return False
    if len(bbox) == 6:
        query = [float(bbox[0]), float(bbox[1]), float(bbox[3]), float(bbox[4])]
    elif len(bbox) == 4:
        query = [float(v) for v in bbox]
    else:
        return False
    item_bbox = _item_bbox(item)
    if item_bbox is None:
        return False
    return not (
        item_bbox[2] < query[0]
        or item_bbox[0] > query[2]
        or item_bbox[3] < query[1]
        or item_bbox[1] > query[3]
    )


def evaluate(expr: Any, item: Mapping[str, Any]) -> bool:
    """Evaluate a CQL2-JSON expression against a STAC Item.

    Args:
        expr: the CQL2 expression, in ``cql2-json`` form.
        item: the Item to test.

    Returns:
        ``True`` when the Item satisfies the expression.

    Raises:
        UnsupportedCql2Function: if the expression uses an operator that
            cannot be evaluated locally.
        InvalidCql2Expression: if the expression is malformed.
    """
    if not isinstance(expr, Mapping) or "op" not in expr:
        # A bare literal (or a bare boolean property) used as a predicate.
        if isinstance(expr, Mapping) and "property" in expr:
            value = _resolve_property(item, str(expr["property"]))
        else:
            value = _literal_value(expr)
        return value is not MISSING and bool(value)

    operator = canonical_operator(str(expr["op"]))
    args = expr.get("args")
    if not isinstance(args, list):
        raise InvalidCql2Expression(f"Missing CQL2 arguments for {operator!r}")

    if operator == "and":
        return all(evaluate(arg, item) for arg in args)
    if operator == "or":
        return any(evaluate(arg, item) for arg in args)
    if operator == "not":
        _check_arity(operator, args)
        return not evaluate(args[0], item)

    if operator in _COMPARISON:
        _check_arity(operator, args)
        return _compare(_value(args[0], item), _value(args[1], item), operator)

    if operator in _LIKE:
        _check_arity(operator, args)
        value = _value(args[0], item)
        if value is MISSING:
            # A predicate over an absent property never matches, even negated.
            return False
        result = _like(value, _value(args[1], item))
        return not result if operator == "notLike" else result

    if operator in _MEMBERSHIP:
        _check_arity(operator, args)
        needle = _value(args[0], item)
        if needle is MISSING:
            return False
        haystack = args[1]
        if not isinstance(haystack, list):
            raise InvalidCql2Expression(f"{operator!r} expects an array literal")
        values = [_value(entry, item) for entry in haystack]
        result = any(needle == value for value in values)
        return not result if operator == "notIn" else result

    if operator in ("between", "notBetween"):
        _check_arity(operator, args)
        value = _value(args[0], item)
        lower = _value(args[1], item)
        upper = _value(args[2], item)
        if value is MISSING or lower is MISSING or upper is MISSING:
            return False
        try:
            result = lower <= value <= upper
        except TypeError:
            result = False
        return not result if operator == "notBetween" else result

    if operator in _NULL:
        _check_arity(operator, args)
        value = _value(args[0], item)
        is_null = value is MISSING or value is None
        return is_null if operator == "isNull" else not is_null

    if operator in _ARITHMETIC:
        _check_arity(operator, args)
        return bool(_arithmetic(operator, _value(args[0], item), _value(args[1], item)))

    if operator in _TEMPORAL:
        _check_arity(operator, args)
        return _temporal(_value(args[0], item), _value(args[1], item), operator)

    if operator == BBOX_INTERSECTS:
        _check_arity(operator, args)
        return _bbox_intersects(item, args[1])

    raise UnsupportedCql2Function(
        f"Unsupported CQL2 operator for local evaluation: {operator!r}"
    )


def _value(expr: Any, item: Mapping[str, Any]) -> Any:
    if isinstance(expr, Mapping):
        if "property" in expr:
            return _resolve_property(item, str(expr["property"]))
        if "op" in expr:
            operator = canonical_operator(str(expr["op"]))
            args = expr.get("args")
            if not isinstance(args, list):
                raise InvalidCql2Expression(f"Missing CQL2 arguments for {operator!r}")
            if operator in _ARITHMETIC:
                _check_arity(operator, args)
                return _arithmetic(
                    operator, _value(args[0], item), _value(args[1], item)
                )
            raise UnsupportedCql2Function(
                f"Operator {operator!r} cannot be used as a value"
            )
        return _literal_value(expr)
    return _literal_value(expr)


def _arithmetic(operator: str, left: Any, right: Any) -> Any:
    if left is MISSING or right is MISSING or left is None or right is None:
        return MISSING
    try:
        if operator == "+":
            return left + right
        if operator == "-":
            return left - right
        if operator == "*":
            return left * right
        if operator == "/":
            return left / right
        if operator == "%":
            return left % right
    except (TypeError, ZeroDivisionError):
        return MISSING
    raise UnsupportedCql2Function(f"Unsupported arithmetic operator: {operator!r}")


def _check_arity(operator: str, args: list[Any]) -> None:
    expected = _ARITY.get(operator)
    if expected is None:
        return
    if isinstance(expected, tuple):
        if len(args) < expected[0]:
            raise InvalidCql2Expression(
                f"CQL2 operator {operator!r} expects at least {expected[0]} arguments"
            )
        return
    if len(args) != expected:
        raise InvalidCql2Expression(
            f"CQL2 operator {operator!r} expects {expected} arguments, got {len(args)}"
        )


# ---------------------------------------------------------------------------
# Splitting a filter for partial pushdown
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PushdownCapabilities:
    """Describes which CQL2 fragments a backend can execute itself.

    A backend that can evaluate a subset of CQL2 (for example only comparisons on
    indexed properties) advertises that subset here.  :func:`split_filter` then
    keeps exactly the fragments covered by the capabilities for the backend,
    and leaves the rest for local evaluation.

    Attributes:
        operators: canonical operator names the backend can execute.  The
            ``and``, ``or`` and ``not`` names may be listed but are never
            needed: the splitter combines pushable children on its own.
        properties: property names the backend can filter on.  ``None`` means
            every property is supported.  The special name ``"*"`` also means
            every property.
        functions: positional function names (such as ``lower``) the backend
            can execute.
    """

    operators: frozenset[str] = frozenset()
    properties: frozenset[str] | None = None
    functions: frozenset[str] = frozenset()

    def supports_property(self, name: str) -> bool:
        """Return whether the backend can filter on ``name``."""
        if self.properties is None or "*" in self.properties:
            return True
        return name in self.properties

    def supports_operator(self, operator: str) -> bool:
        """Return whether the backend can execute ``operator``."""
        return operator in self.operators


@dataclass(frozen=True)
class SplitResult:
    """The result of splitting a CQL2 filter for partial pushdown.

    Attributes:
        pushdown: the fragment that the backend can execute, or ``None`` when
            nothing can be pushed down.  This fragment is a *necessary*
            condition: every matching Item satisfies it.
        residual: the fragment that must be evaluated locally on the candidates
            returned by the backend, or ``None`` when the pushdown is complete.
        is_complete: ``True`` when ``residual`` is ``None``, i.e. the backend can
            answer the whole filter and no local evaluation is needed.
    """

    pushdown: Any | None
    residual: Any | None
    is_complete: bool

    @property
    def matches_all(self) -> bool:
        """Return whether the backend has to return every candidate."""
        return self.pushdown is None


def _conjunction(parts: list[Any]) -> Any | None:
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return {"op": "and", "args": parts}


def _disjunction(parts: list[Any]) -> Any | None:
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return {"op": "or", "args": parts}


def _split(
    expr: Any, capabilities: PushdownCapabilities
) -> tuple[Any | None, Any | None]:
    """Return ``(pushdown, residual)`` for ``expr``.

    ``pushdown`` is a necessary condition that the backend can execute.
    ``residual`` is the remainder, or ``None`` if the pushdown is exact.
    """
    if not isinstance(expr, Mapping) or "op" not in expr:
        # Literals and bare property references are decided locally.
        return None, copy.deepcopy(expr)

    operator = _try_canonical(str(expr["op"]))
    args = expr.get("args")
    if not isinstance(args, list):
        raise InvalidCql2Expression(f"Missing CQL2 arguments for {expr['op']!r}")

    if operator == "and":
        if not args:
            # An empty conjunction matches everything.
            return None, None
        push_part: list[Any] = []
        residual_part: list[Any] = []
        for arg in args:
            push, residual = _split(arg, capabilities)
            if push is not None:
                push_part.append(push)
            if residual is not None:
                residual_part.append(residual)
        return _conjunction(push_part), _conjunction(residual_part)

    if operator == "or":
        if not args:
            # An empty disjunction matches nothing; it has to be evaluated.
            return None, copy.deepcopy(expr)
        parts = [_split(arg, capabilities) for arg in args]
        if all(residual is None for _, residual in parts):
            return _disjunction([push for push, _ in parts]), None
        # If any branch cannot be pushed as a whole, the OR cannot be used as a
        # necessary condition: a matching item may satisfy only that branch.
        # Pushing a subset of the branches would drop results.
        return None, copy.deepcopy(expr)

    if operator == "not":
        _check_arity(operator, args)
        push, residual = _split(args[0], capabilities)
        if residual is None and push is not None:
            return {"op": "not", "args": [push]}, None
        # NOT of anything less than a wholly pushable predicate stays local.
        return None, copy.deepcopy(expr)

    if _pushable(operator, expr, capabilities):
        return copy.deepcopy(expr), None

    return None, copy.deepcopy(expr)


def _pushable(
    operator: str | None,
    expr: Mapping[str, Any],
    capabilities: PushdownCapabilities,
) -> bool:
    """Whether ``expr`` can be executed *in full* by the backend.

    Every operator and function anywhere inside ``expr`` -- including nested
    operands such as ``lower(name)`` in ``lower(name) = 'x'`` -- must be
    advertised, and so must every referenced property.
    """
    if not _operators_pushable(expr, capabilities):
        return False
    return all(
        capabilities.supports_property(name) for name in referenced_properties(expr)
    )


def _operators_pushable(expr: Any, capabilities: PushdownCapabilities) -> bool:
    if isinstance(expr, Mapping):
        if "op" in expr:
            operator = _try_canonical(str(expr["op"]))
            if operator is None:
                # An unknown operator is a namespace function; the backend can
                # only take it if it advertises the function by name.
                if str(expr["op"]) not in capabilities.functions:
                    return False
            elif not capabilities.supports_operator(operator):
                return False
        return all(
            _operators_pushable(value, capabilities) for value in expr.values()
        )
    if isinstance(expr, (list, tuple)):
        return all(_operators_pushable(value, capabilities) for value in expr)
    return True


def split_filter(
    expr: Any | None,
    capabilities: PushdownCapabilities,
) -> SplitResult:
    """Split a CQL2 filter into a pushdown fragment and a residual fragment.

    The pushdown fragment is guaranteed to be a *necessary* condition of the
    original filter: every Item matching ``expr`` also matches the pushdown. It
    therefore can be handed to a backend without losing results; the residual
    must then be evaluated on the candidates with :func:`evaluate`.

    ``and`` and ``or`` are handled structurally so that no branch is dropped:

    * ``AND`` pushes down every child that can be pushed and keeps the others
      as residual conditions (both are required to match).
    * ``OR`` is only pushed down as a whole.  If any branch cannot be pushed,
      the whole ``OR`` stays in the residual; a partially pushed ``OR`` would
      restrict results it must not restrict.
    * ``NOT`` is only pushed when its operand can be pushed as a whole.

    Args:
        expr: a CQL2-JSON expression, or ``None`` for no filtering.
        capabilities: what the backend is able to execute.

    Returns:
        A :class:`SplitResult`.

    Raises:
        UnsupportedCql2Function: if the residual uses an operator that cannot be
            evaluated locally.  Rejecting up front prevents a filter from
            silently returning wrong results.
        InvalidCql2Expression: if the expression is malformed.
    """
    if expr is None:
        return SplitResult(pushdown=None, residual=None, is_complete=True)

    pushdown, residual = _split(expr, capabilities)

    if pushdown is not None:
        validate_expression(pushdown, local=False)
    if residual is not None:
        validate_expression(residual, local=True)

    return SplitResult(
        pushdown=pushdown,
        residual=residual,
        is_complete=residual is None,
    )


def is_locally_evaluable(expr: Any) -> bool:
    """Return whether :func:`evaluate` can decide ``expr``."""
    try:
        validate_expression(expr, local=True)
    except Cql2Error:
        return False
    return True


def validate_expression(expr: Any, local: bool = True) -> None:
    """Validate a CQL2-JSON expression.

    Args:
        expr: the expression to validate.
        local: when ``True`` (the default), reject operators that
            :func:`evaluate` cannot handle.  When ``False``, only validate
            structure, so expressions destined for a backend may use any
            operator the backend understands.

    Raises:
        UnsupportedCql2Function: if ``local`` and an operator cannot be
            evaluated locally.
        InvalidCql2Expression: if the expression is malformed.
    """
    if not isinstance(expr, Mapping) or "op" not in expr:
        return

    operator = _try_canonical(str(expr["op"]))
    args = expr.get("args")
    if not isinstance(args, list):
        raise InvalidCql2Expression(f"Missing CQL2 arguments for {expr['op']!r}")

    if operator is None:
        # A namespace function such as ``lower`` or a ``custom:op``.  It may be
        # valid for the backend; it can never be evaluated locally.
        if local:
            raise UnsupportedCql2Function(
                f"Unsupported CQL2 operator: {expr['op']!r}"
            )
        _check_arity(str(expr["op"]), args)
        for arg in args:
            _validate_value(arg, local=local)
        return

    _check_arity(operator, args)

    if operator in _SPATIAL or operator in _ARRAY:
        if local:
            raise UnsupportedCql2Function(
                f"CQL2 operator {operator!r} cannot be evaluated locally"
            )
        return

    if operator in _LOGICAL:
        for arg in args:
            validate_expression(arg, local=local)
        return

    if operator in _ARITHMETIC | _COMPARISON | _LIKE | _MEMBERSHIP | _TEMPORAL | _NULL | {
        "between",
        "notBetween",
        BBOX_INTERSECTS,
    }:
        for arg in args:
            _validate_value(arg, local)
        return

    raise UnsupportedCql2Function(f"Unsupported CQL2 operator: {operator!r}")


def _validate_value(expr: Any, local: bool) -> None:
    if isinstance(expr, Mapping) and "op" in expr:
        validate_expression(expr, local=local)


# ---------------------------------------------------------------------------
# Request selectors
# ---------------------------------------------------------------------------


def selectors_to_filter(
    collections: list[str] | None = None,
    ids: list[str] | None = None,
    bbox: Any | None = None,
    datetime: str | None = None,
) -> dict[str, Any] | None:
    """Translate STAC request selectors into a local CQL2-JSON filter.

    Backends commonly accept ``collections``, ``ids``, ``bbox`` and
    ``datetime`` even when they cannot execute CQL2.  When such selectors are
    only used to *narrow* candidates (rather than applied exactly), they have
    to be re-checked locally; this helper expresses them as CQL2.

    Args:
        collections: collection ids; an Item matches when its collection is in
            the list.
        ids: item ids; an Item matches when its id is in the list.
        bbox: a 2D or 3D bounding box.  Only the horizontal extent is used for
            local evaluation.
        datetime: an RFC 3339 instant or interval, with ``..`` for open bounds.

    Returns:
        A CQL2-JSON expression, or ``None`` when no selector narrows the search.

    Raises:
        InvalidCql2Expression: if a datetime interval is malformed.
    """
    parts: list[dict[str, Any]] = []

    if collections:
        parts.append(
            {"op": "in", "args": [{"property": "collection"}, list(collections)]}
        )
    if ids:
        parts.append({"op": "in", "args": [{"property": "id"}, list(ids)]})
    if bbox is not None:
        parts.append(
            {
                "op": BBOX_INTERSECTS,
                "args": [{"property": "geometry"}, [float(v) for v in bbox]],
            }
        )
    if datetime:
        parts.append(_datetime_filter(datetime))

    return conjoin(*parts)


def _datetime_filter(value: str) -> dict[str, Any]:
    from fastapi import HTTPException

    try:
        interval = str_to_interval(value)
    except HTTPException as e:
        raise InvalidCql2Expression(str(e.detail)) from e

    if interval is None:
        raise InvalidCql2Expression(f"Invalid datetime: {value!r}")

    if not isinstance(interval, tuple):
        # A single instant intersects an Item whose extent contains it.
        return {
            "op": "t_intersects",
            "args": [
                {"property": "properties"},
                {"interval": [_to_rfc3339(interval), _to_rfc3339(interval)]},
            ],
        }

    start, end = interval
    return {
        "op": "t_intersects",
        "args": [
            {"property": "properties"},
            {
                "interval": [
                    _to_rfc3339(start) if start is not None else "..",
                    _to_rfc3339(end) if end is not None else "..",
                ]
            },
        ],
    }


def _to_rfc3339(value: datetime) -> str:
    return datetime_to_str(value)


def conjoin(*exprs: Any | None) -> dict[str, Any] | None:
    """Combine expressions with ``and``, dropping ``None`` terms.

    Args:
        *exprs: expressions to combine.

    Returns:
        ``None`` when all arguments are ``None``, the single expression when
        only one remains, and an ``and`` expression otherwise.
    """
    parts = [expr for expr in exprs if expr is not None]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return {"op": "and", "args": parts}


def referenced_properties(expr: Any) -> set[str]:
    """Return the set of property names referenced by a CQL2-JSON expression.

    Args:
        expr: a CQL2-JSON expression.

    Returns:
        Every property name used by the expression, including inside function
        arguments.
    """
    found: set[str] = set()
    _collect_properties(expr, found)
    return found


def _collect_properties(expr: Any, found: set[str]) -> None:
    if isinstance(expr, Mapping):
        if "property" in expr and len(expr) == 1:
            found.add(str(expr["property"]))
            return
        for value in expr.values():
            _collect_properties(value, found)
    elif isinstance(expr, (list, tuple)):
        for value in expr:
            _collect_properties(value, found)


def parse_cql2(value: Any, filter_lang: str | None = None) -> Any | None:
    """Parse a filter value into its ``cql2-json`` form.

    Args:
        value: the raw filter value: a mapping for ``cql2-json``, a string for
            ``cql2-text`` or for a JSON-encoded ``cql2-json`` string.
        filter_lang: one of ``"cql2-json"`` or ``"cql2-text"``.  When ``None`` a
            string that parses as JSON is treated as JSON, otherwise as text.

    Returns:
        The expression as ``cql2-json``, or ``None`` when ``value`` is empty.

    Raises:
        InvalidCql2Expression: if the value cannot be parsed.
        UnsupportedCql2Function: if the text uses an unsupported construct.
    """
    if value is None:
        return None
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    if not isinstance(value, str):
        raise InvalidCql2Expression(f"Invalid CQL2 filter: {value!r}")

    if filter_lang == "cql2-text":
        return parse_cql2_text(value)

    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        if filter_lang == "cql2-json":
            raise InvalidCql2Expression("Invalid JSON in cql2-json filter")
        return parse_cql2_text(value)

    if not isinstance(parsed, Mapping):
        raise InvalidCql2Expression("A cql2-json filter must be a JSON object")
    return parsed


# ---------------------------------------------------------------------------
# cql2-text parser
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"""
    (?P<space>\s+)
  | (?P<string>'(?:[^']|'')*')
  | (?P<quoted>(?:"(?:[^"]|"")*")|(?:`(?:[^`]|``)*`))
  | (?P<number>[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?)
  | (?P<op><=|>=|<>|!=|==|=|<|>|\+|-|\*|/|%|\(|\)|,)
  | (?P<word>[A-Za-z_][A-Za-z0-9_:\-]*)
    """,
    re.VERBOSE,
)

_KEYWORD_OPERATORS = {
    "and": "and",
    "or": "or",
    "not": "not",
    "like": "like",
    "between": "between",
    "in": "in",
    "is": "is",
    "null": "null",
}

_GEOMETRY_CONSTRUCTORS = frozenset(
    {
        "point",
        "linestring",
        "polygon",
        "multipoint",
        "multilinestring",
        "multipolygon",
        "geometrycollection",
        "bbox",
    }
)


@dataclass
class _Token:
    kind: str
    value: str


def _tokenize(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    position = 0
    while position < len(text):
        match = _TOKEN_RE.match(text, position)
        if match is None:
            raise InvalidCql2Expression(
                f"Unexpected character in cql2-text at position {position}: "
                f"{text[position:position + 20]!r}"
            )
        position = match.end()
        kind = match.lastgroup
        value = match.group()
        if kind == "space":
            continue
        tokens.append(_Token(kind, value))
    tokens.append(_Token("eof", ""))
    return tokens


def _unquote(value: str) -> str:
    quote = value[0]
    body = value[1:-1]
    return body.replace(quote * 2, quote)


class _TextParser:
    """A recursive-descent parser for the supported subset of cql2-text."""

    def __init__(self, tokens: list[_Token]) -> None:
        """Store the token stream to parse."""
        self._tokens = tokens
        self._index = 0

    # -- token helpers ---------------------------------------------------

    def _peek(self, offset: int = 0) -> _Token:
        return self._tokens[min(self._index + offset, len(self._tokens) - 1)]

    def _advance(self) -> _Token:
        token = self._tokens[self._index]
        if token.kind != "eof":
            self._index += 1
        return token

    def _matches_word(self, *words: str) -> bool:
        token = self._peek()
        return token.kind == "word" and token.value.lower() in words

    def _matches_op(self, *ops: str) -> bool:
        token = self._peek()
        return token.kind == "op" and token.value in ops

    def _expect_op(self, op: str) -> None:
        if not self._matches_op(op):
            raise InvalidCql2Expression(f"Expected {op!r} in cql2-text")
        self._advance()

    def _expect_word(self, word: str) -> None:
        if not self._matches_word(word):
            raise InvalidCql2Expression(f"Expected {word!r} in cql2-text")
        self._advance()

    # -- grammar ---------------------------------------------------------

    def parse(self) -> Any:
        """Parse the whole stream into a cql2-json expression."""
        expression = self._parse_or()
        if self._peek().kind != "eof":
            raise InvalidCql2Expression(
                f"Unexpected token in cql2-text: {self._peek().value!r}"
            )
        return expression

    def _parse_or(self) -> Any:
        parts = [self._parse_and()]
        while self._matches_word("or"):
            self._advance()
            parts.append(self._parse_and())
        return parts[0] if len(parts) == 1 else {"op": "or", "args": parts}

    def _parse_and(self) -> Any:
        parts = [self._parse_not()]
        while self._matches_word("and"):
            self._advance()
            parts.append(self._parse_not())
        return parts[0] if len(parts) == 1 else {"op": "and", "args": parts}

    def _parse_not(self) -> Any:
        if self._matches_word("not"):
            self._advance()
            return {"op": "not", "args": [self._parse_not()]}
        return self._parse_predicate()

    def _parse_predicate(self) -> Any:
        if self._matches_op("("):
            self._advance()
            expression = self._parse_or()
            self._expect_op(")")
            return expression

        left = self._parse_operand()

        # IS [NOT] NULL
        if self._matches_word("is"):
            self._advance()
            negate = False
            if self._matches_word("not"):
                self._advance()
                negate = True
            self._expect_word("null")
            return {"op": "isNotNull" if negate else "isNull", "args": [left]}

        negate = False
        if self._matches_word("not"):
            self._advance()
            negate = True

        # [NOT] BETWEEN low AND high
        if self._matches_word("between"):
            self._advance()
            low = self._parse_operand()
            self._expect_word("and")
            high = self._parse_operand()
            return {
                "op": "notBetween" if negate else "between",
                "args": [left, low, high],
            }

        # [NOT] LIKE pattern
        if self._matches_word("like"):
            self._advance()
            pattern = self._parse_operand()
            return {
                "op": "notLike" if negate else "like",
                "args": [left, pattern],
            }

        # [NOT] IN (list)
        if self._matches_word("in"):
            self._advance()
            self._expect_op("(")
            values = [self._parse_operand()]
            while self._matches_op(","):
                self._advance()
                values.append(self._parse_operand())
            self._expect_op(")")
            return {
                "op": "notIn" if negate else "in",
                "args": [left, values],
            }

        if negate:
            raise InvalidCql2Expression(
                "NOT must be followed by BETWEEN, LIKE or IN in cql2-text"
            )

        # Comparison operators
        token = self._peek()
        if token.kind == "op" and token.value in {"=", "<>", "!=", "<", "<=", ">", ">="}:
            self._advance()
            operator = "<>" if token.value in {"<>", "!="} else token.value
            right = self._parse_operand()
            return {"op": operator, "args": [left, right]}

        # A bare operand is allowed as a boolean predicate.
        return left

    def _parse_operand(self) -> Any:
        left = self._parse_multiplicative()
        while self._matches_op("+", "-"):
            operator = self._advance().value
            right = self._parse_multiplicative()
            left = {"op": operator, "args": [left, right]}
        return left

    def _parse_multiplicative(self) -> Any:
        left = self._parse_unary()
        while self._matches_op("*", "/", "%"):
            operator = self._advance().value
            right = self._parse_unary()
            left = {"op": operator, "args": [left, right]}
        return left

    def _parse_unary(self) -> Any:
        if self._matches_op("-"):
            self._advance()
            operand = self._parse_unary()
            return {"op": "-", "args": [0, operand]}
        return self._parse_atom()

    def _parse_atom(self) -> Any:
        token = self._peek()
        if token.kind == "op" and token.value == "(":
            self._advance()
            expression = self._parse_operand()
            self._expect_op(")")
            return expression

        if token.kind == "string":
            self._advance()
            return _unquote(token.value)

        if token.kind == "quoted":
            self._advance()
            # Quoted identifiers are always property references.
            return {"property": _unquote(token.value)}

        if token.kind == "number":
            self._advance()
            return float(token.value) if any(c in token.value for c in ".eE") else int(
                token.value
            )

        if token.kind == "word":
            self._advance()
            lowered = token.value.lower()
            if lowered == "true":
                return True
            if lowered == "false":
                return False
            if lowered == "null":
                return None
            if self._matches_op("("):
                if lowered in _GEOMETRY_CONSTRUCTORS:
                    # Geometry literals are not parsed here.  A backend that
                    # supports spatial predicates can still receive them as
                    # cql2-json.
                    raise UnsupportedCql2Function(
                        "Geometry literals in cql2-text are not supported; "
                        "pass the filter as cql2-json instead "
                        f"(found {token.value.upper()})"
                    )
                return self._parse_call(token.value)
            return {"property": token.value}

        raise InvalidCql2Expression(f"Unexpected token in cql2-text: {token.value!r}")

    def _parse_call(self, name: str) -> Any:
        self._expect_op("(")
        args: list[Any] = []
        if not self._matches_op(")"):
            args.append(self._parse_operand())
            while self._matches_op(","):
                self._advance()
                args.append(self._parse_operand())
        self._expect_op(")")

        lowered = name.lower()
        if lowered == "timestamp":
            if len(args) != 1 or not isinstance(args[0], str):
                raise InvalidCql2Expression(
                    "TIMESTAMP() expects a single string literal"
                )
            return {"timestamp": args[0]}
        if lowered == "date":
            if len(args) != 1 or not isinstance(args[0], str):
                raise InvalidCql2Expression("DATE() expects a single string literal")
            return {"date": args[0]}
        if lowered == "interval":
            if len(args) != 2 or not all(isinstance(arg, str) for arg in args):
                raise InvalidCql2Expression(
                    "INTERVAL() expects two string literals"
                )
            return {"interval": list(args)}

        if lowered in _GEOMETRY_CONSTRUCTORS:
            # WKT geometry literals are not parsed here.  A backend that
            # supports spatial predicates can still receive them as cql2-json.
            raise UnsupportedCql2Function(
                f"Geometry literals in cql2-text are not supported; "
                f"pass the filter as cql2-json instead (found {name.upper()})"
            )

        return {"op": name, "args": args}


def parse_cql2_text(text: str) -> Any:
    """Parse a ``cql2-text`` expression into ``cql2-json``.

    Args:
        text: the CQL2 text expression.

    Returns:
        The equivalent ``cql2-json`` expression.

    Raises:
        InvalidCql2Expression: if the text cannot be parsed.
    """
    if not isinstance(text, str):
        raise InvalidCql2Expression("A cql2-text filter must be a string")
    if not text.strip():
        raise InvalidCql2Expression("Empty cql2-text filter")
    return _TextParser(_tokenize(text)).parse()
