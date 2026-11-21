"""Rebase hrefs of STAC objects that are imported into a new location.

When a catalog is moved (for example from an internal web root to object
storage), the `assets` and catalog `links` stored in its Items may still be
relative.  Those relative hrefs used to be resolved against the old serving
address, so after the move they resolve to the wrong target.

This module provides an explicit, opt-in rebasing step for imports:

* the original `self` link of each Item is used as the base reference for
  relative hrefs (it is the canonical location the Item was published at),
* absolute hrefs are rewritten through the declared prefix mappings
  (e.g. ``https://stac.internal/catalog`` -> ``s3://bucket/catalog``),
* hrefs that are already absolute and outside the declared prefixes are kept
  verbatim (external assets never move),
* running the same import twice does not apply a prefix twice,
* Items whose original reference cannot be determined are rejected instead of
  being guessed.

Rebasing happens before an Item is stored, so every read path (single Item and
search) returns the same, already absolute, href.
"""

import copy
import posixpath
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import attr
from stac_pydantic.links import Relations

from .errors import RebaseError, UnknownReferenceError
from .links import INFERRED_LINK_RELS, is_absolute_href

__all__ = [
    "PrefixMapping",
    "HrefRebaser",
    "rebase_item",
    "rebase_item_collection",
]


def _resolve_reference(base_href: str, href: str) -> str:
    """Resolve a relative href against an absolute reference of any scheme.

    ``urljoin`` only understands a handful of schemes (``http``, ``https``,
    ``ftp``, ``file``) and returns unknown ones such as ``s3://`` or ``gs://``
    untouched. Fall back to a scheme-agnostic join for those.
    """
    resolved = urljoin(base_href, href)
    if is_absolute_href(resolved):
        return resolved

    base = urlsplit(base_href)
    ref = urlsplit(href)
    if ref.netloc:  # scheme-relative: keep the base scheme
        return f"{base.scheme}:{href}"

    if ref.path.startswith("/"):
        path = ref.path
    else:
        path = posixpath.join(posixpath.dirname(base.path), ref.path)
        path = posixpath.normpath(path)
        if ref.path.endswith("/") and not path.endswith("/"):
            path += "/"

    return urlunsplit((base.scheme, base.netloc, path, ref.query, ref.fragment))


def _normalize_prefix(prefix: str) -> str:
    """Normalize a declared prefix for boundary-aware comparisons."""
    if not prefix:
        raise ValueError("Prefix mappings must not contain empty prefixes.")
    if prefix.endswith("://"):
        return prefix
    return prefix.rstrip("/")


@attr.s(frozen=True)
class PrefixMapping:
    """Mapping between an old href prefix and its new location.

    A prefix only matches on a path segment boundary: mapping
    ``https://old/catalog`` to ``s3://new`` rewrites
    ``https://old/catalog/items/a`` but not ``https://old/catalogue``.
    """

    source: str = attr.ib(converter=_normalize_prefix)
    target: str = attr.ib(converter=_normalize_prefix)

    def match(self, href: str) -> str | None:
        """Return the rebased href, or ``None`` when the prefix does not match."""
        source = self.source
        if href == source or href == f"{source}/":
            return self.target
        if href.startswith(f"{source}/"):
            return f"{self.target}{href[len(source) :]}"
        return None

    def is_target(self, href: str) -> bool:
        """Return whether an href already points into the mapped target."""
        return href == self.target or href.startswith(f"{self.target}/")


def _coerce_prefix_mappings(
    mappings: (
        Mapping[str, str]
        | Iterable[tuple[str, str]]
        | Iterable[PrefixMapping]
        | None
    ),
) -> tuple[PrefixMapping, ...]:
    """Normalize the many accepted ways of declaring prefix mappings."""
    if mappings is None:
        return ()
    if isinstance(mappings, PrefixMapping):
        mappings = [mappings]
    elif isinstance(mappings, Mapping):
        mappings = list(mappings.items())

    normalized: dict[str, str] = {}
    for mapping in mappings:
        if isinstance(mapping, PrefixMapping):
            prefix_mapping = mapping
        else:
            source, target = mapping
            prefix_mapping = PrefixMapping(source=source, target=target)

        previous = normalized.get(prefix_mapping.source)
        if previous is not None and previous != prefix_mapping.target:
            raise ValueError(
                f"Conflicting prefix mappings for {prefix_mapping.source!r}: "
                f"{previous!r} and {prefix_mapping.target!r}."
            )
        normalized[prefix_mapping.source] = prefix_mapping.target

    # Longest prefix first so the most specific mapping wins, independent of the
    # order in which mappings were declared.
    return tuple(
        sorted(
            (PrefixMapping(source, target) for source, target in normalized.items()),
            key=lambda mapping: len(mapping.source),
            reverse=True,
        )
    )


def _as_dict(value: Any) -> dict[str, Any]:
    """Return a plain dict for either a mapping or a pydantic model."""
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    raise TypeError(f"Expected a mapping or a model, got {type(value)!r}.")


def _link_rel(link: Mapping[str, Any]) -> str | None:
    rel = link.get("rel")
    if hasattr(rel, "value"):
        rel = rel.value
    return rel if isinstance(rel, str) else None


