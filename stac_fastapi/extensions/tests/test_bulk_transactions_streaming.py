"""Tests for streaming (NDJSON) bulk item ingestion."""

import asyncio
import json
from collections.abc import Iterator

import pytest
from fastapi import Depends, HTTPException, Response, security, status
from starlette.testclient import TestClient

from stac_fastapi.api.app import StacApi
from stac_fastapi.extensions.bulk_transactions import (
    AsyncBaseBulkTransactionsClient,
    BaseBulkTransactionsClient,
    BulkTransaction,
    BulkTransactionExtension,
    Items,
)
from stac_fastapi.extensions.bulk_transactions.streaming import (
    BulkStreamIngester,
    StreamItem,
    StreamLineError,
    parse_ndjson_stream,
)
from stac_fastapi.types.config import ApiSettings
from stac_fastapi.types.core import BaseCoreClient


def make_item(item_id: str, collection: str = "a-collection") -> dict:
    """A minimal STAC Item dictionary."""
    return {"type": "Feature", "id": item_id, "collection": collection}


def to_ndjson(*items: dict) -> bytes:
    """Serialize items as newline-delimited JSON."""
    return b"".join(json.dumps(item).encode() + b"\n" for item in items)


async def byte_stream(*chunks: bytes):
    """Turn chunks into an async byte stream."""
    for chunk in chunks:
        yield chunk


def chunk_bytes(data: bytes, chunk_size: int | None) -> list[bytes]:
    """Split data into chunks, or return it whole."""
    if not chunk_size:
        return [data]
    return [data[i : i + chunk_size] for i in range(0, len(data), chunk_size)]


def parse_all(data: bytes, max_line_bytes: int = 1024, chunk_size: int | None = None):
    """Run the NDJSON parser over data and collect the events."""

    async def run():
        stream = byte_stream(*chunk_bytes(data, chunk_size))
        return [
            event
            async for event in parse_ndjson_stream(
                stream, max_line_bytes=max_line_bytes
            )
        ]

    return asyncio.run(run())


def run_ingest(
    ingester: BulkStreamIngester,
    data: bytes,
    chunk_size: int | None = None,
    **kwargs,
) -> BulkTransaction:
    """Run an ingester over data."""

    async def run():
        stream = byte_stream(*chunk_bytes(data, chunk_size))
        return await ingester.ingest(stream, **kwargs)

    return asyncio.run(run())


# --- PARSER TESTS ---


def test_parse_valid_lines_with_line_numbers() -> None:
    """Items are parsed with their 1-based source line numbers."""
    events = parse_all(to_ndjson(make_item("a"), make_item("b"), make_item("c")))
    assert events == [
        StreamItem(line_number=1, item=make_item("a")),
        StreamItem(line_number=2, item=make_item("b")),
        StreamItem(line_number=3, item=make_item("c")),
    ]


def test_parse_lines_split_across_chunks() -> None:
    """Lines split over many transport chunks are reassembled."""
    data = to_ndjson(make_item("a"), make_item("b"))
    for chunk_size in (1, 3, 7):
        events = parse_all(data, chunk_size=chunk_size)
        assert [event.item["id"] for event in events] == ["a", "b"]
        assert [event.line_number for event in events] == [1, 2]


def test_parse_blank_lines_are_skipped() -> None:
    """Blank lines do not produce events but still consume line numbers."""
    events = parse_all(b'{"id": "a"}\n\n   \n{"id": "b"}\n')
    assert [event.line_number for event in events] == [1, 4]


def test_parse_invalid_json_reports_line_number() -> None:
    """Invalid JSON is reported with its line number; parsing continues."""
    events = parse_all(b'{"id": "a"}\n{"id": \n{"id": "b"}\n')
    assert isinstance(events[0], StreamItem)
    assert isinstance(events[1], StreamLineError)
    assert events[1].line_number == 2
    assert "line 2" in events[1].msg
    assert "invalid JSON" in events[1].msg
    assert isinstance(events[2], StreamItem)
    assert events[2].item["id"] == "b"


def test_parse_non_object_line() -> None:
    """JSON values that are not objects are rejected."""
    events = parse_all(b'[1, 2]\n"a string"\n{"id": "a"}\n')
    assert isinstance(events[0], StreamLineError)
    assert "line 1" in events[0].msg
    assert isinstance(events[1], StreamLineError)
    assert "line 2" in events[1].msg
    assert isinstance(events[2], StreamItem)


