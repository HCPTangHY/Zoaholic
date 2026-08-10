import base64
import json
import time
from datetime import datetime, timezone
from pathlib import Path
import sys

# 修改原因：pytest 直接运行 tests/ 或 core/test 目录时，项目根目录不一定在 sys.path 中。
# 修改方式：从当前文件向上查找包含 core/ 和 routes/ 的目录，再插入导入路径。
# 目的：让测试在单文件运行和仓库根目录运行时都能稳定找到项目代码。
ROOT = next(
    parent for parent in Path(__file__).resolve().parents
    if (parent / "core").is_dir() and (parent / "routes").is_dir()
)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from urllib.parse import parse_qs, urlparse

import pytest


# 修改原因：Codex OAuth 是新增认证路径，必须先用无网络测试固定凭据刷新、PKCE 和解析行为。
# 修改方式：用假的 httpx.AsyncClient 捕获表单请求，并返回可控 token 响应。
# 目的：避免 OAuth 实现回退时泄露 token、丢失 refresh_token rotation 或错误拼接授权参数。
def _jwt_with_payload(payload: dict) -> str:
    header = {"alg": "none", "typ": "JWT"}

    def encode(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{encode(header)}.{encode(payload)}.signature"


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeAsyncClient:
    calls = []
    response_payload = {}

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, data=None, headers=None):
        self.calls.append({"url": url, "data": data, "headers": headers})
        return FakeResponse(self.response_payload)


def test_codex_provider_resolves_token_url_from_runtime_config():
    from core.oauth.providers.codex import DEFAULT_TOKEN_URL, CodexProvider

    # 修改原因：CodexProvider 不能在启动时把 token_url 烤进实例属性，否则前端保存的新配置不会生效。
    # 修改方式：直接测试 _resolve_token_url 对当前运行时配置的解析结果，覆盖默认值、反代域名和完整 endpoint 三种情况。
    # 目的：保证每次刷新或授权码交换都能使用 app.state.config 中的最新 token_url。
    provider = CodexProvider()
    assert not hasattr(provider, "token_url")
    assert provider._resolve_token_url({}) == DEFAULT_TOKEN_URL
    assert provider._resolve_token_url({"providers": [{"engine": "openai", "token_url": "https://wrong.example"}]}) == DEFAULT_TOKEN_URL
    assert provider._resolve_token_url({"providers": [{"engine": "codex", "token_url": "https://auth-proxy.example"}]}) == "https://auth-proxy.example/oauth/token"
    assert provider._resolve_token_url({"providers": [{"engine": "codex", "token_url": "https://auth-proxy.example/oauth/token"}]}) == "https://auth-proxy.example/oauth/token"


def test_codex_channel_register_oauth_provider_does_not_bake_token_url():
    from core.channels.codex_channel import register_oauth_provider

    class FakeOAuthManager:
        def __init__(self):
            self.providers = {}

        def register_provider(self, name: str, provider):
            self.providers[name] = provider

    oauth_manager = FakeOAuthManager()

    # 修改原因：register_oauth_provider 不能再把启动时 providers 中的 token_url 传入 CodexProvider。
    # 修改方式：即使传入旧 providers 参数，也只注册无 token_url 实例，并用新的运行时配置解析出当前 endpoint。
    # 目的：防止保存 api.yaml 后仍继续请求旧 auth.openai.com 或旧反代地址。
    register_oauth_provider(oauth_manager, [
        {"engine": "codex", "base_url": "https://api-proxy.example/v1", "token_url": "https://old-auth.example"},
    ])

    provider = oauth_manager.providers["codex"]
    assert not hasattr(provider, "token_url")
    assert provider._resolve_token_url({"providers": [{"engine": "codex", "token_url": "https://new-auth.example"}]}) == "https://new-auth.example/oauth/token"
    assert provider.get_default_base_url() == "https://chatgpt.com/backend-api/codex"


