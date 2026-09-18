import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

# 修改原因：Antigravity 渠道需要先用无网络测试固定 OAuth、请求头和请求体协议，避免实现时依赖真实 Google 服务。
# 修改方式：从当前文件向上查找项目根目录并写入 sys.path，让单文件运行和 tests 全量运行都能导入真实代码。
# 目的：保证后续实现改动不会破坏 Antigravity 的 OAuth 登录、Token 刷新、请求伪装和响应解析。
ROOT = next(
    parent for parent in Path(__file__).resolve().parents
    if (parent / "core").is_dir() and (parent / "routes").is_dir()
)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        # 修改原因：OAuth token、userinfo 和 loadCodeAssist 都需要无网络替身响应。
        # 修改方式：只实现渠道 provider 会调用的 status_code、json 和 text 属性。
        # 目的：让测试聚焦协议字段，不访问外部网络。
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeAsyncClient:
    calls = []
    token_payload = {}
    userinfo_payload = {}
    load_code_assist_payload = {}
    # 修改原因：fetchAvailableModels 是 Antigravity quota 和模型列表的数据源，原测试替身只覆盖了 loadCodeAssist。
    # 修改方式：为 fake client 增加可控的 available models 响应，并在 post 中按 endpoint 分流。
    # 目的：新增回归测试可以无网络验证 modelQuotas 解析和 models_adapter 行为。
    available_models_payload = {}
    userinfo_status = 200

    def __init__(self, *args, **kwargs):
        # 修改原因：Antigravity 对 HTTP/1.1 有强要求，测试需要能观察 AsyncClient 初始化参数。
        # 修改方式：保存 kwargs，并保持 httpx.AsyncClient 的异步上下文管理协议。
        # 目的：防止后续 token 或 loadCodeAssist 调用误开启 HTTP/2。
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, data=None, json=None, headers=None, **kwargs):
        # 修改原因：Google OAuth 使用 form-urlencoded，而 Cloud Code Assist 使用 JSON，新增 quota 查询后还会访问 fetchAvailableModels。
        # 修改方式：同时记录 data、json、headers 和 kwargs，按 URL endpoint 返回不同替身响应。
        # 目的：确保 token、loadCodeAssist 和 fetchAvailableModels 三类请求不会互相套用错误的提交格式或请求头。
        self.calls.append({"method": "POST", "url": url, "data": data, "json": json, "headers": headers, "kwargs": kwargs, "client_kwargs": self.kwargs})
        if url.endswith("/v1internal:fetchAvailableModels"):
            return FakeResponse(self.available_models_payload)
        if url.endswith("/v1internal:loadCodeAssist"):
            return FakeResponse(self.load_code_assist_payload)
        return FakeResponse(self.token_payload)

    async def get(self, url, headers=None, **kwargs):
        # 修改原因：授权码交换后需要用 access_token 请求 Google userinfo 获取邮箱。
        # 修改方式：记录 GET 请求并返回可控邮箱数据。
        # 目的：保证 oauth_state 能保存稳定账号标识。
        self.calls.append({"method": "GET", "url": url, "headers": headers, "kwargs": kwargs, "client_kwargs": self.kwargs})
        return FakeResponse(self.userinfo_payload, self.userinfo_status)


@pytest.mark.asyncio
async def test_antigravity_build_auth_url_uses_google_oauth_scopes():
    from core.channels.antigravity_channel import DEFAULT_REDIRECT_URI, SCOPES, AntigravityProvider, CLIENT_ID

    provider = AntigravityProvider()
    auth_url, verifier = provider.build_auth_url("state-1", DEFAULT_REDIRECT_URI)
    parsed = urlparse(auth_url)
    params = parse_qs(parsed.query)

    # 修改原因：Antigravity 使用 Google OAuth installed app，不使用 PKCE，并且必须带 CPA 提取出的额外 Cloud Code scope。
    # 修改方式：断言授权 URL 的关键参数、scope 集合和手动回调模式。
    # 目的：避免后续误复用 Codex/Claude Code 的 OAuth 参数。
    assert provider.type_name == "antigravity"
    assert provider.redirect_mode == "manual"
    assert provider.localhost_redirect_uri == DEFAULT_REDIRECT_URI
    assert parsed.scheme == "https"
    assert parsed.netloc == "accounts.google.com"
    assert params["client_id"] == [CLIENT_ID]
    assert params["response_type"] == ["code"]
    assert params["redirect_uri"] == [DEFAULT_REDIRECT_URI]
    assert params["scope"] == [" ".join(SCOPES)]
    assert "https://www.googleapis.com/auth/cclog" in SCOPES
    assert "https://www.googleapis.com/auth/experimentsandconfigs" in SCOPES
    assert params["access_type"] == ["offline"]
    assert params["prompt"] == ["consent"]
    assert verifier == ""


