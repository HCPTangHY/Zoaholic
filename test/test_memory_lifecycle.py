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
    fake_app = SimpleNamespace(state=SimpleNamespace(admin_initialized=True, api_list=['initialized-test-key'], config={"preferences": {"log_raw_data_retention_hours": 0}}))

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
    fake = SimpleNamespace(state=SimpleNamespace(admin_initialized=True, api_list=['initialized-test-key'], config={}))
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


@pytest.mark.asyncio
async def test_passthrough_shell_clone_shares_large_content_and_isolates_channel_mutations():
    from core.channels.claude_channel import patch_passthrough_claude_payload
    from core.channels.gemini_channel import patch_passthrough_gemini_payload
    from core.channels.vertex_channel import patch_passthrough_vertex_gemini_payload
    from core.dialects.passthrough import apply_passthrough_modifications
    from core.models import Message, RequestModel

    large_user_text = "U" * (256 * 1024)
    large_b64 = "B" * (512 * 1024)
    inline_obj = {"mimeType": "image/png", "data": large_b64}
    original_gemini = {
        "systemInstruction": {"parts": [{"text": "base system"}]},
        "contents": [
            {"parts": [{"text": large_user_text}, {"inlineData": inline_obj}]},
            {"role": "system", "parts": [{"text": "extra instruction"}]},
            {"role": "model", "parts": [{"functionCall": {"name": "lookup", "args": {"q": "a"}}}]},
        ],
        "safetySettings": [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "OFF"},
            {"category": "HARM_CATEGORY_IMAGE_HATE", "threshold": "OFF"},
        ],
        "generationConfig": {"temperature": 0.7},
    }
    req_stub = RequestModel.model_construct(
        model="gemini-test",
        messages=[Message.model_construct(role="user", content="")],
        stream=True,
    )

    # Attempt 1: AI Studio passthrough mutates roles, trailing model turn, thoughtSignature, safetySettings, overrides
    ais_payload = apply_passthrough_modifications(
        original_gemini,
        {"overrides": {"all": {"generationConfig": {"thinkingConfig": {"includeThoughts": True}}}}},
        "gemini",
        "gemini-test",
        "gemini-test",
    )
    ais_payload = await patch_passthrough_gemini_payload(
        ais_payload, {}, req_stub, "gemini", {"provider": "ais", "engine": "gemini"}
    )

    assert ais_payload["contents"][0]["parts"][0]["text"] is large_user_text
    assert ais_payload["contents"][0]["parts"][1]["inlineData"] is inline_obj
    assert ais_payload["contents"][0]["parts"][1]["inlineData"]["data"] is large_b64
    assert ais_payload["contents"][-1]["role"] == "user"
    assert ais_payload["contents"][2]["parts"][0]["thoughtSignature"] == "skip_thought_signature_validator"

    # Original payload must remain completely untouched for Attempt 2 (Vertex)
    assert "role" not in original_gemini["contents"][0]
    assert original_gemini["contents"][1]["role"] == "system"
    assert len(original_gemini["contents"]) == 3
    assert "thoughtSignature" not in original_gemini["contents"][2]["parts"][0]
    assert "thinkingConfig" not in original_gemini["generationConfig"]
    assert len(original_gemini["safetySettings"]) == 2

    vertex_payload = apply_passthrough_modifications(original_gemini, {}, "gemini", "gemini-test", "gemini-test")
    vertex_payload = await patch_passthrough_vertex_gemini_payload(
        vertex_payload, {}, req_stub, "vertex-gemini", {"provider": "vtx", "engine": "vertex-gemini"}
    )
    assert vertex_payload["contents"][0]["role"] == "user"
    assert vertex_payload["contents"][0]["parts"][0]["text"] is large_user_text
    assert "role" not in original_gemini["contents"][0]

    # Claude passthrough system block mutation isolation
    large_claude_msg = "C" * (256 * 1024)
    original_claude = {
        "model": "claude-sonnet-4-5",
        "system": [{"type": "text", "text": "client sys"}],
        "messages": [{"role": "user", "content": [{"type": "text", "text": large_claude_msg}]}],
    }
    c_payload = apply_passthrough_modifications(
        original_claude, {"system_prompt": "channel sys"}, "claude", "claude-sonnet-4-5", "claude-sonnet-4-5"
    )
    c_payload = await patch_passthrough_claude_payload(
        c_payload, {"system_prompt": "channel sys"}, req_stub, "claude", {"provider": "cc"}
    )
    assert c_payload["messages"][0]["content"][0]["text"] is large_claude_msg
    assert c_payload["system"][0]["text"].startswith("channel sys")
    assert original_claude["system"][0]["text"] == "client sys"


