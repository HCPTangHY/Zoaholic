import json
from pathlib import Path
import sys

# 修改原因：新增测试文件会被单独运行，项目根目录不一定在 Python 导入路径中。
# 修改方式：从当前文件向上查找包含 core/ 和 routes/ 的目录，并在缺失时插入 sys.path。
# 目的：让 quota 与 rename 的回归测试在仓库根目录和单文件运行两种方式下都能导入项目模块。
ROOT = next(
    parent for parent in Path(__file__).resolve().parents
    if (parent / "core").is_dir() and (parent / "routes").is_dir()
)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest


class QuotaFakeResponse:
    def __init__(self, headers: dict[str, str]):
        self.headers = headers


class QuotaFakeAsyncClient:
    calls: list[dict] = []
    response_headers: dict[str, str] = {}

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json=None, headers=None):
        # 修改原因：Codex OAuth 的 /models scope 不足，主动额度查询必须改为轻量 chat completions 请求。
        # 修改方式：假的 AsyncClient 记录 POST JSON payload、headers 和构造时 timeout，供测试断言。
        # 目的：避免实现回退到旧的 GET /models 额度查询路径。
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": self.kwargs.get("timeout")})
        return QuotaFakeResponse(self.response_headers)


@pytest.mark.asyncio
async def test_codex_fetch_quota_reads_rate_limit_headers(monkeypatch):
    from core.channels import codex_channel as codex_module

    # 修改原因：Codex 没有独立额度接口，旧的 GET /models 会因 scope 不足返回 403。
    # 修改方式：用假的 httpx.AsyncClient 捕获轻量 POST /chat/completions，并返回可控 ratelimit 响应头。
    # 目的：固定 quota_inner、quota_outer 和 raw 字段的统一返回结构，避免后续前端无法稳定展示双弧。
    QuotaFakeAsyncClient.calls = []
    QuotaFakeAsyncClient.response_headers = {
        "x-ratelimit-limit-requests": "100",
        "x-ratelimit-remaining-requests": "75",
        "x-ratelimit-reset-requests": "10m",
        "x-ratelimit-limit-tokens": "1000",
        "x-ratelimit-remaining-tokens": "900",
        "x-ratelimit-reset-tokens": "5d",
    }
    from core.channels import codex_channel

    monkeypatch.setattr(codex_channel.httpx, "AsyncClient", QuotaFakeAsyncClient)

    provider = codex_module.CodexProvider()
    quota = await provider.fetch_quota(
        {"access_token": "access-1"},
        config={"providers": [{"engine": "codex", "base_url": "https://api-proxy.example/v1"}]},
    )

    assert QuotaFakeAsyncClient.calls == [
        {
            "url": "https://api-proxy.example/v1/responses",
            "json": {
                "model": "gpt-5.3-codex",
                "stream": True,
                "store": False,
                "instructions": "Reply with one word.",
                "input": [{"role": "user", "content": "hi"}],
            },
            "headers": {
                "Authorization": "Bearer access-1",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "User-Agent": "codex_cli_rs/0.118.0 (Mac OS 26.3.1; arm64) iTerm.app/3.6.9",
            },
            "timeout": 20,
        }
    ]
    assert quota == {
        "quota_inner": 75.0,
        "quota_outer": 90.0,
        "raw": {
            "remaining_requests": "75",
            "limit_requests": "100",
            "remaining_tokens": "900",
            "limit_tokens": "1000",
            "reset_requests": "10m",
            "reset_tokens": "5d",
        },
    }


@pytest.mark.asyncio
async def test_codex_fetch_quota_returns_none_without_access_token():
    from core.channels.codex_channel import CodexProvider

    # 修改原因：额度查询只能使用 access_token 调用上游 API，缺失凭据时不能发起无效请求。
    # 修改方式：直接传入空凭据并断言返回 None。
    # 目的：让 OAuthManager 和路由可以把不支持或不可查询统一映射为 404。
    assert await CodexProvider().fetch_quota({}) is None