def test_parse_invalid_utf8() -> None:
    """Lines that are not valid UTF-8 are rejected."""
    events = parse_all(b'{"id": "\xff"}\n{"id": "a"}\n')
    assert isinstance(events[0], StreamLineError)
    assert "line 1" in events[0].msg
    assert isinstance(events[1], StreamItem)


def test_parse_overlong_line() -> None:
    """Lines longer than the limit are rejected and skipped."""
    long_item = json.dumps({"id": "x" * 100}).encode()
    data = b'{"id": "a"}\n' + long_item + b'\n{"id": "b"}\n'
    events = parse_all(data, max_line_bytes=50)
    assert isinstance(events[0], StreamItem)
    assert isinstance(events[1], StreamLineError)
    assert events[1].line_number == 2
    assert "line 2" in events[1].msg
    assert "maximum length" in events[1].msg
    assert isinstance(events[2], StreamItem)
    assert events[2].item["id"] == "b"


def test_parse_overlong_line_split_across_chunks() -> None:
    """The overlong remainder is discarded even when it spans chunks."""
    long_item = json.dumps({"id": "x" * 100}).encode()
    data = long_item + b'\n{"id": "b"}\n'
    events = parse_all(data, max_line_bytes=50, chunk_size=7)
    assert isinstance(events[0], StreamLineError)
    assert events[0].line_number == 1
    assert isinstance(events[1], StreamItem)
    assert events[1].item["id"] == "b"


def test_parse_line_at_exact_limit_is_accepted() -> None:
    """A line of exactly max_line_bytes is still valid."""
    line = json.dumps({"id": "abcd"}).encode()
    events = parse_all(line + b"\n", max_line_bytes=len(line))
    assert events == [StreamItem(line_number=1, item={"id": "abcd"})]


def test_parse_truncated_final_line() -> None:
    """A final line without a newline terminator is reported as truncated."""
    events = parse_all(b'{"id": "a"}\n{"id": "b"')
    assert isinstance(events[0], StreamItem)
    assert isinstance(events[1], StreamLineError)
    assert events[1].line_number == 2
    assert "line 2" in events[1].msg
    assert "truncated" in events[1].msg


def test_parse_whitespace_tail_is_ignored() -> None:
    """Trailing whitespace after the last newline is not an error."""
    events = parse_all(b'{"id": "a"}\n  \n')
    assert events == [StreamItem(line_number=1, item={"id": "a"})]


# --- INGESTER TESTS ---


class RecordingBulkClient(AsyncBaseBulkTransactionsClient):
    """Bulk client recording batches and reporting per-item failures."""

    def __init__(self, fail_ids=(), fail_batch_ids=()):
        """fail_ids are reported as item errors; batches containing
        fail_batch_ids raise an exception."""
        self.batches: list[list[dict]] = []
        self.methods: list[str] = []
        self.kwargs: list[dict] = []
        self.in_flight = 0
        self.max_in_flight_seen = 0
        self.fail_ids = set(fail_ids)
        self.fail_batch_ids = set(fail_batch_ids)

    async def bulk_item_insert(
        self, items: Items, **kwargs
    ) -> BulkTransaction | Response:
        """Record the batch and fail as configured."""
        self.in_flight += 1
        self.max_in_flight_seen = max(self.max_in_flight_seen, self.in_flight)
        await asyncio.sleep(0.001)
        self.in_flight -= 1
        batch = list(items)
        self.batches.append(batch)
        self.methods.append(items.method)
        self.kwargs.append(kwargs)
        if any(item["id"] in self.fail_batch_ids for item in batch):
            raise RuntimeError("backend is down")
        errors = [
            {"id": item["id"], "msg": "item already exists"}
            for item in batch
            if item["id"] in self.fail_ids
        ]
        return BulkTransaction(
            received=len(batch),
            success=len(batch) - len(errors),
            skipped=len(errors),
            errors=errors,
        )


class RecordingSyncBulkClient(BaseBulkTransactionsClient):
    """Synchronous bulk client recording batches."""

    def __init__(self):
        self.batches: list[list[dict]] = []

    def bulk_item_insert(
        self, items: Items, chunk_size: int | None = None, **kwargs
    ) -> BulkTransaction | Response:
        """Record the batch."""
        batch = list(items)
        self.batches.append(batch)
        return BulkTransaction(
            received=len(batch), success=len(batch), skipped=0, errors=[]
        )


def make_ingester(client, collection_id="a-collection", **kwargs) -> BulkStreamIngester:
    """Build an ingester for the recording client."""
    return BulkStreamIngester(client=client, collection_id=collection_id, **kwargs)