@pytest.mark.asyncio
async def test_dialect_passthrough_skips_canonical_parse_with_request_plugin_and_materializes_once_on_fallback(monkeypatch):
    from core.channel_manager import ChannelManager
    from core.channels.registry import get_channel
    from core.dialects import gemini as gemini_dialect_mod
    from core.dialects.registry import get_dialect, register_dialect
    from core.handler import ModelRequestHandler
    from core.plugins.interceptors import register_request_interceptor, unregister_request_interceptor

    orig_dialect = get_dialect("gemini")
    if orig_dialect is None:
        gemini_dialect_mod.register()
        orig_dialect = get_dialect("gemini")
    parse_calls = 0
    orig_parse = gemini_dialect_mod.parse_gemini_request

    async def counting_parse(native_body, path_params, headers):
        nonlocal parse_calls
        parse_calls += 1
        return await orig_parse(native_body, path_params, headers)

    monkeypatch.setattr(gemini_dialect_mod, "parse_gemini_request", counting_parse)
    register_dialect(
        gemini_dialect_mod.DialectDefinition(
            id="gemini",
            name="Google Gemini",
            description="test",
            parse_request=counting_parse,
            render_response=gemini_dialect_mod.render_gemini_response,
            render_stream=gemini_dialect_mod.render_gemini_stream,
            parse_usage=gemini_dialect_mod.parse_gemini_usage,
            target_engine="gemini",
            endpoints=[],
        ),
        overwrite=True,
    )

    plugin_seen = []

    async def req_plugin(request, engine, provider, api_key, url, headers, payload):
        plugin_seen.append((provider["provider"], getattr(request, "model", ""), isinstance(payload, dict)))
        return url, headers, payload

    register_request_interceptor(
        "test_mem_req_plugin", req_plugin, priority=50, plugin_name="test_mem_plugin", overwrite=True
    )

    passthrough_calls = 0
    non_passthrough_messages = []

    async def fake_passthrough(request_data, provider, *args, **kwargs):
        nonlocal passthrough_calls
        passthrough_calls += 1
        from core.plugins.interceptors import apply_request_interceptors
        await apply_request_interceptors(
            request_data, provider["engine"], provider, "k1", "https://u", {}, kwargs["passthrough_ctx"].original_payload,
            provider.get("preferences", {}).get("enabled_plugins"),
        )
        if provider["provider"].endswith("-fail"):
            raise HTTPException(status_code=429, detail="rate limit")
        return LoggingStreamingResponse(iter(()), media_type="text/event-stream")

    async def fake_process_request(request_data, provider, *args, **kwargs):
        non_passthrough_messages.append([m.content for m in request_data.messages])
        if provider["provider"].endswith("-fail"):
            raise HTTPException(status_code=502, detail="bad gateway")
        return LoggingStreamingResponse(iter(()), media_type="text/event-stream")

    monkeypatch.setattr("core.handler.process_request_passthrough", fake_passthrough)
    monkeypatch.setattr("core.handler.process_request", fake_process_request)

    class DummyRateLimiter:
        async def next(self, model):
            return "zk-test"

    fake_app = SimpleNamespace(
        state=SimpleNamespace(
            admin_initialized=True,
            api_list=["zk-test"],
            api_keys_db=[{"api": "zk-test", "model": ["all"], "role": "user"}],
            user_api_keys_rate_limit={"zk-test": DummyRateLimiter()},
            provider_timeouts={"global": {"default": 60}},
            keepalive_interval={"global": {"default": 15}},
            stream_header_timeouts={"global": {"default": 30}},
            error_triggers=[],
            channel_manager=ChannelManager(cooldown_period=0),
            config={
                "preferences": {"SCHEDULING_ALGORITHM": "fixed_priority"},
                "api_keys": [{"api": "zk-test", "model": ["all"], "preferences": {"AUTO_RETRY": True}}],
                "providers": [],
            },
        )
    )
    info = {"request_id": "r1", "api_key": "zk-test"}
    req_handler = ModelRequestHandler(fake_app, lambda: info, lambda *a, **kw: None)
    monkeypatch.setattr("routes.deps.get_model_handler", lambda: req_handler)
    monkeypatch.setattr("core.dialects.router._create_dialect_verify_api_key", lambda dialect_id: (lambda: 0))

    route = _create_generic_handler(
        "gemini",
        EndpointDefinition(path="/v1beta/models/{model}:streamGenerateContent", methods=["POST"]),
    )

    body_dict = {"contents": [{"role": "user", "parts": [{"text": "hello passthrough"}]}]}
    raw_bytes = json.dumps(body_dict).encode("utf-8")

    try:
        # Case 1: Pure passthrough WITH channel request_interceptor plugin -> 0 full canonical parses
        fake_app.state.config["providers"] = [
            {
                "provider": "ais-1",
                "engine": "gemini",
                "enabled": True,
                "model": ["gemini-2.5-pro"],
                "_model_dict_cache": {"gemini-2.5-pro": "gemini-2.5-pro"},
                "preferences": {"enabled_plugins": ["test_mem_plugin"]},
            }
        ]
        assert get_channel("gemini") is not None

        async def receive_1():
            return {"type": "http.request", "body": raw_bytes, "more_body": False}

        scope_1 = {
            "type": "http",
            "path": "/v1beta/models/gemini-2.5-pro:streamGenerateContent",
            "path_params": {"model": "gemini-2.5-pro"},
            "method": "POST",
            "headers": [(b"content-type", b"application/json")],
            "query_string": b"alt=sse",
            "app": fake_app,
            "_zoaholic_parsed_json": body_dict,
        }
        req_1 = Request(scope_1, receive_1)
        resp_1 = await route(req_1, BackgroundTasks(), api_index=0)
        assert resp_1.status_code == 200
        assert parse_calls == 0
        assert plugin_seen == [("ais-1", "gemini-2.5-pro", True)]
        assert getattr(req_1, "_body", b"") == b""
        assert not hasattr(req_1, "_json")

        # Case 2: Passthrough 429 -> fallback to two non-passthrough providers -> materialized ONCE
        fake_app.state.config["providers"] = [
            {
                "provider": "ais-fail",
                "engine": "gemini",
                "enabled": True,
                "_fail": True,
                "model": ["gemini-2.5-pro"],
                "_model_dict_cache": {"gemini-2.5-pro": "gemini-2.5-pro"},
                "preferences": {"enabled_plugins": ["test_mem_plugin"]},
            },
            {
                "provider": "oai-fail",
                "engine": "openai",
                "enabled": True,
                "_fail": True,
                "model": ["gemini-2.5-pro"],
                "_model_dict_cache": {"gemini-2.5-pro": "gemini-2.5-pro"},
            },
            {
                "provider": "oai-ok",
                "engine": "openai",
                "enabled": True,
                "model": ["gemini-2.5-pro"],
                "_model_dict_cache": {"gemini-2.5-pro": "gemini-2.5-pro"},
            },
        ]

        async def receive_2():
            return {"type": "http.request", "body": raw_bytes, "more_body": False}

        scope_2 = {
            "type": "http",
            "path": "/v1beta/models/gemini-2.5-pro:streamGenerateContent",
            "path_params": {"model": "gemini-2.5-pro"},
            "method": "POST",
            "headers": [(b"content-type", b"application/json")],
            "query_string": b"alt=sse",
            "app": fake_app,
            "_zoaholic_parsed_json": body_dict,
        }
        req_2 = Request(scope_2, receive_2)
        resp_2 = await route(req_2, BackgroundTasks(), api_index=0)
        assert resp_2.status_code == 200
        assert parse_calls == 1
        assert len(non_passthrough_messages) == 2
        assert non_passthrough_messages[0] == ["hello passthrough"]
        assert non_passthrough_messages[1] == ["hello passthrough"]
    finally:
        unregister_request_interceptor("test_mem_req_plugin")
        if orig_dialect is not None:
            register_dialect(orig_dialect, overwrite=True)


