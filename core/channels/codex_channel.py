"""Codex OAuth 渠道适配器。

本文件自包含 Codex OAuth provider、渠道注册和响应头额度采集逻辑。
"""

import base64
import hashlib
import json
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from core.oauth.providers.base import OAuthProvider
from core.channels.openai_responses_channel import (
    fetch_responses_stream,
    fetch_responses_response,
    get_responses_payload,
)
from core.channels.codex_identity import apply_codex_identity_confuse, restore_codex_identity


_oauth_manager = None


def _percentage(remaining: str | None, limit: str | None) -> float | None:
    """计算 ratelimit 剩余额度百分比。"""
    # 修改原因：ratelimit 响应头来自 OpenAI 或反代，可能缺失、为空或不是数字。
    # 修改方式：只在 remaining 与 limit 都可解析且 limit 大于 0 时计算百分比，并裁剪到 0 到 100。
    # 目的：避免异常响应头导致 quota 查询接口或被动采集路径报错。
    if remaining is None or limit is None:
        return None
    try:
        value = float(remaining) / float(limit) * 100
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return round(max(0.0, min(100.0, value)), 1)


def _parse_ratelimit_headers(headers) -> dict | None:
    """从响应头提取统一 quota 数据。优先读 x-codex-*，fallback x-ratelimit-*。"""
    if not headers:
        return None

    result: dict[str, Any] = {}

    # 优先读 x-codex-* 头（chatgpt.com/backend-api/codex 端点）
    primary_used = headers.get("x-codex-primary-used-percent")
    secondary_used = headers.get("x-codex-secondary-used-percent")
    if primary_used is not None:
        try:
            result["quota_outer"] = round(max(0.0, 100.0 - float(primary_used)), 2)
        except (TypeError, ValueError):
            pass
    if secondary_used is not None:
        try:
            result["quota_inner"] = round(max(0.0, 100.0 - float(secondary_used)), 2)
        except (TypeError, ValueError):
            pass
    codex_raw = {}
    for k, v in headers.items():
        kl = k.lower() if isinstance(k, str) else str(k).lower()
        if kl.startswith("x-codex-"):
            codex_raw[kl] = v
    if codex_raw:
        result["raw"] = codex_raw
        if result.get("quota_inner") is not None or result.get("quota_outer") is not None:
            return result

    # Fallback: x-ratelimit-*（api.openai.com 端点）
    remaining_requests = headers.get("x-ratelimit-remaining-requests")
    limit_requests = headers.get("x-ratelimit-limit-requests")
    remaining_tokens = headers.get("x-ratelimit-remaining-tokens")
    limit_tokens = headers.get("x-ratelimit-limit-tokens")
    reset_requests = headers.get("x-ratelimit-reset-requests")
    reset_tokens = headers.get("x-ratelimit-reset-tokens")

    quota_inner = _percentage(remaining_requests, limit_requests)
    quota_outer = _percentage(remaining_tokens, limit_tokens)
    if quota_inner is not None:
        result["quota_inner"] = quota_inner
    if quota_outer is not None:
        result["quota_outer"] = quota_outer

    raw = {
        k: v
        for k, v in {
            "remaining_requests": remaining_requests,
            "limit_requests": limit_requests,
            "remaining_tokens": remaining_tokens,
            "limit_tokens": limit_tokens,
            "reset_requests": reset_requests,
            "reset_tokens": reset_tokens,
        }.items()
        if v is not None
    }
    if raw:
        result.setdefault("raw", {})
        result["raw"].update(raw)
    return result if result else None