@pytest.mark.asyncio
async def test_oauth_manager_fetch_quota_injects_config(tmp_path: Path):
    from core.oauth.manager import OAuthManager

    # 修改原因：quota 查询和 refresh 一样依赖当前 provider 配置中的 base_url。
    # 修改方式：用支持 config 参数的假 provider 记录收到的 credential 与 config。
    # 目的：确保 OAuthManager.fetch_quota 复用运行时配置注入机制，不读取启动时旧配置。
    # 修改原因：fetch_quota 当前会按 channel_id 查找具体 provider config，而不是传入完整运行时配置对象。
    # 修改方式：测试配置补充 provider 名，后续断言 provider 收到该渠道配置 dict。
    # 目的：让用例贴合额度查询路径中实际使用的配置注入语义。
    runtime_config = {"providers": [{"provider": "Codex-Main", "engine": "codex", "base_url": "https://api-proxy.example/v1"}]}

    class Provider:
        def __init__(self):
            self.calls = []

        async def fetch_quota(self, credential: dict, config: dict | None = None) -> dict:
            # 修改原因：OAuthManager 会在 provider 返回后把 quota 写回同一个 state 字典，测试需要记录调用时快照。
            # 修改方式：存储 credential 的浅拷贝，避免后续缓存写入影响断言。
            # 目的：让测试只验证 config 注入和缓存短路，不依赖可变对象副作用。
            self.calls.append((dict(credential), config))
            return {"quota_inner": 50.0}

    manager = OAuthManager(state_path=str(tmp_path / "oauth_state.json"))
    manager.set_config_ref(lambda: runtime_config)
    provider = Provider()
    manager.register_provider("codex", provider)
    # 修改原因：当前 fetch_quota 会在 token 接近过期时先刷新，配置注入测试不应走刷新分支。
    # 修改方式：给测试凭据补充一个远期 expires_at，保持测试聚焦在 fetch_quota 的 config 参数传递。
    # 目的：避免假 provider 未实现 refresh_token 时干扰本用例断言。
    manager._state["Codex-Main"] = {"old@example.com": {"type": "codex", "access_token": "access-1", "expires_at": 4102444800}}

    assert await manager.fetch_quota("Codex-Main", "old@example.com") == {"quota_inner": 50.0}
    assert provider.calls == [({"type": "codex", "access_token": "access-1", "expires_at": 4102444800}, runtime_config["providers"][0])]

    manager._state["Codex-Main"]["cached@example.com"] = {"type": "codex", "access_token": "access-2", "quota_inner": 77.0}
    assert await manager.fetch_quota("Codex-Main", "cached@example.com") == {"quota_inner": 77.0}
    assert provider.calls == [({"type": "codex", "access_token": "access-1", "expires_at": 4102444800}, runtime_config["providers"][0])]
    assert await manager.fetch_quota("Codex-Main", "missing@example.com") is None


def test_oauth_manager_update_quota_updates_memory_without_immediate_disk_write(tmp_path: Path):
    from core.oauth.manager import OAuthManager

    # 修改原因：被动响应头采集发生在普通请求路径，不能每次请求都同步写 oauth_state.json。
    # 修改方式：调用 update_quota 后只检查内存 state 和 dirty generation，不进入运行中的事件循环触发 30 秒定时落盘。
    # 目的：固定“先写内存、稍后批量落盘”的额度缓存行为。
    manager = OAuthManager(state_path=str(tmp_path / "oauth_state.json"))
    manager._state["Codex-Main"] = {"old@example.com": {"type": "codex", "access_token": "access-1"}}

    manager.update_quota("Codex-Main", "old@example.com", {
        "quota_inner": 75.0,
        "quota_outer": 90.0,
        "raw": {"remaining_requests": "75"},
    })
    manager.update_quota("Codex-Main", "missing@example.com", {"quota_inner": 1.0})

    assert manager._state["Codex-Main"]["old@example.com"]["quota_inner"] == 75.0
    assert manager._state["Codex-Main"]["old@example.com"]["quota_outer"] == 90.0
    assert manager._state["Codex-Main"]["old@example.com"]["quota_raw"] == {"remaining_requests": "75"}
    assert manager._quota_update_generation == 1
    assert not (tmp_path / "oauth_state.json").exists()


