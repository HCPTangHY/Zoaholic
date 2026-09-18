import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.routing import get_matching_providers
from core.utils import get_model_dict


class _DummyApp:
    """为路由函数提供最小 app.state，避免测试依赖真实服务进程。"""

    def __init__(self):
        self.state = SimpleNamespace(api_list=[], models_list={}, api_keys_db=[])


def _provider(name, models, *, prefix="", pool_sharing=False, upstream_alias=False):
    """构造渠道配置，目的在于只测试 model_prefix 与 pool_sharing 的路由行为。"""
    if upstream_alias:
        model_config = [{"deepseek-ai/DeepSeek-V3": "deepseek-chat"}]
    else:
        model_config = models

    provider = {
        "provider": name,
        "base_url": "https://example.test/v1/chat/completions",
        "api": "sk-test",
        "model": model_config,
        "model_prefix": prefix,
        "preferences": {"weight": 10, "pool_sharing": pool_sharing},
        "groups": ["default"],
    }
    # 缓存要与生产配置加载时一致，测试才能覆盖实际路由入口。
    provider["_model_dict_cache"] = get_model_dict(provider)
    return provider


def _config(providers, model_rules=None):
    return {
        "providers": providers,
        "api_keys": [
            {
                "model": model_rules or ["all"],
                "groups": ["default"],
                "preferences": {},
            }
        ],
    }


@pytest.mark.asyncio
async def test_pool_sharing_adds_prefixed_provider_to_unprefixed_all_pool():
    """开启共享路由池后，无前缀请求应同时命中普通渠道和带前缀渠道。"""
    providers = [
        _provider("ds", ["deepseek-chat"]),
        _provider("sili", ["deepseek-chat"], prefix="[sili]", pool_sharing=True, upstream_alias=True),
    ]

    matched = await get_matching_providers("deepseek-chat", _config(providers), 0, _DummyApp())

    assert [p["provider"] for p in matched] == ["ds", "sili"]
    sili_provider = next(p for p in matched if p["provider"] == "sili")
    # 共享路由池的 provider 副本要保留用户请求名，同时映射到带前缀渠道的真实上游模型。
    assert get_model_dict(sili_provider)["deepseek-chat"] == "deepseek-ai/DeepSeek-V3"


@pytest.mark.asyncio
async def test_pool_sharing_default_false_keeps_prefixed_provider_out_of_unprefixed_pool():
    """pool_sharing 默认关闭时，带前缀渠道不应进入无前缀请求池。"""
    providers = [
        _provider("ds", ["deepseek-chat"]),
        _provider("sili", ["deepseek-chat"], prefix="[sili]", upstream_alias=True),
    ]

    matched = await get_matching_providers("deepseek-chat", _config(providers), 0, _DummyApp())

    assert [p["provider"] for p in matched] == ["ds"]


@pytest.mark.asyncio
async def test_pool_sharing_default_false_applies_to_explicit_model_rule():
    """显式模型规则下也要保持默认关闭，避免前缀渠道被无前缀名称误命中。"""
    providers = [
        _provider("ds", ["deepseek-chat"]),
        _provider("sili", ["deepseek-chat"], prefix="[sili]", upstream_alias=True),
    ]

    matched = await get_matching_providers("deepseek-chat", _config(providers, ["deepseek-chat"]), 0, _DummyApp())

    assert [p["provider"] for p in matched] == ["ds"]


@pytest.mark.asyncio
async def test_prefixed_request_still_hits_prefixed_provider_precisely():
    """带前缀请求仍按外部模型名精准命中，不走无前缀共享逻辑。"""
    providers = [
        _provider("ds", ["deepseek-chat"]),
        _provider("sili", ["deepseek-chat"], prefix="[sili]", pool_sharing=True, upstream_alias=True),
    ]

    matched = await get_matching_providers("[sili]deepseek-chat", _config(providers), 0, _DummyApp())

    assert [p["provider"] for p in matched] == ["sili"]
    assert get_model_dict(matched[0])["[sili]deepseek-chat"] == "deepseek-ai/DeepSeek-V3"


@pytest.mark.asyncio
async def test_pool_sharing_applies_to_explicit_model_rule():
    """显式模型规则也要支持共享路由池，避免只在 all 规则下生效。"""
    providers = [
        _provider("ds", ["deepseek-chat"]),
        _provider("sili", ["deepseek-chat"], prefix="[sili]", pool_sharing=True, upstream_alias=True),
    ]

    matched = await get_matching_providers("deepseek-chat", _config(providers, ["deepseek-chat"]), 0, _DummyApp())

    assert [p["provider"] for p in matched] == ["ds", "sili"]