def _get_quota_context_from_request() -> tuple[str, str] | None:
    """从当前请求上下文读取 OAuth 渠道名和原始 key_id。"""
    # 修改原因：oauth_state.json 已按 provider name 分层，quota 回写只知道 key_id 会写错同邮箱的其他渠道。
    # 修改方式：优先读取 handler 写入的 _oauth_channel_id，再读取 provider_id/provider；key_id 仍兼容 _oauth_key_id 和 _used_api_key。
    # 目的：不把 access_token 当作账号 key，也不把 quota 写入错误渠道。
    try:
        from core.middleware import request_info

        current_info = request_info.get()
    except Exception:
        return None
    if not isinstance(current_info, dict):
        return None
    channel_id = current_info.get("_oauth_channel_id") or current_info.get("provider_id") or current_info.get("provider")
    key_id = current_info.get("_oauth_key_id") or current_info.get("_used_api_key")
    if not channel_id or not key_id:
        return None
    return str(channel_id), str(key_id)


def _get_identity_confuse_auth_id():
    """从 current_info 获取 OAuth 账号 ID，用作 Codex 身份混淆种子。
    
    Codex 只有 OAuth 模式，永久启用混淆。
    """
    try:
        from core.middleware import request_info

        current_info = request_info.get()
    except Exception:
        return None
    if not isinstance(current_info, dict):
        return None

    return str(current_info.get("_oauth_key_id") or "") or None