@pytest.mark.asyncio
async def test_oauth_manager_rename_moves_state_lock_and_persists(tmp_path: Path):
    from core.oauth.manager import OAuthManager

    # 修改原因：前端重命名 OAuth Key 后，oauth_state.json 的字典键必须同步迁移。
    # 修改方式：直接调用 manager.rename，检查内存 state、账号锁和落盘 JSON 都从旧 key 迁移到新 key。
    # 目的：避免 api.yaml 保存了新标识符而 OAuthManager 仍只能解析旧标识符。
    state_path = tmp_path / "oauth_state.json"
    manager = OAuthManager(state_path=str(state_path))
    manager._state["Codex-Main"] = {"old@example.com": {"type": "codex", "access_token": "access-1"}}
    old_lock = manager._get_lock("Codex-Main", "old@example.com")

    await manager.rename("Codex-Main", "old@example.com", "new@example.com")

    assert "old@example.com" not in manager._state["Codex-Main"]
    assert manager._state["Codex-Main"]["new@example.com"] == {"type": "codex", "access_token": "access-1"}
    assert "Codex-Main:old@example.com" not in manager._locks
    assert manager._locks["Codex-Main:new@example.com"] is old_lock
    assert json.loads(state_path.read_text()) == {
        "Codex-Main": {"new@example.com": {"type": "codex", "access_token": "access-1"}}
    }


@pytest.mark.asyncio
async def test_oauth_manager_rename_rejects_missing_and_duplicate(tmp_path: Path):
    from core.oauth.manager import OAuthManager

    # 修改原因：rename 会改变 OAuth 状态主键，必须拒绝不存在的旧账号和会覆盖其他账号的新账号。
    # 修改方式：分别构造缺失旧 key 与重复新 key 两类输入，断言抛出 ValueError。
    # 目的：防止用户误输入时破坏其他 OAuth 账号状态。
    manager = OAuthManager(state_path=str(tmp_path / "oauth_state.json"))
    manager._state = {
        "Codex-Main": {
            "old@example.com": {"type": "codex"},
            "used@example.com": {"type": "codex"},
        }
    }

    with pytest.raises(ValueError, match="Account not found"):
        await manager.rename("Codex-Main", "missing@example.com", "new@example.com")
    with pytest.raises(ValueError, match="Account already exists"):
        await manager.rename("Codex-Main", "old@example.com", "used@example.com")


@pytest.mark.asyncio
async def test_oauth_routes_expose_quota_and_rename():
    from routes.oauth import get_account_quota, rename_account

    # 修改原因：前端只通过 HTTP API 获取 quota 与同步 rename，路由需要把 manager 方法正确暴露出来。
    # 修改方式：构造假的 Request 和 OAuthManager，直接调用两个路由函数并检查参数传递。
    # 目的：避免后端 manager 已实现但管理端点遗漏，导致前端无法使用新能力。
    class OAuthManager:
        def __init__(self):
            self.rename_calls = []

        async def fetch_quota(self, channel_id: str, key_id: str):
            return {"quota_inner": 88.0} if channel_id == "Codex-Main" and key_id == "old@example.com" else None

        async def rename(self, channel_id: str, old_key_id: str, new_key_id: str):
            self.rename_calls.append((channel_id, old_key_id, new_key_id))

    manager = OAuthManager()

    class Request:
        app = type("App", (), {"state": type("State", (), {"oauth_manager": manager})()})()

        async def json(self):
            return {"provider": "Codex-Main", "new_key_id": "new@example.com"}

    assert await get_account_quota("old@example.com", Request(), provider="Codex-Main") == {"quota_inner": 88.0}
    quota_missing = await get_account_quota("missing@example.com", Request(), provider="Codex-Main")
    assert quota_missing.status_code == 404

    assert await rename_account("old@example.com", Request()) == {
        "message": "Account renamed",
        "old_key_id": "old@example.com",
        "new_key_id": "new@example.com",
    }
    assert manager.rename_calls == [("Codex-Main", "old@example.com", "new@example.com")]


