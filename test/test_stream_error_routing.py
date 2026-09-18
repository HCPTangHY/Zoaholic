import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from core.middleware import request_info
from core.stream_errors import UpstreamStreamError, extract_stream_error, guard_stream
from core.stream_pipeline import error_handling_wrapper
from core.passthrough import _passthrough_error_wrapper


ERROR = {
    "type": "error",
    "error": {
        "type": "rate_limit_error",
        "code": "rate_limit_exceeded",
        "message": "request rate limit exceeded",
    },
}


@pytest.mark.asyncio
async def test_extract_nested_responses_failure():
    value = {"type": "response.failed", "response": {"error": ERROR["error"]}}
    found = extract_stream_error(value)
    assert found["type"] == "rate_limit_error"
    assert found["code"] == "rate_limit_exceeded"


@pytest.mark.asyncio
async def test_guard_raises_before_semantic_output_and_records_error():
    info = {}
    token = request_info.set(info)

    async def source():
        yield "event: response.created\ndata: {\"type\":\"response.created\"}\n\n"
        yield "data: " + json.dumps(ERROR) + "\n\n"

    try:
        with pytest.raises(UpstreamStreamError):
            await anext(guard_stream(source()))
            await anext(guard_stream(source()))
    finally:
        request_info.reset(token)


@pytest.mark.asyncio
async def test_guard_keeps_error_after_semantic_output_and_records_it():
    info = {}
    token = request_info.set(info)
    source = guard_stream(_source_after_output())
    try:
        first = await anext(source)
        assert "hello" in first
        # 仅 ASGI 发送层才能提交 HTTP；内部预读不会关闭重试窗口。
        info["_stream_committed"] = True
        second = await anext(source)
        assert "rate_limit_exceeded" in second
        assert info["_stream_error"]["code"] == "rate_limit_exceeded"
    finally:
        await source.aclose()
        request_info.reset(token)


async def _source_after_output():
    yield 'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
    yield "data: " + json.dumps(ERROR) + "\n\n"


@pytest.mark.asyncio
async def test_passthrough_wrapper_raises_midstream_error_before_output():
    async def source():
        yield "event: response.created\ndata: {\"type\":\"response.created\"}\n\n"
        yield "data: " + json.dumps(ERROR) + "\n\n"

    # 语义输出前的流内错误必须在返回响应前抛回，才能进入 handler 的路由重试循环。
    with pytest.raises(UpstreamStreamError):
        await _passthrough_error_wrapper(source())


@pytest.mark.asyncio
async def test_channel_classifier_overrides_core_default():
    """渠道注册的分类器应覆盖 core 通用判断：未知事件可被渠道声明为可暂存。"""
    info = {}

    # core 通用判断中未知事件会立即提交；渠道分类器将其声明为可暂存后，
    # 随后的流内错误仍然能回到路由重试循环。
    def custom_classifier(event):
        if isinstance(event, dict) and event.get("type") == "custom.handshake":
            return True
        return None

    async def source():
        yield 'data: {"type": "custom.handshake", "session": "abc"}\n\n'
        yield "data: " + json.dumps(ERROR) + "\n\n"

    with pytest.raises(UpstreamStreamError) as exc:
        await anext(guard_stream(source(), info=info, classifier=custom_classifier))
    assert exc.value.status_code == 429


@pytest.mark.asyncio
async def test_builtin_channels_register_stream_classifiers():
    """内置渠道在注册表上声明自己的流式事件分类器。"""
    from core.channels import get_channel

    gemini = get_channel("gemini")
    assert gemini is not None and gemini.stream_event_classifier is not None
    # Gemini：空 candidates 可暂存，带文本 parts 提交。
    assert gemini.stream_event_classifier({"candidates": [{"content": {"role": "model", "parts": []}}]}) is True
    assert gemini.stream_event_classifier({"candidates": [{"content": {"parts": [{"text": "hi"}]}}]}) is False

    claude = get_channel("claude")
    assert claude is not None and claude.stream_event_classifier is not None
    assert claude.stream_event_classifier({"type": "message_start", "message": {"role": "assistant", "content": []}}) is True
    assert claude.stream_event_classifier({"type": "ping"}) is True

    responses = get_channel("openai-responses")
    assert responses is not None and responses.stream_event_classifier is not None
    assert responses.stream_event_classifier({"type": "response.created", "response": {}}) is True
    assert responses.stream_event_classifier({"type": "response.output_text.delta", "delta": "x"}) is False


