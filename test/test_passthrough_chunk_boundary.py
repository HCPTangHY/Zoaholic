"""透传响应的网络分片边界：响应拦截器应收到完整的行 / 完整的非流式响应体。"""
import asyncio
import json

import httpx

import core.passthrough as passthrough_mod
import core.response as response_mod
from core.middleware import request_info
from core.passthrough import _fetch_passthrough_stream
from core.stream_utils import aiter_line_aligned_text
from plugins import gemini_empty_retry


GEMINI_EVENTS = [
    {"candidates": [{"content": {"role": "model", "parts": [{"text": "你好，"}]}, "index": 0}]},
    {"candidates": [{"content": {"role": "model", "parts": [{"functionCall": {"name": "get_weather", "args": {"city": "Paris"}}}]}, "index": 0}]},
    {"candidates": [{"content": {"role": "model", "parts": [{"text": "世界"}]}, "finishReason": "STOP", "index": 0}],
     "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15}},
]
GEMINI_RESPONSE = {
    "candidates": [{"content": {"role": "model", "parts": [{"text": "PONG，测试"}]}, "finishReason": "STOP", "index": 0}],
    "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1, "totalTokenCount": 4},
}


def _split_bytes(data: bytes, size: int):
    """按固定字节数切分，会切在行中间，也会切在多字节 UTF-8 字符中间。"""
    return [data[i:i + size] for i in range(0, len(data), size)]


def _mock_client(chunks):
    async def body():
        for chunk in chunks:
            yield chunk

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body())

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_aiter_line_aligned_text_keeps_bytes_and_aligns_to_lines():
    async def source():
        for piece in ["data: {\"a\"", ": 1}\r\n", "\r\nda", "ta: {\"b\": 2}\n\nta", "il"]:
            yield piece

    async def collect():
        return [chunk async for chunk in aiter_line_aligned_text(source())]

    out = asyncio.run(collect())
    # 拼接结果逐字节不变（含 \r\n 与空行）
    assert "".join(out) == "data: {\"a\": 1}\r\n\r\ndata: {\"b\": 2}\n\ntail"
    # 除流结束时冲刷的尾部外，每段都以换行结尾
    assert all(chunk.endswith("\n") for chunk in out[:-1])
    assert out[-1] == "tail"


def test_passthrough_stream_interceptors_receive_complete_lines(monkeypatch):
    body = "".join(f"data: {json.dumps(e, ensure_ascii=False)}\r\n\r\n" for e in GEMINI_EVENTS).encode()
    seen = []

    async def recording_interceptors(chunk, engine, model, is_stream, **kwargs):
        seen.append(chunk)
        return chunk

    monkeypatch.setattr(response_mod, "_apply_response_path_interceptors", recording_interceptors)

    async def run():
        async with _mock_client(_split_bytes(body, 37)) as client:
            return [
                chunk async for chunk in _fetch_passthrough_stream(
                    client, "https://upstream.test/v1beta/models/m:streamGenerateContent?alt=sse",
                    {}, {"contents": []}, 30, engine="gemini", model="m",
                    enabled_plugins=["some_line_based_plugin"],
                )
            ]

    out = asyncio.run(run())

    # 透传内容逐字节保持不变
    assert "".join(out).encode() == body
    # 每次交给拦截器的都是以换行结尾的完整行，每个 data 行都能独立解析
    assert seen and all(chunk.endswith("\n") for chunk in seen)
    data_lines = [line for chunk in seen for line in chunk.splitlines() if line.startswith("data:")]
    assert [json.loads(line[5:]) for line in data_lines] == GEMINI_EVENTS


def _collect_wrapped_non_stream(monkeypatch, info):
    upstream_calls = []
    body = json.dumps(GEMINI_RESPONSE, ensure_ascii=False)

    async def fragmented_fetch(client, url, headers, payload, timeout, **kwargs):
        # 未启用响应插件时 _fetch_passthrough_response 走快路径，按网络分片多次 yield 响应体
        upstream_calls.append(url)
        for i in range(0, len(body), 16):
            yield body[i:i + 16]

    monkeypatch.setattr(passthrough_mod, "_fetch_passthrough_response", fragmented_fetch)

    async def run():
        token = request_info.set(info)
        try:
            return [
                chunk async for chunk in gemini_empty_retry.wrapped_passthrough_gemini_response(
                    None, "https://upstream.test/v1beta/models/m:generateContent", {}, {"contents": []}, "m", 30
                )
            ]
        finally:
            request_info.reset(token)

    return asyncio.run(run()), upstream_calls


def test_empty_retry_passthrough_non_stream_yields_complete_body(monkeypatch):
    chunks, upstream_calls = _collect_wrapped_non_stream(monkeypatch, {})

    # core 会对 passthrough_response_adapter 产出的每个 chunk 单独执行非流式响应拦截器，
    # 因此非流式响应必须作为一个完整响应体交出
    assert len(chunks) == 1
    assert json.loads(chunks[0]) == GEMINI_RESPONSE
    assert len(upstream_calls) == 1


def test_empty_retry_enabled_does_not_retry_fragmented_non_empty_response(monkeypatch):
    info = {"_gemini_empty_retry_enabled": True, "_gemini_empty_retry_max": 2}
    chunks, upstream_calls = _collect_wrapped_non_stream(monkeypatch, info)

    # 响应有正文，不应被误判为空回而换 Key 重试
    assert len(upstream_calls) == 1
    assert json.loads("".join(chunks)) == GEMINI_RESPONSE
