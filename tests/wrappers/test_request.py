from __future__ import annotations

import asyncio
import dataclasses
from urllib.parse import urlencode

import pytest
from hypercorn.typing import HTTPScope
from werkzeug.datastructures import Headers
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.exceptions import RequestTimeout

from quart.testing import no_op_push
from quart.wrappers.base import ClientDisconnectedError
from quart.wrappers.request import Body
from quart.wrappers.request import BodyReadStats
from quart.wrappers.request import Request


async def _fill_body(body: Body, semaphore: asyncio.Semaphore, limit: int) -> None:
    for number in range(limit):
        await body.put(b"%d" % number)
        await semaphore.acquire()
    body.set_complete()


async def test_full_body() -> None:
    body = Body(None, None)
    limit = 3
    semaphore = asyncio.Semaphore(limit)
    asyncio.ensure_future(_fill_body(body, semaphore, limit))
    assert b"012" == await body


async def test_body_streaming() -> None:
    body = Body(None, None)
    limit = 3
    semaphore = asyncio.Semaphore(0)
    asyncio.ensure_future(_fill_body(body, semaphore, limit))
    index = 0
    result = []
    async for data in body:
        semaphore.release()
        result.append(data)
        index += 1
    assert result == [b"0", b"1", b"2", b""]
    assert b"" == await body


async def test_body_stream_single_chunk() -> None:
    body = Body(None, None)
    body.set_result(b"data")

    async def _check_data() -> None:
        async for data in body:
            assert data == b"data"

    await asyncio.wait_for(_check_data(), 1)


async def test_body_streaming_no_data() -> None:
    body = Body(None, None)
    semaphore = asyncio.Semaphore(0)
    asyncio.ensure_future(_fill_body(body, semaphore, 0))
    async for data in body:
        assert data == b""
    assert b"" == await body


async def test_body_exceeds_max_content_length() -> None:
    max_content_length = 5
    body = Body(None, max_content_length)
    semaphore = asyncio.Semaphore(max_content_length + 1)
    asyncio.ensure_future(_fill_body(body, semaphore, max_content_length + 1))
    with pytest.raises(RequestEntityTooLarge):
        await body


async def test_body_read_stats_disabled_by_default() -> None:
    body = Body(None, None)
    assert body.read_stats is None
    await body.put(b"abc")
    body.set_complete()
    assert body.read_stats is None


async def test_body_read_stats_chunked_reading() -> None:
    body = Body(None, None, enable_read_stats=True)
    assert body.read_stats == BodyReadStats(0, 0, "receiving")

    limit = 3
    semaphore = asyncio.Semaphore(0)
    asyncio.ensure_future(_fill_body(body, semaphore, limit))
    chunks = []
    async for data in body:
        chunks.append(data)
        semaphore.release()

    assert chunks == [b"0", b"1", b"2", b""]
    assert body.read_stats == BodyReadStats(3, 3, "completed")


async def test_body_read_stats_snapshot_is_read_only() -> None:
    body = Body(None, None, enable_read_stats=True)
    await body.put(b"abc")

    snapshot = body.read_stats
    assert snapshot == BodyReadStats(3, 1, "receiving")

    # The snapshot must be immutable
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.bytes_received = 99  # type: ignore[misc]

    # Each snapshot is an independent copy
    assert body.read_stats is not snapshot
    assert body.read_stats == snapshot

    body.set_complete()
    assert body.read_stats == BodyReadStats(3, 1, "completed")
    assert snapshot.status == "receiving"

    # Only metadata is exposed, never body contents
    assert {field.name for field in dataclasses.fields(BodyReadStats)} == {
        "bytes_received",
        "chunks_received",
        "status",
    }


async def test_body_read_stats_empty_body() -> None:
    body = Body(None, None, enable_read_stats=True)
    body.set_complete()
    assert body.read_stats == BodyReadStats(0, 0, "completed")
    assert await body == b""


async def test_body_read_stats_empty_chunks_not_counted() -> None:
    body = Body(None, None, enable_read_stats=True)
    await body.put(b"")
    body.set_complete()
    assert body.read_stats == BodyReadStats(0, 0, "completed")
    assert await body == b""


async def test_body_read_stats_too_large_declared_content_length() -> None:
    body = Body(expected_content_length=6, max_content_length=5, enable_read_stats=True)
    assert body.read_stats == BodyReadStats(0, 0, "too_large")
    with pytest.raises(RequestEntityTooLarge):
        await body
    assert body.read_stats.status == "too_large"


