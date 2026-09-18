import json
from pathlib import Path
import sys

import pytest

# 修改原因：本测试会直接导入 routes 与 core 模块，单文件运行时项目根目录可能不在 sys.path 中。
# 修改方式：从测试文件向上查找同时包含 core/ 和 routes/ 的目录，并在缺失时插入导入路径。
# 目的：让 OAuth 余额入口回归测试在完整测试集和单文件测试两种方式下都能稳定运行。
ROOT = next(
    parent for parent in Path(__file__).resolve().parents
    if (parent / "core").is_dir() and (parent / "routes").is_dir()
)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _json_response_payload(response) -> dict:
    """读取 FastAPI JSONResponse 的 JSON 内容。"""
    # 修改原因：余额路由直接返回 JSONResponse，测试不能依赖 ASGI 客户端才能读取响应体。
    # 修改方式：对 response.body 解码后用 json.loads 还原为 dict。
    # 目的：让路由分流测试只关注返回结构，不引入额外 HTTP 测试栈。
    return json.loads(response.body.decode())


def test_channel_definition_exposes_oauth_marker():
    from core.channels.registry import get_all_channels, get_channel, register_channel, unregister_channel

    # 修改原因：OAuth provider 现在应由渠道注册表统一保存，不能再依赖 main.py 的渠道硬编码。
    # 修改方式：注册一个只传 oauth_provider 的临时渠道，断言它会自动成为 OAuth 渠道，并且 API 输出不泄露 provider 实例。
    # 目的：保证内置渠道和插件渠道都能通过 register_channel 声明 OAuth provider，同时保持前端渠道字典输出稳定。
    engine = "oauth-balance-test-registry"
    provider = object()
    unregister_channel(engine)
    try:
        register_channel(id=engine, type_name="openai", oauth_provider=provider)
        channel = get_channel(engine)
        assert channel is not None
        assert channel.is_oauth is True
        assert channel.oauth_provider is provider
        assert get_all_channels()[engine].oauth_provider is provider
        assert channel.to_dict()["is_oauth"] is True
        assert "oauth_provider" not in channel.to_dict()
    finally:
        unregister_channel(engine)


def test_builtin_oauth_channels_declare_registry_providers():
    from core.channels import get_channel
    from core.channels.claude_code_channel import ClaudeCodeProvider
    from core.channels.codex_channel import CodexProvider
    from core.channels.gemini_cli_channel import GeminiCLIProvider
    from core.channels.vertex_channel import VertexProvider

    # 修改原因：main.py 不应再硬编码 Codex、Claude Code、Gemini CLI 或 Vertex 的 OAuth provider 注册。
    # 修改方式：检查所有内置 OAuth 渠道都把 provider 实例挂在 ChannelDefinition.oauth_provider 上。
    # 目的：让启动流程只扫描注册表即可发现内置渠道，也让插件渠道能走完全相同的声明路径。
    expected = {
        "codex": CodexProvider,
        "claude-code": ClaudeCodeProvider,
        "gemini-cli": GeminiCLIProvider,
        "vertex-gemini": VertexProvider,
        "vertex-claude": VertexProvider,
    }
    for channel_id, provider_class in expected.items():
        channel = get_channel(channel_id)
        assert channel is not None
        assert channel.is_oauth is True
        assert isinstance(channel.oauth_provider, provider_class)
        assert channel.to_dict()["is_oauth"] is True
        assert "oauth_provider" not in channel.to_dict()


