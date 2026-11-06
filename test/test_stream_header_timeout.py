"""Tests for stream_header_timeout (default 60s), decoupling from keepalive_interval, and gemini_empty_retry sticky_ip rotation."""
import asyncio
import json
from contextlib import asynccontextmanager
from time import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import BackgroundTasks

from core.handler import ModelRequestHandler, get_preference, normalize_stream_header_timeout
from core.key_pool import ThreadSafeCircularList
from core.middleware import request_info
from core.models import RequestModel
from main import init_preference, DEFAULT_STREAM_HEADER_TIMEOUT


def test_init_preference_global_and_channel_override():
    assert DEFAULT_STREAM_HEADER_TIMEOUT == 60
    cfg = {
        "preferences": {
            "stream_header_timeout": {"default": 45, "gemini-4-argon": 75},
        },
        "providers": [
            {
                "provider": "firebaseVertex",
                "preferences": {"stream_header_timeout": {"default": 90, "gemini-4-argon": 120}},
            },
            {
                "provider": "intChannel",
                "preferences": {"header_timeout": 80},
            },
            {
                "provider": "inheritChannel",
                "preferences": {},
            },
        ],
    }
    table = init_preference(cfg, "stream_header_timeout", DEFAULT_STREAM_HEADER_TIMEOUT, alias_keys=("header_timeout",))
    # 渠道配置覆盖全局（模型级 + default 级）
    assert get_preference(table, "firebaseVertex", ("gemini-4-argon", "gemini-4-argon"), 60) == 120
    assert get_preference(table, "firebaseVertex", ("gemini-3.8-flash", "gemini-3.8-flash"), 60) == 90
    # 单渠道直接写整数 + 别名 header_timeout
    assert get_preference(table, "intChannel", ("gemini-4-argon", "gemini-4-argon"), 60) == 80
    # 未配置渠道继承全局
    assert get_preference(table, "inheritChannel", ("gemini-4-argon", "gemini-4-argon"), 60) == 75
    assert get_preference(table, "inheritChannel", ("other-model", "other-model"), 60) == 45


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["passthrough", "converted"])
async def test_slow_429_after_keepalive_interval_within_header_timeout_retries_next_key(monkeypatch, kind):
    """上游在超过 keepalive_interval 但未超过 stream_header_timeout 时返回 HTTP 429：
    不应提前发 HTTP 200 + keepalive，而应抛回 handler 触发 key_rules 冷却并由 sticky_ip 换下一个 Key 重试成功。
    """
    provider = {
        "provider": "fb-test",
        "engine": "openai-responses",
        "model": ["test"],
        "_model_dict_cache": {"test": "test"},
        "api": ["fake-a", "fake-b"],
        "preferences": {
            "api_key_schedule_algorithm": "sticky_ip",
            "key_rules": [{"match": {"status": [429]}, "duration": 30, "retry": True}],
        },
    }
    pool = ThreadSafeCircularList(["fake-a", "fake-b"], schedule_algorithm="sticky_ip")
    pool._sticky_sessions["test-client"] = (0, time() + 3600)
    monkeypatch.setattr("core.key_pool._save_all_auto_disabled", lambda: None)
    from core.utils import provider_api_circular_list
    monkeypatch.setitem(provider_api_circular_list, "fb-test", pool)

    used_keys = []

    class SuccessBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"type":"response.output_text.delta","delta":"recovered"}\n\n'
            yield b'data: {"type":"response.completed","response":{"usage":{"input_tokens":1,"output_tokens":1}}}\n\n'

    async def upstream(request):
        auth = request.headers.get("authorization", "")
        used_keys.append(auth)
        if len(used_keys) == 1:
            # 模拟慢 429：耗时 0.05s（大于 keepalive_interval=0.01s，但小于 stream_header_timeout=0.5s）
            await asyncio.sleep(0.05)
            return httpx.Response(
                429,
                json={"error": {"code": 429, "message": "TPM quota exhausted"}},
                headers={"content-type": "application/json"},
            )
        return httpx.Response(200, stream=SuccessBody(), headers={"content-type": "text/event-stream"})

    info = {
        "provider": "fb-test",
        "provider_id": "fb-test",
        "_provider_cfg": provider,
        "client_ip": "test-client",
        "success": True,
        "status_code": 200,
        "start_time": time(), "request_id": "req-1", "api_key": "client-k", "model": "test",
    }
    monkeypatch.setattr("core.handler._fire_and_forget_channel_stats", lambda *a, **kw: None)
    monkeypatch.setattr("core.handler.enqueue_stats", lambda *a, **kw: None)
    monkeypatch.setattr("core.streaming.enqueue_stats", lambda *a, **kw: None)
    monkeypatch.setattr("core.handler.normalize_keepalive_interval", lambda *a: 0.01)

    token = request_info.set(info)
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            class ClientManager:
                @asynccontextmanager
                async def get_client(self, *args):
                    yield client

            class _App:
                pass
            app = _App()
            app.state = SimpleNamespace(
                    config={"preferences": {}, "providers": [provider]},
                    client_manager=ClientManager(),
                    error_triggers=[],
                    provider_timeouts={"global": {"default": 10}},
                    keepalive_interval={"global": {"default": 1}},
                    stream_header_timeouts={"global": {"default": 0.5}},
                    channel_manager=SimpleNamespace(cooldown_period=0),
            )
            handler = ModelRequestHandler(app, lambda: info, AsyncMock())
            kwargs = {}
            if kind == "passthrough":
                kwargs.update(
                    dialect_id="openai-responses",
                    original_payload={"model": "test", "input": "hi", "stream": True},
                )
            resp = await handler.request_model(
                RequestModel(model="test", stream=True, messages=[{"role": "user", "content": "hi"}]),
                0,
                BackgroundTasks(),
                endpoint="/v1/responses",
                override_providers=[provider],
                override_auto_retry=True,
                **kwargs,
            )
            chunks = []
            async for ch in resp.body_iterator:
                chunks.append(ch.decode() if isinstance(ch, bytes) else str(ch))
            wire = "".join(chunks)
            assert used_keys == ["Bearer fake-a", "Bearer fake-b"]
            assert "recovered" in wire
            assert "TPM quota exhausted" not in wire
            assert await pool.is_rate_limited("fake-a")
    finally:
        request_info.reset(token)


