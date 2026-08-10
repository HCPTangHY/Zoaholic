import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

# 修改原因：新增 Gemini CLI OAuth 渠道需要先用无网络测试固定 Google OAuth 参数和渠道注册行为。
# 修改方式：从当前文件向上查找项目根目录并插入 sys.path，保持单文件运行和 tests/ 全量运行都能导入真实代码。
# 目的：防止实现依赖运行目录，确保后续实现改动不会破坏 OAuth 登录、刷新和 Bearer 认证适配。
ROOT = next(
    parent for parent in Path(__file__).resolve().parents
    if (parent / "core").is_dir() and (parent / "routes").is_dir()
)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class FakeTokenResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        # 修改原因：Gemini CLI OAuth 的 token endpoint 和 userinfo endpoint 都需要无网络测试替身。
        # 修改方式：测试响应对象只实现 json、status_code 和 raise_for_status 这几个 provider 会调用的接口。
        # 目的：固定表单提交与邮箱解析行为，避免测试依赖 Google 真实服务。
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeAsyncClient:
    calls = []
    token_payload = {}
    userinfo_payload = {}
    userinfo_status = 200

    def __init__(self, *args, **kwargs):
        # 修改原因：provider 会分别以不同 timeout 创建 httpx.AsyncClient。
        # 修改方式：记录初始化参数，便于必要时检查请求路径没有绕过替身。
        # 目的：保持测试替身与真实 httpx.AsyncClient 的上下文管理用法兼容。
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, data=None, json=None, headers=None, **kwargs):
        # 修改原因：Google OAuth token endpoint 必须使用 form-urlencoded，而不是 JSON。
        # 修改方式：同时记录 data 与 json 参数，测试中断言 json 没有被使用。
        # 目的：防止以后复用 Claude Code 逻辑时误把 Google token 请求改为 JSON body。
        self.calls.append({"method": "POST", "url": url, "data": data, "json": json, "headers": headers, "kwargs": kwargs})
        return FakeTokenResponse(self.token_payload)

    async def get(self, url, headers=None, **kwargs):
        # 修改原因：授权码交换后需要用 access_token 请求 Google userinfo 获取邮箱。
        # 修改方式：记录 GET 请求并返回可控邮箱数据。
        # 目的：保证 oauth_state 能保存稳定的账号邮箱。
        self.calls.append({"method": "GET", "url": url, "headers": headers, "kwargs": kwargs})
        return FakeTokenResponse(self.userinfo_payload, self.userinfo_status)


@pytest.mark.asyncio
async def test_gemini_cli_exchange_code_posts_form_and_fetches_email(monkeypatch):
    from core.channels import gemini_cli_channel as gc

    FakeAsyncClient.calls = []
    FakeAsyncClient.token_payload = {
        "access_token": "access-code",
        "refresh_token": "refresh-code",
        "token_type": "Bearer",
        "expires_in": 1800,
    }
    FakeAsyncClient.userinfo_payload = {"email": "dev@example.com"}
    FakeAsyncClient.userinfo_status = 200
    monkeypatch.setattr(gc.httpx, "AsyncClient", FakeAsyncClient)

    provider = gc.GeminiCLIProvider()
    before = time.time()
    updated = await provider.exchange_code(
        "auth-code",
        "https://zoaholic.example/v1/oauth/callback",
        config={"providers": [{"engine": "gemini-cli", "token_url": "https://oauth-proxy.example", "project_id": "project-123"}]},
    )

    # 修改原因：Google token exchange 必须带 client_secret，并以 form data 提交到动态 token_url。
    # 修改方式：用替身捕获 POST 参数，同时断言 userinfo 请求使用新 access_token。
    # 目的：保证登录成功后既保存 refresh_token，又能用邮箱作为稳定账号标识。
    assert FakeAsyncClient.calls[0]["method"] == "POST"
    assert FakeAsyncClient.calls[0]["url"] == "https://oauth-proxy.example/token"
    assert FakeAsyncClient.calls[0]["json"] is None
    assert FakeAsyncClient.calls[0]["data"] == {
        "code": "auth-code",
        "client_id": gc.CLIENT_ID,
        "client_secret": gc.CLIENT_SECRET,
        "redirect_uri": "https://zoaholic.example/v1/oauth/callback",
        "grant_type": "authorization_code",
    }
    assert FakeAsyncClient.calls[1]["method"] == "GET"
    assert FakeAsyncClient.calls[1]["url"] == "https://www.googleapis.com/oauth2/v1/userinfo?alt=json"
    assert FakeAsyncClient.calls[1]["headers"] == {"Authorization": "Bearer access-code"}
    assert updated["access_token"] == "access-code"
    assert updated["refresh_token"] == "refresh-code"
    assert updated["token_type"] == "Bearer"
    assert updated["email"] == "dev@example.com"
    assert updated["project_id"] == "project-123"
    assert before + 1790 <= updated["expires_at"] <= time.time() + 1810


