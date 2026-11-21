"""Reconciliation extension.

Adds ``POST /collections/{collection_id}/reconcile``, which imports a snapshot
of a collection and, when the snapshot is declared complete and is fully
received, retires Items of that collection that the snapshot no longer
contains.
"""

from __future__ import annotations

import abc
from collections.abc import Sequence
from enum import StrEnum
from typing import Any

import attr
from fastapi import APIRouter, Depends, FastAPI
from fastapi.responses import JSONResponse, Response
from starlette.requests import Request

from stac_fastapi.types.extension import ApiExtension

from .engine import Reconciler, SnapshotStateStore
from .request import ReconciliationRequest, ReconciliationResult


class ReconciliationConformanceClass(StrEnum):
    """Conformance class advertised by the reconciliation extension."""

    RECONCILIATION = "https://stac-utils.github.io/stac-fastapi/reconciliation"


@attr.s
class BaseReconciliationClient(abc.ABC):
    """Client contract for reconciliation imports."""

    @abc.abstractmethod
    async def reconcile(
        self,
        collection_id: str,
        snapshot: ReconciliationRequest,
        **kwargs: Any,
    ) -> ReconciliationResult | Response:
        """Reconcile a snapshot against a collection.

        Args:
            collection_id: the collection from the request path.
            snapshot: the snapshot to reconcile.

        Returns:
            The reconciliation result, or a Response.
        """
        raise NotImplementedError


@attr.s
class ReconciliationClient(BaseReconciliationClient):
    """Default reconciliation client backed by a :class:`Reconciler`."""

    reconciler: Reconciler = attr.ib()

    async def reconcile(
        self,
        collection_id: str,
        snapshot: ReconciliationRequest,
        **kwargs: Any,
    ) -> ReconciliationResult | Response:
        """Reconcile a snapshot against a collection."""
        return await self.reconciler.reconcile(collection_id, snapshot)


@attr.s
class ReconciliationExtension(ApiExtension):
    """Reconciliation extension.

    The extension adds ``POST /collections/{collection_id}/reconcile``. The
    request body is a :class:`ReconciliationRequest`: a snapshot of the
    collection that declares its ``version`` and ``scope``.

    Items are compared against what the backend actually persists, using each
    Item's own ``id``. The extension then generates and applies add, update and
    retire actions:

    * Items new to the collection are created.
    * Items whose content changed are updated.
    * Items missing from a ``complete`` snapshot are deleted.

    A snapshot that failed validation, that is missing segments, that declares
    a different Item count, or whose checksum does not match never retires
    anything. A snapshot older than the last applied version is rejected, so an
    old snapshot replayed later cannot overwrite newer data. After a successful
    complete reconciliation, ``GET /collections/{collection_id}/items`` returns
    exactly the Items of the confirmed snapshot.

    Version watermarks and staged segments live in a
    :class:`~stac_fastapi.extensions.reconciliation.engine.SnapshotStateStore`.
    The default store is in memory, which protects a single process. To keep
    replay protection across a restart, or to share it between workers, pass a
    store whose methods persist the collection watermark and staged segments.

    Either pass a ``client`` implementing :class:`BaseReconciliationClient`, or
    pass ``core_client`` and ``transactions_client`` to use the built-in
    :class:`ReconciliationClient`.

    Attributes:
        client: reconciliation application logic.
        core_client: core client used to read persisted Items.
        transactions_client: transaction client used to write Items.
        state_store: watermark/segment store; defaults to an in-memory store.
        page_limit: page size when paging through persisted Items.
        verify: re-read the collection after applying and check it matches the
            confirmed snapshot.
    """

    client: BaseReconciliationClient | None = attr.ib(default=None)
    core_client: Any | None = attr.ib(default=None)
    transactions_client: Any | None = attr.ib(default=None)
    state_store: SnapshotStateStore | None = attr.ib(default=None)
    page_limit: int = attr.ib(default=1_000)
    verify: bool = attr.ib(default=True)
    conformance_classes: list[str] = attr.ib(factory=list)
    schema_href: str | None = attr.ib(default=None)
    route_dependencies: Sequence[Depends] | None = attr.ib(default=None)
    enable_response_models: bool = attr.ib(default=True)

    def __attrs_post_init__(self) -> None:
        """Build the default client when core and transaction clients are given."""
        if self.client is not None:
            return
        if self.core_client is None or self.transactions_client is None:
            raise ValueError(
                "ReconciliationExtension requires either a client or both "
                "core_client and transactions_client"
            )
        self.client = ReconciliationClient(
            Reconciler(
                core_client=self.core_client,
                transactions_client=self.transactions_client,
                state_store=self.state_store or SnapshotStateStore(),
                page_limit=self.page_limit,
                verify=self.verify,
            )
        )

    async def _reconcile(
        self,
        collection_id: str,
        request_data: ReconciliationRequest,
        request: Request,
    ) -> ReconciliationResult | Response:
        """Dispatch a request to the reconciliation client."""
        return await self.client.reconcile(
            collection_id, request_data, request=request
        )

    def register(self, app: FastAPI) -> None:
        """Register the extension with a FastAPI application.

        Args:
            app: target FastAPI application.

        Returns:
            None
        """
        router = APIRouter(prefix=app.state.router_prefix)
        router.add_api_route(
            name="Reconcile Collection",
            path="/collections/{collection_id}/reconcile",
            response_model=ReconciliationResult
            if self.enable_response_models
            else None,
            responses={
                200: {
                    "content": {
                        "application/json": {},
                    },
                    "model": ReconciliationResult,
                },
            },
            response_class=JSONResponse,
            response_model_exclude_unset=False,
            methods=["POST"],
            endpoint=self._reconcile,
            dependencies=self.route_dependencies,
        )
        app.include_router(router, tags=["Reconciliation Extension"])
