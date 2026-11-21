"""Configuration and response models for federated sources.

The models in this module are deliberately plain ``attrs`` classes and
``TypedDict`` payloads so they compose with the existing stac-fastapi style
(which uses attrs for clients and request models) and serialize through the
same JSON response path as ordinary STAC payloads.
"""

from enum import StrEnum
from typing import Any, NotRequired, TypedDict

import attr

from stac_fastapi.types.core import AsyncBaseCoreClient, BaseCoreClient

# Prefix used for all federation-specific, non-STAC-standard fields. Keeping a
# single prefix (mirroring the STAC extension naming convention) makes it
# trivial for clients to recognize provenance/coverage metadata and for us to
# strip it when validating against the strict STAC models.
FEDERATION_EXTENSION_PREFIX = "https://stac-utils.github.io/stac-fastapi/federation/v1"

# Property names carrying federation provenance on individual records.
PROVENANCE_SOURCE_PROPERTY = "federation:source"
PROVENANCE_COLLECTION_PROPERTY = "federation:original_collection_id"


class ConflictResolution(StrEnum):
    """How to resolve same-named collections across sources.

    Attributes:
        PRIORITY: Keep exactly one variant per collection id, selected by the
            static, configured source priority (lowest ``priority`` wins; ties
            broken by registration order). The losing variants are still
            discoverable through the ``federation:variants`` coverage metadata.
            Selection never depends on which source answers fastest.
        SPLIT: Expose every source variant as its own federated collection
            under the stable id ``"{source_id}:{collection_id}"`` (with the
            original id retained as ``federation:original_collection_id``), so
            no source's data is hidden behind another's.
    """

    PRIORITY = "priority"
    SPLIT = "split"


@attr.s(frozen=True)
class SourceCapabilities:
    """Query conditions a source backend is able to execute server-side.

    Any condition flagged ``False`` here is *not* forwarded to the source; the
    federation layer applies it locally on the source's returned records
    instead. This lets a backend with limited query support (e.g. one that only
    filters by datetime) participate without silently ignoring constraints.
    """

    bbox: bool = attr.ib(default=True)
    intersects: bool = attr.ib(default=True)
    datetime: bool = attr.ib(default=True)
    ids: bool = attr.ib(default=True)

    def supports(self, condition: str) -> bool:
        """Return whether the named search condition is executed server-side."""
        return bool(getattr(self, condition, False))


@attr.s
class SourceConfig:
    """A single upstream source in the federation.

    Attributes:
        source_id: Stable, unique identifier of the source. It is used in
            federated collection ids, provenance properties and routing hints,
            so it must never change between deployments.
        client: The backend client (sync ``BaseCoreClient`` or async
            ``AsyncBaseCoreClient``) implementing the actual STAC calls.
        title: Human-readable source name, surfaced in coverage reports.
        capabilities: Which spatial/temporal conditions this source executes.
        priority: Static conflict priority for ``ConflictResolution.PRIORITY``;
            lower wins. Defaults to registration order at registry build time.
        collections: Optional allow-list of collection ids this source hosts.
            When ``None`` the source is queried for any collection; when set,
            the federation layer routes by this declaration without probing.
    """

    source_id: str = attr.ib()
    client: AsyncBaseCoreClient | BaseCoreClient = attr.ib()
    title: str | None = attr.ib(default=None)
    capabilities: SourceCapabilities = attr.ib(factory=SourceCapabilities)
    priority: int = attr.ib(default=0)
    collections: list[str] | None = attr.ib(default=None)

    @source_id.validator
    def _validate_source_id(self, attribute, value):
        if not value or ":" in value:
            # ":" is the separator in split federated ids, so forbid it in
            # source ids to keep routing hints unambiguous.
            raise ValueError(
                "source_id must be a non-empty string without ':' characters, "
                f"got {value!r}"
            )

    @property
    def is_async(self) -> bool:
        """Whether the wrapped client exposes coroutine methods."""
        return isinstance(self.client, AsyncBaseCoreClient)


class SourceStatus(TypedDict):
    """Per-source outcome attached to every aggregated federation response."""

    source: str
    status: str  # "ok" | "error" | "skipped" | "unsupported" | "not_hosted"
    detail: NotRequired[str]
    http_status: NotRequired[int]


class CoverageReport(TypedDict):
    """The ``federation:coverage`` block of an aggregated response."""

    resolution: str
    queried_sources: list[str]
    coverage: list[SourceStatus]
    partial: bool


class FederatedSearchRequest(TypedDict, total=False):
    """Normalized representation of a search request inside the federation.

    This decouples the GET/POST request models from the per-source dispatch
    logic: both entry points are reduced to this single structure.
    """

    collections: list[str]
    ids: list[str]
    bbox: tuple[float, ...]
    intersects: dict[str, Any]
    datetime: str
    limit: int
    extra: dict[str, Any]