@pytest.mark.asyncio
async def test_passthrough_stream_releases_request_payload_before_sse_completion():
    allow_finish = asyncio.Event()
    captured_response = []

    class TrackableDict(dict):
        pass

    class SlowSSEStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"candidates":[{"content":{"parts":[{"text":"hi"}]}}]}\n\n'
            await allow_finish.wait()
            yield b"data: [DONE]\n\n"

    async def upstream(request):
        assert len(request.content) > 200_000
        resp = httpx.Response(200, stream=SlowSSEStream(), headers={"content-type": "text/event-stream"})
        captured_response.append(resp)
        return resp

    payload = TrackableDict({"contents": [{"role": "user", "parts": [{"text": "X" * 250_000}]}]})
    payload_ref = weakref.ref(payload)

    was_enabled = gc.isenabled()
    gc.disable()
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            gen = _fetch_passthrough_stream(client, "https://test.invalid/stream", {}, payload, 30)
            del payload
            first_chunk = await anext(gen)
            assert "hi" in first_chunk
            # While stream is still open waiting for remaining SSE events, request payload & bytes are already freed
            assert payload_ref() is None
            assert captured_response and captured_response[0].request._content == b""
            allow_finish.set()
            rest = [c async for c in gen]
            assert rest
    finally:
        allow_finish.set()
        if was_enabled:
            gc.enable()


