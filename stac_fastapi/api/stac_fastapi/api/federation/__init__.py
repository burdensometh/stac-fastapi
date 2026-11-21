"""Federation of multiple STAC backends behind a single client.

This package provides :class:`FederatedClient`, an implementation of
:class:`~stac_fastapi.types.core.AsyncBaseCoreClient` that fans each request
out to every *source* backend which can serve it, merges the responses under
explicit, deterministic rules and keeps the provenance (source id and original
collection id) of every record stable across the Collection, Item and Search
routes.

The design rules implemented here are:

* Every source declares which query conditions it supports (spatial:
  ``bbox`` / ``intersects``, temporal: ``datetime``). Conditions a source does
  not support are never sent to it; they are applied locally instead, so every
  source executes only what it actually supports.
* Same-named collections are merged by declared attribution, never by response
  speed. Conflicts are resolved either by an explicit static priority
  (``ConflictResolution.PRIORITY``) or by exposing every source variant
  separately (``ConflictResolution.SPLIT``).
* Routing to a single record (``get_item`` / ``item_collection`` /
  ``get_collection``) always resolves through the same stable
  source/collection registry used for search, so details and items routes land
  on the same real source the record came from.
* A source failure never silently removes coverage. Every response that
  aggregates sources carries an explicit per-source status report
  (``federation:coverage``); when no source can answer, the request fails with
  :class:`FederationCoverageError`.
"""

from .client import FederatedClient
from .errors import (
    FederationCoverageError,
    FederationError,
    SourceError,
)
from .handlers import add_federation_exception_handlers
from .models import (
    FEDERATION_EXTENSION_PREFIX,
    ConflictResolution,
    FederatedSearchRequest,
    SourceCapabilities,
    SourceConfig,
    SourceStatus,
)
from .routing import SourceRegistry, encode_source_hint, parse_source_hint

__all__ = [
    "FEDERATION_EXTENSION_PREFIX",
    "ConflictResolution",
    "FederatedClient",
    "FederatedSearchRequest",
    "FederationCoverageError",
    "FederationError",
    "SourceCapabilities",
    "SourceConfig",
    "SourceError",
    "SourceRegistry",
    "SourceStatus",
    "add_federation_exception_handlers",
    "encode_source_hint",
    "parse_source_hint",
]
