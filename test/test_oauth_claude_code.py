import base64
import gzip
import json
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

# 修改原因：新增 Claude Code OAuth 对照 CPA 的测试，pytest 从 tests/ 目录直接运行时需要稳定找到项目根目录。
# 修改方式：从当前文件向上查找包含 core/ 和 routes/ 的目录，并放入 sys.path。
# 目的：让这些测试在仓库根目录和单文件运行两种方式下都能导入真实实现。
ROOT = next(
    parent for parent in Path(__file__).resolve().parents
    if (parent / "core").is_dir() and (parent / "routes").is_dir()
)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class FakeTokenResponse:
    def __init__(self, payload: dict, status_code: int = 200, headers: dict | None = None, text: str = ""):
        # 修改原因：Claude Code OAuth provider 需要同时测试成功响应和 429 响应头。
        # 修改方式：测试替身保留 payload、status_code、headers 和 text，并提供 httpx.Response 需要的最小接口。
        # 目的：无需真实访问 Anthropic token endpoint，也能固定 JSON body、Retry-After 和字段解析行为。
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text or json.dumps(payload)

    def json(self):
        return self._payload


class FakeAsyncClient:
    calls = []
    responses = []

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json=None, headers=None, **kwargs):
        # 修改原因：CPA 的 Claude token endpoint 必须使用 JSON body，不应回退到 form-urlencoded。
        # 修改方式：测试替身只记录 json 参数和 headers 参数，供断言检查。
        # 目的：防止后续改动把授权码交换或刷新请求格式改错。
        self.calls.append({"url": url, "json": json, "headers": headers, "kwargs": kwargs})
        if self.responses:
            return self.responses.pop(0)
        return FakeTokenResponse({})

    async def get(self, url, headers=None, **kwargs):
        # 修改原因：fetch_quota 会访问 usage GET 接口，本轮需要固定 subscription_type 从凭据透传到 quota_raw 的行为。
        # 修改方式：测试替身增加 GET 记录和可控响应，不访问真实 Anthropic 端点。
        # 目的：让额度查询测试覆盖请求方法、quota 解析和订阅 tier 透传。
        self.calls.append({"url": url, "headers": headers, "kwargs": kwargs, "method": "GET"})
        if self.responses:
            return self.responses.pop(0)
        return FakeTokenResponse({})


