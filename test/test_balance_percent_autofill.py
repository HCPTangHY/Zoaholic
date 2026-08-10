import sys
from pathlib import Path

import pytest

# 修改原因：本测试直接调用 core.balance.query_provider_balance，单文件运行时需要导入当前仓库源码。
# 修改方式：从测试文件向上查找包含 core/ 的项目根目录，并把它加入 sys.path。
# 目的：确保 percent 自动补算测试覆盖本仓库实现，而不是环境中的同名模块。
ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / "core").is_dir())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class FakeResponse:
    def __init__(self, payload):
        # 修改原因：query_provider_balance 只需要 json、text 和 raise_for_status 三个响应能力。
        # 修改方式：用最小 fake response 保存 payload，并按真实响应接口返回数据。
        # 目的：验证余额字段归一化逻辑，不向外部余额接口发真实请求。
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class FakeClient:
    def __init__(self, payload):
        # 修改原因：余额补算测试只关注响应解析，不依赖 httpx 客户端实现。
        # 修改方式：记录测试 payload，get/post 都返回同一个 fake response。
        # 目的：让测试稳定覆盖 GET 与配置解析后的结果处理路径。
        self._payload = payload

    async def get(self, url, headers=None, timeout=None):
        return FakeResponse(self._payload)

    async def post(self, url, headers=None, timeout=None):
        return FakeResponse(self._payload)


def make_provider(mapping):
    # 修改原因：query_provider_balance 从 provider.preferences.balance 读取 endpoint 和 mapping。
    # 修改方式：构造最小 provider 配置，并显式关闭认证头依赖。
    # 目的：把测试输入限制在 amount 模式字段组合本身。
    return {
        "provider": "Percent Autofill Test",
        "base_url": "https://example.test/v1",
        "api": "sk-test",
        "preferences": {
            "balance": {
                "endpoint": "/balance",
                "method": "GET",
                "auth": "none",
                "mapping": {"value_type": "'amount'", **mapping},
            }
        },
    }


@pytest.mark.asyncio
async def test_amount_balance_autofills_percent_from_available_and_total():
    from core.balance import query_provider_balance

    result = await query_provider_balance(
        FakeClient({"data": {"total": 250_000_000, "available": 178_800_000}}),
        make_provider({"total": "data.total", "available": "data.available"}),
    )

    assert result["used"] == 71_200_000
    assert result["percent"] == 71.52


@pytest.mark.asyncio
async def test_amount_balance_autofills_percent_from_used_and_total():
    from core.balance import query_provider_balance

    result = await query_provider_balance(
        FakeClient({"data": {"total": 250_000_000, "used": 71_200_000}}),
        make_provider({"total": "data.total", "used": "data.used"}),
    )

    assert result["available"] == 178_800_000
    assert result["percent"] == 71.52