@pytest.mark.asyncio
async def test_gemini_cli_refresh_posts_form_and_keeps_old_email(monkeypatch):
    from core.channels import gemini_cli_channel as gc

    FakeAsyncClient.calls = []
    FakeAsyncClient.token_payload = {
        "access_token": "access-new",
        "token_type": "Bearer",
        "expires_in": 3600,
    }
    FakeAsyncClient.userinfo_payload = {}
    FakeAsyncClient.userinfo_status = 200
    monkeypatch.setattr(gc.httpx, "AsyncClient", FakeAsyncClient)

    provider = gc.GeminiCLIProvider()
    updated = await provider.refresh_token({"refresh_token": "refresh-old", "email": "old@example.com", "project_id": "project-old"})

    # 修改原因：Google refresh 通常不返回新的 refresh_token，也不返回邮箱。
    # 修改方式：断言刷新请求为 form data，且构建凭据时保留原 refresh_token 与 email。
    # 目的：避免一次正常刷新后丢失账号身份或后续刷新所需的 refresh_token。
    assert FakeAsyncClient.calls == [{
        "method": "POST",
        "url": gc.TOKEN_URL,
        "data": {
            "client_id": gc.CLIENT_ID,
            "client_secret": gc.CLIENT_SECRET,
            "refresh_token": "refresh-old",
            "grant_type": "refresh_token",
        },
        "json": None,
        "headers": None,
        "kwargs": {},
    }]
    assert updated["access_token"] == "access-new"
    assert updated["refresh_token"] == "refresh-old"
    assert updated["email"] == "old@example.com"
    assert updated["project_id"] == "project-old"


@pytest.mark.asyncio
async def test_gemini_cli_payload_replaces_google_key_with_bearer(monkeypatch):
    from core.channels import gemini_cli_channel as gc

    class Request:
        model = "alias-model"
        stream = True

    async def fake_gemini_payload(request, engine, provider, api_key=None):
        # 修改原因：当前普通 Gemini adapter 使用 x-goog-api-key，但历史实现可能使用 key 查询参数。
        # 修改方式：假 adapter 同时返回 header key 和 query key，覆盖两种清理路径。
        # 目的：保证 Gemini CLI OAuth 不会把 access_token 当作 API key 发送。
        return (
            "https://cloudcode-pa.googleapis.com/models/gemini:streamGenerateContent?alt=sse&key=access-token&trace=1",
            {"Content-Type": "application/json", "x-goog-api-key": "access-token"},
            {"contents": []},
        )

    monkeypatch.setattr(gc, "get_gemini_payload", fake_gemini_payload)

    provider = {"model": [{"gemini-2.5-pro": "alias-model"}], "project_id": "project-123"}
    url, headers, payload = await gc.get_gemini_cli_payload(Request(), "gemini-cli", provider, "access-token")

    # 修改原因：CPA 当前 Gemini CLI 执行路径使用 cloudcode-pa 的 v1internal 端点和 request 包裹结构。
    # 修改方式：断言 adapter 不再沿用普通 Gemini /models URL，而是改用 v1internal、Bearer 头和 Gemini CLI payload。
    # 目的：避免 access_token 泄漏到 URL，同时避免 cloudcode-pa 对 /models 路径返回 404。
    assert url == "https://cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse"
    assert "x-goog-api-key" not in {key.lower(): value for key, value in headers.items()}
    assert headers["Authorization"] == "Bearer access-token"
    assert headers["Accept"] == "application/json"
    assert headers["X-Goog-Api-Client"] == "google-genai-sdk/1.41.0 gl-node/v22.19.0"
    assert headers["User-Agent"].startswith("GeminiCLI/0.34.0/gemini-2.5-pro ")
    assert payload == {"project": "project-123", "request": {"contents": []}, "model": "gemini-2.5-pro"}