def test_ingest_commits_valid_items_in_chunks() -> None:
    """Valid items are committed in chunk_size batches."""
    client = RecordingBulkClient()
    ingester = make_ingester(client, chunk_size=2)
    data = to_ndjson(*[make_item(f"item-{i}") for i in range(5)])

    result = run_ingest(ingester, data, request="the-request")

    assert result == {"received": 5, "success": 5, "skipped": 0, "errors": []}
    assert [len(batch) for batch in client.batches] == [2, 2, 1]
    assert client.methods == ["insert", "insert", "insert"]
    # extra kwargs (e.g. request) are forwarded to the client
    assert all(kwargs["request"] == "the-request" for kwargs in client.kwargs)


def test_ingest_success_reflects_client_results() -> None:
    """The returned totals match what the client actually committed."""
    client = RecordingBulkClient(fail_ids={"item-1", "item-3"})
    ingester = make_ingester(client, chunk_size=2)
    data = to_ndjson(*[make_item(f"item-{i}") for i in range(4)])

    result = run_ingest(ingester, data)

    assert result["received"] == 4
    assert result["success"] == 2
    assert result["skipped"] == 2
    # errors are merged in batch completion order, which is not deterministic
    assert sorted(result["errors"], key=lambda e: e["id"]) == [
        {"id": "item-1", "msg": "item already exists"},
        {"id": "item-3", "msg": "item already exists"},
    ]


def test_ingest_rejects_invalid_items_with_line_numbers() -> None:
    """Invalid JSON, missing ids and foreign collections carry line numbers."""
    client = RecordingBulkClient()
    ingester = make_ingester(client, chunk_size=10)
    data = b"\n".join(
        [
            json.dumps(make_item("ok-1")).encode(),
            b'{"id": ',
            json.dumps({"type": "Feature", "collection": "a-collection"}).encode(),
            json.dumps(make_item("alien", collection="other")).encode(),
            json.dumps(make_item("ok-2")).encode(),
        ]
    ) + b"\n"

    result = run_ingest(ingester, data)

    assert result["received"] == 5
    assert result["success"] == 2
    assert [item["id"] for batch in client.batches for item in batch] == [
        "ok-1",
        "ok-2",
    ]
    assert len(result["errors"]) == 3
    assert "line 2" in result["errors"][0]["msg"]
    assert "invalid JSON" in result["errors"][0]["msg"]
    assert "line 3" in result["errors"][1]["msg"]
    assert "'id'" in result["errors"][1]["msg"]
    assert result["errors"][2]["id"] == "alien"
    assert "line 4" in result["errors"][2]["msg"]
    assert "other" in result["errors"][2]["msg"]


def test_ingest_truncated_and_overlong_lines() -> None:
    """Truncated and overlong lines carry line numbers and are not committed."""
    client = RecordingBulkClient()
    ingester = make_ingester(client, chunk_size=10, max_line_bytes=64)
    data = (
        json.dumps(make_item("ok")).encode()
        + b"\n"
        + json.dumps({"id": "x" * 100}).encode()
        + b"\n"
        + b'{"id": "cut-off"'
    )

    result = run_ingest(ingester, data)

    assert result["received"] == 3
    assert result["success"] == 1
    assert len(result["errors"]) == 2
    assert "line 2" in result["errors"][0]["msg"]
    assert "maximum length" in result["errors"][0]["msg"]
    assert "line 3" in result["errors"][1]["msg"]
    assert "truncated" in result["errors"][1]["msg"]


def test_ingest_committed_batches_stay_visible_when_a_batch_fails() -> None:
    """Committed batches count; items of a failed batch do not."""
    client = RecordingBulkClient(fail_batch_ids={"bad"})
    ingester = make_ingester(client, chunk_size=2, max_in_flight=1)
    data = to_ndjson(make_item("ok-1"), make_item("ok-2"), make_item("bad"))

    result = run_ingest(ingester, data)

    assert result["received"] == 3
    # the first batch was committed, the second was not
    assert result["success"] == 2
    assert result["errors"] == [
        {
            "id": "bad",
            "msg": "line 3: batch was not committed "
            "(RuntimeError: backend is down)",
        }
    ]


