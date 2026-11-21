"""Merging and provenance helpers for federated responses.

All provenance is written through stable, machine-readable fields so clients
can always trace a record back to its real source and original collection id,
regardless of which conflict strategy the federation uses:

* on every Collection and Item:
    - ``properties``/top-level ``federation:source`` (source id)
    - ``federation:original_collection_id`` (upstream collection id)
* on every aggregated response a ``federation:coverage`` block lists, per
  source, whether it answered, failed, was skipped or did not host the data.

Items additionally get a stable routing hint in their links so the item
details route reaches the exact same source even in PRIORITY mode, where the
federated collection id alone would be ambiguous.
"""

from typing import Any
from urllib.parse import urlencode

from stac_pydantic.links import Relations

from .models import (
    FEDERATION_EXTENSION_PREFIX,
    PROVENANCE_COLLECTION_PROPERTY,
    PROVENANCE_SOURCE_PROPERTY,
    ConflictResolution,
    CoverageReport,
    SourceConfig,
    SourceStatus,
)
from .routing import SourceRegistry, encode_source_hint

# Header used to pin item routes to a source. Mirrors the link-level hint so
# clients that cannot parse links still have a standard transport mechanism.
SOURCE_HINT_HEADER = "x-stac-source"


def _declare_extension(document: dict[str, Any]) -> None:
    """Register the federation extension id on a STAC document in place."""
    extensions = list(document.get("stac_extensions") or [])
    if FEDERATION_EXTENSION_PREFIX not in extensions:
        extensions.append(FEDERATION_EXTENSION_PREFIX)
    document["stac_extensions"] = extensions


def stamp_collection(
    collection: dict[str, Any],
    source: SourceConfig,
    registry: SourceRegistry,
    variants: list[dict[str, str]] | None = None,
    original_collection_id: str | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Attach federation provenance to a Collection document (mutating copy).

    ``original_collection_id`` is the id used by the upstream source; when not
    given the document's current id is used (callers that rewrite the id for
    SPLIT mode must pass it explicitly).
    """
    upstream_id = original_collection_id or collection.get("id")
    stamped = dict(collection)
    federation: dict[str, Any] = {
        PROVENANCE_SOURCE_PROPERTY: source.source_id,
        PROVENANCE_COLLECTION_PROPERTY: upstream_id,
        "federation:source_title": source.title or source.source_id,
    }
    if variants:
        federation["federation:variants"] = variants
    stamped["federation:provenance"] = federation

    # Fill in standard navigation links when the upstream omitted them so the
    # federated document is valid under strict STAC response validation.
    base = base_url.rstrip("/") if base_url else ""
    federated_id = stamped.get("id")
    links = [dict(link) for link in stamped.get("links", [])]
    existing_rels = {link.get("rel") for link in links}
    collection_href = f"{base}/collections/{federated_id}"
    for rel, href in (
        (Relations.self.value, collection_href),
        (Relations.root.value, f"{base}/"),
        (Relations.parent.value, f"{base}/"),
        (Relations.items.value, f"{collection_href}/items"),
    ):
        if rel not in existing_rels:
            links.append({"rel": rel, "type": "application/json", "href": href})
    stamped["links"] = links
    _declare_extension(stamped)
    return stamped


def stamp_item(
    item: dict[str, Any],
    source: SourceConfig,
    original_collection_id: str,
    registry: SourceRegistry,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Attach provenance and a source-pinned self link to an Item."""
    stamped = dict(item)
    properties = dict(stamped.get("properties", {}))
    properties[PROVENANCE_SOURCE_PROPERTY] = source.source_id
    properties[PROVENANCE_COLLECTION_PROPERTY] = original_collection_id
    stamped["properties"] = properties

    # Rewrite collection reference to the federated collection id while
    # keeping the upstream id explicitly in provenance.
    stamped["collection"] = registry.federated_collection_id(
        source, original_collection_id
    )

    links = [dict(link) for link in stamped.get("links", [])]
    hint = urlencode({SOURCE_HINT_HEADER: source.source_id})
    federated_id = registry.federated_collection_id(source, original_collection_id)
    item_id = stamped.get("id")
    base = base_url.rstrip("/") if base_url else ""
    self_href = (
        f"{base}/collections/{federated_id}/items/{item_id}?{hint}"
    )
    # Replace any existing self link; keep upstream links (their href still
    # points at the origin catalog, which is valid provenance).
    links = [link for link in links if link.get("rel") != Relations.self.value]
    links.append(
        {
            "rel": Relations.self.value,
            "type": "application/geo+json",
            "href": self_href,
            SOURCE_HINT_HEADER: source.source_id,
        }
    )
    # Ensure the standard STAC navigation links resolve inside the federation
    # (upstream documents often omit them or point at the origin catalog).
    existing_rels = {link.get("rel") for link in links}
    collection_href = f"{base}/collections/{federated_id}"
    for rel, href, media_type in (
        (Relations.collection.value, collection_href, "application/json"),
        (Relations.parent.value, collection_href, "application/json"),
        (Relations.root.value, f"{base}/", "application/json"),
    ):
        if rel not in existing_rels:
            links.append({"rel": rel, "type": media_type, "href": href})
    links.append(
        {
            "rel": "federation-source",
            "href": source.source_id,
            "title": source.title or source.source_id,
        }
    )
    stamped["links"] = links
    _declare_extension(stamped)
    return stamped