@pytest.mark.asyncio
async def test_antigravity_refresh_posts_form_with_go_user_agent(monkeypatch):
    from core.channels import antigravity_channel as ag

    FakeAsyncClient.calls = []
    FakeAsyncClient.token_payload = {
        "access_token": "access-new",
        "token_type": "Bearer",
        "expires_in": 3600,
    }
    FakeAsyncClient.userinfo_payload = {}
    FakeAsyncClient.load_code_assist_payload = {}
    monkeypatch.setattr(ag.httpx, "AsyncClient", FakeAsyncClient)

    provider = ag.AntigravityProvider()
    updated = await provider.refresh_token({"refresh_token": "refresh-old", "email": "old@example.com", "project_id": "project-old"})

    # 修改原因：刷新 token 是高频路径，必须保持 Antigravity CPA 提取出的 Go UA，并保留旧 refresh_token、邮箱和项目。
    # 修改方式：断言 refresh 请求的 form data 与 headers，同时检查凭据构建保留旧字段。
    # 目的：防止正常刷新后丢失账号身份或项目标识。
    assert FakeAsyncClient.calls[0]["url"] == ag.TOKEN_URL
    assert FakeAsyncClient.calls[0]["data"] == {
        "client_id": ag.CLIENT_ID,
        "client_secret": ag.CLIENT_SECRET,
        "refresh_token": "refresh-old",
        "grant_type": "refresh_token",
    }
    assert FakeAsyncClient.calls[0]["headers"] == {"User-Agent": "Go-http-client/2.0"}
    assert updated["access_token"] == "access-new"
    assert updated["refresh_token"] == "refresh-old"
    assert updated["email"] == "old@example.com"
    assert updated["project_id"] == "project-old"


def test_antigravity_stream_line_parses_json_lines_without_sse_prefix():
    from core.channels import antigravity_channel as ag

    bare_line = '{"response":{"candidates":[{"content":{"parts":[{"thought":true,"text":"think","thoughtSignature":"sig"}]}}]}}'
    data_line = 'data: {"response":{"candidates":[{"content":{"parts":[{"text":"ok"}]},"finishReason":"STOP"}]}}'

    # 修改原因：Antigravity 流式响应是 JSON Lines，不是标准 SSE，但代理也应容忍反代补上的 data: 前缀。
    # 修改方式：直接测试行解析辅助函数会同时接受裸 JSON 行和 data 行，并去掉 response 外壳。
    # 目的：防止把 JSON Lines 当作普通 Gemini SSE 处理后丢失思考内容或正文。
    thought = ag._parse_antigravity_stream_json_line(bare_line)
    content = ag._parse_antigravity_stream_json_line(data_line)
    assert thought["candidates"][0]["content"]["parts"][0]["thought"] is True
    assert thought["candidates"][0]["content"]["parts"][0]["thoughtSignature"] == "sig"
    assert content["candidates"][0]["content"]["parts"][0]["text"] == "ok"
    assert content["candidates"][0]["finishReason"] == "STOP"


def test_antigravity_non_stream_payload_unwraps_cloud_code_response():
    from core.channels import antigravity_channel as ag

    wrapped = {
        "response": {
            "candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}],
            "usageMetadata": {"totalTokenCount": 3},
        },
        "traceId": "trace-1",
    }

    # 修改原因：Antigravity 非流式响应也带 response 外壳，Gemini 通用解析器不能直接识别。
    # 修改方式：直接固定响应解包辅助函数的输入输出结构，避免后续响应 adapter 回退成空响应。
    # 目的：保证非流式 Antigravity 能转换成 OpenAI chat completions，而不是把 trace 外壳传给下游。
    unwrapped = ag._unwrap_antigravity_response_payload(wrapped)
    assert unwrapped["candidates"][0]["content"]["parts"][0]["text"] == "ok"
    assert unwrapped["usageMetadata"]["totalTokenCount"] == 3


