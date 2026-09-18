"""子渠道展开 enabled 继承规则回归测试

背景：主渠道禁用后，带显式 enabled=true 的子渠道（前端子渠道启停开关写入）
曾覆盖继承值继续被路由。修复后子渠道 enabled = 父渠道总闸 AND 子渠道自身开关。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config.service import _expand_sub_channels


def _provider(**overrides):
    base = {
        "provider": "p1",
        "engine": "openai",
        "api": ["sk-1"],
        "base_url": "https://api.example.com/v1",
        "model": ["m1"],
    }
    base.update(overrides)
    return base


def _sub(**overrides):
    base = {"engine": "openai", "model": ["m2"]}
    base.update(overrides)
    return base


def test_parent_enabled_sub_inherits_true():
    out = _expand_sub_channels([_provider(sub_channels=[_sub()])])
    sub = [p for p in out if p.get("_is_sub_channel")][0]
    assert sub["enabled"] is True


def test_parent_enabled_sub_explicit_false_stays_disabled():
    out = _expand_sub_channels([_provider(sub_channels=[_sub(enabled=False)])])
    sub = [p for p in out if p.get("_is_sub_channel")][0]
    assert sub["enabled"] is False


def test_parent_disabled_sub_inherits_false():
    out = _expand_sub_channels([_provider(enabled=False, sub_channels=[_sub()])])
    sub = [p for p in out if p.get("_is_sub_channel")][0]
    assert sub["enabled"] is False


def test_parent_disabled_sub_explicit_true_overridden():
    """核心回归：主渠道禁用是总闸，子渠道显式 true 不得越过。"""
    out = _expand_sub_channels([_provider(enabled=False, sub_channels=[_sub(enabled=True)])])
    sub = [p for p in out if p.get("_is_sub_channel")][0]
    assert sub["enabled"] is False


def test_mixed_subs_each_evaluated_independently():
    out = _expand_sub_channels([
        _provider(sub_channels=[_sub(), _sub(enabled=False), _sub(enabled=True)])
    ])
    subs = [p for p in out if p.get("_is_sub_channel")]
    assert [s["enabled"] for s in subs] == [True, False, True]


def test_parent_kept_in_output_regardless():
    out = _expand_sub_channels([_provider(sub_channels=[_sub()])])
    parents = [p for p in out if not p.get("_is_sub_channel")]
    assert len(parents) == 1
