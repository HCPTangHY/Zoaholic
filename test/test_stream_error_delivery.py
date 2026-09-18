"""Offline HTTP transport -> stream wrappers -> ASGI send regressions.

No production requests, configuration changes, or database writes.
"""
import asyncio
import json
from contextlib import asynccontextmanager
from time import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import BackgroundTasks
from starlette.requests import Request

from core.dialects.registry import EndpointDefinition, get_dialect
from core.dialects.router import _create_generic_handler
from core.key_pool import ThreadSafeCircularList
from core.middleware import request_info
from core.passthrough import _fetch_passthrough_stream, _passthrough_error_wrapper
from core.response import fetch_response_stream
from core.stream_errors import UpstreamStreamError, guard_stream
from core.stream_pipeline import error_handling_wrapper
from core.streaming import LoggingStreamingResponse
from core.utils import truncate_for_logging

MESSAGE = "The encrypted content for item rs_test could not be decrypted or parsed."
ERROR = {"error": {"message": MESSAGE, "type": "invalid_request_error", "code": "invalid_encrypted_content"}}
PATHS = {
    "openai": "/v1/chat/completions",
    "openai-responses": "/v1/responses",
    "claude": "/v1/messages",
    "gemini": "/v1beta/models/test:streamGenerateContent",
}


def events(wire):
    return [json.loads(line[5:].strip()) for line in wire.decode().splitlines()
            if line.startswith("data:") and line[5:].strip() != "[DONE]"]


@pytest.fixture
def state(monkeypatch):
    provider = {"provider": "delivery-test", "preferences": {"key_rules": [
        {"match": {"status": 400}, "duration": 60},
    ]}}
    pool = ThreadSafeCircularList(["fake-a", "fake-b"], schedule_algorithm="sticky_ip")
    pool._sticky_sessions["test-client"] = (0, time() + 3600)
    pool.index = 0  # Even this cursor must not select the disabled Key again.
    monkeypatch.setattr("core.key_pool._save_all_auto_disabled", lambda: None)
    from core.utils import provider_api_circular_list
    monkeypatch.setitem(provider_api_circular_list, provider["provider"], pool)
    info = {"provider": provider["provider"], "provider_id": provider["provider"],
            "_provider_cfg": provider, "_used_api_key": "fake-a", "client_ip": "test-client",
            "success": True, "status_code": 200, "raw_data_expires_at": True, "start_time": time()}
    records = []
    monkeypatch.setattr("core.streaming.enqueue_stats", lambda data, **kw: records.append(dict(data)))
    return SimpleNamespace(info=info, provider=provider, pool=pool, records=records)


@asynccontextmanager
async def delayed_response(kind, dialect_id, state, monkeypatch, *, log_raw=True):
    gate, closed = asyncio.Event(), asyncio.Event()
    state.info["dialect_id"] = dialect_id
    state.info["raw_data_expires_at"] = True if log_raw else None
    token = request_info.set(state.info)

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield json.dumps(ERROR).encode()

        async def aclose(self):
            closed.set()

    async def upstream(request):
        # Deterministic: the error headers only arrive after ASGI has sent a heartbeat.
        await gate.wait()
        return httpx.Response(400, stream=Body(), headers={"content-type": "application/json"})

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            if kind == "passthrough":
                source = _fetch_passthrough_stream(client, "https://test.invalid/v1/responses", {},
                                                   {"stream": True}, 5, engine="openai-responses")
                wrapped, _ = await _passthrough_error_wrapper(
                    source, "delivery-test", keepalive_interval=0.002,
                    current_info=state.info, engine="openai-responses")
            else:
                source = fetch_response_stream(client, "https://test.invalid/v1/responses", {},
                                               {"stream": True}, "openai-responses", "test", 5)
                wrapped, _ = await error_handling_wrapper(
                    source, "delivery-test", "openai-responses", True, [],
                    keepalive_interval=0.002, current_info=state.info)
            response = LoggingStreamingResponse(wrapped, media_type="text/event-stream", current_info=state.info)
            if kind == "converted":
                class Handler:
                    async def request_model(self, **kwargs):
                        return response
                monkeypatch.setattr("routes.deps.get_model_handler", lambda: Handler())
                route = _create_generic_handler(dialect_id, EndpointDefinition(path=PATHS[dialect_id], methods=["POST"]))
                payload = {"model": "test", "stream": True, "input": "hi",
                           "messages": [{"role": "user", "content": "hi"}],
                           "contents": [{"role": "user", "parts": [{"text": "hi"}]}]}
                async def receive_request():
                    return {"type": "http.request", "body": json.dumps(payload).encode(), "more_body": False}
                scope = {"type": "http", "method": "POST", "path": PATHS[dialect_id],
                         "headers": [], "query_string": b"", "path_params": {"model": "test"}}
                response = await route(Request(scope, receive_request), BackgroundTasks(), api_index=0)
            try:
                yield response, gate, closed
            finally:
                await response.close()
    finally:
        request_info.reset(token)