@pytest.mark.asyncio
async def test_oauth_rename_route_validates_new_key_id():
    from routes.oauth import rename_account

    # 修改原因：空的新账号标识会让 api.yaml 和 oauth_state.json 同时出现不可解析的空 key。
    # 修改方式：提交只含空白字符串的 body，断言路由直接返回 400。
    # 目的：在写入 OAuthManager 前拦截无效输入。
    class OAuthManager:
        async def rename(self, channel_id: str, old_key_id: str, new_key_id: str):
            raise AssertionError("rename should not be called for empty new_key_id")

    class Request:
        app = type("App", (), {"state": type("State", (), {"oauth_manager": OAuthManager()})()})()

        async def json(self):
            return {"provider": "Codex-Main", "new_key_id": "   "}

    response = await rename_account("old@example.com", Request())
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_oauth_export_route_returns_unmasked_provider_credentials():
    from routes.oauth import export_credentials

    # 修改原因：管理端需要一个显式导出入口用于迁移或备份，普通账号列表仍然隐藏 token。
    # 修改方式：假的 OAuthManager 在 include_tokens=True 时返回完整凭据，路由按 provider 查询并保留 refresh_token。
    # 目的：防止导出接口误复用脱敏列表，导致备份文件不可用于恢复。
    class OAuthManager:
        def list_accounts(self, channel_id: str | None = None, include_tokens: bool = False):
            assert channel_id == "Codex-Main"
            assert include_tokens is True
            return {
                "dev@example.com": {
                    "type": "codex",
                    "email": "dev@example.com",
                    "access_token": "access-1",
                    "refresh_token": "refresh-1",
                    "expires_at": 123,
                    "status": "active",
                }
            }

    class Request:
        app = type("App", (), {"state": type("State", (), {"oauth_manager": OAuthManager()})()})()

    result = await export_credentials(provider="Codex-Main", request=Request())

    assert result == {
        "provider": "Codex-Main",
        "accounts": [{
            "key_id": "dev@example.com",
            "type": "codex",
            "email": "dev@example.com",
            "refresh_token": "refresh-1",
            "access_token": "access-1",
            "expires_at": 123,
            "status": "active",
        }],
    }


@pytest.mark.asyncio
async def test_oauth_rename_route_updates_provider_api_and_persists(monkeypatch):
    from routes import admin
    from routes.oauth import rename_account

    # 修改原因：OAuth 账号改名后只改 oauth_state.json 会让 api.yaml 继续保存旧 key_id。
    # 修改方式：构造带 api 列表的假 app，调用 rename 路由后断言当前渠道 api 被替换并触发 providers 持久化。
    # 目的：固定“改名一次同时同步运行时 OAuth state 与 api.yaml 配置”的后端契约。
    class OAuthManager:
        def __init__(self):
            self.rename_calls = []

        async def rename(self, channel_id: str, old_key_id: str, new_key_id: str):
            self.rename_calls.append((channel_id, old_key_id, new_key_id))

    persist_calls = []

    async def fake_persist_config(app, sections_to_verify=None, changed_providers=None):
        persist_calls.append({
            "app": app,
            "sections_to_verify": sections_to_verify,
            "changed_providers": changed_providers,
        })

    manager = OAuthManager()
    app = type("App", (), {})()
    app.state = type("State", (), {})()
    app.state.oauth_manager = manager
    app.state.config = {
        "providers": [
            {"provider": "Codex-Main", "engine": "codex", "api": ["old@example.com", "keep@example.com"]},
            {"provider": "Other", "engine": "codex", "api": ["old@example.com"]},
        ]
    }
    monkeypatch.setattr(admin, "_persist_config", fake_persist_config)

    class Request:
        async def json(self):
            return {"provider": "Codex-Main", "new_key_id": "new@example.com"}

    # 修改原因：Python class body 不能可靠引用同一函数内的同名 app 变量。
    # 修改方式：先创建 Request 实例，再把假 app 挂到实例属性上。
    # 目的：让路由测试只验证同步逻辑，不受测试替身作用域影响。
    request = Request()
    request.app = app

    assert await rename_account("old@example.com", request) == {
        "message": "Account renamed",
        "old_key_id": "old@example.com",
        "new_key_id": "new@example.com",
    }
    assert manager.rename_calls == [("Codex-Main", "old@example.com", "new@example.com")]
    assert app.state.config["providers"][0]["api"] == ["new@example.com", "keep@example.com"]
    assert app.state.config["providers"][1]["api"] == ["old@example.com"]
    assert persist_calls == [{
        "app": app,
        "sections_to_verify": ["providers"],
        "changed_providers": {"Codex-Main"},
    }]