@pytest.mark.asyncio
async def test_stream_header_timeout_exceeded_rotates_to_next_key(monkeypatch):
    """当第一把 Key 卡死超过 stream_header_timeout 仍未返回响应头时，立即抛 ReadTimeout 换第二把 Key 重试成功。"""
    provider = {
        "provider": "timeout-test",
        "engine": "openai-responses",
        "model": ["test"],
        "_model_dict_cache": {"test": "test"},
        "api": ["hung-key", "fast-key"],
        "preferences": {
            "api_key_schedule_algorithm": "sticky_ip",
            "key_rules": [{"match": "default", "duration": 30, "retry": True}],
        },
    }
    pool = ThreadSafeCircularList(["hung-key", "fast-key"], schedule_algorithm="sticky_ip")
    pool._sticky_sessions["test-client"] = (0, time() + 3600)
    monkeypatch.setattr("core.key_pool._save_all_auto_disabled", lambda: None)
    from core.utils import provider_api_circular_list
    monkeypatch.setitem(provider_api_circular_list, "timeout-test", pool)

    used_keys = []

    class SuccessBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"type":"response.output_text.delta","delta":"fast-ok"}\n\n'
            yield b'data: {"type":"response.completed","response":{"usage":{"input_tokens":1,"output_tokens":1}}}\n\n'

    async def upstream(request):
        used_keys.append(request.headers.get("authorization", ""))
        if len(used_keys) == 1:
            await asyncio.sleep(5.0)
        return httpx.Response(200, stream=SuccessBody(), headers={"content-type": "text/event-stream"})

    info = {
        "provider": "timeout-test",
        "provider_id": "timeout-test",
        "_provider_cfg": provider,
        "client_ip": "test-client",
        "success": True,
        "status_code": 200,
        "start_time": time(), "request_id": "req-1", "api_key": "client-k", "model": "test",
    }
    monkeypatch.setattr("core.handler._fire_and_forget_channel_stats", lambda *a, **kw: None)
    monkeypatch.setattr("core.handler.enqueue_stats", lambda *a, **kw: None)
    monkeypatch.setattr("core.streaming.enqueue_stats", lambda *a, **kw: None)
    monkeypatch.setattr("core.handler.normalize_keepalive_interval", lambda *a: 0.02)

    token = request_info.set(info)
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            class ClientManager:
                @asynccontextmanager
                async def get_client(self, *args):
                    yield client

            class _App:
                pass
            app = _App()
            app.state = SimpleNamespace(
                    config={"preferences": {}, "providers": [provider]},
                    client_manager=ClientManager(),
                    error_triggers=[],
                    provider_timeouts={"global": {"default": 10}},
                    keepalive_interval={"global": {"default": 1}},
                    stream_header_timeouts={"global": {"default": 0.05}},
                    channel_manager=SimpleNamespace(cooldown_period=0),
            )
            handler = ModelRequestHandler(app, lambda: info, AsyncMock())
            resp = await handler.request_model(
                RequestModel(model="test", stream=True, messages=[{"role": "user", "content": "hi"}]),
                0,
                BackgroundTasks(),
                endpoint="/v1/responses",
                override_providers=[provider],
                override_auto_retry=True,
                dialect_id="openai-responses",
                original_payload={"model": "test", "input": "hi", "stream": True},
            )
            chunks = []
            async for ch in resp.body_iterator:
                chunks.append(ch.decode() if isinstance(ch, bytes) else str(ch))
            wire = "".join(chunks)
            assert used_keys == ["Bearer hung-key", "Bearer fast-key"]
            assert "fast-ok" in wire
    finally:
        request_info.reset(token)