async def send_response(response, gate, on_body=None):
    sent = []
    async def send(message):
        body = message.get("body", b"")
        if on_body and body:
            await on_body(body)
        sent.append(message.copy())
        if b"keepalive" in body:
            gate.set()
    async def receive():
        return {"type": "http.disconnect"}
    await asyncio.wait_for(response({}, receive, send), 2)
    return sent, b"".join(m.get("body", b"") for m in sent)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["passthrough", "converted"])
@pytest.mark.parametrize("dialect_id", ["openai", "openai-responses", "claude", "gemini"])
async def test_delayed_http_error_is_typed_logged_and_cools_before_send(state, monkeypatch, kind, dialect_id):
    async def check_order(body):
        if MESSAGE.encode() in body:
            assert await state.pool.is_rate_limited("fake-a")
            assert "test-client" not in state.pool._sticky_sessions
            assert state.info["status_code"] == 502 and state.info["success"] is False
    async with delayed_response(kind, dialect_id, state, monkeypatch) as (response, gate, closed):
        sent, wire = await send_response(response, gate, check_order)
    assert sent[0]["status"] == 200  # Headers and heartbeat are already on the wire.
    assert sent[-1]["more_body"] is False
    assert MESSAGE.encode() in wire and wire.count(MESSAGE.encode()) == 1
    error = events(wire)[-1]
    if dialect_id in {"openai-responses", "claude"}:
        assert b"event: error\n" in wire and error["type"] == "error"
    if dialect_id == "openai-responses":
        assert error["message"] == MESSAGE
        assert error["code"] == "invalid_encrypted_content"
    elif dialect_id == "gemini":
        assert error["error"]["code"] == 400
    else:
        assert error["error"]["message"] == MESSAGE
    assert len(state.records) == 1
    assert state.records[0]["response_body"] == truncate_for_logging(wire)
    assert state.records[0]["status_code"] == 502
    assert state.records[0]["_stream_error"]["status_code"] == 400
    assert closed.is_set()
    assert await state.pool.next("test") == "fake-b"


@pytest.mark.asyncio
async def test_delayed_error_does_not_depend_on_raw_logging(state, monkeypatch):
    async with delayed_response("passthrough", "openai-responses", state, monkeypatch, log_raw=False) as (response, gate, closed):
        _, wire = await send_response(response, gate)
    assert MESSAGE.encode() in wire
    assert state.records[0]["success"] is False
    assert "response_body" not in state.records[0]
    assert await state.pool.is_rate_limited("fake-a")
    assert closed.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("newer_session", [False, True])
async def test_unmatched_rule_does_not_disable_key_or_clear_newer_session(state, monkeypatch, newer_session):
    state.provider["preferences"]["key_rules"] = [{"match": {"status": [401, 403]}, "duration": -1}]
    if newer_session:
        state.pool._sticky_sessions["test-client"] = (1, time() + 3600)
    async with delayed_response("passthrough", "openai-responses", state, monkeypatch) as (response, gate, _):
        _, wire = await send_response(response, gate)
    assert MESSAGE.encode() in wire
    assert not state.pool.auto_disabled_info
    assert ("test-client" in state.pool._sticky_sessions) is newer_session
    if newer_session:
        assert state.pool._sticky_sessions["test-client"][0] == 1