@pytest.mark.asyncio
async def test_antigravity_fetch_quota_preserves_model_quotas_from_list_payload(monkeypatch):
    from core.channels import antigravity_channel as ag

    FakeAsyncClient.calls = []
    FakeAsyncClient.available_models_payload = {
        "models": [
            {
                "name": "gemini-3-pro",
                "displayName": "Gemini 3 Pro",
                "modelProvider": "MODEL_PROVIDER_GOOGLE",
                "quotaInfo": {"remainingFraction": 1, "resetTime": "2026-05-19T06:00:00Z", "isExhausted": False},
            },
            {
                "id": "claude-sonnet-4-5",
                "displayName": "Claude Sonnet 4.5",
                "modelProvider": "MODEL_PROVIDER_ANTHROPIC",
                "quotaInfo": {"remainingFraction": 0.42, "resetTime": "2026-05-19T07:00:00Z", "isExhausted": False},
            },
        ]
    }
    FakeAsyncClient.load_code_assist_payload = {
        "cloudaicompanionProject": "cloud-project-2",
        "paidTier": {"name": "AI Pro", "availableCredits": [{"creditAmount": "120"}]},
    }
    monkeypatch.setattr(ag.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(ag, "get_antigravity_version", lambda: ag._async_return("1.21.9"))

    provider = ag.AntigravityProvider()
    quota = await provider.fetch_quota({"access_token": "access-quota"})

    # 修改原因：真实 fetchAvailableModels 可能返回 list 形态，并且 Antigravity UI 现在把 Gemini 和 External 拆成上下两条弧。
    # 修改方式：测试同时覆盖 name/id、官方 MODEL_PROVIDER_* 和 provider 分组后的 quota_inner/quota_outer。
    # 目的：防止 balance 接口和前端 Key 行再次退回“所有模型取同一个最低值”。
    assert quota["quota_inner"] == 100.0
    assert quota["quota_outer"] == 42.0
    assert quota["raw"]["modelQuotas"] == [
        {
            "model": "gemini-3-pro",
            "displayName": "Gemini 3 Pro",
            "modelProvider": "MODEL_PROVIDER_GOOGLE",
            "remainingFraction": 1,
            "resetTime": "2026-05-19T06:00:00Z",
            "isExhausted": False,
        },
        {
            "model": "claude-sonnet-4-5",
            "displayName": "Claude Sonnet 4.5",
            "modelProvider": "MODEL_PROVIDER_ANTHROPIC",
            "remainingFraction": 0.42,
            "resetTime": "2026-05-19T07:00:00Z",
            "isExhausted": False,
        },
    ]
    assert quota["raw"]["paidTier"]["name"] == "AI Pro"
    assert quota["raw"]["availableCredits"] == [{"creditAmount": "120"}]
    assert quota["raw"]["cloudaicompanionProject"] == "cloud-project-2"


@pytest.mark.asyncio
async def test_antigravity_fetch_quota_groups_provider_minimums_and_ignores_tab_chat(monkeypatch):
    from core.channels import antigravity_channel as ag

    FakeAsyncClient.calls = []
    FakeAsyncClient.available_models_payload = {
        "models": [
            {"name": "gemini-3-pro", "modelProvider": "MODEL_PROVIDER_GOOGLE", "quotaInfo": {"remainingFraction": "0.75"}},
            {"name": "gemini-3-flash", "modelProvider": "MODEL_PROVIDER_GOOGLE", "quotaInfo": {"remainingFraction": 0.31}},
            {"name": "tab_gemini-3-pro", "modelProvider": "MODEL_PROVIDER_GOOGLE", "quotaInfo": {"remainingFraction": 0.01}},
            {"name": "claude-sonnet-4-5", "modelProvider": "MODEL_PROVIDER_ANTHROPIC", "quotaInfo": {"remainingFraction": "0.64"}},
            {"name": "gpt-5", "modelProvider": "MODEL_PROVIDER_OPENAI", "quotaInfo": {"remainingFraction": 0.22}},
            {"name": "chat_gpt-5", "modelProvider": "MODEL_PROVIDER_OPENAI", "quotaInfo": {"remainingFraction": 0.02}},
        ]
    }
    FakeAsyncClient.load_code_assist_payload = {}
    monkeypatch.setattr(ag.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(ag, "get_antigravity_version", lambda: ag._async_return("1.21.9"))

    provider = ag.AntigravityProvider()
    quota = await provider.fetch_quota({"access_token": "access-quota"})

    # 修改原因：前端不再按 engine === 'antigravity' 读取 raw.modelQuotas，后端必须在 fetch_quota 阶段写入正确缓存值。
    # 修改方式：用字符串 remainingFraction、多个 provider 和 tab_/chat_ 干扰项验证 Gemini、External 分组最低值。
    # 目的：确保 OAuthManager 缓存的 quota_inner/quota_outer 可直接驱动通用前端 QuotaBorderOverlay。
    assert quota["quota_inner"] == pytest.approx(31.0)
    assert quota["quota_outer"] == pytest.approx(22.0)


@pytest.mark.asyncio
async def test_fetch_antigravity_models_supports_list_payload(monkeypatch):
    from core.channels import antigravity_channel as ag

    FakeAsyncClient.calls = []
    FakeAsyncClient.available_models_payload = {
        "models": [
            {"name": "gemini-3-pro", "displayName": "Gemini 3 Pro"},
            {"id": "claude-sonnet-4-5", "displayName": "Claude Sonnet 4.5"},
        ]
    }
    monkeypatch.setattr(ag.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(ag, "get_antigravity_version", lambda: ag._async_return("1.21.9"))

    models = await ag.fetch_antigravity_models(None, {"api": "access-models"})

    # 修改原因：模型列表接口和额度接口共享 fetchAvailableModels，列表形态必须同时被 models_adapter 支持。
    # 修改方式：断言 adapter 从 list 中读取 name/id，而不是只读取 dict keys。
    # 目的：保证管理端“获取模型”不会因为上游返回数组而显示空列表。
    assert models == ["gemini-3-pro", "claude-sonnet-4-5"]