def test_ingest_unexpected_client_result_is_not_counted() -> None:
    """A client returning something that is not a BulkTransaction commits nothing."""

    class WeirdClient(AsyncBaseBulkTransactionsClient):
        async def bulk_item_insert(self, items, **kwargs):
            return Response(status_code=200)

    ingester = make_ingester(WeirdClient(), chunk_size=2)
    result = run_ingest(ingester, to_ndjson(make_item("a"), make_item("b")))

    assert result["received"] == 2
    assert result["success"] == 0
    assert len(result["errors"]) == 2
    assert all("batch was not committed" in e["msg"] for e in result["errors"])


def test_ingest_limits_in_flight_batches() -> None:
    """No more than max_in_flight batches are uncommitted at any time."""
    client = RecordingBulkClient()
    ingester = make_ingester(client, chunk_size=1, max_in_flight=2)
    data = to_ndjson(*[make_item(f"item-{i}") for i in range(6)])

    result = run_ingest(ingester, data)

    assert result["success"] == 6
    assert len(client.batches) == 6
    assert client.max_in_flight_seen == 2


def test_ingest_sequential_when_max_in_flight_is_one() -> None:
    """max_in_flight=1 commits batches one at a time."""
    client = RecordingBulkClient()
    ingester = make_ingester(client, chunk_size=2, max_in_flight=1)
    data = to_ndjson(*[make_item(f"item-{i}") for i in range(5)])

    result = run_ingest(ingester, data)

    assert result["success"] == 5
    assert client.max_in_flight_seen == 1


def test_ingest_duplicate_ids_in_batch() -> None:
    """A duplicated id within one batch is rejected with its line number."""
    client = RecordingBulkClient()
    ingester = make_ingester(client, chunk_size=10)
    data = to_ndjson(make_item("a"), make_item("b"), make_item("a"))

    result = run_ingest(ingester, data)

    assert result["received"] == 3
    assert result["success"] == 2
    assert result["errors"][0]["id"] == "a"
    assert "line 3" in result["errors"][0]["msg"]
    assert "duplicate" in result["errors"][0]["msg"]


def test_ingest_errors_are_capped() -> None:
    """The number of collected errors is bounded."""
    client = RecordingBulkClient()
    ingester = make_ingester(client, chunk_size=10, max_errors=3)
    data = b"".join(b'{"no id"}\n' for _ in range(10))

    result = run_ingest(ingester, data)

    assert result["received"] == 10
    assert result["success"] == 0
    assert len(result["errors"]) == 4
    assert result["errors"][-1]["msg"] == "7 further error(s) omitted"


def test_ingest_sync_client() -> None:
    """Synchronous bulk clients are supported via the threadpool."""
    client = RecordingSyncBulkClient()
    ingester = make_ingester(client, chunk_size=2)
    data = to_ndjson(*[make_item(f"item-{i}") for i in range(3)])

    result = run_ingest(ingester, data)

    assert result == {"received": 3, "success": 3, "skipped": 0, "errors": []}
    assert [len(batch) for batch in client.batches] == [2, 1]


def test_ingester_validates_limits() -> None:
    """Batch and buffer limits must be positive."""
    client = RecordingBulkClient()
    with pytest.raises(ValueError):
        make_ingester(client, chunk_size=0)
    with pytest.raises(ValueError):
        make_ingester(client, max_in_flight=0)
    with pytest.raises(ValueError):
        make_ingester(client, max_line_bytes=0)


# --- ENDPOINT TESTS ---


class DummyCoreClient(BaseCoreClient):
    """Dummy core client for routing purposes."""

    def all_collections(self, *args, **kwargs):
        raise NotImplementedError

    def get_collection(self, *args, **kwargs):
        raise NotImplementedError

    def get_item(self, *args, **kwargs):
        raise NotImplementedError

    def get_search(self, *args, **kwargs):
        raise NotImplementedError

    def post_search(self, *args, **kwargs):
        raise NotImplementedError

    def item_collection(self, *args, **kwargs):
        raise NotImplementedError


@pytest.fixture
def bulk_client() -> RecordingBulkClient:
    return RecordingBulkClient()


@pytest.fixture
def client(bulk_client: RecordingBulkClient) -> Iterator[TestClient]:
    """TestClient with the streaming bulk transaction endpoint."""
    api = StacApi(
        settings=ApiSettings(),
        client=DummyCoreClient(),
        extensions=[
            BulkTransactionExtension(
                client=bulk_client,
                stream_chunk_size=2,
                stream_max_in_flight=1,
            ),
        ],
    )
    with TestClient(api.app) as test_client:
        yield test_client


