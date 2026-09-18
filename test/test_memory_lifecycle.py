"""Offline regressions for request retention, cancellation and memory accounting."""
import asyncio
import gc
import json
import sys
import weakref
from types import SimpleNamespace

import httpx
import pytest
from fastapi import BackgroundTasks, HTTPException
from starlette.requests import Request

from core.dialects.registry import EndpointDefinition
from core.dialects.router import _create_generic_handler
from core.metrics import _extract_pool_info, get_request_metrics
from core.middleware import StatsMiddleware, request_info
from core.passthrough import _fetch_passthrough_stream, _passthrough_error_wrapper
from core.response import check_response
from core.stream_pipeline import error_handling_wrapper, iter_sse_with_keepalive
from core.streaming import LoggingStreamingResponse


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_heartbeat", [False, True])
async def test_cancelled_keepalive_owns_its_read_task(initial_heartbeat):
    started, closed, io_ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
    before = asyncio.all_tasks()

    async def source():
        try:
            started.set()
            await io_ready.wait()
            yield "unused"
        finally:
            # Cleanup itself must be allowed to await.
            await asyncio.sleep(0)
            closed.set()

    gen = source()
    stream = iter_sse_with_keepalive(gen, 0.001 if initial_heartbeat else 60)
    outer = None
    try:
        if initial_heartbeat:
            assert await anext(stream) == ": keepalive\n\n"
            await stream.aclose()
        else:
            outer = asyncio.create_task(anext(stream))
            await started.wait()
            outer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await outer
        assert closed.is_set()
        assert not [t for t in asyncio.all_tasks() - before if not t.done()]
    finally:
        pending = [t for t in asyncio.all_tasks() - before if not t.done()]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await stream.aclose()
        await gen.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["pump", "converted", "passthrough"])
@pytest.mark.parametrize("interval", [None, 0.01])
async def test_closing_outer_stream_closes_source_after_first_chunk(kind, interval):
    if kind == "pump" and interval is None:
        return
    closed = asyncio.Event()

    async def source():
        try:
            yield 'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            await asyncio.sleep(3600)
        finally:
            closed.set()

    gen = source()
    if kind == "pump":
        stream = iter_sse_with_keepalive(gen, interval)
    elif kind == "converted":
        stream, _ = await error_handling_wrapper(gen, "test", "openai", True, [], keepalive_interval=interval)
    else:
        stream, _ = await _passthrough_error_wrapper(gen, "test", keepalive_interval=interval)
    try:
        await anext(stream)
        await stream.aclose()
        assert closed.is_set()
    finally:
        await gen.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["converted", "passthrough"])
async def test_rejected_first_chunk_closes_source(kind):
    closed = asyncio.Event()

    async def source():
        try:
            yield {"error": "failed", "status_code": 503, "details": "unavailable"}
        finally:
            closed.set()

    gen = source()
    try:
        with pytest.raises(HTTPException):
            if kind == "converted":
                await error_handling_wrapper(gen, "test", "openai", True, [])
            else:
                await _passthrough_error_wrapper(gen, "test")
        assert closed.is_set()
    finally:
        await gen.aclose()


class CaptureResponse:
    status_code = 200
    headers = {}

    async def aiter_bytes(self, chunk_size=None):
        yield b"a" * (2 * 1024 * 1024)

    async def aiter_text(self, chunk_size=None):
        yield "中" * (1024 * 1024)

    async def aread(self):
        return b'{"ok":true}'


@pytest.mark.asyncio
async def test_log_wrappers_do_not_create_response_reference_cycle():
    token = request_info.set({"raw_data_expires_at": True})
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        response = CaptureResponse()
        await check_response(response, "test")
        ref = weakref.ref(response)
        del response
        assert ref() is None  # No gc.collect(): refcount alone must release it.
    finally:
        if was_enabled:
            gc.enable()
        request_info.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["aiter_bytes", "aiter_text"])
