"""Stable source/collection routing for the federation.

The registry is the single source of truth for mapping federated collection
ids to real upstream ``(source, collection_id)`` pairs. Every route — search,
collection details, item listings and item details — resolves through this
same registry, which is what guarantees that a record's details/items routes
land on the same real source the record was retrieved from.

Routing never depends on response timing: mappings are either declared
statically via :class:`SourceConfig` or discovered once and cached, and the
result is always a deterministic ordering (priority, then registration order).
"""

import inspect
from collections.abc import Awaitable

import attr

from stac_fastapi.types.errors import NotFoundError

from .models import ConflictResolution, SourceConfig

SEPARATOR = ":"


def encode_source_hint(source_id: str, collection_id: str) -> str:
    """Build the stable federated collection id used in SPLIT mode."""
    return f"{source_id}{SEPARATOR}{collection_id}"


def parse_source_hint(federated_collection_id: str) -> tuple[str, str] | None:
    """Split a ``source:collection`` id back into its parts.

    Returns ``None`` when the id carries no source hint (a bare collection id
    in PRIORITY mode).
    """
    if SEPARATOR not in federated_collection_id:
        return None
    source_id, _, collection_id = federated_collection_id.partition(SEPARATOR)
    if not source_id or not collection_id:
        return None
    return source_id, collection_id


async def maybe_await(result: Awaitable | object) -> object:
    """Await ``result`` when awaitable, otherwise return it unchanged."""
    if inspect.isawaitable(result):
        return await result
    return result