@pytest.mark.asyncio
async def test_key_processing_is_once_and_cannot_replace_upstream_error(state, monkeypatch):
    disable = AsyncMock(side_effect=RuntimeError("test persistence unavailable"))
    monkeypatch.setattr(state.pool, "set_auto_disabled", disable)
    async with delayed_response("passthrough", "openai-responses", state, monkeypatch) as (response, gate, _):
        _, wire = await send_response(response, gate)
    assert MESSAGE.encode() in wire
    assert b"test persistence unavailable" not in wire
    assert state.records[0]["success"] is False
    assert disable.await_count == 1


@pytest.mark.asyncio
async def test_disconnect_during_heartbeat_does_not_disable_upstream(state, monkeypatch):
    async def disconnected(body):
        raise BrokenPipeError("client disconnected")
    async with delayed_response("passthrough", "openai-responses", state, monkeypatch) as (response, gate, _):
        sent, wire = await send_response(response, gate, disconnected)
    assert wire == b""
    assert not state.pool.auto_disabled_info
    assert state.records[0]["success"] is False
    assert state.records[0]["status_code"] == 499
    assert not state.records[0].get("response_body")
    assert response.body_iterator is None


@pytest.mark.asyncio
async def test_error_send_failure_logs_only_successfully_sent_heartbeat(state, monkeypatch):
    async def disconnected(body):
        if MESSAGE.encode() in body:
            raise BrokenPipeError("client disconnected after error")
    async with delayed_response("passthrough", "openai-responses", state, monkeypatch) as (response, gate, _):
        _, wire = await send_response(response, gate, disconnected)
    assert wire == b": keepalive\n\n"
    assert state.records[0]["response_body"] == truncate_for_logging(wire)
    assert state.records[0]["status_code"] == 502
    assert await state.pool.is_rate_limited("fake-a")


@pytest.mark.asyncio
async def test_consuming_stream_for_json_does_not_commit_http_or_apply_key_rules(state):
    async def source():
        yield 'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
        yield {"error": ERROR["error"], "status_code": 400}
    stream = guard_stream(source(), info=state.info)
    try:
        assert "hello" in await anext(stream)
        with pytest.raises(UpstreamStreamError) as exc:
            await anext(stream)
        assert exc.value.status_code == 400
        assert not state.info.get("_stream_committed")
        assert not state.pool.auto_disabled_info  # The handler, not a stream finalizer, owns this attempt.
    finally:
        await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("dialect_id", ["openai-responses", "claude", "gemini"])
async def test_dialect_renderer_preserves_canonical_error(dialect_id):
    dialect = get_dialect(dialect_id)
    renderer = dialect.render_stream_factory() if dialect.render_stream_factory else dialect.render_stream
    chunk = "data: " + json.dumps({**ERROR, "status_code": 400}) + "\n\n"
    result = await renderer(chunk)
    assert MESSAGE in result
    if dialect_id in {"openai-responses", "claude"}:
        assert events(result.encode())[0]["type"] == "error"


@pytest.mark.asyncio
async def test_successful_content_without_usage_is_not_false_502(state):
    async def source():
        yield 'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
        yield 'data: [DONE]\n\n'
    response = LoggingStreamingResponse(source(), media_type="text/event-stream", current_info=state.info)
    _, wire = await send_response(response, asyncio.Event())
    assert b"hello" in wire
    assert state.records[0]["success"] is True
    assert not state.pool.auto_disabled_info


@asynccontextmanager
async def handler_case(state, monkeypatch, upstream, *, force_stream=False):
    """Use the actual handler, key pool, request builder and channel adapters."""
    from core.handler import ModelRequestHandler

    state.provider.update(
        engine="openai-responses", base_url="https://test.invalid/v1",
        api=list(state.pool.items), _model_dict_cache={"test": "test"}, model=["test"],
    )
    if force_stream:
        state.provider["preferences"]["stream_mode"] = "force_stream"
    state.info.update(request_id="offline-request", api_key="fake-client-key", model="test")
    channel_stats = []
    monkeypatch.setattr("core.handler._fire_and_forget_channel_stats",
                        lambda func, *args, **kw: channel_stats.append((args, kw)))
    monkeypatch.setattr("core.handler.enqueue_stats", lambda data, **kw: state.records.append(dict(data)))
    # Keep production scheduling logic; shorten only the heartbeat timer in this offline test.
    monkeypatch.setattr("core.handler.normalize_keepalive_interval", lambda *args: 0.002)
    token = request_info.set(state.info)
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        class ClientManager:
            @asynccontextmanager
            async def get_client(self, *args):
                yield client

        class App:
            def __init__(self):
                self.state = SimpleNamespace(
                    config={"preferences": {}, "providers": [state.provider]},
                    client_manager=ClientManager(), error_triggers=[],
                    provider_timeouts={"global": {"default": 10}},
                    keepalive_interval={"global": {"default": 1}},
                    channel_manager=SimpleNamespace(cooldown_period=0),
                )
        app = App()
        handler = ModelRequestHandler(app, lambda: state.info, AsyncMock())
        try:
            yield handler, channel_stats
        finally:
            request_info.reset(token)


