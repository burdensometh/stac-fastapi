"""Request models for the fields extension."""

from typing import Annotated

import attr
from fastapi import Query
from pydantic import BaseModel, Field, model_validator

from stac_fastapi.types.fields import (
    FieldsProjection,
    get_fields_projection,
    split_fields,
)
from stac_fastapi.types.search import APIRequest, str2list


class PostFieldsExtension(BaseModel):
    """FieldsExtension.

    Both ``GET`` and ``POST`` requests end up here: the GET ``fields`` query
    parameter is split into ``include``/``exclude`` sets by
    :func:`stac_fastapi.types.fields.split_fields` and the POST body is
    validated into the same shape. Resolving the selection in one place keeps
    the parent/child precedence rules identical for both verbs.

    Attributes:
        include: set of fields to include.
        exclude: set of fields to exclude.
    """

    include: set[str] | None = set()
    exclude: set[str] | None = set()

    @model_validator(mode="after")
    def _resolve(self) -> "PostFieldsExtension":
        """Fix include/exclude precedence once, at validation time."""
        include = self.include or set()
        exclude = self.exclude or set()
        if include and exclude:
            projection = FieldsProjection.from_sets(include, exclude)
            self.include = set(projection.include)
            self.exclude = set(projection.exclude)
        return self

    @classmethod
    def from_param(cls, fields: str | list[str] | None) -> "PostFieldsExtension":
        """Build the model from a GET style ``fields`` query parameter."""
        if fields is None:
            return cls()
        selection = fields.split(",") if isinstance(fields, str) else list(fields)
        include, exclude = split_fields(selection)
        return cls(include=set(include), exclude=set(exclude))

    @property
    def projection(self) -> FieldsProjection:
        """Return the unified field projection for this selection."""
        return FieldsProjection.from_sets(self.include, self.exclude, requested=True)

    def backend_fields(self) -> list[str] | None:
        """Fields the backend should read, or ``None`` for the full document."""
        return self.projection.backend_fields()

    @staticmethod
    def _get_field_dict(fields: set[str] | None) -> dict:
        """Pydantic include/excludes notation.

        Internal method to create a dictionary for advanced include or exclude
        of pydantic fields on model export
        Ref: https://pydantic-docs.helpmanual.io/usage/exporting_models/#advanced-include-and-exclude
        """
        field_dict = {}
        for field in fields or []:
            if "." in field:
                parent, key = field.split(".", 1)
                if parent not in field_dict:
                    field_dict[parent] = {key}
                elif field_dict[parent] is not ...:
                    field_dict[parent].add(key)
            else:
                field_dict[field] = ...  # type:ignore

        return field_dict


def _fields_converter(
    val: Annotated[
        str | None,
        Query(
            description="Include or exclude fields from items body.",
            openapi_examples={
                "user-provided": {"value": None},
                "datetime": {"value": "properties.datetime"},
            },
        ),
    ] = None,
) -> list[str] | None:
    return str2list(val)


@attr.s
class FieldsExtensionGetRequest(APIRequest):
    """Additional fields for the GET request."""

    fields: list[str] | None = attr.ib(default=None, converter=_fields_converter)

    @property
    def fields_projection(self) -> FieldsProjection:
        """Return the unified field projection for this selection."""
        return get_fields_projection(self) or FieldsProjection()


class FieldsExtensionPostRequest(BaseModel):
    """Additional fields and schema for the POST request."""

    fields: PostFieldsExtension | None = Field(
        PostFieldsExtension(),
        description="Include or exclude fields from items body.",
    )

    @property
    def fields_projection(self) -> FieldsProjection:
        """Return the unified field projection for this selection."""
        return get_fields_projection(self) or FieldsProjection()
