import os
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.byok import (
    build_byok_prefixes,
    is_byok_api_key,
    is_byok_provider,
    resolve_byok_token,
)
from core.auth import verify_api_key
from core.handler import ModelRequestHandler
from core.models import RequestModel
from core.middleware import request_info
from core.process_request import process_request
from core.utils import get_model_dict, provider_api_circular_list
from routes.models import build_models_for_request
from utils import update_config


class _State(SimpleNamespace):
    """测试用可变 state；目的在于不启动完整 FastAPI 应用。"""


class _App:
    """测试用最小 app；目的在于承载 verify_api_key 和 handler 需要的 state。"""

    def __init__(self, **state):
        self.state = _State(**state)


class _Request:
    """测试用最小 Request；目的在于模拟 FastAPI Request 的 headers/app/state。"""

    def __init__(self, app, headers=None):
        self.app = app
        self.headers = headers or {}
        self.state = _State()


class _Credentials:
    """测试用凭证对象；目的在于覆盖 Authorization: Bearer 提取路径。"""

    def __init__(self, credentials):
        self.credentials = credentials


@pytest.mark.asyncio
async def test_update_config_builds_byok_prefixes_and_does_not_create_empty_key_pool():
    """BYOK 配置应生成前缀表，同时 api: ["*"] 的 provider 不应创建空 key 池。"""
    provider_api_circular_list.clear()
    config = {
        "providers": [
            {
                "provider": "byok-gemini",
                "engine": "gemini",
                "base_url": "https://generativelanguage.googleapis.com/v1beta",
                "model": ["*"],
                "api": ["*"],
                "groups": ["byok-gemini"],
            }
        ],
        "api_keys": [
            {"api": "byok-*", "model": ["byok-gemini/*"], "groups": ["byok-gemini"]},
            {"api": "byok-gemini-*", "model": ["byok-gemini/*"], "groups": ["byok-gemini"]},
        ],
        "preferences": {},
    }

    _, api_keys_db, api_list = await update_config(
        config,
        save_to_file=False,
        save_to_db=False,
    )
    byok_prefixes = build_byok_prefixes(api_keys_db)

    assert api_list == ["byok-*", "byok-gemini-*"]
    assert byok_prefixes == [("byok-gemini-", 1), ("byok-", 0)]
    assert is_byok_api_key(api_keys_db, 1)
    assert provider_api_circular_list.get("byok-gemini") is None


@pytest.mark.asyncio
async def test_update_config_removes_stale_key_pool_when_provider_becomes_byok():
    """热更新为 BYOK provider 时，应删除旧的本地 key 池，避免继续使用旧上游 key。"""
    provider_api_circular_list.clear()
    initial_config = {
        "providers": [
            {
                "provider": "byok-gemini",
                "engine": "gemini",
                "base_url": "https://generativelanguage.googleapis.com/v1beta",
                "model": ["*"],
                "api": ["stale-local-key"],
                "groups": ["byok-gemini"],
            }
        ],
        "api_keys": [{"api": "byok-gemini-*", "model": ["byok-gemini/*"], "groups": ["byok-gemini"]}],
        "preferences": {},
    }
    runtime_config, _, _ = await update_config(initial_config, save_to_file=False, save_to_db=False)
    assert provider_api_circular_list.get("byok-gemini") is not None

    runtime_config["providers"][0]["api"] = ["*"]
    await update_config(runtime_config, save_to_file=False, save_to_db=False, changed_providers={"byok-gemini"})

    assert provider_api_circular_list.get("byok-gemini") is None


@pytest.mark.asyncio
async def test_byok_models_list_uses_real_key_and_keeps_group_authorization(monkeypatch):
    """BYOK 模型列表应只对授权分组调用上游，并使用请求中的真实上游 key。"""
    provider = {
        "provider": "byok-gemini",
        "engine": "gemini",
        "base_url": "https://example.test/v1beta",
        "api": ["*"],
        "model": ["*"],
        "groups": ["byok"],
    }
    provider["_model_dict_cache"] = get_model_dict(provider)
    app = _App(
        config={
            "providers": [
                provider,
                {
                    "provider": "blocked-byok",
                    "engine": "gemini",
                    "base_url": "https://blocked.test/v1beta",
                    "api": ["*"],
                    "model": ["*"],
                    "groups": ["blocked"],
                    "_model_dict_cache": {"*": "*"},
                },
            ],
            "api_keys": [{"api": "byok-gemini-*", "model": ["byok-gemini/*"], "groups": ["byok"]}],
            "preferences": {},
        },
        api_list=["byok-gemini-*"],
        models_list={},
    )
    calls = []

    class _ClientManager:
        def get_client(self, url, proxy=None):
            class _ClientContext:
                async def __aenter__(self):
                    return object()

                async def __aexit__(self, exc_type, exc, tb):
                    return False

            return _ClientContext()

    class _Channel:
        default_base_url = "https://default.test"

        async def models_adapter(self, client, provider_arg):
            calls.append(provider_arg)
            return ["gemini-2.5-pro", "gemini-2.5-flash"]

    app.state.client_manager = _ClientManager()
    monkeypatch.setattr("routes.models.get_channel", lambda engine: _Channel())

    context_tokens = __import__("core.byok", fromlist=["set_byok_context"]).set_byok_context("AIzaSyXXX", "byok-gemini-*")
    try:
        models = await build_models_for_request(0, app)
    finally:
        __import__("core.byok", fromlist=["reset_byok_context"]).reset_byok_context(context_tokens)

    assert [m["id"] for m in models] == ["gemini-2.5-flash", "gemini-2.5-pro"]
    assert len(calls) == 1
    assert calls[0]["provider"] == "byok-gemini"
    assert calls[0]["api"] == "AIzaSyXXX"


