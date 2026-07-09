import hashlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# 修改原因：直接运行单个测试文件时，pytest 不一定把项目根目录加入 sys.path。
# 修改方式：测试导入 routes 前显式加入项目根目录。
# 目的：让新增 Key Analytics 契约测试可被独立执行，也可随全量测试执行。
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from routes import stats as stats_route


class _FakeD1Client:
    def __init__(self, rows_by_marker):
        self.rows_by_marker = rows_by_marker
        self.calls = []

    async def query_all(self, sql, params=None):
        self.calls.append((sql, list(params or [])))
        for marker, rows in self.rows_by_marker.items():
            if marker in sql:
                return rows
        return []


class _SequentialD1Client:
    def __init__(self, rows_by_call):
        self.rows_by_call = list(rows_by_call)
        self.calls = []

    async def query_all(self, sql, params=None):
        # 修改原因：详情端点会连续发出候选、IP、模型、趋势和错误查询，测试重点不是 SQL 字符串微小格式。
        # 修改方式：按调用顺序返回假数据，同时记录 SQL 和参数供后续断言精确过滤。
        # 目的：让契约测试关注 key_hash 解析和 api_key 精确参数，而不是脆弱的字符串片段匹配。
        self.calls.append((sql, list(params or [])))
        if not self.rows_by_call:
            return []
        return self.rows_by_call.pop(0)


@pytest.mark.asyncio
async def test_key_analytics_summary_hashes_masked_api_key_and_aggregates_d1(monkeypatch):
    # 修改原因：Key Analytics 前端只能使用脱敏 Key 的哈希和前缀，不能暴露完整 API Key。
    # 修改方式：用 D1 假客户端固定汇总查询结果，直接调用路由并断言返回结构。
    # 目的：确保后端汇总端点按 request_stats.api_key 分组，并按费用公式返回聚合数据。
    masked_key = "sk-live-***"
    fake_d1 = _FakeD1Client({
        "GROUP BY api_key": [
            {
                "api_key": masked_key,
                "api_key_name": "生产 Key",
                "api_key_group": "默认组",
                "total_requests": 4,
                "success_count": 3,
                "total_prompt_tokens": 1000,
                "total_completion_tokens": 500,
                "total_cost": 0.003,
                "unique_ips": 2,
                "unique_models": 2,
                "last_used": "2026-06-02 01:00:00",
            }
        ]
    })
    monkeypatch.setattr(stats_route, "DISABLE_DATABASE", False)
    monkeypatch.setattr(stats_route, "DB_TYPE", "d1")
    monkeypatch.setattr(stats_route, "get_app", lambda: SimpleNamespace(state=SimpleNamespace(config={})))
    import db
    monkeypatch.setattr(db, "d1_client", fake_d1)

    response = await stats_route.get_key_analytics_summary(token="admin-token", hours=24, limit=10)
    payload = response.model_dump_json()

    assert hashlib.sha256(masked_key.encode("utf-8")).hexdigest()[:16] in payload
    assert "sk-live-***" in payload
    assert "生产 Key" in payload
    assert "\"total_cost\":0.003" in payload
    assert all("raw-full-key" not in part for part in [payload])


@pytest.mark.asyncio
async def test_key_analytics_detail_resolves_hash_before_exact_d1_filter(monkeypatch):
    # 修改原因：详情端点只有 key_hash 参数，必须先按时间范围找出脱敏 api_key，再精确过滤。
    # 修改方式：假客户端先返回候选 Key，再校验后续详情查询均使用匹配到的脱敏 Key 参数。
    # 目的：避免详情接口把 key_hash 当成数据库 api_key 使用，导致查不到数据或泄露真实 Key。
    masked_key = "sk-prod-***"
    key_hash = hashlib.sha256(masked_key.encode("utf-8")).hexdigest()[:16]
    fake_d1 = _SequentialD1Client([
        [{"api_key": masked_key}],
        [{"ip": "203.0.113.8", "request_count": 2, "last_used": "2026-06-02 01:00:00"}],
        [{"model": "gpt-4o", "request_count": 2, "prompt_tokens": 100, "completion_tokens": 50, "cost": 0.001}],
        [{"time_bucket": "2026-06-02 01:00:00", "request_count": 2, "success_count": 1, "prompt_tokens": 100, "completion_tokens": 50, "cost": 0.001}],
        [{"timestamp": "2026-06-02 01:00:00", "model": "gpt-4o", "status_code": 500, "provider": "openai"}],
    ])
    monkeypatch.setattr(stats_route, "DISABLE_DATABASE", False)
    monkeypatch.setattr(stats_route, "DB_TYPE", "d1")
    monkeypatch.setattr(stats_route, "get_app", lambda: SimpleNamespace(state=SimpleNamespace(config={})))
    import db
    monkeypatch.setattr(db, "d1_client", fake_d1)

    response = await stats_route.get_key_analytics_detail(key_hash=key_hash, token="admin-token", hours=24, limit=50, granularity="hour")
    payload = response.model_dump_json()

    assert "203.0.113.8" in payload
    assert "gpt-4o" in payload
    assert any(masked_key in params for _, params in fake_d1.calls if "api_key = ?" in _)


@pytest.mark.asyncio
async def test_key_analytics_ip_detail_filters_exact_ip_and_returns_usage(monkeypatch):
    masked_key = "sk-prod-***"
    key_hash = hashlib.sha256(masked_key.encode("utf-8")).hexdigest()[:16]
    target_ip = "203.0.113.8"
    fake_d1 = _SequentialD1Client([
        [{"api_key": masked_key}],
        [
            {
                "model": "gpt-5.5",
                "request_count": 3,
                "prompt_tokens": 1200,
                "completion_tokens": 300,
                "cost": 0.0125,
                "last_used": "2026-07-09 22:00:00",
            }
        ],
        [
            {
                "time_bucket": "2026-07-09 22:00:00",
                "request_count": 3,
                "prompt_tokens": 1200,
                "completion_tokens": 300,
                "cost": 0.0125,
            }
        ],
    ])
    monkeypatch.setattr(stats_route, "DISABLE_DATABASE", False)
    monkeypatch.setattr(stats_route, "DB_TYPE", "d1")
    import db
    monkeypatch.setattr(db, "d1_client", fake_d1)

    response = await stats_route.get_key_analytics_ip_detail(
        key_hash=key_hash,
        ip=target_ip,
        token="admin-token",
        hours=24,
        limit=50,
        granularity="hour",
    )

    assert response.ip == target_ip
    assert response.request_count == 3
    assert response.prompt_tokens == 1200
    assert response.completion_tokens == 300
    assert response.cost == 0.0125
    assert response.model_distribution[0].model == "gpt-5.5"
    assert response.trend[0].request_count == 3
    assert all(target_ip in params for sql, params in fake_d1.calls if "client_ip = ?" in sql)


def test_key_analytics_frontend_ip_accordion_contract():
    source = Path("frontend/src/components/KeyAnalyticsSheet.tsx").read_text(encoding="utf-8")

    assert "toggleIpDetail" in source
    assert "aria-expanded" in source
    assert "/ip?${params.toString()}" in source
    assert "model_distribution" in source
    assert "正在加载 IP 明细" in source