async def call_handler(handler, provider, kind, *, stream=True):
    from core.models import RequestModel

    kwargs = {}
    if kind == "passthrough":
        kwargs.update(dialect_id="openai-responses",
                      original_payload={"model": "test", "input": "hi", "stream": stream})
    return await handler.request_model(
        RequestModel(model="test", stream=stream, messages=[{"role": "user", "content": "hi"}]),
        0, BackgroundTasks(), endpoint="/v1/responses", override_providers=[provider],
        override_auto_retry=True, **kwargs,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["passthrough", "converted"])
async def test_handler_delayed_error_defers_channel_stats_and_next_request_avoids_key(state, monkeypatch, kind):
    gate = asyncio.Event()
    used_keys = []
    async def upstream(request):
        used_keys.append(request.headers["authorization"])
        await gate.wait()
        return httpx.Response(400, json=ERROR)

    async with handler_case(state, monkeypatch, upstream) as (handler, stats):
        response = await asyncio.wait_for(call_handler(handler, state.provider, kind), 2)
        assert stats == []
        assert used_keys == ["Bearer fake-a"]
        _, wire = await send_response(response, gate)
        assert MESSAGE.encode() in wire
        assert len(stats) == 1 and stats[0][1]["success"] is False
        assert stats[0][1]["provider_api_key"] == "fake-a"
        assert state.records[0]["status_code"] == 502
        # A client retry must choose a different Key, not replay the committed request internally.
        response2 = await asyncio.wait_for(call_handler(handler, state.provider, kind), 2)
        assert used_keys == ["Bearer fake-a", "Bearer fake-b"]
        assert response2.status_code == 400
        assert len(stats) == 2 and all(not kw["success"] for _, kw in stats)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["passthrough", "converted"])
async def test_handler_force_stream_error_is_http_400_not_partial_success(state, monkeypatch, kind):
    closed = asyncio.Event()
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"type":"response.output_text.delta","delta":"partial text"}\n\n'
            await asyncio.sleep(0)
            yield ('event: error\ndata: ' + json.dumps({"type": "error", **ERROR, "status_code": 400}) + '\n\n').encode()
        async def aclose(self):
            closed.set()
    async def upstream(request):
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, stream=Body(), headers={"content-type": "text/event-stream"})

    async with handler_case(state, monkeypatch, upstream, force_stream=True) as (handler, stats):
        response = await asyncio.wait_for(call_handler(handler, state.provider, kind, stream=False), 2)
        assert response.status_code == 400
        assert MESSAGE in json.loads(response.body)["error"]["message"]
        assert b"partial text" not in response.body
        assert not state.info.get("_stream_committed")
        assert len(stats) == 1 and stats[0][1]["success"] is False
        assert await state.pool.is_rate_limited("fake-a")  # Applied by handler, not stream finalizer.
        assert closed.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["passthrough", "converted"])