@pytest.mark.asyncio
async def test_claude_code_build_auth_url_uses_cpa_redirect_and_pkce():
    from core.channels.claude_code_channel import CLIENT_ID, DEFAULT_REDIRECT_URI, SCOPES, ClaudeCodeProvider

    provider = ClaudeCodeProvider()

    # 修改原因：CPA 固定使用 localhost:54545/callback；当前路由会读取 provider.localhost_redirect_uri。
    # 修改方式：直接断言 provider 暴露的手动回调地址和授权 URL 参数都等于 CPA 常量。
    # 目的：防止手动 OAuth 登录误用基类默认的 localhost:8080/callback。
    assert provider.redirect_mode == "manual"
    assert provider.localhost_redirect_uri == DEFAULT_REDIRECT_URI

    auth_url, verifier = provider.build_auth_url("state-1", provider.localhost_redirect_uri)
    parsed = urlparse(auth_url)
    params = parse_qs(parsed.query)
    expected_challenge = base64.urlsafe_b64encode(
        __import__("hashlib").sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")

    assert parsed.scheme == "https"
    assert parsed.netloc == "claude.ai"
    assert len(verifier) == 128
    assert params["code"] == ["true"]
    assert params["client_id"] == [CLIENT_ID]
    assert params["redirect_uri"] == [DEFAULT_REDIRECT_URI]
    assert params["scope"] == [SCOPES]
    assert params["code_challenge"] == [expected_challenge]
    assert params["code_challenge_method"] == ["S256"]
    assert params["state"] == ["state-1"]


@pytest.mark.asyncio
async def test_claude_code_fetch_quota_includes_saved_subscription_type(monkeypatch):
    from core.channels import claude_code_channel as cc

    FakeAsyncClient.calls = []
    FakeAsyncClient.responses = [FakeTokenResponse({
        "five_hour": {"utilization": 32.5, "resets_at": "2026-01-01T00:00:00Z"},
    })]
    monkeypatch.setattr(cc.httpx, "AsyncClient", FakeAsyncClient)

    provider = cc.ClaudeCodeProvider()
    result = await provider.fetch_quota({"access_token": "access-token", "subscription_type": "max"})

    # 修改原因：quota_display 从 quota_raw 或 account 读取 subscription_type，fetch_quota 结果必须保留已保存的订阅类型。
    # 修改方式：用假的 usage 响应只返回 five_hour quota，再断言结果额外带上凭据中的 subscription_type。
    # 目的：保证前端刷新额度后仍能显示 Max 这类 tier 标签。
    assert FakeAsyncClient.calls[0]["method"] == "GET"
    assert result["quota_inner"] == 67.5
    assert result["quota_inner_resets_at"] == "2026-01-01T00:00:00Z"
    assert result["subscription_type"] == "max"


@pytest.mark.asyncio
async def test_claude_code_refresh_posts_json_without_scope_and_handles_rotation(monkeypatch):
    from core.channels import claude_code_channel as cc

    FakeAsyncClient.calls = []
    FakeAsyncClient.responses = [FakeTokenResponse({
        "access_token": "access-new",
        "refresh_token": "refresh-new",
        "token_type": "Bearer",
        "expires_in": 3600,
        "account": {"uuid": "acct-1", "email_address": "dev@example.com"},
    })]
    monkeypatch.setattr(cc.httpx, "AsyncClient", FakeAsyncClient)

    provider = cc.ClaudeCodeProvider()
    updated = await provider.refresh_token({"refresh_token": "refresh-old", "email": "old@example.com"})

    # 修改原因：CPA refresh 请求没有 scope 参数，并且 refresh_token rotation 后必须覆盖旧值。
    # 修改方式：断言 JSON body 精确等于 CPA refresh 字段，同时检查新 refresh_token 与邮箱字段。
    # 目的：避免刷新成功后继续保存已失效的旧 refresh_token，或向 Anthropic 发送多余 scope。
    assert FakeAsyncClient.calls[0]["json"] == {
        "client_id": cc.CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": "refresh-old",
    }
    assert "scope" not in FakeAsyncClient.calls[0]["json"]
    assert updated["access_token"] == "access-new"
    assert updated["refresh_token"] == "refresh-new"
    assert updated["email"] == "dev@example.com"
    assert updated["expired"].endswith("Z")


@pytest.mark.asyncio
async def test_claude_code_refresh_429_sets_retry_after_block(monkeypatch):
    from core.channels import claude_code_channel as cc

    FakeAsyncClient.calls = []
    FakeAsyncClient.responses = [FakeTokenResponse({}, 429, {"Retry-After-Ms": "60000"}, "too many")]
    monkeypatch.setattr(cc.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(cc.time, "time", lambda: 1000.0)
    cc._claude_refresh_blocked_until.clear()

    provider = cc.ClaudeCodeProvider()
    with pytest.raises(Exception, match="429"):
        await provider.refresh_token({"refresh_token": "refresh-old"})
    assert cc._claude_refresh_blocked_until["refresh-old"] == 1060.0

    # 修改原因：CPA 在 Retry-After 窗口内不会重复访问 token endpoint。
    # 修改方式：第二次刷新沿用同一个 refresh_token，并断言请求次数没有增加。
    # 目的：防止 429 限流期间的并发或重试继续打到 Anthropic。
    with pytest.raises(Exception, match="blocked"):
        await provider.refresh_token({"refresh_token": "refresh-old"})
    assert len(FakeAsyncClient.calls) == 1


@pytest.mark.asyncio
async def test_claude_code_response_wrapper_decodes_missing_gzip_header():
    from core.channels import claude_code_channel as cc

    class RawResponse:
        status_code = 200
        headers = {}

        async def aread(self):
            # 修改原因：Claude 有时返回 gzip 内容但缺少 Content-Encoding，普通 httpx 不会自动解压这种响应。
            # 修改方式：构造没有 gzip 响应头但 body 有 gzip magic bytes 的响应替身。
            # 目的：固定 Claude Code 渠道的 best-effort gzip magic-byte 解压行为。
            return gzip.compress(b'{"ok": true}')

    wrapped = cc._GzipAwareResponse(RawResponse())
    assert await wrapped.aread() == b'{"ok": true}'