async def test_upstream_capture_has_strict_byte_limit(monkeypatch, method):
    saved = []
    monkeypatch.setattr("core.response.truncate_for_logging", lambda value: saved.append(value) or "saved")
    token = request_info.set({"raw_data_expires_at": True})
    try:
        response = CaptureResponse()
        await check_response(response, "test")
        size = 0
        async for chunk in getattr(response, method)():
            size += len(chunk)
        assert size >= 1024 * 1024  # The client output must not be truncated.
        assert saved
        assert all(len(v.encode() if isinstance(v, str) else v) <= 100 * 1024 for v in saved)
    finally:
        request_info.reset(token)


@pytest.mark.asyncio
async def test_wrapped_httpx_text_preserves_chunk_size_and_utf8():
    token = request_info.set({"raw_data_expires_at": True})
    try:
        response = httpx.Response(200, content="中文内容".encode())
        await check_response(response, "test")
        await check_response(response, "test")  # Repeated checking is idempotent.
        assert "".join([c async for c in response.aiter_text(chunk_size=1)]) == "中文内容"
        assert await response.aread() == "中文内容".encode()
    finally:
        request_info.reset(token)


@pytest.mark.asyncio
async def test_downstream_capture_does_not_retain_oversized_chunk(monkeypatch):
    saved = []
    monkeypatch.setattr("core.streaming.truncate_for_logging", lambda value: saved.append(value) or "saved")

    async def source():
        yield b"x" * (2 * 1024 * 1024)

    response = LoggingStreamingResponse(source(), current_info={"raw_data_expires_at": True, "adapter_metrics_managed": True, "completion_tokens": 1})
    monkeypatch.setattr("core.streaming.enqueue_stats", lambda *args, **kwargs: None)
    size = 0

    async def send(message):
        nonlocal size
        size += len(message.get("body", b""))

    async def receive():
        return {"type": "http.disconnect"}

    await response({}, receive, send)
    assert size == 2 * 1024 * 1024
    assert len(saved[0]) <= 100 * 1024


@pytest.mark.asyncio
async def test_passthrough_respects_read_timeout_and_sends_bytes():
    captured = {}

    async def upstream(request):
        captured.update(request.extensions["timeout"])
        return httpx.Response(200, content=b'data: {}\n\n')

    class InspectClient(httpx.AsyncClient):
        def stream(self, *args, **kwargs):
            assert isinstance(kwargs["content"], bytes)
            return super().stream(*args, **kwargs)

    async with InspectClient(transport=httpx.MockTransport(upstream)) as client:
        output = [c async for c in _fetch_passthrough_stream(client, "https://test.invalid", {}, {"text": "中文"}, 17)]
    assert output == ['data: {}\n\n']
    assert captured["read"] == 17
    assert captured["pool"] == 10


def test_pool_metrics_call_httpcore_predicates_and_count_proxy_mounts():
    class Conn:
        def is_closed(self):
            return False

        def is_idle(self):
            return False

    transport = SimpleNamespace(_pool=SimpleNamespace(_connections=[Conn()]))
    direct = SimpleNamespace(_pool=SimpleNamespace(_connections=[]))
    client = SimpleNamespace(_transport=direct, _mounts={"https": transport, "http": transport})
    result = _extract_pool_info(client)
    assert result["active_connections"] == 1
    assert result["closed_connections"] == 0


