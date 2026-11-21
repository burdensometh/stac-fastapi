"""Request models for the Collection-Search extension."""

from datetime import datetime as dt
from typing import Annotated, cast

import attr
from fastapi import HTTPException, Query
from pydantic import BaseModel, Field, PrivateAttr, ValidationInfo, field_validator
from stac_pydantic.shared import BBox

from stac_fastapi.types.search import (
    APIRequest,
    DatetimeMixin,
    DateTimeQueryType,
    Limit,
    _bbox_converter,
    _validate_datetime,
)
from stac_fastapi.types.temporal import DateTimeInterval, parse_datetime_interval


@attr.s
class BaseCollectionSearchGetRequest(APIRequest, DatetimeMixin):
    """Basics additional Collection-Search parameters for the GET request."""

    bbox: BBox | None = attr.ib(default=None, converter=_bbox_converter)  # type: ignore
    datetime: DateTimeQueryType = attr.ib(default=None, validator=_validate_datetime)
    limit: Annotated[
        Limit | None,
        Query(
            description="Limits the number of results that are included in each page of the response."  # noqa: E501
        ),
    ] = attr.ib(default=10)


class BaseCollectionSearchPostRequest(BaseModel):
    """Collection-Search POST model."""

    bbox: BBox | None = Field(
        default=None,
        description="Only return items intersecting this bounding box. Mutually exclusive with **intersects**.",  # noqa: E501
        json_schema_extra={
            "examples": [
                # user-provided
                None,
                # Montreal
                "-73.896103,45.364690,-73.413734,45.674283",
            ],
        },
    )
    datetime: str | None = Field(
        default=None,
        description="""Only return items that have a temporal property that intersects this value.\n
Either a date-time or an interval, open or closed. Date and time expressions adhere to RFC 3339. Open intervals are expressed using double-dots.""",  # noqa: E501
        json_schema_extra={
            "examples": [
                # user-provided
                None,
                # single datetime
                "2018-02-12T23:20:50Z",
                # closed inverval
                "2018-02-12T00:00:00Z/2018-03-18T12:31:12Z",
                # open interval FROM
                "2018-02-12T00:00:00Z/..",
                # open interval TO
                "../2018-03-18T12:31:12Z",
            ],
        },
    )
    limit: Limit | None = Field(
        10,
        description="Limits the number of results that are included in each page of the response (capped to 10_000).",  # noqa: E501
    )

    # Private properties to store the parsed datetime values.
    # Not part of the model schema.
    _start_date: dt | None = PrivateAttr(default=None)
    _end_date: dt | None = PrivateAttr(default=None)

    # Properties to return the private values
    @property
    def start_date(self) -> dt | None:
        """Start Date (UTC), or None if the interval is open or unset."""
        return self._start_date

    @property
    def end_date(self) -> dt | None:
        """End Date (UTC), or None if the interval is open or unset.

        For a single datetime query this equals :attr:`start_date`, so
        backends can always filter with a closed ``start <= t <= end``
        interval intersection instead of special-casing instants.
        """
        return self._end_date

    @property
    def datetime_interval(self) -> DateTimeInterval | None:
        """The query window as a normalized ``(start, end)`` UTC interval.

        A single datetime is returned as ``(t, t)`` and open interval ends as
        ``None``, following the shared temporal contract in
        :mod:`stac_fastapi.types.temporal`. Returns ``None`` when no datetime
        was given.
        """
        if self.datetime is None:
            return None
        return (self._start_date, self._end_date)

    @field_validator("bbox")
    @classmethod
    def validate_bbox(cls, v: BBox) -> BBox:
        """validate bbox."""
        if v:
            # Validate order
            if len(v) == 4:
                xmin, ymin, xmax, ymax = cast(tuple[int, int, int, int], v)
            else:
                xmin, ymin, min_elev, xmax, ymax, max_elev = cast(
                    tuple[int, int, int, int, int, int], v
                )
                if max_elev < min_elev:
                    raise ValueError(
                        "Maximum elevation must greater than minimum elevation"
                    )
            # Validate against WGS84
            if xmin < -180 or ymin < -90 or xmax > 180 or ymax > 90:
                raise ValueError("Bounding box must be within (-180, -90, 180, 90)")

            if ymax < ymin:
                raise ValueError(
                    "Maximum longitude must be greater than minimum longitude"
                )

        return v

    @field_validator("datetime", mode="after")
    @classmethod
    def validate_datetime(cls, value: str | None, info: ValidationInfo) -> str | None:
        """Validate datetime with the shared temporal contract.

        Uses the same parser as Item Search (GET and POST) and Collection
        Search (GET) so every endpoint applies identical interval semantics,
        UTC normalization, and open-end rules.
        """
        if value is None:
            return value

        try:
            interval = parse_datetime_interval(value)
        except HTTPException as e:
            # Match the pydantic validation error behavior (422) of the other
            # POST request models instead of propagating a 400 HTTPException.
            raise ValueError(e.detail) from e

        # Store the parsed dates; a single datetime is stored for both bounds
        # and open interval ends are stored as None.
        assert interval is not None
        info.data["_start_date"] = interval[0]
        info.data["_end_date"] = interval[1]

        # Return the original string value
        return value