def _restore_codex_identity_chunk(chunk, state: dict):
    """按 chunk 类型还原 Codex 身份，兼容当前 adapter 的 str/dict 输出。"""
    # 修改原因：CPA 处理的是原始 bytes，但 Zoaholic 的 Responses adapter 会把上游流转换为 str SSE，非流式会转换为 dict。
    # 修改方式：bytes 直接替换，str 经 UTF-8 编码后替换再解码，dict/list 经 JSON 序列化后替换再解析。
    # 目的：在不重写 Responses adapter 的前提下，对客户端保持身份字段透明。
    if not state or not state.get("replacements"):
        return chunk
    if isinstance(chunk, bytes):
        return restore_codex_identity(chunk, state)
    if isinstance(chunk, str):
        restored = restore_codex_identity(chunk.encode("utf-8"), state)
        return restored.decode("utf-8")
    if isinstance(chunk, (dict, list)):
        try:
            raw = json.dumps(chunk, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            restored = restore_codex_identity(raw, state)
            if restored == raw:
                return chunk
            return json.loads(restored.decode("utf-8"))
        except Exception:
            return chunk
    return chunk


def _store_quota_from_headers(headers) -> None:
    """把响应头中的 quota 数据写入 OAuthManager 内存缓存。"""
    # 修改原因：Codex 普通请求会自然带回 x-ratelimit-*，无需每次打开管理页都主动消耗一次请求额度。
    # 修改方式：wrapper 捕获响应头后解析 quota，并从请求上下文取 channel_id/key_id 调用 OAuthManager.update_quota。
    # 目的：实现被动额度采集，同时避免每次请求同步写磁盘或写错同名账号渠道。
    quota = _parse_ratelimit_headers(headers)
    if not quota or _oauth_manager is None:
        return
    quota_context = _get_quota_context_from_request()
    if not quota_context:
        return
    channel_id, key_id = quota_context
    updater = getattr(_oauth_manager, "update_quota", None)
    if callable(updater):
        updater(channel_id, key_id, quota)


class _QuotaCapturingStreamContext:
    """包装 httpx stream context，在进入上下文后读取 response headers。"""

    def __init__(self, inner_context):
        self._inner_context = inner_context

    async def __aenter__(self):
        response = await self._inner_context.__aenter__()
        # 修改原因：fetch_gpt_response_stream 在内部持有 httpx response，外层 wrapper 只能包住 stream context 才能拿到 headers。
        # 修改方式：在 __aenter__ 返回原 response 前采集 ratelimit headers，不影响后续 OpenAI adapter 读取 body。
        # 目的：在不修改 openai_channel.py 的前提下完成 Codex 被动额度采集。
        _store_quota_from_headers(getattr(response, "headers", None))
        return response

    async def __aexit__(self, exc_type, exc, tb):
        return await self._inner_context.__aexit__(exc_type, exc, tb)


class _QuotaCapturingClient:
    """代理 httpx.AsyncClient，只拦截 post 和 stream 的响应头。"""

    def __init__(self, client):
        self._client = client

    def __getattr__(self, name):
        return getattr(self._client, name)

    async def post(self, *args, **kwargs):
        response = await self._client.post(*args, **kwargs)
        # 修改原因：非流式 OpenAI adapter 直接调用 client.post，只有代理 client 能在不复制解析逻辑的情况下看到 response。
        # 修改方式：post 返回前采集 headers，然后把原 response 原样交给 fetch_openai_response。
        # 目的：复用 OpenAI 非流式解析，同时增加 Codex quota 被动缓存。
        _store_quota_from_headers(getattr(response, "headers", None))
        return response

    def stream(self, *args, **kwargs):
        return _QuotaCapturingStreamContext(self._client.stream(*args, **kwargs))


async def fetch_codex_response_stream(client, url, headers, payload, model, timeout):
    """包装 Responses API 流式 adapter，从响应头采集 quota，并按需做身份混淆还原。"""
    # 修改原因：Codex 身份混淆必须在真正发送上游请求前完成，而 quota 捕获仍需要复用原有 client wrapper。
    # 修改方式：读取当前 OAuth 账号和 provider 开关，先混淆 headers/payload，再把响应 chunk 按 state 还原给客户端。
    # 目的：让上游看到混淆身份，同时保持客户端对请求和响应中的身份标识无感知。
    state = None
    auth_id = _get_identity_confuse_auth_id()
    if auth_id:
        headers, payload, state = apply_codex_identity_confuse(headers, payload, auth_id)

    capturing_client = _QuotaCapturingClient(client)
    async for chunk in fetch_responses_stream(capturing_client, url, headers, payload, model, timeout):
        yield _restore_codex_identity_chunk(chunk, state)


async def fetch_codex_response(client, url, headers, payload, model, timeout):
    """包装 Responses API 非流式 adapter，从响应头采集 quota，并按需做身份混淆还原。"""
    # 修改原因：非流式 Codex 响应也可能在内容或错误中回显混淆身份，需要与流式路径保持一致。
    # 修改方式：请求发送前应用同一套混淆逻辑，adapter 产出的 dict/bytes/str chunk 再统一还原。
    # 目的：避免同一功能在流式和非流式请求之间出现可见差异。
    state = None
    auth_id = _get_identity_confuse_auth_id()
    if auth_id:
        headers, payload, state = apply_codex_identity_confuse(headers, payload, auth_id)

    capturing_client = _QuotaCapturingClient(client)
    async for chunk in fetch_responses_response(capturing_client, url, headers, payload, model, timeout):
        yield _restore_codex_identity_chunk(chunk, state)


AUTH_URL = "https://auth.openai.com/oauth/authorize"
DEFAULT_TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
DEFAULT_REDIRECT_URI = "http://localhost:1455/auth/callback"
SCOPES = "openid email profile offline_access"
DEFAULT_BASE_URL = "https://chatgpt.com/backend-api/codex"


class CodexProvider(OAuthProvider):
    """管理 Codex OAuth token 的刷新和授权码交换。"""

    redirect_mode = "manual"
    localhost_redirect_uri = DEFAULT_REDIRECT_URI

    def __init__(self):
        """初始化 CodexProvider，不缓存 token_url。"""
        # 修改原因：CodexProvider 在应用启动时创建，而前端保存 token_url 后只会更新运行时配置，不会重建 provider。
        # 修改方式：这里只保存配置读取函数占位，不保存 token_url；实际 endpoint 在 _post_token 每次请求前解析。
        # 目的：保证刷新 token 和授权码交换能实时使用 app.state.config 中的新 token_url。
        self._config_getter = None

    def set_config_getter(self, config_getter):
        """设置运行时配置读取函数。"""
        # 修改原因：直接从 main import app 容易引入循环导入，provider 应通过 manager 注入的函数读取配置。
        # 修改方式：OAuthManager.register_provider 注册 provider 时传入自身 get_config 方法。
        # 目的：让 provider 不依赖 FastAPI app 全局对象，也能拿到最新配置。
        self._config_getter = config_getter

    def set_oauth_manager(self, oauth_manager):
        """绑定共享 OAuthManager，供 Codex 被动额度采集使用。"""
        global _oauth_manager
        # 修改原因：main.py 改为扫描 registry 后不再调用 register_oauth_provider，原先在该函数中的全局 manager 赋值会丢失。
        # 修改方式：给 registry 中保存的 CodexProvider 提供显式绑定钩子，由通用扫描逻辑在注册 provider 前调用。
        # 目的：保留 Codex 响应头被动额度采集的 update_quota 副作用，同时不恢复 main.py 的渠道硬编码。
        _oauth_manager = oauth_manager

    def _get_runtime_config(self) -> dict:
        """读取当前运行时配置。"""
        # 修改原因：配置读取函数可能尚未设置，或读取过程中因测试替身、热更新状态异常而失败。
        # 修改方式：有 getter 时尝试调用，并只接受 dict；异常和非 dict 都回退为空配置。
        # 目的：确保 OAuth 刷新路径不会因为配置读取失败而中断到不可用状态。
        if not self._config_getter:
            return {}
        try:
            config = self._config_getter()
        except Exception:
            return {}
        return config if isinstance(config, dict) else {}

    def _resolve_token_url(self, config: dict | None = None) -> str:
        """从当前运行时配置动态获取 token_url，未配置则用默认值。"""
        # 修改原因：token_url 会由前端写入 api.yaml 并同步到 app.state.config，不能在启动时固化。
        # 修改方式：每次请求前遍历当前 providers，找到 engine=codex 的 token_url 后规范化为 /oauth/token endpoint。
        # 目的：保存配置后不重启服务也能让 OAuth token exchange/refresh 使用新地址。
        runtime_config = config if config is not None else self._get_runtime_config()
        for provider in (runtime_config or {}).get("providers", []):
            if not isinstance(provider, dict):
                continue
            if provider.get("engine") != "codex":
                continue
            custom = provider.get("token_url")
            custom = custom.strip() if isinstance(custom, str) else custom
            if custom:
                if "/oauth/token" in custom:
                    return custom
                return str(custom).rstrip("/") + "/oauth/token"
            break
        return DEFAULT_TOKEN_URL

    def _resolve_proxy(self, config: dict | None = None) -> str | None:
        """从当前运行时配置动态获取 codex provider 的 proxy。"""
        runtime_config = config if config is not None else self._get_runtime_config()
        # provider 级 proxy
        for provider in (runtime_config or {}).get("providers", []):
            if not isinstance(provider, dict):
                continue
            if provider.get("engine") != "codex":
                continue
            proxy = provider.get("preferences", {}).get("proxy")
            if isinstance(proxy, str) and proxy.strip():
                return proxy.strip()
            break
        # 全局 proxy
        global_proxy = (runtime_config or {}).get("preferences", {}).get("proxy")
        if isinstance(global_proxy, str) and global_proxy.strip():
            return global_proxy.strip()
        return None

    def _resolve_base_url(self, config: dict | None = None) -> str:
        """从当前运行时配置动态获取 Codex API base_url。"""
        # 修改原因：额度查询需要访问 OpenAI API 端点，而用户可能在 api.yaml 中配置了 Codex API 反代地址。
        # 修改方式：每次查询前遍历当前 providers，找到 engine=codex 的 base_url；未配置时回退 OpenAI 默认地址。
        # 目的：让 quota 查询和真实 Codex 请求访问同一个上游 API 地址。
        runtime_config = config if config is not None else self._get_runtime_config()
        for provider in (runtime_config or {}).get("providers", []):
            if not isinstance(provider, dict):
                continue
            if provider.get("engine") != "codex":
                continue
            custom = provider.get("base_url")
            custom = custom.strip() if isinstance(custom, str) else custom
            if custom:
                return str(custom)
            break
        return DEFAULT_BASE_URL

    @staticmethod
    def _generate_pkce() -> tuple[str, str]:
        """生成 PKCE verifier 和 S256 challenge。"""
        # 修改原因：Codex OAuth 要求 PKCE，缺少 verifier 会导致授权码无法交换 token。
        # 修改方式：生成 URL 安全随机 verifier，并按 S256 规则派生 challenge。
        # 目的：与 Codex CLI 的 OAuth 登录流程保持一致。
        verifier = secrets.token_urlsafe(32)
        digest = hashlib.sha256(verifier.encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        return verifier, challenge

    def build_auth_url(self, state: str, redirect_uri: str = DEFAULT_REDIRECT_URI) -> tuple[str, str]:
        """构建 Codex OAuth 授权 URL，并返回后续 exchange 需要的 verifier。"""
        verifier, challenge = self._generate_pkce()
        params = {
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": redirect_uri or DEFAULT_REDIRECT_URI,
            "scope": SCOPES,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "login",
            # 修改原因：CLIProxyAPI 的 Codex OAuth 请求会要求 id_token 携带组织和账号信息。
            # 修改方式：保留 OpenAI Codex CLI 兼容参数，不影响标准 OAuth 参数解析。
            # 目的：提高 id_token 中 account_id、plan 等信息的可用性。
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
        }
        return f"{AUTH_URL}?{urlencode(params)}", verifier

    async def refresh_token(self, credential: dict, config: dict | None = None) -> dict:
        """用 refresh_token 刷新 Codex access_token。"""
        refresh_token = credential.get("refresh_token")
        if not refresh_token:
            raise ValueError("refresh_token is required")

        data = {
            "client_id": CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": "openid profile email",
        }
        # 修改原因：token_url 必须在刷新发生时从最新配置读取，而不是使用启动时的旧值。
        # 修改方式：把可选 config 继续传给 _post_token，由 _resolve_token_url 统一处理默认值和路径规范化。
        # 目的：支持 api.yaml 保存后下一次刷新立即走新的 OAuth endpoint。
        token_response = await self._post_token(data, config=config)
        return self._build_credential(credential, token_response)

    async def exchange_code(
        self,
        code: str,
        redirect_uri: str = DEFAULT_REDIRECT_URI,
        code_verifier: str | None = None,
        config: dict | None = None,
    ) -> dict:
        """用授权码和 PKCE verifier 换取 Codex token。"""
        if not code_verifier:
            raise ValueError("code_verifier is required")

        data = {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": code,
            "redirect_uri": redirect_uri or DEFAULT_REDIRECT_URI,
            "code_verifier": code_verifier,
        }
        # 修改原因：授权码交换同样访问 OAuth token endpoint，也必须读取最新 token_url。
        # 修改方式：把可选 config 传入 _post_token，未显式传入时由 provider 的配置 getter 读取运行时配置。
        # 目的：确保新保存的 token_url 同时覆盖登录交换和后台刷新两条路径。
        token_response = await self._post_token(data, config=config)
        return self._build_credential({}, token_response)

    def get_default_base_url(self) -> str:
        """返回 Codex API 默认上游地址。"""
        return DEFAULT_BASE_URL

    def _parse_ratelimit_headers(self, headers) -> dict | None:
        """把 OpenAI ratelimit 响应头转换为前端展示的 quota 结构。"""
        # 修改原因：主动查询和被动 wrapper 都要用同一套响应头解析规则，避免两个路径展示不一致。
        # 修改方式：CodexProvider 方法委托给模块级 _parse_ratelimit_headers，供测试和 wrapper 共用。
        # 目的：让 quota_inner、quota_outer 与 raw 字段在所有采集路径中保持稳定。
        return _parse_ratelimit_headers(headers)

    async def fetch_quota(self, credential: dict, config: dict | None = None) -> dict | None:
        """发一个轻量 Responses API 请求，从响应头读取 Codex quota 信息。"""
        access_token = credential.get("access_token")
        if not access_token:
            return None

        from core.utils import resolve_base_url

        base_url = self._resolve_base_url(config)
        quota_url = resolve_base_url(base_url, "/responses")
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": "codex_cli_rs/0.118.0 (Mac OS 26.3.1; arm64) iTerm.app/3.6.9",
        }
        payload = {
            "model": "gpt-5.3-codex",
            "stream": True,
            "store": False,
            "instructions": "Reply with one word.",
            "input": [{"role": "user", "content": "hi"}],
        }
        try:
            proxy = self._resolve_proxy(config)
            async with httpx.AsyncClient(timeout=20, proxy=proxy) as client:
                response = await client.post(quota_url, json=payload, headers=headers)
        except Exception:
            return None

        return self._parse_ratelimit_headers(response.headers)

    async def _post_token(self, data: dict, config: dict | None = None) -> dict:
        """向 OpenAI OAuth token endpoint 提交 form 请求。"""
        # 修改原因：Codex token endpoint 需要 application/x-www-form-urlencoded 表单，且 endpoint 可能在运行时变化。
        # 修改方式：请求前调用 _resolve_token_url 从最新配置解析 token_url，再用 httpx.AsyncClient.post 发送 form data。
        # 目的：确保刷新和授权码交换既保持 OpenAI OAuth 表单格式，又不再使用启动时固化的地址。
        token_url = self._resolve_token_url(config)
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept-Encoding": "identity",
        }
        proxy = self._resolve_proxy(config)
        async with httpx.AsyncClient(timeout=30, proxy=proxy) as client:
            response = await client.post(token_url, data=data, headers=headers)
            if response.status_code >= 400:
                raise ValueError(f"{response.status_code} {response.text}")
            payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Invalid token response")
        return payload

    def _build_credential(self, original: dict, token_response: dict) -> dict:
        """把 token endpoint 响应转换成 oauth_state 凭据对象。"""
        # 修改原因：OpenAI 会轮换 refresh_token，刷新结果必须覆盖旧 token，同时保留旧响应中未返回的身份信息。
        # 修改方式：先复制原凭据，再写入 access_token、refresh_token、id_token 和 expires_at。
        # 目的：防止刷新成功后仍保存旧 refresh_token，或因响应缺少非必填字段丢失账号标识。
        access_token = token_response.get("access_token")
        if not access_token:
            raise ValueError("Token response missing access_token")

        updated = dict(original or {})
        updated["access_token"] = access_token
        if token_response.get("refresh_token"):
            updated["refresh_token"] = token_response["refresh_token"]
        if token_response.get("id_token"):
            updated["id_token"] = token_response["id_token"]
        if token_response.get("token_type"):
            updated["token_type"] = token_response["token_type"]
        if token_response.get("scope"):
            updated["scope"] = token_response["scope"]

        expires_in = int(token_response.get("expires_in") or 0)
        if expires_in > 0:
            updated["expires_at"] = time.time() + expires_in

        identity = _decode_codex_identity(updated.get("id_token"))
        if identity.get("email"):
            updated["email"] = identity["email"]
        if identity.get("account_id"):
            updated["account_id"] = identity["account_id"]
        return updated