def build_variants(
    variants: list[tuple[SourceConfig, str]],
) -> list[dict[str, str]]:
    """Describe every source hosting a same-named collection."""
    return [
        {
            "source": source.source_id,
            "title": source.title or source.source_id,
            "original_collection_id": original_id,
            "federated_collection_id": encode_source_hint(
                source.source_id, original_id
            ),
        }
        for source, original_id in variants
    ]


def merge_collections(
    per_source: list[tuple[SourceConfig, list[dict[str, Any]]]],
    registry: SourceRegistry,
    base_url: str | None = None,
) -> list[dict[str, Any]]:
    """Merge Collection documents under the configured conflict rule.

    Args:
        per_source: ``(source, collections_from_that_source)`` pairs in
            deterministic source order.
        registry: routing registry defining the resolution strategy.
        base_url: optional federation base URL for rewritten navigation links.

    Returns the merged, provenance-stamped collection list with a stable
    ordering (by federated collection id).
    """
    # original collection id -> every (source, document) pair, in source order
    grouped: dict[str, list[tuple[SourceConfig, dict[str, Any]]]] = {}
    for source, collections in per_source:
        for collection in collections:
            original_id = collection["id"]
            grouped.setdefault(original_id, []).append((source, collection))

    merged: list[dict[str, Any]] = []
    for original_id, occurrences in grouped.items():
        variants = build_variants(
            [(source, original_id) for source, _ in occurrences]
        )
        if (
            registry.resolution == ConflictResolution.SPLIT
            or len(occurrences) == 1
        ):
            for source, document in occurrences:
                doc = dict(document)
                doc["id"] = registry.federated_collection_id(source, original_id)
                merged.append(
                    stamp_collection(
                        doc,
                        source,
                        registry,
                        variants,
                        original_collection_id=original_id,
                        base_url=base_url,
                    )
                )
        else:
            # PRIORITY: occurrences are already in registry priority order.
            winner_source, winner_doc = occurrences[0]
            merged.append(
                stamp_collection(
                    winner_doc,
                    winner_source,
                    registry,
                    variants=variants,
                    original_collection_id=original_id,
                    base_url=base_url,
                )
            )

    merged.sort(key=lambda doc: doc["id"])
    return merged


def merge_item_collections(
    per_source: list[tuple[SourceConfig, list[dict[str, Any]], str]],
    registry: SourceRegistry,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Merge ItemCollection features with stable provenance.

    Args:
        per_source: ``(source, items, original_collection_id)`` triples.
        registry: routing registry.
        base_url: optional base URL for rewritten self links.

    Duplicate items (same source/collection/item identity) are de-duplicated;
    items with the same id from *different* sources are all retained, each
    carrying its own provenance — federation never silently picks one.
    """
    features: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for source, items, original_collection_id in per_source:
        for item in items:
            identity = (
                source.source_id,
                original_collection_id,
                str(item.get("id")),
            )
            if identity in seen:
                continue
            seen.add(identity)
            features.append(
                stamp_item(
                    item, source, original_collection_id, registry, base_url
                )
            )

    # Deterministic order: source priority, then collection, then item id.
    order = {s.source_id: i for i, s in enumerate(registry.sources)}
    features.sort(
        key=lambda item: (
            order[item["properties"][PROVENANCE_SOURCE_PROPERTY]],
            item["properties"][PROVENANCE_COLLECTION_PROPERTY],
            str(item.get("id")),
        )
    )
    base = base_url.rstrip("/") if base_url else ""
    return {
        "type": "FeatureCollection",
        "features": features,
        "links": [
            {"rel": "root", "type": "application/json", "href": f"{base}/"},
        ],
        "numberReturned": len(features),
    }


def build_coverage(
    statuses: list[SourceStatus],
    resolution: ConflictResolution,
    queried_sources: list[str],
) -> CoverageReport:
    """Assemble the coverage block attached to aggregated responses."""
    partial = any(status["status"] != "ok" for status in statuses)
    return CoverageReport(
        resolution=str(resolution),
        queried_sources=list(queried_sources),
        coverage=statuses,
        partial=partial,
    )


def attach_coverage(payload: dict[str, Any], coverage: CoverageReport) -> dict[str, Any]:
    """Attach the coverage block to a response payload (mutating copy)."""
    payload["federation:coverage"] = dict(coverage)
    return payload