@pytest.mark.asyncio
async def test_large_json_body_uses_worker_thread_in_stats_middleware(monkeypatch):
    import threading

    ready = asyncio.Event()
    ready.set()
    monkeypatch.setitem(sys.modules, "main", SimpleNamespace(_db_ready=ready))
    main_tid = threading.get_ident()
    decode_tids = []
    orig_loads = json.loads

    def record_thread_loads(raw):
        decode_tids.append(threading.get_ident())
        return orig_loads(raw)

    monkeypatch.setattr("core.middleware.json_loads", record_thread_loads)

    async def inner_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}", "more_body": False})

    big_body = json.dumps({"model": "m", "messages": [{"role": "user", "content": "Z" * 80_000}]}).encode("utf-8")
    assert len(big_body) > 65536

    async def receive():
        return {"type": "http.request", "body": big_body, "more_body": False}

    async def send(msg):
        pass

    mw = StatsMiddleware(inner_app)
    mw._dialect_prefixes = ["/v1"]
    fake_app = SimpleNamespace(
        state=SimpleNamespace(
            admin_initialized=True,
            api_list=["k"],
            config={"preferences": {"log_raw_data_retention_hours": 0}},
        )
    )
    scope = {
        "type": "http",
        "path": "/v1/chat/completions",
        "method": "POST",
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"",
        "app": fake_app,
    }
    await mw(scope, receive, send)
    assert decode_tids and decode_tids[0] != main_tid


def test_app_middleware_stack_contains_no_base_http_middleware():
    from starlette.middleware.base import BaseHTTPMiddleware
    import main as main_mod

    offenders = [
        m.cls for m in main_mod.app.user_middleware
        if isinstance(getattr(m, "cls", None), type) and issubclass(m.cls, BaseHTTPMiddleware)
    ]
    assert offenders == []


@pytest.mark.asyncio
async def test_init_db_disposes_previous_engine_and_close_db_clears_reference(monkeypatch):
    import db as db_mod

    disposed = []

    class DummyOldEngine:
        async def dispose(self):
            disposed.append("old")

    class DummyNewEngine:
        sync_engine = None

        async def dispose(self):
            disposed.append("new")

    orig_engine = db_mod.db_engine
    orig_session = db_mod._legacy_async_session
    orig_disable = db_mod.DISABLE_DATABASE
    orig_type = db_mod.DB_TYPE
    try:
        monkeypatch.setattr(db_mod, "DISABLE_DATABASE", False)
        monkeypatch.setattr(db_mod, "DB_TYPE", "sqlite")
        monkeypatch.setattr(db_mod, "_create_sqlite_engine", lambda debug: DummyNewEngine())
        monkeypatch.setattr(db_mod, "sessionmaker", lambda *a, **kw: "dummy_session_factory")
        db_mod.db_engine = DummyOldEngine()

        await db_mod.init_db()
        assert disposed == ["old"]
        assert isinstance(db_mod.db_engine, DummyNewEngine)
        assert db_mod._legacy_async_session == "dummy_session_factory"

        await db_mod.close_db()
        assert disposed == ["old", "new"]
        assert db_mod.db_engine is None
        assert db_mod._legacy_async_session is None
    finally:
        db_mod.db_engine = orig_engine
        db_mod._legacy_async_session = orig_session
        db_mod.DISABLE_DATABASE = orig_disable
        db_mod.DB_TYPE = orig_type


