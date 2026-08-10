import json
from pathlib import Path
import sys

import pytest

# 修改原因：balance_enricher 属于插件系统核心能力，单文件运行测试时需要稳定导入项目真实代码。
# 修改方式：从当前测试文件向上查找同时包含 core/、routes/ 和 plugins/ 的目录，并插入 sys.path。
# 目的：确保回归测试覆盖本仓库实现，而不是导入到环境中的同名包。
ROOT = next(
    parent for parent in Path(__file__).resolve().parents
    if (parent / "core").is_dir() and (parent / "routes").is_dir() and (parent / "plugins").is_dir()
)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _json_response_payload(response) -> dict:
    """读取 FastAPI JSONResponse 的 JSON 内容。"""
    # 修改原因：路由测试直接调用 handler，返回值不是 HTTP 客户端响应对象。
    # 修改方式：读取 response.body 并用 json.loads 还原为 dict。
    # 目的：让测试只关注 balance_enricher 是否注入结果，不依赖额外 ASGI 测试栈。
    return json.loads(response.body.decode())


@pytest.mark.asyncio
async def test_balance_enricher_filters_by_enabled_plugin_and_exposes_options():
    from core.plugins.interceptors import InterceptorRegistry, get_current_plugin_options

    calls = []
    registry = InterceptorRegistry()

    async def skipped_enricher(result, engine, provider):
        # 修改原因：带 plugin_name 的 enricher 只能在渠道显式启用对应插件时执行。
        # 修改方式：测试中注册一个未启用插件的 enricher，如果被调用就写入 skipped 标记。
        # 目的：防止余额查询绕过 enabled_plugins 过滤执行其他插件逻辑。
        calls.append("skipped")
        result["skipped"] = True
        return result

    async def active_enricher(result, engine, provider):
        # 修改原因：balance_enricher 需要与 response_interceptor 一样支持插件参数上下文。
        # 修改方式：在回调里读取当前插件参数，并把 tier 写入 result。
        # 目的：保证后续插件可通过 enabled_plugins 中的 plugin:options 调整余额补充行为。
        calls.append((engine, get_current_plugin_options("tier_test")))
        result["tier"] = "Tier 3"
        return result

    registry.register_balance_enricher("skipped", skipped_enricher, priority=10, plugin_name="other_plugin")
    registry.register_balance_enricher("active", active_enricher, priority=20, plugin_name="tier_test")

    result = await registry.apply_balance_enrichers(
        {"supported": True},
        "openai",
        {"preferences": {"enabled_plugins": ["tier_test:passive"]}},
        ["tier_test:passive"],
    )

    assert calls == [("openai", "passive")]
    assert result == {"supported": True, "tier": "Tier 3"}


def test_balance_enricher_stats_unregister_and_clear():
    from core.plugins.interceptors import InterceptorRegistry

    async def enricher(result, engine, provider):
        return result

    registry = InterceptorRegistry()
    registry.register_balance_enricher("tier", enricher, plugin_name="tier_test")

    # 修改原因：新增拦截器类型后，统计、按插件注销和清空逻辑都必须覆盖它。
    # 修改方式：先检查 get_stats，再按插件注销，最后重新注册并 clear。
    # 目的：避免插件卸载后 balance_enricher 残留并影响其他渠道余额查询。
    stats = registry.get_stats()
    assert stats["balance_enrichers"]["total"] == 1
    assert stats["balance_enrichers"]["enabled"] == 1
    assert stats["balance_enrichers"]["enrichers"][0]["id"] == "tier"
    assert registry.unregister_plugin_interceptors("tier_test") == 1
    assert registry.get_stats()["balance_enrichers"]["total"] == 0

    registry.register_balance_enricher("tier", enricher, plugin_name="tier_test")
    registry.clear()
    assert registry.get_stats()["balance_enrichers"]["total"] == 0


@pytest.mark.asyncio
async def test_query_channel_balance_applies_enricher_to_oauth_result(monkeypatch):
    from core.channels.registry import register_channel, unregister_channel
    from core.plugins.interceptors import register_balance_enricher, reset_interceptor_registry
    from routes import channels as channels_route

    async def fake_oauth_balance(app, provider):
        return {"supported": True, "value_type": "percent", "available": 80.0}

    async def tier_enricher(result, engine, provider):
        # 修改原因：OAuth 余额结果也需要经过通用 enricher，才能显示被动采集到的 tier 字段。
        # 修改方式：测试替身在 result 上写入 tier，并断言 provider name 已被路由保留。
        # 目的：固定 OAuth 分支的变量作用域和 enricher 调用位置。
        assert engine == "balance-enricher-oauth"
        assert provider["provider"] == "OAuth-Tier-Test"
        result["tier"] = "Tier 4"
        return result

    engine = "balance-enricher-oauth"
    reset_interceptor_registry()
    unregister_channel(engine)
    monkeypatch.setattr(channels_route, "get_app", lambda: object())
    monkeypatch.setattr(channels_route, "_query_oauth_channel_balance", fake_oauth_balance)

    try:
        register_channel(id=engine, type_name="openai", is_oauth=True)
        register_balance_enricher("tier", tier_enricher, plugin_name="oai_tier")
        response = await channels_route.query_channel_balance(
            token="admin",
            provider_config={
                "provider": "OAuth-Tier-Test",
                "engine": engine,
                "api_key": "account@example.com",
                "preferences": {"enabled_plugins": ["oai_tier"]},
            },
        )
    finally:
        unregister_channel(engine)
        reset_interceptor_registry()

    payload = _json_response_payload(response)
    assert payload["tier"] == "Tier 4"


@pytest.mark.asyncio
async def test_query_channel_balance_applies_enricher_to_normal_result(monkeypatch):
    from core.plugins.interceptors import register_balance_enricher, reset_interceptor_registry
    from routes import channels as channels_route
    import core.balance as balance_module

    class ClientContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class ClientManager:
        def get_client(self, target_url, proxy):
            return ClientContext()

    async def fake_query_provider_balance(client, provider):
        return {"supported": True, "value_type": "amount", "available": 12.5}

    async def tier_enricher(result, engine, provider):
        # 修改原因：普通 API Key 渠道的余额结果需要保留 balance 字段并额外补充 tier。
        # 修改方式：在 fake balance 返回后通过 enricher 写入 Tier 3。
        # 目的：保证非 OAuth 分支也会调用 apply_balance_enrichers 且使用同一份 enabled_plugins。
        assert engine == "openai"
        assert provider["api"] == "sk-normal-tier-test"
        result["tier"] = "Tier 3"
        return result

    app = type(
        "App",
        (),
        {"state": type("State", (), {"client_manager": ClientManager(), "config": {}})()},
    )()

    reset_interceptor_registry()
    monkeypatch.setattr(channels_route, "get_app", lambda: app)
    monkeypatch.setattr(balance_module, "build_balance_config", lambda provider: {"template": "test"})
    monkeypatch.setattr(balance_module, "query_provider_balance", fake_query_provider_balance)

    try:
        register_balance_enricher("tier", tier_enricher, plugin_name="oai_tier")
        response = await channels_route.query_channel_balance(
            token="admin",
            provider_config={
                "engine": "openai",
                "base_url": "https://api.openai.com/v1",
                "api_key": "sk-normal-tier-test",
                "preferences": {"enabled_plugins": ["oai_tier"], "balance": {"template": "test"}},
            },
        )
    finally:
        reset_interceptor_registry()

    payload = _json_response_payload(response)
    assert payload["available"] == 12.5
    assert payload["tier"] == "Tier 3"