@pytest.mark.asyncio
async def test_codex_refresh_token_posts_form_and_handles_rotation(monkeypatch):
    from core.oauth.providers import codex as codex_module

    id_token = _jwt_with_payload({
        "email": "new@example.com",
        "https://api.openai.com/auth": {"chatgpt_account_id": "acc-123"},
    })
    FakeAsyncClient.calls = []
    FakeAsyncClient.response_payload = {
        "access_token": "access-new",
        "refresh_token": "refresh-new",
        "id_token": id_token,
        "token_type": "Bearer",
        "expires_in": 3600,
    }
    from core.channels import codex_channel

    monkeypatch.setattr(codex_channel.httpx, "AsyncClient", FakeAsyncClient)

    provider = codex_module.CodexProvider()
    before = time.time()
    updated = await provider.refresh_token({"refresh_token": "refresh-old", "type": "codex"})

    assert FakeAsyncClient.calls[0]["url"] == codex_module.DEFAULT_TOKEN_URL
    assert FakeAsyncClient.calls[0]["data"] == {
        "client_id": codex_module.CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": "refresh-old",
        "scope": "openid profile email",
    }
    assert updated["access_token"] == "access-new"
    assert updated["refresh_token"] == "refresh-new"
    assert updated["id_token"] == id_token
    assert updated["email"] == "new@example.com"
    assert updated["account_id"] == "acc-123"
    assert before + 3590 <= updated["expires_at"] <= time.time() + 3610


@pytest.mark.asyncio
async def test_codex_refresh_token_uses_runtime_config_token_url(monkeypatch):
    from core.oauth.providers import codex as codex_module

    # 修改原因：refresh_token 是线上请求最常触发的 OAuth token endpoint 调用，必须读取最新 runtime config。
    # 修改方式：用假的 httpx.AsyncClient 捕获请求 URL，并通过 config 参数传入 codex token_url。
    # 目的：验证用户在前端保存 token_url 后，下一次刷新会走新地址而不是启动时旧地址。
    FakeAsyncClient.calls = []
    FakeAsyncClient.response_payload = {
        "access_token": "access-new",
        "refresh_token": "refresh-new",
        "expires_in": 3600,
    }
    from core.channels import codex_channel

    monkeypatch.setattr(codex_channel.httpx, "AsyncClient", FakeAsyncClient)

    provider = codex_module.CodexProvider()
    await provider.refresh_token(
        {"refresh_token": "refresh-old", "type": "codex"},
        config={"providers": [{"engine": "codex", "token_url": "https://auth-proxy.example"}]},
    )

    assert FakeAsyncClient.calls[0]["url"] == "https://auth-proxy.example/oauth/token"