def _decode_jwt_payload(token: str | None) -> dict[str, Any]:
    """不验签解码 JWT payload。"""
    # 修改原因：OAuth token endpoint 已返回 id_token，当前只需要提取非安全决策用途的邮箱和账号 ID。
    # 修改方式：按 JWT base64url 规则补齐 padding 后解析 payload JSON，不做签名验证。
    # 目的：满足凭据展示和账号标识需求，不把验签复杂度引入 Phase 1 MVP。
    if not token:
        return {}
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode())
        data = json.loads(decoded.decode())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _decode_codex_identity(id_token: str | None) -> dict[str, str]:
    """从 Codex id_token 中提取 email 和 ChatGPT account_id。"""
    # 修改原因：Codex id_token 的账号 ID 在 https://api.openai.com/auth 私有 claim 内。
    # 修改方式：优先读取 chatgpt_account_id，并兼容扁平 account_id 与 sub 字段。
    # 目的：手动导入后能够在账户列表中显示稳定账号标识。
    payload = _decode_jwt_payload(id_token)
    auth_info = payload.get("https://api.openai.com/auth") or {}
    if not isinstance(auth_info, dict):
        auth_info = {}
    email = payload.get("email") or ""
    account_id = (
        auth_info.get("chatgpt_account_id")
        or payload.get("account_id")
        or payload.get("sub")
        or ""
    )
    return {"email": str(email), "account_id": str(account_id)}