async def test_body_read_stats_too_large_while_receiving() -> None:
    body = Body(None, max_content_length=5, enable_read_stats=True)
    await body.put(b"abc")
    assert body.read_stats == BodyReadStats(3, 1, "receiving")
    await body.put(b"def")
    assert body.read_stats == BodyReadStats(6, 2, "too_large")

    # The terminal status is sticky, and the limit is still enforced
    body.set_complete()
    assert body.read_stats.status == "too_large"
    with pytest.raises(RequestEntityTooLarge):
        await body
    assert body.read_stats.status == "too_large"


async def test_body_read_stats_cancelled_on_disconnect() -> None:
    body = Body(None, None, enable_read_stats=True)
    await body.put(b"abc")
    body.disconnect()
    assert body.read_stats == BodyReadStats(3, 1, "cancelled")
    # The already delivered chunk can still be read, the disconnect
    # surfaces on the next read attempt
    assert await body.get() == b"abc"
    with pytest.raises(ClientDisconnectedError):
        await body.get()


async def test_body_read_stats_completed_status_sticky() -> None:
    body = Body(None, None, enable_read_stats=True)
    body.set_complete()
    body.disconnect()  # A late disconnect must not overwrite completion
    assert body.read_stats == BodyReadStats(0, 0, "completed")


async def test_request_exceeds_max_content_length(http_scope: HTTPScope) -> None:
    max_content_length = 5
    headers = Headers()
    headers["Content-Length"] = str(max_content_length + 1)
    request = Request(
        "POST",
        "http",
        "/",
        b"",
        headers,
        "",
        "1.1",
        http_scope,
        max_content_length=max_content_length,
        send_push_promise=no_op_push,
    )
    with pytest.raises(RequestEntityTooLarge):
        await request.get_data()


async def test_request_body_read_stats(http_scope: HTTPScope) -> None:
    request = Request(
        "POST",
        "http",
        "/",
        b"",
        Headers(),
        "",
        "1.1",
        http_scope,
        body_read_stats=True,
        send_push_promise=no_op_push,
    )
    request.body.set_result(b"abc")
    assert request.body_read_stats == BodyReadStats(3, 1, "completed")
    assert await request.get_data() == b"abc"


async def test_request_body_read_stats_disabled(http_scope: HTTPScope) -> None:
    request = Request(
        "POST",
        "http",
        "/",
        b"",
        Headers(),
        "",
        "1.1",
        http_scope,
        send_push_promise=no_op_push,
    )
    request.body.set_result(b"abc")
    assert request.body_read_stats is None


async def test_request_get_data_timeout(http_scope: HTTPScope) -> None:
    request = Request(
        "POST",
        "http",
        "/",
        b"",
        Headers(),
        "",
        "1.1",
        http_scope,
        body_timeout=1,
        send_push_promise=no_op_push,
    )
    with pytest.raises(RequestTimeout):
        await request.get_data()


@pytest.mark.parametrize(
    "method, expected",
    [("GET", ["b", "c"]), ("POST", ["b", "c", "d"])],
)
async def test_request_values(
    method: str, expected: list[str], http_scope: HTTPScope
) -> None:
    request = Request(
        method,
        "http",
        "/",
        b"a=b&a=c",
        Headers(
            {"host": "quart.com", "Content-Type": "application/x-www-form-urlencoded"}
        ),
        "",
        "1.1",
        http_scope,
        send_push_promise=no_op_push,
    )
    request.body.set_result(urlencode({"a": "d"}).encode())
    assert (await request.values).getlist("a") == expected


async def test_request_send_push_promise(http_scope: HTTPScope) -> None:
    push_promise: tuple[str, Headers] = None

    async def _push(path: str, headers: Headers) -> None:
        nonlocal push_promise
        push_promise = (path, headers)

    request = Request(
        "GET",
        "http",
        "/",
        b"a=b&a=c",
        Headers(
            {
                "host": "quart.com",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "*/*",
                "Accept-Encoding": "gzip",
                "User-Agent": "quart",
            }
        ),
        "",
        "2",
        http_scope,
        send_push_promise=_push,
    )
    await request.send_push_promise("/")
    assert push_promise[0] == "/"
    valid_headers = {"Accept": "*/*", "Accept-Encoding": "gzip", "User-Agent": "quart"}
    assert len(push_promise[1]) == len(valid_headers)
    for name, value in valid_headers.items():
        assert push_promise[1][name] == value