def post_stream(client: TestClient, data: bytes, **kwargs):
    """POST an NDJSON body to the streaming endpoint."""
    return client.post(
        "/collections/a-collection/bulk_items/stream",
        content=data,
        headers={"content-type": "application/x-ndjson"},
        **kwargs,
    )


def test_stream_endpoint_commits_items(
    client: TestClient, bulk_client: RecordingBulkClient
) -> None:
    """Items are committed in bounded batches as the body streams in."""
    data = to_ndjson(*[make_item(f"item-{i}") for i in range(5)])

    response = post_stream(client, data)

    assert response.is_success, response.text
    assert response.json() == {
        "received": 5,
        "success": 5,
        "skipped": 0,
        "errors": [],
    }
    # stream_chunk_size=2 was applied
    assert [len(batch) for batch in bulk_client.batches] == [2, 2, 1]
    # the request is forwarded to the client, like the JSON endpoint does
    assert all(kwargs.get("request") is not None for kwargs in bulk_client.kwargs)


def test_stream_endpoint_upsert_method(
    client: TestClient, bulk_client: RecordingBulkClient
) -> None:
    """The insert/upsert method is taken from the query string."""
    response = post_stream(client, to_ndjson(make_item("a")), params={"method": "upsert"})

    assert response.is_success, response.text
    assert bulk_client.methods == ["upsert"]


def test_stream_endpoint_invalid_method(client: TestClient) -> None:
    """An unknown method is rejected."""
    response = post_stream(
        client, to_ndjson(make_item("a")), params={"method": "invalid"}
    )

    assert response.status_code == 400


def test_stream_endpoint_partial_errors(
    client: TestClient, bulk_client: RecordingBulkClient
) -> None:
    """Valid items are committed; bad lines are reported with line numbers."""
    data = b"\n".join(
        [
            json.dumps(make_item("ok")).encode(),
            b"not json",
            json.dumps(make_item("alien", collection="other")).encode(),
        ]
    ) + b"\n"

    response = post_stream(client, data)

    assert response.is_success, response.text
    body = response.json()
    assert body["received"] == 3
    assert body["success"] == 1
    assert len(body["errors"]) == 2
    assert "line 2" in body["errors"][0]["msg"]
    assert "line 3" in body["errors"][1]["msg"]
    assert [item["id"] for batch in bulk_client.batches for item in batch] == ["ok"]


def test_stream_endpoint_empty_body(client: TestClient) -> None:
    """An empty stream commits nothing."""
    response = post_stream(client, b"")

    assert response.is_success, response.text
    assert response.json() == {
        "received": 0,
        "success": 0,
        "skipped": 0,
        "errors": [],
    }


def test_stream_endpoint_route_dependencies() -> None:
    """Route dependencies are applied to the streaming route."""
    api = StacApi(
        settings=ApiSettings(),
        client=DummyCoreClient(),
        extensions=[
            BulkTransactionExtension(
                client=RecordingBulkClient(),
                route_dependencies=[Depends(must_be_bob)],
            ),
        ],
    )
    with TestClient(api.app) as test_client:
        unauthenticated_response = post_stream(test_client, to_ndjson(make_item("a")))
        assert unauthenticated_response.status_code == 401

        authenticated_response = test_client.post(
            "/collections/a-collection/bulk_items/stream",
            content=to_ndjson(make_item("a")),
            headers={"content-type": "application/x-ndjson"},
            auth=("bob", "dobbs"),
        )
        assert authenticated_response.is_success, authenticated_response.text
        assert authenticated_response.json()["success"] == 1


def test_stream_endpoint_disabled() -> None:
    """The streaming route can be turned off."""
    api = StacApi(
        settings=ApiSettings(),
        client=DummyCoreClient(),
        extensions=[
            BulkTransactionExtension(
                client=RecordingBulkClient(), enable_streaming=False
            ),
        ],
    )
    with TestClient(api.app) as test_client:
        response = post_stream(test_client, to_ndjson(make_item("a")))
        assert response.status_code == 404

        # the classic JSON endpoint still works
        response = test_client.post(
            "/collections/a-collection/bulk_items",
            json={"items": {"a": make_item("a")}, "method": "insert"},
        )
        assert response.is_success, response.text


def must_be_bob(
    credentials: security.HTTPBasicCredentials = Depends(security.HTTPBasic()),
) -> bool:
    """Require HTTP basic auth for user bob."""
    if credentials.username == "bob":
        return True

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="You're not Bob",
        headers={"WWW-Authenticate": "Basic"},
    )