def test_codex_build_auth_url_returns_pkce_verifier_and_challenge():
    from core.oauth.providers.codex import CLIENT_ID, SCOPES, CodexProvider

    provider = CodexProvider()
    auth_url, verifier = provider.build_auth_url("state-1", "http://localhost:1455/auth/callback")
    parsed = urlparse(auth_url)
    params = parse_qs(parsed.query)

    expected_challenge = base64.urlsafe_b64encode(
        __import__("hashlib").sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()

    assert parsed.scheme == "https"
    assert parsed.netloc == "auth.openai.com"
    assert params["client_id"] == [CLIENT_ID]
    assert params["scope"] == [SCOPES]
    assert params["state"] == ["state-1"]
    assert params["code_challenge"] == [expected_challenge]
    assert params["code_challenge_method"] == ["S256"]
    assert params["prompt"] == ["login"]


@pytest.mark.asyncio
async def test_codex_exchange_code_decodes_email_and_account(monkeypatch):
    from core.oauth.providers import codex as codex_module

    id_token = _jwt_with_payload({
        "email": "dev@example.com",
        "https://api.openai.com/auth": {"chatgpt_account_id": "acc-456"},
    })
    FakeAsyncClient.calls = []
    FakeAsyncClient.response_payload = {
        "access_token": "access-code",
        "refresh_token": "refresh-code",
        "id_token": id_token,
        "expires_in": 1800,
    }
    from core.channels import codex_channel

    monkeypatch.setattr(codex_channel.httpx, "AsyncClient", FakeAsyncClient)

    provider = codex_module.CodexProvider()
    updated = await provider.exchange_code("auth-code", "http://callback", "verifier-1")

    assert FakeAsyncClient.calls[0]["data"]["grant_type"] == "authorization_code"
    assert FakeAsyncClient.calls[0]["data"]["code"] == "auth-code"
    assert FakeAsyncClient.calls[0]["data"]["redirect_uri"] == "http://callback"
    assert FakeAsyncClient.calls[0]["data"]["code_verifier"] == "verifier-1"
    assert updated["email"] == "dev@example.com"
    assert updated["account_id"] == "acc-456"


@pytest.mark.asyncio
async def test_oauth_manager_resolve_refreshes_and_masks_state(tmp_path: Path):
    from core.oauth.manager import OAuthManager

    class DummyProvider:
        def __init__(self):
            self.calls = 0

        async def refresh_token(self, credential: dict) -> dict:
            self.calls += 1
            return {
                **credential,
                "access_token": "fresh-access",
                "refresh_token": "fresh-refresh",
                "expires_at": time.time() + 3600,
            }

    state_path = tmp_path / "oauth_state.json"
    manager = OAuthManager(state_path=str(state_path))
    provider = DummyProvider()
    manager._providers["codex"] = provider
    manager._state["Codex-Main"] = {
        "dev@example.com": {
            "type": "codex",
            "email": "dev@example.com",
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "expires_at": 0,
        }
    }

    assert await manager.resolve("Codex-Main", "dev@example.com") == "fresh-access"
    assert provider.calls == 1
    persisted = json.loads(state_path.read_text())
    assert persisted["Codex-Main"]["dev@example.com"]["status"] == "active"
    assert persisted["Codex-Main"]["dev@example.com"]["email"] == "dev@example.com"
    listed = manager.list_accounts(channel_id="Codex-Main")["dev@example.com"]
    assert listed["access_token"] == "***"
    assert listed["refresh_token"] == "***"
    assert await manager.resolve("Codex-Main", "static-key") is None


@pytest.mark.asyncio
async def test_oauth_manager_migrates_flat_state_into_provider_channels(tmp_path: Path):
    from core.oauth.manager import OAuthManager

    # 修改原因：旧 oauth_state.json 以邮箱为第一层 key，相同邮箱在不同渠道会互相覆盖。
    # 修改方式：写入旧扁平结构，并用运行时 providers 配置把 type 映射到第一个同 engine 渠道名。
    # 目的：启动时自动迁移到“渠道名 -> 邮箱 -> 凭据”的新结构，找不到渠道的凭据进入 _unmapped。
    state_path = tmp_path / "oauth_state.json"
    state_path.write_text(json.dumps({
        "codex@example.com": {"type": "codex", "access_token": "codex-access"},
        "gemini@example.com": {"type": "gemini-cli", "access_token": "gemini-access"},
        "unknown@example.com": {"type": "unknown", "access_token": "unknown-access"},
    }), encoding="utf-8")

    manager = OAuthManager(state_path=str(state_path))
    manager.set_config_ref(lambda: {"providers": [
        {"provider": "Codex-Main", "engine": "codex"},
        {"provider": "Codex-Backup", "engine": "codex"},
        {"provider": "GeminiCLI", "engine": "gemini-cli"},
    ]})

    await manager.init()

    expected = {
        "Codex-Main": {"codex@example.com": {"type": "codex", "access_token": "codex-access"}},
        "GeminiCLI": {"gemini@example.com": {"type": "gemini-cli", "access_token": "gemini-access"}},
        "_unmapped": {"unknown@example.com": {"type": "unknown", "access_token": "unknown-access"}},
    }
    assert manager._state == expected
    assert json.loads(state_path.read_text(encoding="utf-8")) == expected


@pytest.mark.asyncio
async def test_oauth_manager_allows_same_email_in_different_channels(tmp_path: Path):
    from core.oauth.manager import OAuthManager

    # 修改原因：同一个邮箱可同时属于多个 OAuth 渠道，必须按渠道名隔离读取。
    # 修改方式：在两个渠道写入相同 key_id 但不同 access_token，并分别调用 resolve。
    # 目的：防止新结构退化为旧的邮箱全局唯一行为。
    manager = OAuthManager(state_path=str(tmp_path / "oauth_state.json"))
    future = time.time() + 3600
    manager._state = {
        "Codex-Main": {"same@example.com": {"type": "codex", "access_token": "access-main", "expires_at": future}},
        "Codex-Backup": {"same@example.com": {"type": "codex", "access_token": "access-backup", "expires_at": future}},
    }

    assert await manager.resolve("Codex-Main", "same@example.com") == "access-main"
    assert await manager.resolve("Codex-Backup", "same@example.com") == "access-backup"


@pytest.mark.asyncio
async def test_oauth_manager_persist_keeps_existing_file_when_replace_fails(tmp_path: Path, monkeypatch):
    from core.oauth.manager import OAuthManager
    import core.oauth.manager as manager_module

    # 修改原因：OpenAI refresh token rotation 使 oauth_state.json 损坏会造成永久凭据丢失。
    # 修改方式：模拟 os.replace 在写完临时文件后失败，检查旧文件仍保持完整且临时文件被清理。
    # 目的：固定 OAuthManager._persist 的原子写语义，避免回退到直接 open("w") 截断正式状态文件。
    state_path = tmp_path / "oauth_state.json"
    state_path.write_text(json.dumps({"Codex-Main": {"dev@example.com": {"access_token": "old-access"}}}), encoding="utf-8")
    manager = OAuthManager(state_path=str(state_path))
    manager._state = {"Codex-Main": {"dev@example.com": {"access_token": "new-access"}}}

    def fail_replace(src, dst):
        raise RuntimeError("replace failed")

    monkeypatch.setattr(manager_module.os, "replace", fail_replace)

    with pytest.raises(RuntimeError, match="replace failed"):
        await manager._persist()

    assert json.loads(state_path.read_text(encoding="utf-8")) == {"Codex-Main": {"dev@example.com": {"access_token": "old-access"}}}
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.asyncio
async def test_oauth_manager_resolve_records_refresh_failure_and_persists_error(tmp_path: Path):
    from core.oauth.manager import OAuthManager

    # 修改原因：上游刷新异常如果不记录失败状态，请求路径会持续尝试同一个坏凭据。
    # 修改方式：构造会抛错的 provider，调用 resolve 后检查 error_count、last_error、status 和落盘内容。
    # 目的：让 handler 收到 None 后可以切换其他 key，同时保留排查刷新失败所需的状态。
    class Provider:
        async def refresh_token(self, credential: dict) -> dict:
            raise RuntimeError("upstream refresh rejected")

    state_path = tmp_path / "oauth_state.json"
    manager = OAuthManager(state_path=str(state_path))
    manager.register_provider("codex", Provider())
    manager._state["Codex-Main"] = {
        "dev@example.com": {
            "type": "codex",
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "expires_at": 0,
            "error_count": 2,
        }
    }

    assert await manager.resolve("Codex-Main", "dev@example.com") is None

    cred = manager._state["Codex-Main"]["dev@example.com"]
    assert cred["error_count"] == 3
    assert cred["status"] == "error"
    assert cred["last_error"] == "upstream refresh rejected"
    assert "last_error_at" in cred
    persisted = json.loads(state_path.read_text(encoding="utf-8"))["Codex-Main"]["dev@example.com"]
    assert persisted["error_count"] == 3
    assert persisted["status"] == "error"


@pytest.mark.asyncio
async def test_oauth_manager_resolve_skips_recent_repeated_refresh_failures(tmp_path: Path):
    from core.oauth.manager import OAuthManager

    # 修改原因：连续刷新失败的账号需要短时间熔断，否则每个请求都会触发无效 refresh。
    # 修改方式：写入 5 次失败且 5 分钟内失败过的凭据，断言 resolve 直接返回 None 且不调用 provider。
    # 目的：让 handler 可以在熔断窗口内跳过坏 key，避免放大上游错误和本地写盘压力。
    class Provider:
        def __init__(self):
            self.calls = 0

        async def refresh_token(self, credential: dict) -> dict:
            self.calls += 1
            return {**credential, "access_token": "fresh-access", "expires_at": time.time() + 3600}

    provider = Provider()
    manager = OAuthManager(state_path=str(tmp_path / "oauth_state.json"))
    manager.register_provider("codex", provider)
    manager._state["Codex-Main"] = {
        "dev@example.com": {
            "type": "codex",
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "expires_at": 0,
            "error_count": 5,
            "last_error_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
    }

    assert await manager.resolve("Codex-Main", "dev@example.com") is None
    assert provider.calls == 0


def test_oauth_load_state_backs_up_corrupt_json(tmp_path: Path):
    from core.oauth.state import load_state

    # 修改原因：oauth_state.json 可能因旧的非原子写或手工编辑而损坏，启动时不能直接崩溃。
    # 修改方式：写入非法 JSON 后调用 load_state，检查返回空状态并生成带 corrupt 后缀的备份。
    # 目的：保留损坏文件用于人工恢复，同时让服务能继续启动并等待重新导入凭据。
    state_path = tmp_path / "oauth_state.json"
    state_path.write_text("{ broken json", encoding="utf-8")

    assert load_state(str(state_path)) == {}
    backups = list(tmp_path.glob("oauth_state.json.corrupt.*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{ broken json"


@pytest.mark.asyncio
async def test_oauth_manager_passes_latest_config_to_config_aware_provider(tmp_path: Path):
    from core.oauth.manager import OAuthManager

    # 修改原因：OAuthManager 的生命周期长于 api.yaml 配置，provider 调用时必须读取当前配置引用。
    # 修改方式：用可变 dict 模拟 app.state.config，刷新前修改 token_url，并断言 provider 收到的是修改后的对象。
    # 目的：保证前端保存 token_url 后，下一次 OAuth refresh 不需要重启服务即可生效。
    runtime_config = {"providers": [{"engine": "codex", "token_url": "https://old-auth.example"}]}

    class ConfigAwareProvider:
        def __init__(self):
            self.configs = []

        async def refresh_token(self, credential: dict, config: dict | None = None) -> dict:
            self.configs.append(config)
            return {
                **credential,
                "access_token": "fresh-access",
                "expires_at": time.time() + 3600,
            }

    state_path = tmp_path / "oauth_state.json"
    manager = OAuthManager(state_path=str(state_path))
    manager.set_config_ref(lambda: runtime_config)
    provider = ConfigAwareProvider()
    manager.register_provider("codex", provider)
    manager._state["Codex-Main"] = {
        "dev@example.com": {
            "type": "codex",
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "expires_at": 0,
        }
    }

    runtime_config["providers"][0]["token_url"] = "https://new-auth.example"
    assert await manager.resolve("Codex-Main", "dev@example.com") == "fresh-access"
    assert provider.configs == [runtime_config]


@pytest.mark.asyncio
async def test_handler_oauth_resolver_keeps_static_keys():
    from core.handler import _resolve_oauth_api_key

    class OAuthManager:
        def __init__(self):
            self.calls = []

        async def resolve(self, channel_id: str, key_id: str):
            self.calls.append((channel_id, key_id))
            return "resolved-access" if channel_id == "Codex-Main" and key_id == "dev@example.com" else None

    class State:
        oauth_manager = OAuthManager()

    class App:
        state = State()

    app = App()
    assert await _resolve_oauth_api_key(app, "dev@example.com", channel_id="Codex-Main") == "resolved-access"
    assert await _resolve_oauth_api_key(app, "static-key", channel_id="Codex-Main") == "static-key"
    assert app.state.oauth_manager.calls == [("Codex-Main", "dev@example.com"), ("Codex-Main", "static-key")]
    assert await _resolve_oauth_api_key(App(), None) is None


@pytest.mark.asyncio
async def test_codex_channel_passthrough_uses_bearer_default_url():
    from core.channels.codex_channel import get_codex_passthrough_meta

    url, headers, payload = await get_codex_passthrough_meta(None, "codex", {}, "access-token")

    assert url == "https://chatgpt.com/backend-api/codex/responses"
    assert headers["Authorization"] == "Bearer access-token"
    assert headers["content-type"] == "application/json"
    assert payload == {}


@pytest.mark.asyncio
async def test_oauth_authorize_rejects_unknown_provider():
    from routes.oauth import oauth_authorize

    # 修改原因：前端会把当前 engine 作为 type 传给 authorize，未知类型必须给出明确 400 响应。
    # 修改方式：构造空 provider 注册表，直接调用路由函数检查 JSONResponse 状态码。
    # 目的：避免未知 OAuth 类型被误认为普通服务器错误。
    class OAuthManager:
        _providers = {}

    class Request:
        headers = {}
        app = type("App", (), {"state": type("State", (), {"oauth_manager": OAuthManager()})()})()

    response = await oauth_authorize(type="missing", provider="Codex-Main", request=Request())

    assert response.status_code == 400
    assert b"Unknown OAuth type: missing" in response.body


@pytest.mark.asyncio
async def test_oauth_exchange_requires_code_and_state():
    from routes.oauth import oauth_exchange

    # 修改原因：exchange 端点由前端脚本调用，缺少 code 或 state 时不能进入 token 交换。
    # 修改方式：构造空 JSON 请求，直接断言返回 400 JSONResponse。
    # 目的：让前端捕获失败或手动粘贴错误 URL 时得到明确错误，而不是消耗 pending flow。
    class OAuthManager:
        _providers = {}

    class Request:
        app = type("App", (), {"state": type("State", (), {"oauth_manager": OAuthManager()})()})()

        async def json(self):
            return {}

    response = await oauth_exchange(Request())

    assert response.status_code == 400
    assert b"code and state are required" in response.body


@pytest.mark.asyncio
async def test_oauth_callback_exchanges_code_registers_account_and_posts_message(monkeypatch):
    from routes import oauth as oauth_route
    from routes.oauth import oauth_callback

    # 修改原因：OAuth provider 回跳后，callback 必须消费 state、使用 verifier 换 token，并通知前端刷新账号列表。
    # 修改方式：预置 pending flow，假的 provider 返回邮箱，假的 manager 记录 register 调用。
    # 目的：确保完整浏览器登录流能把新账号写入 OAuth 状态并传回 key_id。
    class Provider:
        def __init__(self):
            self.exchange_calls = []

        async def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None = None) -> dict:
            self.exchange_calls.append((code, redirect_uri, code_verifier))
            return {"access_token": "access-1", "refresh_token": "refresh-1", "email": "dev@example.com"}

    class OAuthManager:
        def __init__(self):
            self.provider = Provider()
            self._providers = {"codex": self.provider}
            self.register_calls = []

        async def register(self, channel_id: str, key_id: str, type_name: str, token_data: dict):
            self.register_calls.append((channel_id, key_id, type_name, token_data))

    manager = OAuthManager()

    class Request:
        app = type("App", (), {"state": type("State", (), {"oauth_manager": manager})()})()

    oauth_route._pending_flows.clear()
    monkeypatch.setattr(oauth_route.time, "time", lambda: 1000.0)
    oauth_route._pending_flows["state-1"] = {
        "type": "codex",
        "verifier": "verifier-1",
        "redirect_uri": "https://zoaholic.example/v1/oauth/callback",
        "created_at": 900.0,
        "provider": "Codex-Main",
    }

    response = await oauth_callback("code-1", "state-1", Request())

    assert response.status_code == 200
    assert manager.provider.exchange_calls == [("code-1", "https://zoaholic.example/v1/oauth/callback", "verifier-1")]
    assert manager.register_calls == [
        ("Codex-Main", "dev@example.com", "codex", {"access_token": "access-1", "refresh_token": "refresh-1", "email": "dev@example.com"})
    ]
    assert "oauth_callback_success" in response.body.decode()
    assert "dev@example.com" in response.body.decode()
    assert "state-1" in response.body.decode()
    assert "state-1" not in oauth_route._pending_flows


@pytest.mark.asyncio
async def test_oauth_callback_rejects_expired_state(monkeypatch):
    from routes import oauth as oauth_route
    from routes.oauth import oauth_callback

    # 修改原因：pending flow 只允许短期有效，超时 callback 不应继续换取 token。
    # 修改方式：把 created_at 设置到 TTL 之外，并断言返回授权超时页面。
    # 目的：保证内存 state 能承担最小 CSRF 和重放保护。
    class OAuthManager:
        _providers = {}

    class Request:
        app = type("App", (), {"state": type("State", (), {"oauth_manager": OAuthManager()})()})()

    oauth_route._pending_flows.clear()
    oauth_route._pending_flows["state-old"] = {
        "type": "codex",
        "verifier": "verifier-old",
        "redirect_uri": "https://zoaholic.example/v1/oauth/callback",
        "created_at": 0.0,
    }
    monkeypatch.setattr(oauth_route.time, "time", lambda: 1000.0)

    response = await oauth_callback("code-old", "state-old", Request())

    assert response.status_code == 400
    assert "授权超时" in response.body.decode()
    assert "state-old" not in oauth_route._pending_flows


@pytest.mark.asyncio
async def test_codex_response_wrappers_capture_ratelimit_headers():
    from core.channels import codex_channel
    from core.middleware import request_info

    class FakeManager:
        def __init__(self):
            self.providers = {}
            self.updates = []

        def register_provider(self, name: str, provider):
            self.providers[name] = provider

        def update_quota(self, channel_id: str, key_id: str, quota_data: dict):
            self.updates.append((channel_id, key_id, quota_data))

    class FakeResponse:
        status_code = 200
        headers = {
            "x-ratelimit-limit-requests": "100",
            "x-ratelimit-remaining-requests": "80",
            "x-ratelimit-limit-tokens": "1000",
            "x-ratelimit-remaining-tokens": "700",
        }

        async def aread(self):
            # 修改原因：fetch_codex_response 复用 Responses API adapter，响应解析会先读取 Responses API 原生 output 再转换为 Chat Completions。
            # 修改方式：测试替身改为返回最小 Responses API 非流式响应，而不是旧 Chat Completions 响应。
            # 目的：让该测试继续聚焦 quota header wrapper，同时符合当前真实 adapter 的输入契约。
            return b'{"output":[{"type":"message","content":[{"type":"output_text","text":"ok"}]}],"usage":{}}'

        async def aiter_text(self):
            yield ''

        async def aiter_bytes(self):
            yield b''

    class FakeClient:
        async def post(self, url, headers=None, content=None, timeout=None):
            return FakeResponse()

    manager = FakeManager()
    codex_channel.register_oauth_provider(manager)
    token = request_info.set({"_oauth_channel_id": "Codex-Main", "_used_api_key": "old@example.com"})
    try:
        chunks = []
        async for chunk in codex_channel.fetch_codex_response(
            FakeClient(),
            "https://chatgpt.com/backend-api/codex/responses",
            {},
            {"model": "gpt-4o-mini", "messages": []},
            "gpt-4o-mini",
            30,
        ):
            chunks.append(chunk)
    finally:
        request_info.reset(token)

    # 修改原因：Codex 额度头只能在响应对象创建后读取，wrapper 必须在不改 OpenAI adapter 的前提下捕获 headers。
    # 修改方式：通过 fake client 的 post 响应头验证 register_oauth_provider 挂载的 manager 收到归一化额度。
    # 目的：防止后续把 codex 渠道改回直接复用 fetch_openai_response 而丢失被动采集。
    # 修改原因：Responses API adapter 会补齐 id、created、model 等动态 Chat Completions 字段，不能按旧原始响应精确比较。
    # 修改方式：只断言本测试关心的转换后文本内容，quota 归一化仍在下方做精确断言。
    # 目的：避免动态字段和 adapter 转换细节遮蔽 OAuth quota wrapper 的测试目标。
    assert chunks[0]["choices"][0]["message"]["content"] == "ok"
    assert manager.updates == [(
        "Codex-Main",
        "old@example.com",
        {
            "quota_inner": 80.0,
            "quota_outer": 70.0,
            "raw": {
                "remaining_requests": "80",
                "limit_requests": "100",
                "remaining_tokens": "700",
                "limit_tokens": "1000",
            },
        },
    )]


def test_codex_oauth_provider_bridge_exports_channel_symbols():
    from core.channels.codex_channel import CodexProvider as ChannelCodexProvider
    from core.oauth.providers.codex import CLIENT_ID, DEFAULT_TOKEN_URL, SCOPES, CodexProvider

    # 修改原因：CodexProvider 已迁移到渠道文件，但旧导入路径仍是外部测试和调用方的兼容边界。
    # 修改方式：断言桥接模块导出的类对象就是渠道文件中的类对象，并保留原常量。
    # 目的：迁移实现位置时不破坏 from core.oauth.providers.codex import ... 的用法。
    assert CodexProvider is ChannelCodexProvider
    assert CLIENT_ID
    assert SCOPES
    assert DEFAULT_TOKEN_URL.endswith("/oauth/token")
