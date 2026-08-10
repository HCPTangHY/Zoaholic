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
