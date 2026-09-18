"""中间件方言端点精确匹配回归测试。

背景：StatsMiddleware 原来用前缀 startswith 判断方言端点。Gemini 方言注册了
/v1 前缀后，所有 /v1/* 管理端点都被误判为方言端点，中间件的标准 API Key
鉴权分支成为死代码，端点安全完全依赖各路由自己挂依赖，漏挂即裸奔。
修复后只有真正注册的方言端点路径跳过标准鉴权，其余 /v1 端点恢复统一鉴权。
"""

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.middleware import StatsMiddleware


def _middleware() -> StatsMiddleware:
    return StatsMiddleware(app=None, debug=False)


@pytest.mark.parametrize(
    "path,expected",
    [
        # 已注册的方言端点：必须继续走方言路由自有鉴权（含 BYOK/JWT/黑名单）
        ("/v1/chat/completions", True),
        ("/v1/responses", True),
        ("/v1/responses/resp_abc", True),
        ("/v1/messages", True),
        ("/v1/messages/count_tokens", True),
        ("/v1/models", True),
        ("/v1beta/models", True),
        ("/v1/models/gemini-2.5-pro", True),
        ("/v1beta/models/gemini-2.5-pro", True),
        ("/v1/models/gemini-2.5-pro:generateContent", True),
        ("/v1beta/models/gemini-2.5-pro:streamGenerateContent", True),
        # 非方言 /v1 端点：必须走中间件标准鉴权（回归点：曾被 /v1 前缀误判）
        ("/v1/logs", False),
        ("/v1/api_config", False),
        ("/v1/generate-api-key", False),
        ("/v1/stats/provider_activity", False),
        ("/v1/stats/resolve_prices", False),
        ("/v1/embeddings", False),
        ("/v1/moderations", False),
        ("/v1/audio/speech", False),
        ("/v1/images/generations", False),
        ("/v1/oauth/callback", False),
        ("/v1/token_usage", False),
        # 路径打擦边球也不应误判为方言端点
        ("/v1/chat/completions/extra", False),
        ("/v1/modelsx", False),
        ("/v1/messagesx", False),
        ("/v1betax/models", False),
    ],
)
def test_is_dialect_endpoint_exact_match(path: str, expected: bool):
    assert _middleware()._is_dialect_endpoint(path) is expected


async def _run_middleware(path: str, headers: list, downstream_called: dict, statuses: list):
    """构造最小 ASGI 环境跑一遍 StatsMiddleware，记录下游是否被调用。"""

    async def downstream(scope, receive, send):
        downstream_called["hit"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            statuses.append(message.get("status"))

    fake_app = SimpleNamespace(state=SimpleNamespace(config={}))
    middleware = StatsMiddleware(downstream, debug=False)
    scope = {
        "type": "http",
        "path": path,
        "method": "GET",
        "headers": headers,
        "app": fake_app,
        "query_string": b"",
    }
    await middleware(scope, receive, send)


@pytest.mark.asyncio
async def test_non_dialect_v1_without_key_rejected_at_middleware():
    """无 Key 访问非方言 /v1 管理端点：中间件直接 403，请求不得到达路由层。"""
    downstream_called: dict = {}
    statuses: list = []
    await _run_middleware("/v1/stats/provider_activity", [], downstream_called, statuses)
    assert statuses == [403]
    assert "hit" not in downstream_called


@pytest.mark.asyncio
async def test_dialect_path_without_key_still_reaches_router():
    """无 Key 访问方言端点：中间件不拦截（方言路由自己做 403），请求到达路由层。"""
    downstream_called: dict = {}
    statuses: list = []
    await _run_middleware("/v1/chat/completions", [], downstream_called, statuses)
    assert downstream_called.get("hit") is True
    assert statuses == [200]


@pytest.mark.asyncio
async def test_oauth_callback_path_bypasses_standard_auth():
    """OAuth 回调是浏览器跳转入口：公开白名单放行，不要求 API Key。"""
    downstream_called: dict = {}
    statuses: list = []
    await _run_middleware("/v1/oauth/callback", [], downstream_called, statuses)
    assert downstream_called.get("hit") is True
    assert statuses == [200]