async def test_handler_pre_output_rate_limit_retries_and_only_successful_attempt_is_sent(state, monkeypatch, kind):
    used_keys, closed = [], []
    state.provider["preferences"]["key_rules"] = [{"match": {"status": 429}, "duration": 60}]
    class Body(httpx.AsyncByteStream):
        def __init__(self, fail):
            self.fail = fail
        async def __aiter__(self):
            yield b'event: response.created\ndata: {"type":"response.created","response":{"id":"provisional"}}\n\n'
            if self.fail:
                yield b'event: error\ndata: {"type":"error","error":{"type":"rate_limit_error","code":"rate_limit_exceeded","message":"quota limit"}}\n\n'
            else:
                yield b'data: {"type":"response.output_text.delta","delta":"success"}\n\n'
                yield b'data: {"type":"response.completed","response":{"usage":{"input_tokens":1,"output_tokens":2}}}\n\n'
        async def aclose(self):
            closed.append(self.fail)
    async def upstream(request):
        used_keys.append(request.headers["authorization"])
        return httpx.Response(200, stream=Body(len(used_keys) == 1), headers={"content-type": "text/event-stream"})

    async with handler_case(state, monkeypatch, upstream) as (handler, stats):
        monkeypatch.setattr("core.handler.normalize_keepalive_interval", lambda *args: 1)
        response = await asyncio.wait_for(call_handler(handler, state.provider, kind), 3)
        assert used_keys == ["Bearer fake-a", "Bearer fake-b"]
        assert len(stats) == 1 and stats[0][1]["success"] is False
        _, wire = await send_response(response, asyncio.Event())
        assert b"success" in wire and b"quota limit" not in wire
        assert len(stats) == 2 and stats[1][1]["success"] is True
        assert len(state.records) == 1 and state.records[0]["success"] is True
        assert not state.records[0].get("_stream_error")
        assert sorted(closed) == [False, True]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["passthrough", "converted"])
async def test_error_after_output_is_delivered_once_and_records_final_channel_failure(state, monkeypatch, kind):
    gate, closed = asyncio.Event(), asyncio.Event()
    used_keys = []
    state.provider["preferences"]["key_rules"] = [{"match": {"status": 429}, "duration": 60}]

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"type":"response.output_text.delta","delta":"hello"}\n\n'
            await gate.wait()
            # Split the error across network chunks. The terminating event must not be lost.
            raw = b'event: error\ndata: {"type":"error","error":{"type":"rate_limit_error","code":"rate_limit_exceeded","message":"late quota error"}}\n\n'
            for start in range(0, len(raw), 11):
                yield raw[start:start + 11]
        async def aclose(self):
            closed.set()

    async def upstream(request):
        used_keys.append(request.headers["authorization"])
        return httpx.Response(200, stream=Body(), headers={"content-type": "text/event-stream"})

    async with handler_case(state, monkeypatch, upstream) as (handler, stats):
        response = await asyncio.wait_for(call_handler(handler, state.provider, kind), 2)
        assert not stats

        async def after_body(body):
            if b"hello" in body:
                gate.set()

        _, wire = await send_response(response, gate, after_body)
        assert b"hello" in wire and wire.count(b"late quota error") == 1
        assert used_keys == ["Bearer fake-a"]
        assert len(stats) == 1 and stats[0][1]["success"] is False
        assert state.records[0]["response_body"] == truncate_for_logging(wire)
        assert state.records[0]["status_code"] == 502
        assert state.records[0]["_stream_error"]["status_code"] == 429
        assert await state.pool.is_rate_limited("fake-a")
        assert closed.is_set()


@pytest.mark.asyncio
async def test_error_without_code_and_renderer_uses_responses_error_type(state, monkeypatch):
    original = {"error": {"message": MESSAGE, "type": "invalid_request_error"}}
    monkeypatch.setattr(__import__(__name__, fromlist=["ERROR"]), "ERROR", original)
    async with delayed_response("passthrough", "openai-responses", state, monkeypatch) as (response, gate, _):
        _, wire = await send_response(response, gate)
    error = events(wire)[-1]
    assert error["type"] == "error"
    assert error["code"] == "invalid_request_error"
    assert error["message"] == MESSAGE


@pytest.mark.asyncio
async def test_byok_error_records_failure_without_touching_pool(state, monkeypatch):
    state.info["_is_byok_request"] = True
    async with delayed_response("passthrough", "openai-responses", state, monkeypatch) as (response, gate, _):
        _, wire = await send_response(response, gate)
    assert MESSAGE.encode() in wire
    assert state.records[0]["status_code"] == 502
    assert not state.pool.auto_disabled_info
    assert state.pool._sticky_sessions["test-client"][0] == 0