def test_byok_prefix_helpers_longest_prefix_and_reject_empty_real_key():
    """前缀匹配必须最长优先，且真实上游 key 为空时拒绝。"""
    prefixes = build_byok_prefixes([
        {"api": "byok-*"},
        {"api": "byok-gemini-*"},
        {"api": "plain-key"},
    ])

    assert prefixes == [("byok-gemini-", 1), ("byok-", 0)]
    assert resolve_byok_token("byok-gemini-AIzaSyXXX", prefixes) == (
        1,
        "byok-gemini-*",
        "AIzaSyXXX",
    )
    assert resolve_byok_token("byok-gemini-", prefixes) is None
    assert resolve_byok_token("byok-gemini-*", prefixes) is None
    assert is_byok_provider({"api": ["*"]}) is True
    assert is_byok_provider({"api": []}) is False
    assert is_byok_provider({"api": ""}) is False
    assert is_byok_provider({}) is False
    assert is_byok_provider({"api": ["sk-real"]}) is False


@pytest.mark.asyncio
async def test_verify_api_key_accepts_byok_token_and_stores_sanitized_state():
    """verify_api_key 精确匹配失败后应支持 BYOK 前缀匹配，并只保存模板身份。"""
    app = _App(
        api_list=["plain", "byok-gemini-*"],
        byok_prefixes=[("byok-gemini-", 1)],
    )
    request = _Request(app, headers={"x-api-key": "byok-gemini-AIzaSyXXX"})

    api_index = await verify_api_key(request)

    assert api_index == 1
    assert request.state.byok_real_key == "AIzaSyXXX"
    assert request.state.byok_template_key == "byok-gemini-*"
    assert request.state.authenticated_token == "byok-gemini-*"


@pytest.mark.asyncio
async def test_verify_api_key_updates_request_info_for_normal_keys():
    """普通 API Key 的 Depends 鉴权也应把 request_info 更新为配置中的 key 名称和分组。"""
    app = _App(
        api_list=["plain"],
        byok_prefixes=[],
        config={"api_keys": [{"api": "plain", "name": "Plain Key", "groups": ["default"]}]},
    )
    request = _Request(app, headers={"x-api-key": "plain"})
    token = request_info.set({"api_key": "plain", "api_key_name": None, "api_key_group": None})
    try:
        api_index = await verify_api_key(request)
        info = request_info.get()
    finally:
        request_info.reset(token)

    assert api_index == 0
    assert info["api_key"] == "plain"
    assert info["api_key_name"] == "Plain Key"
    assert info["api_key_group"] is None