CODEX_USER_AGENT = "codex_cli_rs/0.118.0 (Mac OS 26.3.1; arm64) iTerm.app/3.6.9"

# 修改原因：Codex 的 plan_type 和百分比现在共用 quota_display，前端只保留一个额度挂载点。
# 修改方式：从 data.raw 读取 plan_type，从 data.quota_inner/data.quota_outer 读取最低百分比，并组合为同一个标签。
# 目的：删除独立标签脚本后仍在原 quota_display 位置显示 Codex 套餐和额度。
CODEX_QUOTA_DISPLAY = """
export default function render(ctx) {
    ctx = ctx || {};
    const { el, data } = ctx;
    if (!el) return;
    const mode = ctx.context?.mode || ctx.mode || 'row';

    // 修改原因：Codex quota_display 同时挂载在完整 Key 行和机房卡片圆环中心，完整行药丸样式会在小圆环内溢出。
    // 修改方式：读取 ctx.context.mode 区分 row/rack；rack 只输出短百分比或短 planType，row 保留 planType + 百分比药丸。
    // 目的：让 Codex 在完整行保持原有信息密度，同时在机房卡片中心不再显示过长内容。
    const raw = data?.raw || {};
    const planType = raw['x-codex-plan-type'] || raw.plan_type || '';
    const qInner = typeof data?.quota_inner === 'number' ? data.quota_inner : null;
    const qOuter = typeof data?.quota_outer === 'number' ? data.quota_outer : null;
    const pcts = [qInner, qOuter].filter(v => v != null);
    const minPct = pcts.length ? Math.round(Math.min(...pcts)) : null;

    if (mode === 'rack') {
        if (minPct != null) {
            el.style.display = '';
            el.textContent = minPct + '%';
            el.removeAttribute('title');
            const colorCls = minPct >= 50 ? 'text-emerald-600' : minPct >= 20 ? 'text-amber-600' : 'text-red-500';
            el.className = 'text-[9px] font-bold font-mono leading-none ' + colorCls;
        } else if (planType) {
            el.style.display = '';
            el.textContent = planType;
            el.title = planType;
            el.className = 'text-[8px] font-semibold leading-none text-blue-500 truncate max-w-[50px]';
        } else {
            el.textContent = '';
            el.removeAttribute('title');
            el.style.display = 'none';
        }
        return;
    }

    const parts = [];
    if (planType) parts.push(planType);
    if (minPct != null) parts.push(minPct + '%');

    if (parts.length) {
        const colorCls = minPct == null ? 'bg-blue-500/15 text-blue-500' : minPct >= 50 ? 'bg-emerald-500/15 text-emerald-500' : minPct >= 20 ? 'bg-amber-500/15 text-amber-600' : 'bg-red-500/15 text-red-500';
        el.style.display = '';
        el.textContent = parts.join(' ');
        el.title = parts.join(' · ');
        el.className = 'flex-shrink-0 text-[10px] font-semibold font-mono px-1.5 py-0.5 rounded relative z-[2] cursor-default ' + colorCls;
    } else {
        el.textContent = '';
        el.removeAttribute('title');
        el.style.display = 'none';
    }
}
""".strip()