@pytest.mark.asyncio
async def test_dialect_requests_are_counted_during_body_read_and_reuse_json(monkeypatch):
    ready = asyncio.Event()
    ready.set()
    monkeypatch.setitem(sys.modules, "main", SimpleNamespace(_db_ready=ready))
    baseline = get_request_metrics()["active_requests"]
    payload = {"model": "test-model", "messages": [{"role": "user", "content": "hi"}], "stream": True}
    parse_results = []
    original_loads = json.loads

    def parse_once(body):
        parsed = original_loads(body)
        parse_results.append(parsed)
        return parsed

    monkeypatch.setattr("core.middleware.json_loads", parse_once)

    class Handler:
        async def request_model(self, **kwargs):
            assert kwargs["original_payload"] is parse_results[0]
            async def chunks():
                yield 'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            return LoggingStreamingResponse(chunks(), media_type="text/event-stream")

    monkeypatch.setattr("routes.deps.get_model_handler", lambda: Handler())
    route = _create_generic_handler("openai", EndpointDefinition(path="/v1/chat/completions", methods=["POST"]))
    fake_app = SimpleNamespace(state=SimpleNamespace(config={"preferences": {"log_raw_data_retention_hours": 0}}))

    async def app(scope, receive, send):
        assert get_request_metrics()["active_requests"] == baseline + 1
        response = await route(Request(scope, receive), BackgroundTasks(), api_index=0)
        await response.close()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def receive():
        assert get_request_metrics()["active_requests"] == baseline + 1
        return {"type": "http.request", "body": json.dumps(payload).encode(), "more_body": False}

    async def send(message):
        pass

    middleware = StatsMiddleware(app)
    middleware._dialect_prefixes = ["/v1"]
    scope = {"type": "http", "path": "/v1/chat/completions", "method": "POST", "headers": [(b"content-type", b"application/json")], "app": fake_app, "query_string": b""}
    try:
        await middleware(scope, receive, send)
    finally:
        assert get_request_metrics()["active_requests"] == baseline


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("preloaded", [False, True])
async def test_closed_httpx_response_releases_large_request_without_gc(streaming, preloaded):
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'hello'

    async def upstream(request):
        return httpx.Response(200, content=b'hello') if preloaded else httpx.Response(200, stream=Stream())

    token = request_info.set({"raw_data_expires_at": True})
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            if streaming:
                async with client.stream("POST", "https://test.invalid", content=b'x' * 1048576) as response:
                    await check_response(response, "test")
                    assert b''.join([c async for c in response.aiter_bytes()]) == b'hello'
            else:
                response = await client.post("https://test.invalid", content=b'x' * 1048576)
                await check_response(response, "test")
                assert await response.aread() == b'hello'
            # Retain public semantics: the request body and response are accessible until released.
            assert len(response.request.content) == 1048576
            if not preloaded:
                assert response.elapsed.total_seconds() >= 0
            ref = weakref.ref(response)
            del response
            assert ref() is None
    finally:
        request_info.reset(token)
        if was_enabled:
            gc.enable()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_at", ["headers", "body"])
async def test_asgi_cancel_scope_cannot_skip_response_cleanup(monkeypatch, cancel_at):
    import anyio
    closed, entered = asyncio.Event(), asyncio.Event()
    records = []
    monkeypatch.setattr("core.streaming.enqueue_stats", lambda info, **kw: records.append(dict(info)))

    async def source():
        try:
            yield b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
            await asyncio.sleep(3600)
        finally:
            await asyncio.sleep(0)
            closed.set()

    gen = source()
    # The channel's initial read happens before the ASGI response is invoked.
    first = await anext(gen)
    response = LoggingStreamingResponse(gen, current_info={"raw_data_expires_at": True})

    async def send(message):
        if (cancel_at == "headers" and message["type"] == "http.response.start") or (cancel_at == "body" and message["type"] == "http.response.body"):
            entered.set()
            await asyncio.sleep(3600)

    async def receive():
        return {"type": "http.disconnect"}

    if cancel_at == "body":
        # Source must have another chunk available to reach the downstream send.
        async def forwarded():
            try:
                yield first
                async for item in gen:
                    yield item
            finally:
                await gen.aclose()
        response.body_iterator = forwarded()
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(response, {}, receive, send)
            await asyncio.wait_for(entered.wait(), 1)
            tg.cancel_scope.cancel()
        assert closed.is_set()
        assert response.body_iterator is None
        assert records
        # send 未完成便取消，不能把尚未发送成功的正文记成客户端已收到。
        assert "response_body" not in records[0]
        assert records[0]["status_code"] == 499
    finally:
        await gen.aclose()


