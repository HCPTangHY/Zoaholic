from pathlib import Path
import sys
from urllib.parse import parse_qs, urlparse

import pytest

# 修改原因：pytest 直接运行本文件时，项目根目录不一定已经位于 sys.path。
# 修改方式：从测试文件向上查找包含 core/ 和 routes/ 的目录，并插入导入路径。
# 目的：让 OAuth 登录模式测试在单文件运行和整仓运行时都能稳定导入项目模块。
ROOT = next(
    parent for parent in Path(__file__).resolve().parents
    if (parent / "core").is_dir() and (parent / "routes").is_dir()
)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class FakeProvider:
    """测试用 OAuth provider，记录 authorize 路由传入的 redirect_uri。"""

    def __init__(self, redirect_mode="auto", localhost_redirect_uri="http://localhost:1999/callback"):
        # 修改原因：OAuth authorize 需要按 provider 声明的回调模式选择 redirect_uri。
        # 修改方式：测试替身显式暴露 redirect_mode 和 localhost_redirect_uri 两个属性。
        # 目的：在不访问真实 OAuth 服务的情况下固定双模式分流行为。
        self.redirect_mode = redirect_mode
        self.localhost_redirect_uri = localhost_redirect_uri
        self.calls = []

    def build_auth_url(self, state: str, redirect_uri: str) -> tuple[str, str]:
        # 修改原因：测试需要确认后端把最终 redirect_uri 传给 provider，而不是只检查响应字段。
        # 修改方式：把 state 和 redirect_uri 记录到 calls，并把它们编码到假授权地址中。
        # 目的：让测试能覆盖 pending flow、响应 mode 和授权 URL 的一致性。
        self.calls.append({"state": state, "redirect_uri": redirect_uri})
        return f"https://provider.example/auth?state={state}&redirect_uri={redirect_uri}", "verifier-1"


class FakeOAuthManager:
    def __init__(self, providers: dict):
        # 修改原因：routes.oauth 直接读取 oauth_manager._providers。
        # 修改方式：测试替身提供同名属性，保持直接函数调用足够轻量。
        # 目的：避免为了一个路由分支测试启动完整 FastAPI 应用。
        self._providers = providers


class FakeRequest:
    def __init__(self, manager: FakeOAuthManager, headers: dict[str, str] | None = None, scheme: str = "http"):
        # 修改原因：_build_redirect_uri 会读取 request.app.state.oauth_manager、headers 和 url.scheme。
        # 修改方式：构造与 Starlette Request 足够兼容的最小对象。
        # 目的：让 redirect_uri 推断逻辑可以被纯单元测试覆盖。
        self.app = type("App", (), {"state": type("State", (), {"oauth_manager": manager})()})()
        self.headers = headers or {}
        self.url = type("URL", (), {"scheme": scheme, "netloc": self.headers.get("host", "local.test")})()


@pytest.mark.asyncio
async def test_oauth_authorize_uses_provider_manual_redirect_by_default():
    from routes import oauth as oauth_routes

    # 修改原因：Codex 一类 provider 只允许固定 localhost 回调，authorize 不传 mode 时应自动进入 manual。
    # 修改方式：用 redirect_mode=manual 的测试 provider 调用路由，并检查 pending flow 与响应 mode。
    # 目的：防止以后把 Codex 又误改回线上域名 callback。
    oauth_routes._pending_flows.clear()
    provider = FakeProvider(redirect_mode="manual", localhost_redirect_uri="http://localhost:1455/auth/callback")
    request = FakeRequest(FakeOAuthManager({"codex": provider}), headers={"host": "zoaholic.example"})

    result = await oauth_routes.oauth_authorize(type="codex", provider="Codex-Main", request=request)

    assert result["mode"] == "manual"
    assert provider.calls[0]["redirect_uri"] == "http://localhost:1455/auth/callback"
    assert oauth_routes._pending_flows[result["state"]]["mode"] == "manual"
    assert oauth_routes._pending_flows[result["state"]]["provider"] == "Codex-Main"
    assert oauth_routes._pending_flows[result["state"]]["redirect_uri"] == "http://localhost:1455/auth/callback"


@pytest.mark.asyncio
async def test_oauth_authorize_uses_forwarded_host_for_auto_redirect():
    from routes import oauth as oauth_routes

    # 修改原因：支持自定义 redirect_uri 的 provider 要回跳 Zoaholic 后端，线上部署通常位于反向代理后。
    # 修改方式：传入 X-Forwarded-Proto 与 X-Forwarded-Host，断言生成 /v1/oauth/callback。
    # 目的：保证 auto 模式在 HTTPS 域名部署下生成 provider 可注册的回调地址。
    oauth_routes._pending_flows.clear()
    provider = FakeProvider(redirect_mode="auto")
    request = FakeRequest(
        FakeOAuthManager({"antigravity": provider}),
        headers={"x-forwarded-proto": "https", "x-forwarded-host": "zoaholic.example"},
    )

    result = await oauth_routes.oauth_authorize(type="antigravity", provider="Antigravity-Main", request=request)
    redirect_uri = provider.calls[0]["redirect_uri"]
    params = parse_qs(urlparse(result["auth_url"]).query)

    assert result["mode"] == "auto"
    assert redirect_uri == "https://zoaholic.example/v1/oauth/callback"
    assert oauth_routes._pending_flows[result["state"]]["redirect_uri"] == redirect_uri
    assert oauth_routes._pending_flows[result["state"]]["provider"] == "Antigravity-Main"
    assert params["redirect_uri"] == [redirect_uri]


def test_codex_provider_declares_manual_redirect_mode():
    from core.channels.codex_channel import CodexProvider

    # 修改原因：authorize 的自动分流依赖 provider 自身声明，Codex 必须固定为 manual。
    # 修改方式：直接断言 CodexProvider 的类属性。
    # 目的：避免 Codex 的 localhost 白名单要求被后续 provider 重构遗漏。
    assert CodexProvider.redirect_mode == "manual"
    assert CodexProvider.localhost_redirect_uri == "http://localhost:1455/auth/callback"
