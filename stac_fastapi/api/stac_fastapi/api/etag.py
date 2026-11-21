"""Entity tag (`ETag`) response headers.

The Transaction extension lets a client read an Item, keep the `ETag` the read
returned, and send it back on `PATCH` as the Item's expected version, so a
write built from stale data does not overwrite an update which landed first.
A client does not have to change the way it reads Items: a successful
single-Item response carries the `ETag` of the Item it returned, whichever core
client or backend served it.

A backend which keeps its own version of an Item (a sequence number, a row
version, ...) records it with `set_etag` and that tag is used as it is.
Otherwise the header is derived from the response body with
`stac_fastapi.types.etag.item_version`, the same function the Transaction
extension uses to compute the version of an Item it reads, patches and writes.
A response which already has an `ETag` is never changed.

The tag names the Item's version, not a byte representation: a response in
another content coding carries the same tag.  Only the routes which return a
single Item are inspected, so a search result is never buffered.
"""

import json
import re
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from stac_fastapi.types.etag import item_version, quote_etag

# Routes which return one STAC Item.  The router stores the matched route in
# the scope, so it is known by the time the response starts.
ITEM_ROUTE_NAMES = frozenset(
    {"Get Item", "Create Item", "Update Item", "Patch Item", "Delete Item"}
)
# A path to one item, `/collections/{id}/items/{item_id}` at any prefix.  It is
# checked as well, so a backend which defines its own item routes still gets
# the header.  A collection's items path has no trailing segment and does not
# match.
ITEM_PATH = re.compile(r"/items/[^/]+/?$")
ITEM_MEDIA_TYPES = ("application/json", "application/geo+json")


def set_etag(request: Any, version: str | None) -> None:
    """Record the version of the Item a response returns.

    Call this from an endpoint or client which keeps its own version of the
    Item; `ETagMiddleware` puts it on the response.  It takes precedence over
    the version derived from the response body.

    Args:
        request: the request being served.
        version: the Item's version, unquoted.

    Returns:
        None
    """
    state = getattr(request, "state", None)
    if state is not None and version is not None:
        state.etag = quote_etag(version)


def header_value(headers: list[tuple[bytes, bytes]], name: bytes) -> bytes | None:
    """Return a header value from an ASGI header list, case-insensitively."""
    for key, value in headers:
        if key.lower() == name:
            return value
    return None


class ETagMiddleware:
    """Add an `ETag` header to single-Item responses which carry none.

    A version recorded with `set_etag` is used as it is.  Otherwise a
    successful Item response on one of the single-Item routes is buffered so
    its body can be inspected.  Any other response, and any streaming
    response, is forwarded as it is written.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap the application."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve the request, adding the ETag once the response is known."""
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        state = scope.setdefault("state", {})
        start: Message | None = None
        parts: list[bytes] = []

        async def send_wrapper(message: Message) -> None:
            nonlocal start, parts
            if message["type"] == "http.response.start":
                start = None
                headers = message.get("headers") or []
                recorded = state.get("etag")
                if (
                    recorded
                    and 200 <= message["status"] < 300
                    and header_value(headers, b"etag") is None
                ):
                    return await send(
                        {
                            **message,
                            "headers": [
                                *headers,
                                (b"etag", recorded.encode("latin-1")),
                            ],
                        }
                    )
                if self._should_inspect(scope, message):
                    start = message
                    return
                return await send(message)
            if message["type"] != "http.response.body" or start is None:
                return await send(message)
            if message.get("more_body", False):
                # A streaming response: forward it without inspecting it.
                await send(start)
                start = None
                parts = []
                return await send(message)

            parts.append(message.get("body") or b"")
            body = b"".join(parts)
            parts = []
            etag = self._derive_etag(body)
            headers = list(start.get("headers") or [])
            if etag is not None:
                headers.append((b"etag", etag.encode("latin-1")))
                # The body is unchanged, so `content-length` stays valid.
            await send({**start, "headers": headers})
            start = None
            return await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, send_wrapper)
        if start is not None:
            # The response ended without a body at all.
            await send(start)

    @staticmethod
    def _should_inspect(scope: Scope, message: Message) -> bool:
        """Return whether the response start message describes an inspectable Item."""
        route = scope.get("route")
        if (
            getattr(route, "name", None) not in ITEM_ROUTE_NAMES
            and ITEM_PATH.search(scope.get("path", "")) is None
        ):
            return False
        if not 200 <= message["status"] < 300:
            return False
        headers = message.get("headers") or []
        if header_value(headers, b"etag") is not None:
            return False
        if header_value(headers, b"content-encoding") not in (None, b"identity"):
            return False
        content_type = header_value(headers, b"content-type")
        if content_type is None:
            return False
        media_type = content_type.decode("latin-1").split(";", 1)[0].strip().lower()
        return media_type in ITEM_MEDIA_TYPES

    @staticmethod
    def _derive_etag(body: bytes) -> str | None:
        """Return the ETag of a single-Item response body, or None."""
        try:
            document = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(document, dict) or document.get("type") != "Feature":
            return None
        return quote_etag(item_version(document))