@pytest.mark.asyncio
async def test_gemini_cli_passthrough_meta_builds_cloudcode_url_and_bearer_header():
    from core.channels import gemini_cli_channel as gc

    class Request:
        model = "alias-model"
        stream = True

    provider = {"model": [{"gemini-2.5-pro": "alias-model"}]}
    url, headers, payload = await gc.get_gemini_cli_passthrough_meta(Request(), "gemini-cli", provider, "access-token")

    # 修改原因：Gemini 方言透传时只需要构建目标 URL 和认证头，payload 应由渠道级 payload adapter 包裹。
    # 修改方式：断言 passthrough adapter 使用默认 cloudcode-pa v1internal 端点、模型映射和 Bearer 头，并返回空 payload 占位。
    # 目的：让 Gemini 原生入口命中 gemini-cli 渠道时不再回退到普通 Gemini /models 路径。
    assert url == "https://cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse"
    assert headers["Authorization"] == "Bearer access-token"
    assert headers["Accept"] == "application/json"
    assert headers["X-Goog-Api-Client"] == "google-genai-sdk/1.41.0 gl-node/v22.19.0"
    assert headers["User-Agent"].startswith("GeminiCLI/0.34.0/gemini-2.5-pro ")
    assert payload == {}


@pytest.mark.asyncio
async def test_gemini_cli_passthrough_payload_wraps_native_body_and_project():
    from core.channels import gemini_cli_channel as gc

    class Request:
        model = "alias-model"
        stream = False

    provider = {"model": [{"gemini-2.5-pro": "alias-model"}], "preferences": {"project_id": "project-456"}}
    native_payload = {
        "contents": [],
        "tools": [{"function_declarations": [{"name": "fn", "parameters": {"type": "object"}}]}],
    }

    updated = await gc.patch_gemini_cli_passthrough_payload(native_payload, {}, Request(), "gemini-cli", provider, "access-token")

    # 修改原因：Gemini 方言透传的原生 body 仍是普通 Gemini 格式，不能直接发给 cloudcode-pa v1internal。
    # 修改方式：断言渠道级 payload adapter 会补 project、model、request 包裹，并复刻 CPA 对 parametersJsonSchema 的处理。
    # 目的：保证透传路径和普通 OpenAI 转 Gemini CLI 路径使用同一套 v1internal 请求结构。
    assert updated["project"] == "project-456"
    assert updated["model"] == "gemini-2.5-pro"
    assert updated["request"]["contents"] == []
    declaration = updated["request"]["tools"][0]["function_declarations"][0]
    assert "parameters" not in declaration
    assert declaration["parametersJsonSchema"] == {"type": "object"}


def test_gemini_cli_response_unwraps_cloudcode_response_field():
    from core.channels import gemini_cli_channel as gc

    wrapped = {"response": {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}}

    # 修改原因：cloudcode-pa v1internal 返回值外层带 response，普通 Gemini 响应解析器只能处理内层对象。
    # 修改方式：直接断言解包辅助函数去掉 response 外壳，同时保留非包装响应的兼容性。
    # 目的：防止后续端点修复后响应又被误判为空 Gemini 响应。
    assert gc._unwrap_gemini_cli_response_payload(wrapped) == wrapped["response"]
    assert gc._unwrap_gemini_cli_response_payload({"candidates": []}) == {"candidates": []}


def test_gemini_cli_channel_is_registered_as_oauth_gemini_type():
    from core.channels import get_channel
    from core.channels.gemini_cli_channel import DEFAULT_BASE_URL, TOKEN_URL, get_gemini_cli_passthrough_meta, get_gemini_cli_payload, patch_gemini_cli_passthrough_payload

    channel = get_channel("gemini-cli")

    # 修改原因：Gemini CLI 渠道需要在注册表中声明为 gemini 类型，才能命中 Gemini 方言透传逻辑。
    # 修改方式：检查渠道注册后的 type_name、OAuth 标记、默认端点和 adapter 绑定。
    # 目的：防止只实现 provider 但忘记把渠道暴露给请求路由和管理界面。
    assert channel is not None
    assert channel.type_name == "gemini"
    assert channel.default_base_url == DEFAULT_BASE_URL
    assert channel.default_token_url == TOKEN_URL
    assert channel.auth_header == "Authorization: Bearer {api_key}"
    assert channel.is_oauth is True
    assert channel.request_adapter is get_gemini_cli_payload
    assert channel.passthrough_adapter is get_gemini_cli_passthrough_meta
    assert channel.passthrough_payload_adapter is patch_gemini_cli_passthrough_payload
