"""Streaming (NDJSON) bulk item ingestion.

This module implements incremental bulk ingestion of newline-delimited JSON
STAC Items. Items are parsed and validated one line at a time, gathered into
small batches and committed through an existing bulk transactions client while
the request body is still being received, so arbitrarily large imports never
need to be materialized as a single JSON document.

Two resources are bounded by explicit limits:

* the read buffer never holds more than one line of at most
  ``max_line_bytes`` bytes (plus one transport chunk), and
* at most ``max_in_flight`` batches are uncommitted (in flight) at any time;
  reading pauses while the limit is reached.
"""

import asyncio
import inspect
import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

import attr
from starlette.concurrency import run_in_threadpool

from stac_fastapi.extensions.bulk_transactions.bulk_transactions import (
    DEFAULT_STREAM_CHUNK_SIZE,
    DEFAULT_STREAM_MAX_ERRORS,
    DEFAULT_STREAM_MAX_IN_FLIGHT,
    DEFAULT_STREAM_MAX_LINE_BYTES,
    AsyncBaseBulkTransactionsClient,
    BaseBulkTransactionsClient,
    BulkTransaction,
    BulkTransactionMethod,
    Items,
    TransactionError,
)

__all__ = [
    "BulkStreamIngester",
    "StreamItem",
    "StreamLineError",
    "parse_ndjson_stream",
]


@attr.s(frozen=True)
class StreamItem:
    """A STAC Item parsed from one line of an NDJSON stream."""

    line_number: int = attr.ib()
    item: dict[str, Any] = attr.ib()


@attr.s(frozen=True)
class StreamLineError:
    """An NDJSON line that could not be read as a STAC Item."""

    line_number: int = attr.ib()
    msg: str = attr.ib()


def _parse_line(
    line_number: int, raw: bytes, max_line_bytes: int
) -> StreamItem | StreamLineError | None:
    """Parse a single NDJSON line into a StreamItem or a StreamLineError."""
    if not raw.strip():
        return None
    if len(raw) > max_line_bytes:
        return StreamLineError(
            line_number=line_number,
            msg=f"line {line_number}: line exceeds the maximum length "
            f"of {max_line_bytes} bytes",
        )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return StreamLineError(
            line_number=line_number,
            msg=f"line {line_number}: invalid JSON: {exc.msg}",
        )
    except UnicodeDecodeError:
        return StreamLineError(
            line_number=line_number,
            msg=f"line {line_number}: invalid UTF-8",
        )
    if not isinstance(data, dict):
        return StreamLineError(
            line_number=line_number,
            msg=f"line {line_number}: expected a JSON object (STAC Item), "
            f"got {type(data).__name__}",
        )
    return StreamItem(line_number=line_number, item=data)


async def parse_ndjson_stream(
    stream: AsyncIterator[bytes],
    *,
    max_line_bytes: int = DEFAULT_STREAM_MAX_LINE_BYTES,
) -> AsyncIterator[StreamItem | StreamLineError]:
    """Parse an async byte stream of newline-delimited JSON.

    Yields a :class:`StreamItem` for every well-formed JSON object line and a
    :class:`StreamLineError` (carrying the 1-based source line number) for
    lines with invalid JSON, non-object JSON values, lines longer than
    ``max_line_bytes`` and a final line that is not terminated by a newline
    (a truncated line). Blank lines are skipped.

    At most ``max_line_bytes`` are buffered while waiting for the next
    newline, regardless of the size of the stream.
    """
    buffer = bytearray()
    line_number = 0
    discarding = False  # dropping the remainder of an overlong line

    async for chunk in stream:
        if not chunk:
            continue
        pos = 0
        while pos < len(chunk):
            if discarding:
                newline = chunk.find(b"\n", pos)
                if newline == -1:
                    pos = len(chunk)
                else:
                    discarding = False
                    pos = newline + 1
                continue
            newline = chunk.find(b"\n", pos)
            if newline == -1:
                buffer += chunk[pos:]
                pos = len(chunk)
                if len(buffer) > max_line_bytes:
                    line_number += 1
                    yield StreamLineError(
                        line_number=line_number,
                        msg=f"line {line_number}: line exceeds the maximum "
                        f"length of {max_line_bytes} bytes",
                    )
                    buffer.clear()
                    discarding = True
            else:
                buffer += chunk[pos:newline]
                pos = newline + 1
                line_number += 1
                event = _parse_line(line_number, bytes(buffer), max_line_bytes)
                buffer.clear()
                if event is not None:
                    yield event

    if not discarding and buffer.strip():
        line_number += 1
        yield StreamLineError(
            line_number=line_number,
            msg=f"line {line_number}: truncated line, "
            "the stream ended before a newline",
        )


@attr.s
class _ErrorCollector:
    """Collect transaction errors, up to a configurable limit."""

    limit: int | None = attr.ib()
    errors: list[TransactionError] = attr.ib(factory=list)
    omitted: int = attr.ib(default=0)

    def add(self, id: str, msg: str) -> None:
        """Append one error, unless the limit has been reached."""
        if self.limit is not None and len(self.errors) >= self.limit:
            self.omitted += 1
            return
        self.errors.append({"id": id, "msg": msg})

    def extend(self, errors: Any) -> None:
        """Append errors reported by a bulk client."""
        for error in errors or []:
            if isinstance(error, Mapping):
                self.add(str(error.get("id", "unknown")), str(error.get("msg", "")))
            else:
                self.add("unknown", str(error))

    def finalize(self) -> list[TransactionError]:
        """Return the collected errors, noting how many were omitted."""
        if self.omitted:
            self.errors.append(
                {
                    "id": "stream",
                    "msg": f"{self.omitted} further error(s) omitted",
                }
            )
        return self.errors