# Codex 端点不接受的字段
_CODEX_STRIP_FIELDS = {'max_output_tokens', 'max_tokens', 'max_completion_tokens'}


async def _codex_payload_interceptor(request, engine, provider, api_key, url, headers, payload):
    """Codex 全局拦截器：强制必要参数 + 清理不支持的字段。"""
    if engine != "codex":
        return url, headers, payload
    if not isinstance(payload, dict):
        return url, headers, payload
    # 强制 store=false（Codex 端点要求）
    payload["store"] = False
    # 确保 instructions 存在（Codex 端点必须，模拟官方客户端默认值）
    if not payload.get("instructions"):
        payload["instructions"] = "You are ChatGPT"
    # 清理 Codex 不接受的字段
    for f in _CODEX_STRIP_FIELDS:
        payload.pop(f, None)
    return url, headers, payload


# 注册为内置拦截器（无 plugin_name → 全局生效）
try:
    from core.plugins.interceptors import register_request_interceptor
    register_request_interceptor(
        "codex_payload_enforcer",
        _codex_payload_interceptor,
        priority=3,
    )
except ImportError:
    pass


async def get_codex_payload(request, engine, provider, api_key=None):
    """复用 Responses API adapter 构建 payload，强制 store=false + Bearer + Codex UA。"""
    url, headers, payload = await get_responses_payload(request, "openai-responses", provider, api_key)
    headers["Authorization"] = f"Bearer {api_key}"
    headers["User-Agent"] = CODEX_USER_AGENT
    payload["store"] = False
    return url, headers, payload


