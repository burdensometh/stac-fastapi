"""Request model for the Sort Extension."""

from typing import Annotated, Any

import attr
from fastapi import Query
from pydantic import BaseModel, Field
from stac_pydantic.api.extensions.sort import SortExtension as PostSortModel

from stac_fastapi.types.search import APIRequest, str2list

from .contract import SortContract

__all__ = [
    "SortExtensionGetRequest",
    "SortExtensionPostRequest",
    "sort_contract_from_request",
]


def _sort_converter(
    val: Annotated[
        str | None,
        Query(
            description="An array of property names, prefixed by either '+' for ascending or '-' for descending. If no prefix is provided, '+' is assumed.",  # noqa: E501
            openapi_examples={
                "user-provided": {"value": None},
                "resolution": {"value": "-gsd"},
                "resolution-and-dates": {"value": "-gsd,-datetime"},
            },
        ),
    ],
) -> list[str] | None:
    """Split a comma-separated GET ``sortby`` query value into tokens."""
    return str2list(val)


@attr.s
class SortExtensionGetRequest(APIRequest):
    """Sortby Parameter for GET requests."""

    sortby: list[str] | None = attr.ib(default=None, converter=_sort_converter)

    def to_sort_contract(
        self,
        sortables: dict[str, Any] | None = None,
        *,
        allow_undeclared: bool = False,
    ) -> SortContract:
        """Resolve the GET ``sortby`` tokens into an explicit SortContract.

        The resulting contract is equal to the contract produced by
        :meth:`SortExtensionPostRequest.to_sort_contract` for the equivalent
        POST body.
        """
        return sort_contract_from_request(
            self,
            sortables,
            allow_undeclared=allow_undeclared,
        )


class SortExtensionPostRequest(BaseModel):
    """Sortby parameter for POST requests."""

    sortby: list[PostSortModel] | None = Field(
        None,
        description="An array of property (field) names, and direction in form of '{'field': '<property_name>', 'direction':'<direction>'}'",  # noqa: E501
        json_schema_extra={
            "examples": [
                # user-provided
                None,
                # creation-time
                [
                    {
                        "field": "properties.created",
                        "direction": "asc",
                    }
                ],
            ],
        },
    )

    def to_sort_contract(
        self,
        sortables: dict[str, Any] | None = None,
        *,
        allow_undeclared: bool = False,
    ) -> SortContract:
        """Resolve the POST ``sortby`` objects into an explicit SortContract.

        The resulting contract is equal to the contract produced by
        :meth:`SortExtensionGetRequest.to_sort_contract` for the equivalent
        GET query string.
        """
        return sort_contract_from_request(
            self,
            sortables,
            allow_undeclared=allow_undeclared,
        )


def sort_contract_from_request(
    request: APIRequest | BaseModel,
    sortables: dict[str, Any] | None = None,
    *,
    allow_undeclared: bool = False,
) -> SortContract:
    """Build a :class:`SortContract` from any sort-enabled request object.

    Works for the extension's own request models as well as the combined
    request models produced by :func:`create_request_model`, whose dynamically
    generated POST subclasses do not inherit mixin methods. The request's
    transport is detected from the ``sortby`` payload shape: GET tokens are
    strings with optional ``+``/``-`` prefixes, POST entries are objects with
    ``field``/``direction`` keys. Both transports resolve to equal contracts
    for equivalent expressions.
    """
    sortby = getattr(request, "sortby", None)
    if sortby is None:
        return SortContract.from_get_sortby(
            None, sortables, allow_undeclared=allow_undeclared
        )

    if all(isinstance(token, str) for token in sortby):
        return SortContract.from_get_sortby(
            sortby, sortables, allow_undeclared=allow_undeclared
        )
    return SortContract.from_post_sortby(
        sortby, sortables, allow_undeclared=allow_undeclared
    )