@attr.s
class BulkStreamIngester:
    """Incrementally ingest an NDJSON stream of STAC Items.

    Items are validated against the target collection and for a valid Item
    identity (``id``), gathered into batches of ``chunk_size`` and committed
    through the bulk transactions ``client``. At most ``max_in_flight``
    batches are uncommitted at any time; reading the stream pauses while the
    limit is reached, so memory stays bounded regardless of the stream size.

    The returned :class:`BulkTransaction` counts every candidate line as
    ``received`` but only items the client reported as committed count towards
    ``success``; items that were rejected or whose batch was not committed are
    reported in ``errors`` with their source line number instead.
    """

    client: AsyncBaseBulkTransactionsClient | BaseBulkTransactionsClient = attr.ib()
    collection_id: str = attr.ib()
    method: BulkTransactionMethod = attr.ib(default=BulkTransactionMethod.INSERT)
    chunk_size: int = attr.ib(
        default=DEFAULT_STREAM_CHUNK_SIZE, validator=attr.validators.gt(0)
    )
    max_in_flight: int = attr.ib(
        default=DEFAULT_STREAM_MAX_IN_FLIGHT, validator=attr.validators.gt(0)
    )
    max_line_bytes: int = attr.ib(
        default=DEFAULT_STREAM_MAX_LINE_BYTES, validator=attr.validators.gt(0)
    )
    max_errors: int | None = attr.ib(default=DEFAULT_STREAM_MAX_ERRORS)

    def _validate(self, event: StreamItem) -> TransactionError | None:
        """Validate one parsed item against the target collection."""
        item_id = event.item.get("id")
        if not isinstance(item_id, str) or not item_id:
            return {
                "id": f"line:{event.line_number}",
                "msg": f"line {event.line_number}: item does not have "
                "a valid string 'id'",
            }
        collection = event.item.get("collection")
        if collection is not None and collection != self.collection_id:
            return {
                "id": item_id,
                "msg": f"line {event.line_number}: item collection "
                f"{collection!r} does not match collection {self.collection_id!r}",
            }
        return None

    async def _commit(self, items: Items, **client_kwargs: Any) -> Any:
        """Commit one batch of items through the bulk client."""
        func = self.client.bulk_item_insert
        if inspect.iscoroutinefunction(func):
            return await func(items, **client_kwargs)
        return await run_in_threadpool(func, items, **client_kwargs)

    async def ingest(
        self, stream: AsyncIterator[bytes], **client_kwargs: Any
    ) -> BulkTransaction:
        """Consume the stream and return counts consistent with what was committed.

        Args:
            stream: an async iterator of NDJSON bytes (e.g. ``request.stream()``).
            client_kwargs: extra keyword arguments forwarded to the client's
                ``bulk_item_insert`` (e.g. ``request=request``).

        Returns:
            A BulkTransaction in which ``received`` counts every candidate
            line read from the stream, ``success`` and ``skipped`` are summed
            from the results of the committed batches, and ``errors`` holds
            one entry per rejected line or uncommitted item, carrying the
            source line number.
        """
        received = 0
        success = 0
        skipped = 0
        errors = _ErrorCollector(limit=self.max_errors)

        batch: dict[str, Any] = {}
        batch_lines: dict[str, int] = {}
        in_flight: set[asyncio.Task] = set()

        async def commit(
            batch_items: dict[str, Any], lines: dict[str, int]
        ) -> tuple[dict[str, int], Any, BaseException | None]:
            try:
                result = await self._commit(
                    Items(items=batch_items, method=self.method), **client_kwargs
                )
            except Exception as exc:  # the batch was not committed
                return lines, None, exc
            return lines, result, None

        def merge(committed: tuple[dict[str, int], Any, BaseException | None]) -> None:
            nonlocal success, skipped
            lines, result, exc = committed
            if exc is not None or not isinstance(result, Mapping):
                reason = (
                    f"{type(exc).__name__}: {exc}"
                    if exc is not None
                    else f"unexpected result {result!r}"
                )
                for item_id, line_number in lines.items():
                    errors.add(
                        item_id,
                        f"line {line_number}: batch was not committed ({reason})",
                    )
                return
            success += int(result.get("success", 0) or 0)
            skipped += int(result.get("skipped", 0) or 0)
            errors.extend(result.get("errors", []))

        async def dispatch(batch_items: dict[str, Any], lines: dict[str, int]) -> None:
            nonlocal in_flight
            if len(in_flight) >= self.max_in_flight:
                done, in_flight = await asyncio.wait(
                    in_flight, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    merge(task.result())
            in_flight.add(asyncio.ensure_future(commit(batch_items, lines)))

        async for event in parse_ndjson_stream(
            stream, max_line_bytes=self.max_line_bytes
        ):
            received += 1
            if isinstance(event, StreamLineError):
                errors.add(f"line:{event.line_number}", event.msg)
                continue
            validation_error = self._validate(event)
            if validation_error is not None:
                errors.add(validation_error["id"], validation_error["msg"])
                continue
            item_id = event.item["id"]
            if item_id in batch:
                errors.add(
                    item_id,
                    f"line {event.line_number}: duplicate item id "
                    f"{item_id!r} in the current batch",
                )
                continue
            batch[item_id] = event.item
            batch_lines[item_id] = event.line_number
            if len(batch) >= self.chunk_size:
                await dispatch(batch, batch_lines)
                batch, batch_lines = {}, {}

        if batch:
            await dispatch(batch, batch_lines)
        while in_flight:
            done, in_flight = await asyncio.wait(
                in_flight, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                merge(task.result())

        return BulkTransaction(
            received=received,
            success=success,
            skipped=skipped,
            errors=errors.finalize(),
        )