async def get_codex_passthrough_meta(request, engine, provider, api_key=None):
    """透传模式构建 Responses API URL + Bearer 认证头。"""
    from ..utils import resolve_base_url

    base_url = provider.get("base_url") or DEFAULT_BASE_URL
    url = resolve_base_url(base_url, "/responses")
    headers = {
        "content-type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": CODEX_USER_AGENT,
    }
    return url, headers, {}


def register():
    """注册 Codex OAuth 渠道。"""
    from .registry import register_channel

    register_channel(
        id="codex",
        type_name="openai-responses",
        default_base_url=DEFAULT_BASE_URL,
        default_token_url="https://auth.openai.com",
        auth_header="Authorization: Bearer {api_key}",
        description="OpenAI Codex (OAuth, Responses API)",
        request_adapter=get_codex_payload,
        passthrough_adapter=get_codex_passthrough_meta,
        response_adapter=fetch_codex_response,
        stream_adapter=fetch_codex_response_stream,
        is_oauth=True,
        # 修改原因：OAuth provider 注册要成为 register_channel 的一部分，main.py 只负责扫描注册表。
        # 修改方式：注册渠道时直接创建并传入 CodexProvider 实例，由 registry 保存给启动流程使用。
        # 目的：让 Codex 与插件 OAuth 渠道走同一条自动 provider 注册路径。
        oauth_provider=CodexProvider(),
        # 修改原因：Codex plan_type 已合并到 quota_display，前端不再提供独立标签挂载点。
        # 修改方式：只注册 quota_display，并保留导入占位配置不变。
        # 目的：让 Codex 在同一个额度插槽内同时显示套餐和百分比。
        ui_slots={
            "quota_display": CODEX_QUOTA_DISPLAY,
            "import_placeholder": "rt_xxxxxxxx...",
        },
        source="builtin",
    )


def register_oauth_provider(oauth_manager, providers: list | None = None):
    """向 OAuthManager 注册 Codex provider。app 启动后调用。"""
    global _oauth_manager

    # 修改原因：providers 参数来自启动时配置，继续读取 token_url 会把后续前端保存的新配置挡在旧 provider 实例之外。
    # 修改方式：保留 providers 形参仅用于兼容旧调用方，但注册时始终创建不带 token_url 的 CodexProvider。
    # 目的：让 token_url 统一由 CodexProvider 在每次 token 请求前通过 OAuthManager 的运行时配置引用读取。
    provider = CodexProvider()
    # 修改原因：旧兼容入口仍可能被测试或外部调用，它必须继续完成 Codex 被动额度采集所需的全局 manager 绑定。
    # 修改方式：复用 CodexProvider.set_oauth_manager，再把同一个 provider 注册给 OAuthManager。
    # 目的：保留旧函数的副作用，同时让新 registry 扫描路径和旧调用路径行为一致。
    provider.set_oauth_manager(oauth_manager)
    oauth_manager.register_provider("codex", provider)
