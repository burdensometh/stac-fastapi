"""Reconciliation engine.

The reconciler compares a *declared* snapshot of a collection with the Items
that are actually persisted, and generates add / update / retire actions.

Safety rules implemented here:

* A snapshot must declare both its scope (``complete`` or ``partial``) and its
  version. A ``complete`` snapshot must also declare how many Items it holds.
* Item identity comes from each Item's own ``id``. Duplicate identity inside
  the snapshot, or a mapping key that disagrees with the Item's ``id``, is
  rejected before anything is written. Persisted Items are read back from the
  backend and must have unique ids too.
* Only a ``complete`` snapshot can retire Items.
* A segmented snapshot is only reconciled once every declared segment has been
  received, and only when the declared Item count (and optional checksum)
  matches the assembled content. Failed or incomplete input never retires
  anything.
* A snapshot older than the last applied version for the collection is
  rejected, so replaying an old snapshot cannot overwrite newer data. Reusing
  the last version with different content is a conflict.
* Retirements are only issued after every add and update has succeeded.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

from stac_fastapi.types.errors import (
    ConflictError,
    DatabaseError,
    InvalidSnapshotError,
    StaleSnapshotError,
)

from .request import (
    ItemAction,
    ReconciliationAction,
    ReconciliationRequest,
    ReconciliationResult,
    ReconciliationScope,
)
from .utils import (
    items_equivalent,
    parse_snapshot_item,
    raw_item,
    snapshot_digest,
)

#: Default page size used when paging through persisted Items.
DEFAULT_PAGE_LIMIT = 1_000
#: Hard stop for misbehaving pagination implementations.
MAX_PAGES = 10_000


@dataclass(frozen=True)
class SnapshotWatermark:
    """The last snapshot version successfully applied to a collection."""

    version: int
    digest: str


class SnapshotStateStore:
    """Storage for snapshot watermarks and staged segments.

    The default implementation is in-memory, which is enough for a single
    process. Backends that need the watermark to survive a restart (or to be
    shared between processes) should subclass this and persist both sets of
    keys.
    """

    def __init__(self) -> None:
        self._watermarks: dict[str, SnapshotWatermark] = {}
        self._segments: dict[tuple[str, int], dict[int, dict[str, Any]]] = {}

    def get_watermark(self, collection_id: str) -> SnapshotWatermark | None:
        """Return the last applied snapshot watermark, if any."""
        return self._watermarks.get(collection_id)

    def set_watermark(self, collection_id: str, watermark: SnapshotWatermark) -> None:
        """Record the last applied snapshot watermark."""
        self._watermarks[collection_id] = watermark

    def get_segments(
        self, collection_id: str, version: int
    ) -> dict[int, dict[str, Any]]:
        """Return the staged segments for a snapshot version, by segment number."""
        return self._segments.setdefault((collection_id, version), {})

    def put_segment(
        self, collection_id: str, version: int, segment: int, payload: dict[str, Any]
    ) -> None:
        """Stage one validated segment."""
        self.get_segments(collection_id, version)[segment] = payload

    def clear_segments(self, collection_id: str, version: int) -> None:
        """Drop all staged segments for a snapshot version."""
        self._segments.pop((collection_id, version), None)


@dataclass
class _Segment:
    """The validated content of one snapshot segment."""

    items: list[dict[str, Any]]
    declared_count: int | None
    checksum: str | None


def _as_mapping(value: Any) -> dict[str, Any]:
    """Convert an ItemCollection-like response to a mapping."""
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    if hasattr(value, "to_dict"):
        return value.to_dict()
    raise DatabaseError(
        f"core client returned {type(value).__name__}, expected an ItemCollection"
    )


def _next_token(links: Iterable[Any]) -> str | None:
    """Extract the pagination token from a ``next`` link, if any."""
    for link in links:
        if not isinstance(link, dict) or link.get("rel") != "next":
            continue
        href = link.get("href")
        if isinstance(href, str):
            tokens = parse_qs(urlsplit(href).query).get("token")
            if tokens:
                return tokens[0]
        body = link.get("body")
        if isinstance(body, dict) and isinstance(body.get("token"), str):
            return body["token"]
    return None


@dataclass
class Reconciler:
    """Compute and apply a reconciliation import for a collection.

    Attributes:
        core_client: the core client used to read the Items that are actually
            persisted. This is the source of truth for Item identity.
        transactions_client: the transaction client used to create, update and
            delete Items.
        state_store: where snapshot watermarks and staged segments live.
        page_limit: page size used when paging through persisted Items.
        verify: re-read the collection after applying and check that it matches
            the confirmed snapshot.
    """

    core_client: Any
    transactions_client: Any
    state_store: SnapshotStateStore = field(default_factory=SnapshotStateStore)
    page_limit: int = DEFAULT_PAGE_LIMIT
    verify: bool = True

    # -- client plumbing ---------------------------------------------------

    @staticmethod
    async def _call(func: Any, *args: Any, **kwargs: Any) -> Any:
        """Call a sync or async client method and return its value."""
        result = func(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result

    async def _iter_persisted(
        self, collection_id: str
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield every persisted Item of a collection, following pagination."""
        seen_tokens: set[str] = set()
        token: str | None = None
        for _ in range(MAX_PAGES):
            kwargs: dict[str, Any] = {
                "collection_id": collection_id,
                "limit": self.page_limit,
            }
            if token:
                kwargs["token"] = token
            response = _as_mapping(
                await self._call(self.core_client.item_collection, **kwargs)
            )
            for feature in response.get("features") or []:
                yield raw_item(feature)
            token = _next_token(response.get("links") or [])
            if not token:
                return
            if token in seen_tokens:
                raise DatabaseError(
                    f"pagination for collection {collection_id!r} did not advance"
                )
            seen_tokens.add(token)
        raise DatabaseError(
            f"pagination for collection {collection_id!r} exceeded {MAX_PAGES} pages"
        )

    async def _persisted_by_id(
        self, collection_id: str
    ) -> dict[str, dict[str, Any]]:
        """Return persisted Items keyed by their persisted identity."""
        persisted: dict[str, dict[str, Any]] = {}
        async for item in self._iter_persisted(collection_id):
            item_id = item.get("id")
            if not item_id:
                raise DatabaseError(
                    f"persisted Item in collection {collection_id!r} has no id"
                )
            if item_id in persisted:
                raise ConflictError(
                    f"persisted Item id {item_id!r} is not unique in collection "
                    f"{collection_id!r}"
                )
            persisted[item_id] = item
        return persisted

    # -- snapshot validation ----------------------------------------------

    def _parse_segment(
        self, collection_id: str, request: ReconciliationRequest
    ) -> _Segment:
        """Validate one segment and return its normalized Items.

        Raises:
            InvalidSnapshotError: if any Item, key or declaration in the
                segment is invalid. Nothing is written in that case.
            ConflictError: if the snapshot has ambiguous Item identity.
        """
        if request.collection_id is not None and (
            request.collection_id != collection_id
        ):
            raise InvalidSnapshotError(
                f"snapshot declares collection {request.collection_id!r}, "
                f"not {collection_id!r}"
            )

        if isinstance(request.items, dict):
            raw_items: list[tuple[str | None, Any]] = list(request.items.items())
        else:
            raw_items = [(None, item) for item in request.items]

        items: list[dict[str, Any]] = []
        for key, raw in raw_items:
            if (
                key is not None
                and isinstance(raw, dict)
                and raw.get("id") is not None
                and raw["id"] != key
            ):
                raise InvalidSnapshotError(
                    f"snapshot key {key!r} does not match the Item's own id "
                    f"{raw['id']!r}; Item identity must come from the Item"
                )
            items.append(parse_snapshot_item(raw, collection_id))

        # Duplicate identity within a segment is rejected here; duplicates
        # across segments are caught by _merge_items once everything is in.
        self._merge_items(items)
        return _Segment(
            items=items,
            declared_count=request.item_count,
            checksum=request.checksum,
        )

    @staticmethod
    def _merge_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Deduplicate and conflict-check Items from one or more segments."""
        merged: dict[str, dict[str, Any]] = {}
        for item in items:
            item_id = item["id"]
            if item_id in merged:
                if items_equivalent(merged[item_id], item):
                    raise InvalidSnapshotError(
                        f"snapshot contains Item id {item_id!r} more than once"
                    )
                raise ConflictError(
                    f"snapshot contains conflicting content for Item id {item_id!r}"
                )
            merged[item_id] = item
        return [merged[item_id] for item_id in sorted(merged)]

    # -- planning ----------------------------------------------------------

    @staticmethod
    def _plan(
        persisted: dict[str, dict[str, Any]],
        snapshot: list[dict[str, Any]],
        retire: bool,
    ) -> tuple[list[ItemAction], dict[str, dict[str, Any]]]:
        """Compute the actions for a validated snapshot.

        Returns the actions and the payload to persist for add/update actions.
        """
        actions: list[ItemAction] = []
        payloads: dict[str, dict[str, Any]] = {}
        snapshot_ids = {item["id"] for item in snapshot}

        for item in snapshot:
            item_id = item["id"]
            existing = persisted.get(item_id)
            if existing is None:
                actions.append(
                    ItemAction(action=ReconciliationAction.ADD, item_id=item_id)
                )
                payloads[item_id] = item
            elif not items_equivalent(existing, item):
                actions.append(
                    ItemAction(action=ReconciliationAction.UPDATE, item_id=item_id)
                )
                payloads[item_id] = item

        if retire:
            for item_id in sorted(set(persisted) - snapshot_ids):
                actions.append(
                    ItemAction(action=ReconciliationAction.RETIRE, item_id=item_id)
                )

        actions.sort(key=lambda action: (action.action.value, action.item_id))
        return actions, payloads

    @staticmethod
    def _counts(actions: list[ItemAction]) -> dict[str, int]:
        counts = {action.value: 0 for action in ReconciliationAction}
        for action in actions:
            counts[action.action.value] += 1
        return counts

    # -- public API --------------------------------------------------------

    async def reconcile(
        self, collection_id: str, request: ReconciliationRequest
    ) -> ReconciliationResult:
        """Validate a snapshot and apply it when it is complete.

        Args:
            collection_id: the collection from the request path.
            request: the snapshot.

        Returns:
            The actions that were (or, for a dry run, would be) applied. When a
            segmented snapshot is still missing segments the result has
            ``status="pending"``, no actions and no writes.

        Raises:
            StaleSnapshotError: the snapshot is older than the applied version.
            ConflictError: the snapshot reuses the last version with different
                content, or duplicate identity is ambiguous.
            InvalidSnapshotError: the snapshot is malformed or incomplete.
            DatabaseError: reading or writing failed, or verification failed.
        """
        watermark = self.state_store.get_watermark(collection_id)
        if watermark is not None and request.version < watermark.version:
            raise StaleSnapshotError(
                f"snapshot version {request.version} for collection "
                f"{collection_id!r} is older than the applied version "
                f"{watermark.version}"
            )

        incoming = self._parse_segment(collection_id, request)

        if request.dry_run and request.segments > 1:
            raise InvalidSnapshotError(
                "a dry run needs a single-segment snapshot: a segmented snapshot "
                "can only be planned once every segment has been received"
            )

        incoming_payload = {
            "items": incoming.items,
            "item_count": incoming.declared_count,
            "checksum": incoming.checksum,
        }

        if request.segments == 1:
            staged = {1: incoming_payload}
        else:
            # Validate the declarations before staging, so an inconsistent
            # segment cannot poison the staged state of a snapshot version.
            existing = dict(
                self.state_store.get_segments(collection_id, request.version)
            )
            existing[request.segment] = incoming_payload
            self._check_declarations(existing)
            if not request.dry_run:
                self.state_store.put_segment(
                    collection_id, request.version, request.segment, incoming_payload
                )
                staged = dict(
                    self.state_store.get_segments(collection_id, request.version)
                )
            else:  # pragma: no cover - rejected above
                staged = existing

        received = sorted(staged)
        missing = sorted(set(range(1, request.segments + 1)) - set(received))
        if missing:
            return ReconciliationResult(
                collection_id=collection_id,
                version=request.version,
                status="pending",
                dry_run=request.dry_run,
                received=sum(len(payload["items"]) for payload in staged.values()),
                received_segments=received,
                missing_segments=missing,
            )

        snapshot = self._merge_items(
            [item for number in received for item in staged[number]["items"]]
        )
        digest = snapshot_digest(snapshot)

        if watermark is not None and request.version == watermark.version:
            if digest != watermark.digest:
                raise ConflictError(
                    f"snapshot version {request.version} for collection "
                    f"{collection_id!r} was already applied with different content"
                )
            # Idempotent replay: same version, same content, nothing to do.
            return ReconciliationResult(
                collection_id=collection_id,
                version=request.version,
                status="complete",
                dry_run=request.dry_run,
                received=len(snapshot),
            )

        retire = request.scope is ReconciliationScope.COMPLETE
        self._validate_completeness(snapshot, digest, request, retire)

        persisted = await self._persisted_by_id(collection_id)
        actions, payloads = self._plan(persisted, snapshot, retire=retire)
        counts = self._counts(actions)

        if not request.dry_run:
            await self._apply(collection_id, actions, payloads)
            if self.verify:
                await self._verify(collection_id, snapshot, retire=retire)
            self.state_store.set_watermark(
                collection_id,
                SnapshotWatermark(version=request.version, digest=digest),
            )
            self.state_store.clear_segments(collection_id, request.version)

        return ReconciliationResult(
            collection_id=collection_id,
            version=request.version,
            status="complete",
            dry_run=request.dry_run,
            received=len(snapshot),
            added=counts[ReconciliationAction.ADD.value],
            updated=counts[ReconciliationAction.UPDATE.value],
            retired=counts[ReconciliationAction.RETIRE.value],
            unchanged=len(snapshot)
            - counts[ReconciliationAction.ADD.value]
            - counts[ReconciliationAction.UPDATE.value],
            actions=actions,
        )

    @staticmethod
    def _check_declarations(staged: dict[int, dict[str, Any]]) -> None:
        """Require every segment to declare the same count and checksum."""
        numbers = sorted(staged)
        first = staged[numbers[0]]
        for number in numbers[1:]:
            payload = staged[number]
            if payload["item_count"] != first["item_count"]:
                raise InvalidSnapshotError(
                    f"segment {number} declares item_count "
                    f"{payload['item_count']!r}, segment {numbers[0]} declares "
                    f"{first['item_count']!r}"
                )
            if payload["checksum"] != first["checksum"]:
                raise InvalidSnapshotError(
                    f"segment {number} and segment {numbers[0]} declare "
                    f"different checksums"
                )

    @staticmethod
    def _validate_completeness(
        snapshot: list[dict[str, Any]],
        digest: str,
        request: ReconciliationRequest,
        retire: bool,
    ) -> None:
        """Refuse to retire when a complete snapshot is not provably complete.

        A ``complete`` snapshot must declare its Item count: a truncated or
        partially-read snapshot is exactly the failure this guards against, and
        the count is the only evidence that the snapshot is whole. An optional
        checksum must also agree with the received content.
        """
        if not retire:
            return
        if request.item_count is None:
            raise InvalidSnapshotError(
                "a complete snapshot must declare item_count; refusing to "
                "reconcile or retire"
            )
        if len(snapshot) != request.item_count:
            raise InvalidSnapshotError(
                f"complete snapshot declares {request.item_count} Items but "
                f"contains {len(snapshot)}; refusing to reconcile or retire"
            )
        if request.checksum is not None and request.checksum != digest:
            raise InvalidSnapshotError(
                "complete snapshot checksum does not match its content; "
                "refusing to reconcile or retire"
            )

    async def _apply(
        self,
        collection_id: str,
        actions: list[ItemAction],
        payloads: dict[str, dict[str, Any]],
    ) -> None:
        """Apply add and update actions, then retire.

        Retirement is only reached when every add and update succeeded, so a
        failed write can never be followed by a destructive delete.
        """
        for action in actions:
            if action.action is ReconciliationAction.ADD:
                await self._call(
                    self.transactions_client.create_item,
                    collection_id,
                    copy.deepcopy(payloads[action.item_id]),
                )
            elif action.action is ReconciliationAction.UPDATE:
                await self._call(
                    self.transactions_client.update_item,
                    collection_id,
                    action.item_id,
                    copy.deepcopy(payloads[action.item_id]),
                )

        for action in actions:
            if action.action is ReconciliationAction.RETIRE:
                await self._call(
                    self.transactions_client.delete_item,
                    action.item_id,
                    collection_id,
                )

    async def _verify(
        self,
        collection_id: str,
        snapshot: list[dict[str, Any]],
        retire: bool,
    ) -> None:
        """Check that the persisted collection matches the confirmed snapshot."""
        persisted = await self._persisted_by_id(collection_id)
        expected = {item["id"]: item for item in snapshot}
        if retire:
            extra = sorted(set(persisted) - set(expected))
            if extra:
                raise DatabaseError(
                    f"verification failed: Items {extra} are still persisted in "
                    f"collection {collection_id!r} after a complete reconciliation"
                )
        mismatched = [
            item_id
            for item_id, item in expected.items()
            if item_id not in persisted
            or not items_equivalent(persisted[item_id], item)
        ]
        if mismatched:
            raise DatabaseError(
                f"verification failed: collection {collection_id!r} does not "
                f"match the snapshot for Items {sorted(mismatched)[:10]}"
            )

    # -- sync helpers ------------------------------------------------------

    async def iter_persisted(self, collection_id: str) -> AsyncIterator[dict[str, Any]]:
        """Async iterator over the persisted Items (useful for diagnostics)."""
        async for item in self._iter_persisted(collection_id):
            yield item

    def reconcile_sync(
        self, collection_id: str, request: ReconciliationRequest
    ) -> ReconciliationResult:
        """Synchronous wrapper around :meth:`reconcile`.

        Intended for scripts and synchronous backends; do not call it from
        inside a running event loop.
        """
        return asyncio.run(self.reconcile(collection_id, request))