@pytest.mark.asyncio
async def test_mask_provider_api_key_and_smart_key_sorting_compat(monkeypatch):
    from core import key_stats as key_stats_mod
    from core.stats import mask_provider_api_key

    assert mask_provider_api_key(None) is None
    assert mask_provider_api_key("") == ""
    assert mask_provider_api_key("*") == "*"

    k1 = "sk-proj-11111111111111119999"
    k2 = "sk-proj-22222222222222229999"
    m1 = mask_provider_api_key(k1)
    m2 = mask_provider_api_key(k2)
    assert m1 != k1 and "1111111111" not in m1
    assert m1 != m2  # Distinct short digest even with identical prefix and suffix
    assert mask_provider_api_key(m1) == m1  # Idempotent

    async def fake_query(provider_name, start_dt=None, end_dt=None):
        return [
            # k1 has masked new stats (90%)
            {"api_key": m1, "success_rate": 0.9, "total_requests": 10, "success_count": 9},
            # k2 has both legacy raw stats (1/2) and new masked stats (1/2) -> combined 50% (2/4)
            {"api_key": k2, "success_rate": 0.5, "total_requests": 2, "success_count": 1},
            {"api_key": m2, "success_rate": 0.5, "total_requests": 2, "success_count": 1},
        ]

    monkeypatch.setattr(key_stats_mod, "query_channel_key_stats", fake_query)
    ordered = await key_stats_mod.get_sorted_api_keys("prov", [k2, "sk-unused-key-0000", k1], group_size=100)
    assert ordered == [k1, k2, "sk-unused-key-0000"]


@pytest.mark.asyncio
async def test_middleware_validation_error_log_omits_raw_payload(monkeypatch):
    from pydantic import BaseModel

    class StrictBody(BaseModel):
        count: int

    logged_messages = []
    monkeypatch.setattr(
        "core.middleware.logger.error",
        lambda msg, *args: logged_messages.append(msg % args if args else str(msg)),
    )

    secret_marker = "PRIVATE_CONVERSATION_BODY_MARKER_98765"

    async def failing_app(scope, receive, send):
        StrictBody.model_validate({"count": "not-an-int"})

    raw_bytes = json.dumps({"model": "m", "text": secret_marker}).encode("utf-8")

    async def receive():
        return {"type": "http.request", "body": raw_bytes, "more_body": False}

    captured_status = []

    async def send(msg):
        if msg["type"] == "http.response.start":
            captured_status.append(msg["status"])

    mw = StatsMiddleware(failing_app)
    mw._dialect_prefixes = ["/v1"]
    fake_app = SimpleNamespace(
        state=SimpleNamespace(
            admin_initialized=True,
            api_list=["k"],
            config={"preferences": {"log_raw_data_retention_hours": 0}},
        )
    )
    scope = {
        "type": "http",
        "path": "/v1/chat/completions",
        "method": "POST",
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"",
        "app": fake_app,
    }
    await mw(scope, receive, send)
    assert captured_status == [422]
    assert logged_messages
    assert all(secret_marker not in m for m in logged_messages)
    assert all("not-an-int" not in m for m in logged_messages)


@pytest.mark.asyncio
async def test_db_gate_shared_instance_suspends_and_resumes_middleware():
    from core.db_gate import db_ready
    import main as main_mod

    assert main_mod._db_ready is db_ready
    reached_app = asyncio.Event()

    async def inner_app(scope, receive, send):
        reached_app.set()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        pass

    mw = StatsMiddleware(inner_app)
    scope = {"type": "http", "path": "/-/health", "method": "GET", "headers": []}
    db_ready.clear()
    try:
        task = asyncio.create_task(mw(scope, receive, send))
        await asyncio.sleep(0.02)
        assert not reached_app.is_set()
        db_ready.set()
        await asyncio.wait_for(task, timeout=1.0)
        assert reached_app.is_set()
    finally:
        db_ready.set()


def test_docs_routes_disabled_by_default():
    import main as main_mod

    assert main_mod.app.docs_url is None
    assert main_mod.app.redoc_url is None
    assert main_mod.app.openapi_url is None
    route_paths = {getattr(r, "path", None) for r in main_mod.app.routes}
    assert "/docs" not in route_paths
    assert "/redoc" not in route_paths
    assert "/openapi.json" not in route_paths
    assert "/docs/markdown" not in route_paths
    assert "/-/health" in route_paths