@attr.s(frozen=True)
class HrefRebaser:
    """Resolve and rewrite hrefs so an imported object points at its new home.

    Attributes:
        prefix_mappings: mappings from old href prefixes to their new location,
            either a ``dict`` or an iterable of ``(source, target)`` pairs.
    """

    prefix_mappings: tuple[PrefixMapping, ...] = attr.ib(
        default=(), converter=_coerce_prefix_mappings
    )

    def rebase_href(self, href: Any, base_href: str | None = None) -> Any:
        """Resolve a relative href against the original reference, then remap it.

        Absolute hrefs are only remapped when they fall under a declared prefix;
        everything else is preserved.  An href that already points at a mapped
        target is returned unchanged, which makes repeated imports idempotent.

        Args:
            href: href to rebase. Non-string values are returned unchanged.
            base_href: absolute reference used to resolve a relative href,
                typically the original `self` link of the Item.

        Returns:
            The rebased href.

        Raises:
            UnknownReferenceError: the href is relative and no absolute
                reference is available.
        """
        if not isinstance(href, str) or not href:
            return href

        if not is_absolute_href(href):
            if base_href is None:
                raise UnknownReferenceError(
                    f"Relative href {href!r} cannot be rebased because the "
                    "original reference is unknown."
                )
            href = _resolve_reference(base_href, href)

        # Never remap an href that already points into a declared target: this is
        # what keeps a re-import from stacking prefixes.
        if any(mapping.is_target(href) for mapping in self.prefix_mappings):
            return href

        for mapping in self.prefix_mappings:
            rebased = mapping.match(href)
            if rebased is not None:
                return rebased

        return href

    def _base_href(self, item: Mapping[str, Any]) -> str | None:
        """Return the Item's original absolute `self` href, if it is unambiguous."""
        self_hrefs = {
            link["href"]
            for link in item.get("links") or []
            if isinstance(link, Mapping)
            and _link_rel(link) == Relations.self.value
            and isinstance(link.get("href"), str)
            and link["href"]
        }

        if not self_hrefs:
            return None
        if len(self_hrefs) > 1:
            raise RebaseError(
                "Item has conflicting `self` links; the original reference is "
                f"ambiguous: {sorted(self_hrefs)!r}."
            )

        base_href = self_hrefs.pop()
        if not is_absolute_href(base_href):
            raise UnknownReferenceError(
                f"Item `self` link {base_href!r} is relative; the original "
                "reference cannot be determined."
            )
        return base_href

    @staticmethod
    def _rebasable_links(item: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Return stored links that are not generated by the API on read."""
        return [
            link
            for link in item.get("links") or []
            if isinstance(link, dict) and _link_rel(link) not in INFERRED_LINK_RELS
        ]

    @staticmethod
    def _rebasable_assets(item: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Return stored assets that carry an href."""
        return [
            asset
            for asset in (item.get("assets") or {}).values()
            if isinstance(asset, dict)
        ]

    def rebase_item(self, item: Any) -> dict[str, Any]:
        """Return a copy of an Item with all stored hrefs rebased.

        Relative asset and catalog link hrefs are resolved against the Item's
        original `self` link, then remapped through the declared prefixes.
        Inferred links (`self`, `parent`, `collection`, `root`, ...) are left
        untouched because the API regenerates them on read.

        Args:
            item: the Item, as a mapping or a pydantic model.

        Returns:
            A rebased copy of the Item as a dict.

        Raises:
            UnknownReferenceError: relative hrefs are present but the original
                reference cannot be determined.
            RebaseError: the Item's `self` links contradict each other.
        """
        rebased = copy.deepcopy(_as_dict(item))
        links = self._rebasable_links(rebased)
        assets = self._rebasable_assets(rebased)

        needs_base = any(
            isinstance(entry.get("href"), str)
            and entry["href"]
            and not is_absolute_href(entry["href"])
            for entry in [*links, *assets]
        )
        base_href = self._base_href(rebased) if needs_base else None

        for link in links:
            if "href" in link:
                link["href"] = self.rebase_href(link["href"], base_href)
        for asset in assets:
            if "href" in asset:
                asset["href"] = self.rebase_href(asset["href"], base_href)

        return rebased

    def rebase_item_collection(self, item_collection: Any) -> dict[str, Any]:
        """Return a copy of an ItemCollection with every Item rebased.

        Args:
            item_collection: the ItemCollection, as a mapping or a pydantic model.

        Returns:
            A rebased copy of the ItemCollection as a dict.
        """
        rebased = copy.deepcopy(_as_dict(item_collection))
        features = rebased.get("features") or []
        rebased["features"] = [
            self.rebase_item(feature) if isinstance(feature, Mapping) else feature
            for feature in features
        ]
        return rebased


def rebase_item(
    item: Any,
    prefix_mappings: (
        Mapping[str, str]
        | Iterable[tuple[str, str]]
        | Iterable[PrefixMapping]
        | None
    ) = None,
) -> dict[str, Any]:
    """Rebase a single Item for import (see :class:`HrefRebaser`).

    Args:
        item: the Item, as a mapping or a pydantic model.
        prefix_mappings: old href prefix -> new href prefix mappings.

    Returns:
        A rebased copy of the Item as a dict.
    """
    return HrefRebaser(prefix_mappings=prefix_mappings).rebase_item(item)


def rebase_item_collection(
    item_collection: Any,
    prefix_mappings: (
        Mapping[str, str]
        | Iterable[tuple[str, str]]
        | Iterable[PrefixMapping]
        | None
    ) = None,
) -> dict[str, Any]:
    """Rebase every Item of an ItemCollection for import (see :class:`HrefRebaser`).

    Args:
        item_collection: the ItemCollection, as a mapping or a pydantic model.
        prefix_mappings: old href prefix -> new href prefix mappings.

    Returns:
        A rebased copy of the ItemCollection as a dict.
    """
    return HrefRebaser(prefix_mappings=prefix_mappings).rebase_item_collection(
        item_collection
    )
