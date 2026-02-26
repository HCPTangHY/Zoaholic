import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.handler import ModelRequestHandler


class _DummyApp:
    pass


def _new_handler() -> ModelRequestHandler:
    return ModelRequestHandler(_DummyApp(), lambda: {}, lambda *args, **kwargs: None)


@pytest.mark.asyncio
async def test_weight_slots_do_not_expand_attempt_list_for_single_provider():
    """单渠道即使被权重展开成多个槽位，也只应尝试一次该渠道。"""
    handler = _new_handler()
    providers = [{"provider": "p1"}] * 100

    for _ in range(5):
        ordered = await handler._build_attempt_providers(
            providers,
            request_model_name="demo-model",
            scheduling_algorithm="fixed_priority",
            advance_cursor=True,
        )
        assert [p["provider"] for p in ordered] == ["p1"]


@pytest.mark.asyncio
async def test_weight_slots_preserve_weighted_first_provider_distribution():
    """固定优先级 + 权重槽位时，通过移动起点保持首选渠道的权重占比。"""
    handler = _new_handler()
    # 模拟 weighted_round_robin 输出槽位：A:3, B:1
    providers = [
        {"provider": "A"},
        {"provider": "B"},
        {"provider": "A"},
        {"provider": "A"},
    ]

    first_providers = []
    for _ in range(8):
        ordered = await handler._build_attempt_providers(
            providers,
            request_model_name="demo-model",
            scheduling_algorithm="fixed_priority",
            advance_cursor=True,
        )
        first_providers.append(ordered[0]["provider"])

    # 8 次请求里，A/B 约为 3:1（即 A=6, B=2）
    assert first_providers.count("A") == 6
    assert first_providers.count("B") == 2


@pytest.mark.asyncio
async def test_round_robin_keeps_rotating_on_unique_providers():
    """非 fixed_priority 下，唯一渠道列表保持轮转。"""
    handler = _new_handler()
    providers = [
        {"provider": "A"},
        {"provider": "B"},
        {"provider": "C"},
    ]

    first_providers = []
    for _ in range(6):
        ordered = await handler._build_attempt_providers(
            providers,
            request_model_name="demo-model",
            scheduling_algorithm="round_robin",
            advance_cursor=True,
        )
        first_providers.append(ordered[0]["provider"])

    assert first_providers == ["A", "B", "C", "A", "B", "C"]
