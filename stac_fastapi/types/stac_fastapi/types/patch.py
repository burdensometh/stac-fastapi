"""Compute the result of a patch applied to an Item.

A conditional Item update has to compare the Item's version, compute the
patched Item and write it as one step: if the comparison fails the write does
not happen, so the stored Item and the results of later reads are unchanged
(no half-applied patch, no version bump).

This module has the version comparison and the patch computation, and leaves
the write to the caller, which must perform all three under the same lock or
transaction:

```python
item = read_item(collection_id, item_id)      # under the lock
patched = conditional_patch(item, patch, expected_version)
write_item(patched)                           # same lock; raises nothing
```

Neither `apply_patch` nor `conditional_patch` modifies its arguments, so a
failure part way through leaves the input Item as it was.

Both patch formats of the Transaction extension are supported: a mapping is a
JSON Merge Patch (RFC 7386), and a list of operations is a JSON Patch
(RFC 6902).  The operations may be mappings or the extension's
`PatchOperation` models.
"""

import copy
from collections.abc import Mapping, Sequence
from typing import Any

from .errors import PatchError, VersionConflictError
from .etag import item_payload, item_version, version_matches


def apply_patch(item: Any, patch: Any) -> dict[str, Any]:
    """Return the Item which results from applying a patch, without modifying it.

    Args:
        item: the Item to patch, as a mapping or a pydantic model.
        patch: a mapping (a JSON Merge Patch, RFC 7386) or a list of operations
            (a JSON Patch, RFC 6902).

    Returns:
        The patched Item.

    Raises:
        PatchError: if a JSON Patch operation cannot be applied.
    """
    document = copy.deepcopy(item_payload(item))
    if not isinstance(document, dict):
        raise PatchError("A patch can only be applied to an object.")

    if isinstance(patch, Mapping):
        return _merge_patch(document, patch)
    if isinstance(patch, Sequence) and not isinstance(patch, (str, bytes)):
        return _json_patch(document, patch)
    if hasattr(patch, "model_dump"):
        # The extension's `PartialItem` model is a merge patch.
        return _merge_patch(document, item_payload(patch))

    raise PatchError(
        "A patch must be a mapping (merge patch) or a list of operations "
        f"(JSON Patch), not {type(patch)!r}."
    )


def conditional_patch(
    item: Any, patch: Any, expected_version: str | None = None
) -> dict[str, Any]:
    """Apply a patch only if the Item is still the expected version.

    Call this with the Item you read in the same transaction as the write, so
    the comparison, the computation and the write cannot interleave with
    another update.

    Args:
        item: the stored Item, as a mapping or a pydantic model.
        patch: a merge patch (mapping) or a JSON Patch (list of operations).
        expected_version: the version the caller read; None skips the check,
            so a caller which sends no version is served as before.

    Returns:
        The patched Item, ready to write.

    Raises:
        VersionConflictError: if the Item is no longer the expected version.
            The Item is not modified and nothing should be written.
        PatchError: if the patch cannot be applied.
    """
    if expected_version is not None and not version_matches(
        expected_version, item_version(item)
    ):
        raise VersionConflictError(
            "The item has changed since the version in the request; "
            "read it again and resubmit the patch with its current version."
        )
    return apply_patch(item, patch)


def _operation_field(operation: Any, name: str, *, default: Any = None) -> Any:
    """Read a field of a patch operation, which may be a model or a mapping."""
    if isinstance(operation, Mapping):
        return operation.get(name, default)
    # The extension's models use `from_` for the `from` member.
    alias = f"{name}_"
    if hasattr(operation, alias):
        return getattr(operation, alias)
    return getattr(operation, name, default)


def _operation(operation: Any) -> tuple[str, list[str] | None, list[str], Any]:
    """Return the (op, path, from, value) parts of a patch operation."""
    op = _operation_field(operation, "op")
    if not isinstance(op, str):
        raise PatchError(f"Invalid patch operation: {operation!r}")
    path = _operation_field(operation, "path")
    if not isinstance(path, str):
        raise PatchError(f"Patch operation has no path: {operation!r}")
    from_path = _operation_field(operation, "from")
    value = _operation_field(operation, "value", default=None)
    return (
        op,
        _pointer(path),
        _pointer(from_path) if isinstance(from_path, str) else [],
        value,
    )


def _pointer(pointer: str) -> list[str]:
    """Split a JSON Pointer (RFC 6901) into reference tokens.

    Args:
        pointer: a JSON Pointer, e.g. ``/properties/foo``.

    Returns:
        The unescaped reference tokens.
    """
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise PatchError(f"Invalid JSON Pointer: {pointer!r}")
    return [
        token.replace("~1", "/").replace("~0", "~")
        for token in pointer[1:].split("/")
    ]