@pytest.mark.asyncio
async def test_oauth_channel_balance_uses_oauth_manager_and_merges_results(monkeypatch):
    from core.channels.registry import register_channel, unregister_channel
    from routes import channels as channels_route

    # 修改原因：OAuth 渠道没有 preferences.balance，余额入口必须直接遍历 provider.api 中的账号标识查询 quota。
    # 修改方式：注册临时 OAuth engine，并用假的 OAuthManager 记录 fetch_quota 调用和返回不同账号结果。
    # 目的：固定 /v1/channels/balance 对 OAuth 渠道的统一入口行为，同时保持返回结构可被现有余额展示读取。
    engine = "oauth-balance-test-route"
    unregister_channel(engine)

    class OAuthManager:
        def __init__(self):
            self.calls: list[tuple[str, str]] = []

        async def fetch_quota(self, channel_id: str, key_id: str, force: bool = False):
            # 修改原因：余额入口现在会用 force=True 主动刷新 OAuth quota，测试替身需要匹配真实 OAuthManager 签名。
            # 修改方式：为 fake fetch_quota 增加 force 参数，并断言路由确实请求强制查询。
            # 目的：让本测试继续覆盖路由分流逻辑，而不是因测试替身签名过旧而失败。
            assert force is True
            self.calls.append((channel_id, key_id))
            if key_id == "alpha@example.com":
                return {"quota_inner": 80.0, "quota_outer": 60.0, "raw": {"reset_requests": "10m"}}
            if key_id == "beta@example.com":
                return {"quota_inner": 40.0}
            return None

    oauth_manager = OAuthManager()
    app = type(
        "App",
        (),
        {"state": type("State", (), {"oauth_manager": oauth_manager, "config": {}})()},
    )()
    monkeypatch.setattr(channels_route, "get_app", lambda: app)

    try:
        register_channel(id=engine, type_name="openai", is_oauth=True)
        response = await channels_route.query_channel_balance(
            token="admin",
            provider_config={
                "provider": "OAuth-Balance-Main",
                "engine": engine,
                "api": ["alpha@example.com", "beta@example.com", "missing@example.com"],
                "preferences": {},
            },
        )
    finally:
        unregister_channel(engine)

    payload = _json_response_payload(response)
    assert oauth_manager.calls == [
        ("OAuth-Balance-Main", "alpha@example.com"),
        ("OAuth-Balance-Main", "beta@example.com"),
        ("OAuth-Balance-Main", "missing@example.com"),
    ]
    assert payload["supported"] is True
    assert payload["value_type"] == "percent"
    assert payload["percent"] == 40.0
    assert payload["available"] == 40.0
    assert payload["total"] == 100.0
    assert payload["error"] is None
    assert payload["results"] == {
        "alpha@example.com": {
            "supported": True,
            "value_type": "percent",
            "total": 100.0,
            "used": 40.0,
            "available": 60.0,
            "percent": 60.0,
            "quota_inner": 80.0,
            "quota_outer": 60.0,
            "raw": {"reset_requests": "10m"},
            "error": None,
        },
        "beta@example.com": {
            "supported": True,
            "value_type": "percent",
            "total": 100.0,
            "used": 60.0,
            "available": 40.0,
            "percent": 40.0,
            "quota_inner": 40.0,
            "quota_outer": None,
            "raw": None,
            "error": None,
        },
        "missing@example.com": {
            "supported": True,
            "value_type": "percent",
            "total": None,
            "used": None,
            "available": None,
            "percent": None,
            "quota_inner": None,
            "quota_outer": None,
            "raw": None,
            "error": "OAuth 额度不可用",
        },
    }


@pytest.mark.asyncio
async def test_oauth_channel_balance_returns_single_key_shape(monkeypatch):
    from core.channels.registry import register_channel, unregister_channel
    from routes import channels as channels_route

    # 修改原因：现有前端按单个 Key 调用余额接口，并把响应直接当作 BalanceResult 存入对应行。
    # 修改方式：用 api_key 字符串请求 OAuth 余额，断言顶层直接包含 percent 与 quota 字段，同时保留 results 映射。
    # 目的：让新 OAuth 分流不破坏旧的逐 Key 余额展示数据形状。
    engine = "oauth-balance-test-single"
    unregister_channel(engine)

    class OAuthManager:
        async def fetch_quota(self, channel_id: str, key_id: str, force: bool = False):
            # 修改原因：真实 OAuthManager.fetch_quota 支持 force 参数，余额路由会用它跳过缓存。
            # 修改方式：测试替身同步接受 force 并检查该参数为 True。
            # 目的：保证单 Key 余额测试覆盖最新路由调用约定。
            assert force is True
            assert channel_id == "OAuth-Balance-Main"
            assert key_id == "solo@example.com"
            return {"quota_inner": 90.0, "quota_outer": 70.0}

    app = type(
        "App",
        (),
        {"state": type("State", (), {"oauth_manager": OAuthManager(), "config": {}})()},
    )()
    monkeypatch.setattr(channels_route, "get_app", lambda: app)

    try:
        register_channel(id=engine, type_name="openai", is_oauth=True)
        response = await channels_route.query_channel_balance(
            token="admin",
            provider_config={"provider": "OAuth-Balance-Main", "engine": engine, "api_key": "solo@example.com", "preferences": {}},
        )
    finally:
        unregister_channel(engine)

    payload = _json_response_payload(response)
    assert payload["supported"] is True
    assert payload["value_type"] == "percent"
    assert payload["percent"] == 70.0
    assert payload["available"] == 70.0
    assert payload["quota_inner"] == 90.0
    assert payload["quota_outer"] == 70.0
    assert payload["results"]["solo@example.com"]["percent"] == 70.0