@pytest.mark.asyncio
async def test_slow_error_within_keepalive_window_raises_not_200_stream():
    """首包在 keepalive 窗口内到达的错误必须抛回（对应 HTTP 400），不能退化为 200+流内错误帧。

    回归背景：曾把首包等待缩短到 min(3s, keepalive) 且超时后立即标记已提交，
    导致慢到的上游 400 变成 200 + keepalive + data 错误帧。
    """
    async def source():
        await asyncio.sleep(0.05)
        yield {"error": "passthrough_stream HTTP Error", "status_code": 400,
               "details": {"detail": "The 'gpt-6-astra' model is not supported"}}

    with pytest.raises(UpstreamStreamError) as exc:
        await _passthrough_error_wrapper(source(), "test", keepalive_interval=1)
    assert exc.value.status_code == 400
    # 真实错误原因应从 details.detail 提取，而不是泛化的 "passthrough_stream HTTP Error" 标签。
    assert "not supported" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_error_after_first_packet_timeout_still_raises_structured():
    """首包超时后到达的错误不得因过早的提交标记而原样透传错误 dict。"""
    from core.stream_pipeline import prepare_stream

    info = {}

    async def source():
        await asyncio.sleep(0.05)
        yield {"error": "passthrough_stream HTTP Error", "status_code": 400,
               "details": {"detail": "model not supported"}}

    wrapped, _ = await prepare_stream(source(), current_info=info, keepalive_interval=0.01)
    saw_keepalive = False
    with pytest.raises(UpstreamStreamError) as exc:
        async for chunk in wrapped:
            if "keepalive" in (chunk if isinstance(chunk, str) else ""):
                saw_keepalive = True
    assert saw_keepalive  # 超时分支仍先发心跳注释帧
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_check_response_envelope_message_extraction():
    """Zoaholic 内部错误信封的 message 应提取 details.detail。"""
    found = extract_stream_error({
        "error": "passthrough_stream HTTP Error", "status_code": 400,
        "details": {"detail": "The 'gpt-6-astra' model is not supported when using Codex with a ChatGPT account."},
    })
    assert found is not None
    assert found["status_code"] == 400
    assert "gpt-6-astra" in found["message"]
    assert "passthrough_stream" not in found["message"]


@pytest.mark.asyncio
async def test_error_handling_wrapper_retries_boundary_is_exception():
    async def source():
        yield ERROR

    with pytest.raises(UpstreamStreamError):
        await error_handling_wrapper(source(), "test", "openai", True, [])


@pytest.mark.asyncio
async def test_gemini_instream_rate_limit_raises_before_output():
    """Gemini 空 candidates 帧是暂存事件，随后的流内 429 必须回到路由循环。"""
    info = {}

    async def source():
        yield 'data: {"candidates": [{"content": {"role": "model", "parts": []}}]}\n\n'
        yield 'data: {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "Quota exceeded"}}\n\n'

    with pytest.raises(UpstreamStreamError) as exc:
        await anext(guard_stream(source(), info=info))
    assert exc.value.status_code == 429


@pytest.mark.asyncio
async def test_anthropic_instream_overloaded_raises_before_output():
    info = {}

    async def source():
        yield 'event: message_start\ndata: {"type": "message_start", "message": {"role": "assistant", "content": []}}\n\n'
        yield 'event: error\ndata: {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}\n\n'
    with pytest.raises(UpstreamStreamError) as exc:
        await anext(guard_stream(source(), info=info))
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_gemini_text_output_commits_before_later_error():
    """Gemini 已输出文本后，错误只能记录并透传，不能重试。"""
    info = {}

    async def source():
        yield 'data: {"candidates": [{"content": {"role": "model", "parts": [{"text": "hi"}]}}]}\n\n'
        yield 'data: {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "Quota exceeded"}}\n\n'
    stream = guard_stream(source(), info=info)
    first = await anext(stream)
    info["_stream_committed"] = True  # 模拟首段已交给 ASGI send。
    chunks = [first] + [c async for c in stream]
    assert len(chunks) == 2
    assert info["_stream_error"]["status_code"] == 429
    assert info["_stream_committed"] is True