class SourceRegistry:
    """Holds the configured sources and resolves federated routes.

    Args:
        sources: Source configs in registration order. When priorities are
            equal this order is the tie-breaker, making every selection
            reproducible.
        resolution: Conflict strategy for same-named collections.
        discover: When a source declares no static ``collections`` allow-list,
            whether its ``all_collections`` method may be called to discover
            the ids it hosts. Discovery results are cached for the lifetime of
            the registry; failures degrade to "may host anything" rather than
            dropping the source.
    """

    def __init__(
        self,
        sources: list[SourceConfig],
        resolution: ConflictResolution = ConflictResolution.PRIORITY,
        discover: bool = True,
    ) -> None:
        if not sources:
            raise ValueError("FederatedClient requires at least one source")
        ids = [s.source_id for s in sources]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate source ids are not allowed: {ids}")

        self.resolution = resolution
        self.discover = discover

        # Assign default priorities from registration order before sorting so
        # an explicit priority of 0 on every source keeps registration order
        # as the deterministic tie-break.
        resolved: list[SourceConfig] = []
        for index, source in enumerate(sources):
            if source.priority == 0:
                source = attr.evolve(source, priority=index + 1)
            resolved.append(source)
        self._order = {s.source_id: i for i, s in enumerate(resolved)}
        self._sources: list[SourceConfig] = sorted(
            resolved, key=lambda s: (s.priority, self._order[s.source_id])
        )
        self._by_id: dict[str, SourceConfig] = {
            s.source_id: s for s in self._sources
        }
        # Cache of discovered collection ids per source (only used when
        # discover=True and the source has no static allow-list). A failed
        # discovery is recorded as ``None`` ("hosting unknown") rather than
        # an empty list (which would mean "hosts nothing").
        self._discovered: dict[str, list[str] | None] = {}
        # Sources for which discovery has already been attempted (success or
        # failure), so discovery runs at most once per registry lifetime.
        self._discovery_attempted: set[str] = set()

    @property
    def sources(self) -> list[SourceConfig]:
        """Sources in deterministic (priority, registration) order."""
        return list(self._sources)

    def get(self, source_id: str) -> SourceConfig:
        """Return the config for ``source_id`` or raise ``NotFoundError``."""
        try:
            return self._by_id[source_id]
        except KeyError:
            raise NotFoundError(
                f"unknown federation source: {source_id!r}"
            ) from None

    # ------------------------------------------------------------------
    # Capability-aware dispatch planning
    # ------------------------------------------------------------------
    def plan_collection_routes(
        self, collection_ids: list[str] | None
    ) -> dict[str, list[SourceConfig]]:
        """Resolve which sources must be queried for each requested collection.

        In SPLIT mode request ids may be federated ``source:collection`` hints
        which pin an exact source. Bare ids route to every source known (or
        discovered) to host them. In PRIORITY mode all ids are bare and routed
        the same way; the merge step later applies the priority rule.

        Returns a mapping of the *original* (upstream) collection id to the
        sources hosting it, in deterministic priority order. The key ``"*"``
        means "every source" (a search without a collection filter).
        """
        if collection_ids is None:
            return {"*": list(self._sources)}

        plan: dict[str, list[SourceConfig]] = {}
        for federated_id in collection_ids:
            hint = parse_source_hint(federated_id)
            if hint is not None:
                source_id, original_id = hint
                source = self.get(source_id)
                plan.setdefault(original_id, [])
                if source not in plan[original_id]:
                    plan[original_id].append(source)
            else:
                plan.setdefault(federated_id, [])
                for source in self._sources:
                    if (
                        self._hosts(source, federated_id)
                        and source not in plan[federated_id]
                    ):
                        plan[federated_id].append(source)
            key = original_id if hint is not None else federated_id
            plan[key].sort(key=lambda s: self._order[s.source_id])
        return plan

    def resolve_collection(
        self, federated_collection_id: str
    ) -> list[tuple[SourceConfig, str]]:
        """Resolve a collection route to concrete ``(source, original_id)`` pairs.

        Used by the collection-details and items routes. Returns all hosting
        sources in priority order; the caller applies the conflict rule or, for
        item routes, rejects ambiguous bare ids unless a hint pins the source.
        """
        hint = parse_source_hint(federated_collection_id)
        if hint is not None:
            source_id, original_id = hint
            return [(self.get(source_id), original_id)]

        return [
            (source, federated_collection_id)
            for source in self._sources
            if self._hosts(source, federated_collection_id)
        ]

    def resolve_item_route(
        self, federated_collection_id: str, source_hint: str | None = None
    ) -> tuple[SourceConfig, str]:
        """Resolve an item route to exactly one real ``(source, collection_id)``.

        A ``source_hint`` (carried in item links / an optional request header)
        always wins and pins the route. Without a hint:

        * SPLIT mode requires a federated ``source:collection`` id — a bare id
          is ambiguous because each source exposes its own variant.
        * PRIORITY mode picks the highest-priority hosting source, the same
          choice the collection merge made, so details stay consistent.
        """
        if source_hint:
            source = self.get(source_hint)
            hint = parse_source_hint(federated_collection_id)
            original_id = hint[1] if hint else federated_collection_id
            return source, original_id

        candidates = self.resolve_collection(federated_collection_id)
        if not candidates:
            raise NotFoundError(
                f"no federated source hosts collection {federated_collection_id!r}"
            )

        if parse_source_hint(federated_collection_id) is not None:
            return candidates[0]

        if self.resolution == ConflictResolution.SPLIT:
            available = ", ".join(
                encode_source_hint(s.source_id, cid) for s, cid in candidates
            )
            raise NotFoundError(
                f"collection {federated_collection_id!r} is split across sources; "
                f"qualify the collection id with its source, one of: {available}"
            )

        # PRIORITY: candidates are already in priority order.
        return candidates[0]

    # ------------------------------------------------------------------
    # Hosting knowledge
    # ------------------------------------------------------------------
    def _hosts(self, source: SourceConfig, collection_id: str) -> bool:
        """Declared/cached hosting check without triggering discovery.

        Dynamic discovery is performed explicitly via :meth:`refresh_hosting`;
        this keeps route planning synchronous and side-effect free.
        """
        if source.collections is not None:
            return collection_id in source.collections
        if source.source_id not in self._discovered:
            # Never discovered (e.g. discovery disabled): assume it may host
            # it; dispatch confirms and a 404 is recorded as "not_hosted".
            return True
        discovered = self._discovered[source.source_id]
        if discovered is None:
            # Discovery attempted but failed: hosting unknown, stay permissive.
            return True
        return collection_id in discovered

    async def refresh_hosting(self) -> None:
        """Discover collection ids for sources without a static allow-list.

        Calls each eligible source's ``all_collections`` once and caches the
        result. A failing source is recorded as unknown (empty cache) and
        treated as "may host anything", so one broken catalog does not prevent
        routing to the others.
        """
        for source in self._sources:
            if source.collections is not None:
                continue
            if source.source_id in self._discovery_attempted:
                # Discovery runs at most once per source per registry lifetime.
                continue
            if not self.discover:
                continue
            self._discovery_attempted.add(source.source_id)
            try:
                payload = await maybe_await(source.client.all_collections())
                self._discovered[source.source_id] = [
                    collection["id"]
                    for collection in payload.get("collections", [])
                ]
            except Exception:
                # None means "attempted, unknown": _hosts stays permissive.
                self._discovered[source.source_id] = None

    def federated_collection_id(
        self, source: SourceConfig, original_collection_id: str
    ) -> str:
        """Return the externally-visible collection id for a source record."""
        if self.resolution == ConflictResolution.SPLIT:
            return encode_source_hint(source.source_id, original_collection_id)
        return original_collection_id


__all__ = [
    "SourceRegistry",
    "encode_source_hint",
    "parse_source_hint",
    "maybe_await",
]