@pytest.mark.asyncio
async def test_skip_virtual_returns_normal_pool_not_virtual_candidates():
    """get_right_order_providers(skip_virtual=True) 必须返回普通池而不是重新解析虚拟链。

    回归：skip_virtual 之前没有传给首个 get_matching_providers 调用，
    handler 的 _leave_virtual_route 拿到虚拟候选后被 regular 过滤清零，
    普通池回落不可达（线上表现为 All API keys are rate limited）。
    注意链上渠道不冷却——handler 逃生场景里链渠道是 key 级耗尽而非渠道冷却。
    """
    from core.routing import get_right_order_providers
    from core.channel_manager import ChannelManager
    chain = _provider("chain-a", [{"m1": "m1"}])
    native_b = _provider("native-b", [{"m1": "m1"}])
    native_c = _provider("native-c", [{"m1": "m1"}])
    config = _config([chain, native_b, native_c], model_rules=["m1"])
    config["preferences"] = {"virtual_models": {"m1": {
        "enabled": True,
        "chain": [{"type": "channel", "value": "chain-a", "model": "m1"}],
    }}}
    app = _DummyApp()
    app.state.channel_manager = ChannelManager(cooldown_period=60)
    result = await get_right_order_providers("m1", config, 0, "fixed_priority", app, skip_virtual=True)
    # 普通池包含所有挂载该模型的渠道（链渠道以普通候选身份出现是正常的，
    # 其 key 级耗尽由 handler 的 is_all_rate_limited 跳过），但绝不能带虚拟标记。
    names = sorted(p["provider"] for p in result)
    assert names == ["chain-a", "native-b", "native-c"]
    assert not any(p.get("_virtual_route_provider") for p in result)


@pytest.mark.asyncio
async def test_virtual_chain_all_cooled_falls_through_to_normal_candidates():
    from core.routing import get_right_order_providers
    from core.channel_manager import ChannelManager
    virtual = _provider("virtual-primary", [{"gpt-image-2": "gpt-image-2"}])
    normal = _provider("normal-fallback", [{"gpt-image-2": "gpt-image-2"}])
    config = _config([virtual, normal], model_rules=["gpt-image-2"])
    config["preferences"] = {"virtual_models": {"gpt-image-2": {
        "enabled": True,
        "chain": [{"type": "channel", "value": "virtual-primary", "model": "gpt-image-2"}],
    }}}
    manager = ChannelManager(cooldown_period=60)
    # handler 冷却键：(渠道, 请求模型名)。链上 virtual-primary 刚失败被冷却。
    await manager.exclude_model("virtual-primary", "gpt-image-2")
    app = _DummyApp()
    app.state.channel_manager = manager
    result = await get_right_order_providers("gpt-image-2", config, 0, "fixed_priority", app)
    # 回落到普通池，且冷却中的链上渠道必须被排除，不能在回落时被立刻重试。
    assert [p["provider"] for p in result] == ["normal-fallback"]
    assert not any(p.get("_virtual_route_provider") for p in result)


@pytest.mark.asyncio
async def test_virtual_fallback_pool_filters_cooled_chain_members():
    """普通池回落同样走冷却过滤：只有冷却中的链上渠道被剔除，其余候选保留。"""
    from core.routing import get_right_order_providers
    from core.channel_manager import ChannelManager
    chain = _provider("chain-a", [{"m1": "m1"}])
    native_b = _provider("native-b", [{"m1": "m1"}])
    native_c = _provider("native-c", [{"m1": "m1"}])
    config = _config([chain, native_b, native_c], model_rules=["m1"])
    config["preferences"] = {"virtual_models": {"m1": {
        "enabled": True,
        "chain": [{"type": "channel", "value": "chain-a", "model": "m1"}],
    }}}
    manager = ChannelManager(cooldown_period=60)
    await manager.exclude_model("chain-a", "m1")
    app = _DummyApp()
    app.state.channel_manager = manager
    result = await get_right_order_providers("m1", config, 0, "fixed_priority", app)
    names = sorted(p["provider"] for p in result)
    assert names == ["native-b", "native-c"]


@pytest.mark.asyncio
async def test_virtual_fallback_single_candidate_kept_despite_cooldown():
    """普通池仅剩链上渠道一个候选时沿用既有 num>1 豁免，保持单渠道可用性。"""
    from core.routing import get_right_order_providers
    from core.channel_manager import ChannelManager
    chain_only = _provider("chain-only", [{"m2": "m2"}])
    config = _config([chain_only], model_rules=["m2"])
    config["preferences"] = {"virtual_models": {"m2": {
        "enabled": True,
        "chain": [{"type": "channel", "value": "chain-only", "model": "m2"}],
    }}}
    manager = ChannelManager(cooldown_period=60)
    await manager.exclude_model("chain-only", "m2")
    app = _DummyApp()
    app.state.channel_manager = manager
    result = await get_right_order_providers("m2", config, 0, "fixed_priority", app)
    assert [p["provider"] for p in result] == ["chain-only"]