@pytest.mark.asyncio
async def test_cancel_during_inbound_body_balances_metrics(monkeypatch):
    ready = asyncio.Event()
    ready.set()
    monkeypatch.setitem(sys.modules, "main", SimpleNamespace(_db_ready=ready))
    baseline = get_request_metrics()["active_requests"]
    started = asyncio.Event()

    async def app(*args):
        pytest.fail("Incomplete request must not reach routing")

    async def receive():
        started.set()
        await asyncio.sleep(3600)

    async def send(message):
        pass

    m = StatsMiddleware(app)
    m._dialect_prefixes = ["/v1"]
    fake = SimpleNamespace(state=SimpleNamespace(config={}))
    scope = {"type": "http", "path": "/v1/chat/completions", "method": "POST", "headers": [(b"content-type", b"application/json")], "app": fake}
    task = asyncio.create_task(m(scope, receive, send))
    await started.wait()
    assert get_request_metrics()["active_requests"] == baseline + 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert get_request_metrics()["active_requests"] == baseline
    assert "_zoaholic_parsed_json" not in scope


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["converted", "passthrough"])
@pytest.mark.parametrize("first_is_slow", [False, True])
async def test_close_response_before_iteration_cleans_prefetched_source(kind, first_is_slow):
    closed = asyncio.Event()

    async def source():
        try:
            if first_is_slow:
                await asyncio.sleep(3600)
            yield 'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
        finally:
            await asyncio.sleep(0)
            closed.set()

    gen = source()
    if kind == "converted":
        stream, _ = await error_handling_wrapper(gen, "test", "openai", True, [], keepalive_interval=0.001)
    else:
        stream, _ = await _passthrough_error_wrapper(gen, "test", keepalive_interval=0.001)
    response = LoggingStreamingResponse(stream)
    try:
        await response.close()
        assert closed.is_set()
        await response.close()
    finally:
        await gen.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["converted", "passthrough"])
async def test_cancelled_asgi_response_closes_httpx_transport(monkeypatch, kind):
    import anyio
    from core.response import fetch_response_stream

    closed, reading = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr("core.streaming.enqueue_stats", lambda *args, **kw: None)

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            reading.set()
            await asyncio.sleep(3600)

        async def aclose(self):
            await asyncio.sleep(0)
            closed.set()

    async def upstream(request):
        return httpx.Response(200, stream=Stream())

    async def adapter(client, url, headers, payload, model, timeout):
        async with client.stream("POST", url, content=b'{}') as response:
            await check_response(response, "test")
            async for item in response.aiter_text():
                yield item

    monkeypatch.setattr("core.channels.get_channel", lambda engine: SimpleNamespace(stream_adapter=adapter))
    info = {"raw_data_expires_at": True}
    token = request_info.set(info)
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            if kind == "converted":
                source = fetch_response_stream(client, "https://test.invalid", {}, {}, "test", "test", 60)
                stream, _ = await error_handling_wrapper(source, "test", "openai", True, [], keepalive_interval=60)
            else:
                source = _fetch_passthrough_stream(client, "https://test.invalid", {}, {}, 60)
                stream, _ = await _passthrough_error_wrapper(source, "test", 60)
            response = LoggingStreamingResponse(stream, current_info=info)

            async def send(message):
                await asyncio.sleep(0)

            async def receive():
                return {"type": "http.disconnect"}

            async with anyio.create_task_group() as tg:
                tg.start_soon(response, {}, receive, send)
                await asyncio.wait_for(reading.wait(), 1)
                tg.cancel_scope.cancel()
            assert closed.is_set()
            assert "ok" in info["upstream_response_body"]
    finally:
        request_info.reset(token)


@pytest.mark.asyncio
async def test_http_stream_close_keeps_redirect_history_and_headers():
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'final'

    async def upstream(request):
        if request.url.path == "/first":
            return httpx.Response(307, headers={"location": "/last"}, stream=Stream())
        assert request.content == b'body'
        return httpx.Response(200, headers={"x-trace": "ok"}, stream=Stream())

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream), follow_redirects=True) as client:
        response = await client.post("https://test.invalid/first", content=b'body')
        assert response.content == b'final'
        assert response.headers["x-trace"] == "ok"
        assert response.history[0].status_code == 307
        assert response.history[0].request.content == b'body'
        await response.aclose()
        await response.aclose()
