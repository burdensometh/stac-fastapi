"""Helpers to read a collection snapshot from a directory.

A snapshot is a directory of ``.json`` files. Each file may contain a single
STAC Item, an ``ItemCollection`` (``FeatureCollection``), or a JSON list of
Items. Files are read in sorted name order so a snapshot is deterministic.

These helpers only read and validate JSON. They never execute anything from the
snapshot directory, and they do not import modules from it.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from stac_fastapi.types.errors import InvalidSnapshotError

from .utils import parse_snapshot_item, snapshot_digest


def _iter_documents(path: Path) -> Iterator[Any]:
    """Yield the JSON documents in a snapshot file or directory."""
    if path.is_file():
        files = [path]
    elif path.is_dir():
        files = sorted(
            child
            for child in path.iterdir()
            if child.is_file() and child.suffix == ".json"
        )
    else:
        raise InvalidSnapshotError(f"snapshot path {str(path)!r} does not exist")

    if not files:
        raise InvalidSnapshotError(f"snapshot path {str(path)!r} contains no .json files")

    for file_path in files:
        try:
            with file_path.open(encoding="utf-8") as handle:
                yield json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise InvalidSnapshotError(
                f"could not read snapshot file {file_path.name!r}: {exc}"
            ) from exc


def _flatten(document: Any, source: str) -> Iterator[dict[str, Any]]:
    """Yield Items from a single JSON document."""
    if isinstance(document, list):
        for index, entry in enumerate(document):
            if not isinstance(entry, dict):
                raise InvalidSnapshotError(
                    f"{source}: entry {index} is not a JSON object"
                )
            yield entry
        return
    if not isinstance(document, dict):
        raise InvalidSnapshotError(f"{source}: expected a JSON object or list")

    if document.get("type") == "FeatureCollection" or (
        "features" in document and "id" not in document
    ):
        features = document.get("features")
        if not isinstance(features, list):
            raise InvalidSnapshotError(f"{source}: 'features' is not a list")
        for index, feature in enumerate(features):
            if not isinstance(feature, dict):
                raise InvalidSnapshotError(
                    f"{source}: feature {index} is not a JSON object"
                )
            yield feature
        return

    yield document


def iter_snapshot_items(path: str | Path) -> Iterator[dict[str, Any]]:
    """Yield the raw Items found in a snapshot file or directory."""
    path = Path(path)
    for document in _iter_documents(path):
        yield from _flatten(document, path.name)


def load_snapshot(path: str | Path, collection_id: str) -> list[dict[str, Any]]:
    """Load and validate a snapshot, returning normalized Item payloads.

    Args:
        path: a directory of ``.json`` files, or a single JSON file.
        collection_id: the collection the snapshot belongs to.

    Returns:
        The validated Items, sorted by id.

    Raises:
        InvalidSnapshotError: if a file cannot be read, or an Item is invalid or
            belongs to another collection.
    """
    items: dict[str, dict[str, Any]] = {}
    for raw in iter_snapshot_items(path):
        item = parse_snapshot_item(raw, collection_id)
        if item["id"] in items:
            raise InvalidSnapshotError(
                f"snapshot contains Item id {item['id']!r} more than once"
            )
        items[item["id"]] = item
    return [items[item_id] for item_id in sorted(items)]
def snapshot_declarations(items: Iterable[dict[str, Any]]) -> tuple[int, str]:
    """Return the ``(item_count, checksum)`` declarations for a snapshot."""
    materialized = list(items)
    return len(materialized), snapshot_digest(materialized)
