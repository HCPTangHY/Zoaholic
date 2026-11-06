"""OAuth 定时主动刷新的并发与慢速模式回归测试。

背景：定时主动刷新是有意保留的设计。正常模式下最多 5 个账号并发刷新；
某一轮出现刷新失败后，下一轮改为慢速模式（按过期时间排序、每轮限量、依次刷新），
避免继续并发请求导致一批账号同时进入熔断；某一轮没有失败后恢复正常模式。
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.oauth.manager import OAuthManager


NOW = 1_000_000.0


def _make_manager(tmp_path, count: int) -> OAuthManager:
    manager = OAuthManager(state_path=str(tmp_path / "oauth_state.json"))
    # 测试中不需要真实等待
    manager.PROACTIVE_SLOW_DELAY = 0
    manager.PROACTIVE_SLOW_JITTER = 0
    # 全部账号都在刷新边界内；k0 最早过期，序号越大越晚过期
    manager._state = {
        "codex": {
            f"k{i}": {
                "refresh_token": f"rt-{i}",
                "status": "active",
                "expires_at": NOW + 10 + i,
            }
            for i in range(count)
        }
    }
    return manager


class _FakeResolve:
    """记录并发峰值与调用顺序，按 fail_keys 决定返回 None（失败）还是 token。"""

    def __init__(self, fail_keys=()):
        self.fail_keys = set(fail_keys)
        self.active = 0
        self.peak = 0
        self.calls: list[str] = []

    async def __call__(self, channel_id: str, key_id: str):
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.calls.append(key_id)
        try:
            await asyncio.sleep(0.01)
            return None if key_id in self.fail_keys else f"token-{key_id}"
        finally:
            self.active -= 1


@pytest.mark.asyncio
async def test_normal_mode_concurrency_capped_at_five(tmp_path):
    manager = _make_manager(tmp_path, 12)
    fake = _FakeResolve()
    manager.resolve = fake

    refreshed, failed = await manager._proactive_refresh_cycle(now=NOW)

    assert manager.PROACTIVE_CONCURRENCY == 5
    assert (refreshed, failed) == (12, 0)
    assert fake.peak == 5
    assert manager._proactive_slow_mode is False


@pytest.mark.asyncio
async def test_failure_switches_next_cycle_to_slow_sequential_batches(tmp_path):
    manager = _make_manager(tmp_path, 10)
    first = _FakeResolve(fail_keys={"k3"})
    manager.resolve = first

    refreshed, failed = await manager._proactive_refresh_cycle(now=NOW)
    assert (refreshed, failed) == (9, 1)
    assert manager._proactive_slow_mode is True

    # 慢速模式：依次刷新、每轮最多 PROACTIVE_SLOW_BATCH 个、优先处理最早过期的账号
    second = _FakeResolve(fail_keys={"k1"})
    manager.resolve = second
    refreshed, failed = await manager._proactive_refresh_cycle(now=NOW)

    assert second.peak == 1
    assert second.calls == ["k0", "k1", "k2", "k3"]
    assert (refreshed, failed) == (3, 1)
    # 仍有失败，继续保持慢速模式
    assert manager._proactive_slow_mode is True


@pytest.mark.asyncio
async def test_clean_slow_cycle_returns_to_normal_mode(tmp_path):
    manager = _make_manager(tmp_path, 6)
    manager._proactive_slow_mode = True
    slow = _FakeResolve()
    manager.resolve = slow

    refreshed, failed = await manager._proactive_refresh_cycle(now=NOW)
    assert (refreshed, failed) == (manager.PROACTIVE_SLOW_BATCH, 0)
    assert slow.peak == 1
    assert manager._proactive_slow_mode is False

    # 恢复正常模式后，下一轮重新并发处理全部候选
    normal = _FakeResolve()
    manager.resolve = normal
    refreshed, failed = await manager._proactive_refresh_cycle(now=NOW)
    assert (refreshed, failed) == (6, 0)
    assert normal.peak == 5


@pytest.mark.asyncio
async def test_resolve_exception_counts_as_failure(tmp_path):
    manager = _make_manager(tmp_path, 2)

    async def _boom(channel_id, key_id):
        raise RuntimeError("upstream 400")

    manager.resolve = _boom
    refreshed, failed = await manager._proactive_refresh_cycle(now=NOW)

    assert (refreshed, failed) == (0, 2)
    assert manager._proactive_slow_mode is True


@pytest.mark.asyncio
async def test_no_candidates_keeps_mode_unchanged(tmp_path):
    manager = _make_manager(tmp_path, 0)
    manager._proactive_slow_mode = True
    assert await manager._proactive_refresh_cycle(now=NOW) == (0, 0)
    assert manager._proactive_slow_mode is True