def _merge_patch(target: Any, patch: Any) -> Any:
    """Apply a JSON Merge Patch (RFC 7386) to a copy of ``target``."""
    if not isinstance(patch, Mapping):
        return copy.deepcopy(patch)
    if not isinstance(target, Mapping):
        target = {}
    result = dict(target)
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, Mapping):
            result[key] = _merge_patch(result.get(key), value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _json_patch(document: dict[str, Any], operations: Sequence[Any]) -> dict[str, Any]:
    """Apply a JSON Patch (RFC 6902) to ``document`` and return it."""
    for operation in operations:
        op, path, from_path, value = _operation(operation)
        if op == "add":
            _add(document, path, copy.deepcopy(value))
        elif op == "remove":
            _remove(document, path)
        elif op == "replace":
            _replace(document, path, copy.deepcopy(value))
        elif op == "move":
            if not from_path:
                raise PatchError("A move operation needs a 'from' member.")
            moved = _remove(document, from_path)
            _add(document, path, moved)
        elif op == "copy":
            if not from_path:
                raise PatchError("A copy operation needs a 'from' member.")
            _add(document, path, copy.deepcopy(_get(document, from_path)))
        elif op == "test":
            if _get(document, path) != value:
                raise PatchError(
                    f"The test operation at {_format_pointer(path)} failed."
                )
        else:
            raise PatchError(f"Unsupported patch operation: {op!r}")
    return document


def _format_pointer(tokens: list[str]) -> str:
    if not tokens:
        return ""
    return "/" + "/".join(t.replace("~", "~0").replace("/", "~1") for t in tokens)


def _get(document: Any, tokens: list[str]) -> Any:
    """Return the value a JSON Pointer refers to."""
    current = document
    for token in tokens:
        if isinstance(current, Mapping):
            if token not in current:
                raise PatchError(f"No such member: {_format_pointer(tokens)}")
            current = current[token]
        elif isinstance(current, list):
            current = current[_index(token, current)]
        else:
            raise PatchError(f"Cannot traverse {_format_pointer(tokens)}")
    return current


def _parent(document: Any, tokens: list[str]) -> Any:
    """Return the container which the last token of a JSON Pointer refers into."""
    if not tokens:
        raise PatchError("A patch operation needs a non-empty path.")
    return _get(document, tokens[:-1])


def _index(token: str, sequence: list, *, allow_end: bool = False) -> int:
    """Parse a JSON Pointer array index."""
    if allow_end and token == "-":
        return len(sequence)
    try:
        index = int(token)
    except ValueError:
        raise PatchError(f"Invalid array index: {token!r}")
    limit = len(sequence) if allow_end else len(sequence) - 1
    if index < 0 or index > limit:
        raise PatchError(f"Array index out of range: {token!r}")
    return index


def _add(document: Any, tokens: list[str], value: Any) -> None:
    """Add or replace the value at a JSON Pointer."""
    if not tokens:
        raise PatchError("Cannot add the whole document in place.")
    parent = _parent(document, tokens)
    token = tokens[-1]
    if isinstance(parent, Mapping):
        parent[token] = value
    elif isinstance(parent, list):
        parent.insert(_index(token, parent, allow_end=True), value)
    else:
        raise PatchError(f"Cannot add to {_format_pointer(tokens[:-1])}")


def _remove(document: Any, tokens: list[str]) -> Any:
    """Remove and return the value at a JSON Pointer."""
    parent = _parent(document, tokens)
    token = tokens[-1]
    if isinstance(parent, Mapping):
        if token not in parent:
            raise PatchError(f"No such member: {_format_pointer(tokens)}")
        return parent.pop(token)
    if isinstance(parent, list):
        return parent.pop(_index(token, parent))
    raise PatchError(f"Cannot remove from {_format_pointer(tokens[:-1])}")


def _replace(document: Any, tokens: list[str], value: Any) -> None:
    """Replace the value at a JSON Pointer, which must exist."""
    parent = _parent(document, tokens)
    token = tokens[-1]
    if isinstance(parent, Mapping):
        if token not in parent:
            raise PatchError(f"No such member: {_format_pointer(tokens)}")
        parent[token] = value
    elif isinstance(parent, list):
        parent[_index(token, parent)] = value
    else:
        raise PatchError(f"Cannot replace in {_format_pointer(tokens[:-1])}")