@pytest.mark.asyncio
async def test_gemini_empty_retry_sticky_ip_rotates_key_after_long_thought(monkeypatch):
    """验证 gemini_empty_retry 在 sticky_ip 渠道下思考空回后，会先冷却前一把 Key 再调用 pool.next() 切换到下一把 Key。"""
    from plugins.gemini_empty_retry import _prepare_retry_credentials

    provider = {
        "provider": "gemini-sticky-test",
        "engine": "firebaseVertex",
        "api": ["proj1:key1", "proj2:key2"],
        "preferences": {"api_key_schedule_algorithm": "sticky_ip", "cooldown_period": 1},
    }
    pool = ThreadSafeCircularList(["proj1:key1", "proj2:key2"], schedule_algorithm="sticky_ip")
    pool._sticky_sessions["client-1"] = (0, time() + 3600)
    monkeypatch.setattr("core.key_pool._save_all_auto_disabled", lambda: None)
    from core.utils import provider_api_circular_list
    monkeypatch.setitem(provider_api_circular_list, "gemini-sticky-test", pool)

    info = {
        "client_ip": "client-1",
        "_used_api_key": "proj1:key1",
        "_provider_cfg": provider,
        "_gemini_empty_retry_ctx": {"provider": provider, "engine": "firebaseVertex", "request": None},
    }
    token = request_info.set(info)
    try:
        new_url, new_headers = await _prepare_retry_credentials(
            "https://firebasevertexai.googleapis.com/v1beta/projects/proj1/locations/global/publishers/google/models/gemini-4-argon:streamGenerateContent?alt=sse",
            {"x-goog-api-key": "key1"},
            "gemini-4-argon",
            is_stream=True,
            is_passthrough=True,
        )
        assert info["_used_api_key"] == "proj2:key2"
        assert "/projects/proj2/" in new_url
        assert new_headers["x-goog-api-key"] == "key2"
    finally:
        request_info.reset(token)