@pytest.mark.asyncio
async def test_oauth_manager_copy_channel_state_copies_missing_accounts_only(tmp_path: Path):
    from core.oauth.manager import OAuthManager

    # 修改原因：复制 OAuth 渠道时，api.yaml 会复制账号 key，但 oauth_state.json 没有对应 provider 分组。
    # 修改方式：直接调用 manager.copy_channel_state，断言新渠道获得源渠道凭据且已有同名账号不被覆盖。
    # 目的：避免复制渠道保存后请求用新 provider 查不到 access_token 而返回 401。
    state_path = tmp_path / "oauth_state.json"
    manager = OAuthManager(state_path=str(state_path))
    manager._state = {
        "Source": {
            "dev@example.com": {"type": "codex", "access_token": "source-access", "nested": {"quota": 1}},
            "keep@example.com": {"type": "codex", "access_token": "keep-access"},
        },
        "Target": {
            "keep@example.com": {"type": "codex", "access_token": "target-access"},
        },
    }

    await manager.copy_channel_state("Source", "Target")

    assert manager._state["Target"]["dev@example.com"] == {"type": "codex", "access_token": "source-access", "nested": {"quota": 1}}
    assert manager._state["Target"]["dev@example.com"] is not manager._state["Source"]["dev@example.com"]
    assert manager._state["Target"]["dev@example.com"]["nested"] is not manager._state["Source"]["dev@example.com"]["nested"]
    assert manager._state["Target"]["keep@example.com"] == {"type": "codex", "access_token": "target-access"}
    assert json.loads(state_path.read_text()) == manager._state


@pytest.mark.asyncio
async def test_oauth_copy_provider_route_calls_manager():
    from routes.oauth import copy_provider_oauth_state

    # 修改原因：前端保存复制来的 OAuth 渠道后需要一个后端入口复制 provider 级 OAuth state。
    # 修改方式：用假的 Request 和 OAuthManager 直接调用路由，断言 body 中的 source/target 被校验后传入 manager。
    # 目的：保证新增 HTTP API 不只存在于前端调用代码中。
    class OAuthManager:
        def __init__(self):
            self.copy_calls = []

        async def copy_channel_state(self, source_channel_id: str, target_channel_id: str):
            self.copy_calls.append((source_channel_id, target_channel_id))

    manager = OAuthManager()

    class Request:
        app = type("App", (), {"state": type("State", (), {"oauth_manager": manager})()})()

        async def json(self):
            return {"source_provider": "Source", "target_provider": "Target"}

    assert await copy_provider_oauth_state(Request()) == {
        "message": "OAuth state copied",
        "source_provider": "Source",
        "target_provider": "Target",
    }
    assert manager.copy_calls == [("Source", "Target")]
