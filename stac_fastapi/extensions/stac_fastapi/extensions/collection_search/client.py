"""collection-search extensions clients."""

import abc
from collections.abc import Awaitable, Callable

import attr
from stac_pydantic.shared import BBox

from stac_fastapi.types.bbox import (
    COLLECTION_IDENTITY_KEYS,
    execute_bbox_query,
    execute_bbox_query_async,
)
from stac_fastapi.types.stac import ItemCollection

from .request import BaseCollectionSearchPostRequest


@attr.s
class AsyncBaseCollectionSearchClient(abc.ABC):
    """Defines a pattern for implementing the STAC collection-search POST extension."""

    @abc.abstractmethod
    async def post_all_collections(
        self,
        search_request: BaseCollectionSearchPostRequest,
        **kwargs,
    ) -> ItemCollection:
        """Get all available collections.

        Called with `POST /collections`.

        Returns:
            A list of collections.

        """
        ...

    async def search_collections_with_bbox(
        self,
        bbox: BBox | None,
        execute: Callable[[BBox], Awaitable[dict]],
        *,
        limit: int | None = None,
    ) -> dict:
        """Run a Collection search over an antimeridian-aware query plan.

        Per-window results are deduplicated by Collection identity (``id``)
        before the limit is applied.
        """
        return await execute_bbox_query_async(
            bbox,
            execute,
            result_key="collections",
            identity_keys=COLLECTION_IDENTITY_KEYS,
            limit=limit,
        )


@attr.s
class BaseCollectionSearchClient(abc.ABC):
    """Defines a pattern for implementing the STAC collection-search POST extension."""

    @abc.abstractmethod
    def post_all_collections(
        self, search_request: BaseCollectionSearchPostRequest, **kwargs
    ) -> ItemCollection:
        """Get all available collections.

        Called with `POST /collections`.

        Returns:
            A list of collections.

        """
        ...

    def search_collections_with_bbox(
        self,
        bbox: BBox | None,
        execute: Callable[[BBox], dict],
        *,
        limit: int | None = None,
    ) -> dict:
        """Run a Collection search over an antimeridian-aware query plan.

        Per-window results are deduplicated by Collection identity (``id``)
        before the limit is applied.
        """
        return execute_bbox_query(
            bbox,
            execute,
            result_key="collections",
            identity_keys=COLLECTION_IDENTITY_KEYS,
            limit=limit,
        )