@pytest.mark.asyncio
async def test_verify_api_key_rejects_byok_prefix_without_real_key():
    """只有前缀、没有真实上游 key 的 BYOK token 必须被拒绝。"""
    app = _App(
        api_list=["byok-gemini-*"],
        byok_prefixes=[("byok-gemini-", 0)],
        api_keys_db=[{"api": "byok-gemini-*"}],
    )
    request = _Request(app, headers={"x-api-key": "byok-gemini-"})

    with pytest.raises(HTTPException) as exc_info:
        await verify_api_key(request)

    assert exc_info.value.status_code == 403

    template_request = _Request(app, headers={"x-api-key": "byok-gemini-*"})
    with pytest.raises(HTTPException) as template_exc_info:
        await verify_api_key(template_request)

    assert template_exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_handler_passes_byok_key_from_request_context_and_disables_auto_retry(monkeypatch):
    """handler 应从请求上下文读取 BYOK 真实 key，并对 BYOK 请求关闭自动重试。"""
    provider = {
        "provider": "byok-gemini",
        "engine": "gemini",
        "base_url": "https://example.test",
        "api": ["*"],
        "model": ["*"],
        "groups": ["byok"],
        "preferences": {},
    }
    provider["_model_dict_cache"] = get_model_dict(provider)
    # 修改原因：真实路由在 provider.model=["*"] 时会为本次请求注入 request_model 映射。
    # 修改方式：测试假 provider 直接补入该映射，避免绕过 routing.py 时出现 KeyError。
    # 目的：让本用例只验证 BYOK key 传递和不重试行为，不测试通配符路由本身。
    provider["_model_dict_cache"]["gemini-2.5-pro"] = "gemini-2.5-pro"
    app = _App(
        config={
            "providers": [provider],
            "api_keys": [
                {
                    "api": "byok-gemini-*",
                    "model": ["byok-gemini/*"],
                    "groups": ["byok"],
                    "preferences": {"AUTO_RETRY": True},
                }
            ],
            "preferences": {"SCHEDULING_ALGORITHM": "fixed_priority"},
        },
        api_list=["byok-gemini-*"],
        provider_timeouts={"global": {"default": 600}},
        keepalive_interval={"global": {"default": 15}},
        channel_manager=SimpleNamespace(cooldown_period=300),
        user_api_keys_rate_limit={
            "byok-gemini-*": SimpleNamespace(next=lambda model: None),
        },
    )

    async def _no_rate_limit(_model):
        return None

    app.state.user_api_keys_rate_limit["byok-gemini-*"].next = _no_rate_limit

    info = {
        "api_key": "byok-gemini-*",
        "byok_real_key": "AIzaSyXXX",
        "byok_template_key": "byok-gemini-*",
    }
    calls = []

    async def fake_get_right_order_providers(*args, **kwargs):
        return [provider]

    async def fake_process_request(*args, **kwargs):
        calls.append(kwargs)
        raise HTTPException(status_code=401, detail="upstream rejected BYOK key")

    monkeypatch.setattr("core.handler.get_right_order_providers", fake_get_right_order_providers)
    monkeypatch.setattr("core.handler.process_request", fake_process_request)

    handler = ModelRequestHandler(app, lambda: info, lambda *args, **kwargs: None)
    request_data = RequestModel(
        model="gemini-2.5-pro",
        messages=[{"role": "user", "content": "hello"}],
    )

    response = await handler.request_model(request_data, 0, None)

    assert response.status_code == 401
    assert len(calls) == 1
    assert calls[0]["force_api_key"] == "AIzaSyXXX"
    assert info["byok_real_key"] == "AIzaSyXXX"
    assert info["api_key"] == "byok-gemini-*"


@pytest.mark.asyncio
async def test_process_request_uses_forced_byok_key_without_pool_and_sanitizes_stats(monkeypatch):
    """process_request 收到 force_api_key 时应跳过 key pool，并不把真实 BYOK key 写入统计字段。"""
    provider_api_circular_list.clear()
    provider = {
        "provider": "byok-gemini",
        "engine": "gemini",
        "base_url": "https://example.test",
        "api": ["*"],
        "model": ["*"],
        "groups": ["byok"],
        "preferences": {},
    }
    provider["_model_dict_cache"] = {"gemini-2.5-pro": "gemini-2.5-pro"}
    request_data = RequestModel(
        model="gemini-2.5-pro",
        messages=[{"role": "user", "content": "hello"}],
        stream=False,
    )
    info = {
        "request_id": "rid",
        "api_key": "byok-gemini-*",
        "_byok_real_key": "AIzaSyXXX",
        "_byok_template_key": "byok-gemini-*",
    }
    stats_calls = []

    class _ClientManager:
        def get_client(self, url, proxy=None):
            class _ClientContext:
                async def __aenter__(self):
                    return object()

                async def __aexit__(self, exc_type, exc, tb):
                    return False

            return _ClientContext()

    async def fake_get_payload(request, engine, provider_arg, api_key):
        assert api_key == "AIzaSyXXX"
        return "https://example.test/models/gemini-2.5-pro:generateContent", {}, {"model": request.model}

    async def fake_fetch_response(*args, **kwargs):
        yield '{"ok": true}'

    async def fake_error_wrapper(generator, *args, **kwargs):
        return generator, 0.01

    async def update_channel_stats(*args, **kwargs):
        stats_calls.append((args, kwargs))

    monkeypatch.setattr("core.process_request.get_payload", fake_get_payload)
    monkeypatch.setattr("core.process_request.fetch_response", fake_fetch_response)
    monkeypatch.setattr("core.process_request.error_handling_wrapper", fake_error_wrapper)
    monkeypatch.setattr("core.handler._fire_and_forget_channel_stats", lambda func, *args, **kwargs: stats_calls.append((args, kwargs)))

    app = _App(
        config={"preferences": {}},
        client_manager=_ClientManager(),
        error_triggers=[],
    )

    response = await process_request(
        request_data,
        provider,
        None,
        app,
        lambda: info,
        update_channel_stats,
        force_api_key="AIzaSyXXX",
    )

    assert response is not None
    assert info["_used_api_key"] == "*"
    assert info["provider_key_index"] is None
    assert stats_calls[-1][1]["provider_api_key"] == "*"
