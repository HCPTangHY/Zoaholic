"""Grok (xAI) OAuth 渠道适配器。

本文件自包含 Grok OAuth provider、渠道注册、SSO cookie 转换和额度采集逻辑。

上游形态（与 sub2api / CLIProxyAPI 对齐）：
- 对话走 cli-chat-proxy.grok.com/v1 的 OpenAI Responses 兼容端点，带 Grok CLI 伪装头。
- 计费探测走同一网关的 /billing（weekly credits + monthly 额度）。
- grok-imagine* 模型走 api.x.ai/v1 的 images/videos 端点，响应转回 Chat Completions 格式。
- 认证支持两种录入：OAuth refresh_token，或网页版 sso cookie（服务端自动跑 device flow 换成 at/rt）。
"""

import base64
import hashlib
import json
import re
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from core.log_config import logger
from core.oauth.providers.base import OAuthProvider
from core.channels.openai_responses_channel import (
    fetch_responses_stream,
    fetch_responses_response,
    get_responses_payload,
)


_oauth_manager = None

# ============================================================
# 常量
# ============================================================

OAUTH_ISSUER = "https://auth.x.ai"
AUTHORIZE_URL = OAUTH_ISSUER + "/oauth2/authorize"
DEFAULT_TOKEN_URL = OAUTH_ISSUER + "/oauth2/token"
DEVICE_CODE_URL = OAUTH_ISSUER + "/oauth2/device/code"
DEVICE_VERIFY_URL = OAUTH_ISSUER + "/oauth2/device/verify"
DEVICE_APPROVE_URL = OAUTH_ISSUER + "/oauth2/device/approve"
SSO_ACCOUNTS_URL = "https://accounts.x.ai/"

CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
SCOPE = "openid profile email offline_access grok-cli:access api:access"
# SSO 转换时使用更宽的 scope（含网页对话读写），与 sub2api SSOBuildScope 对齐
SSO_SCOPE = SCOPE + " conversations:read conversations:write"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:56121/callback"

DEFAULT_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
# Imagine 媒体端点（图片/视频）必须走官方 API，cli-chat-proxy 仅接受对话请求
MEDIA_BASE_URL = "https://api.x.ai/v1"

# Grok CLI 身份头（billing 端点强制校验；对话端点带上以降低风控特征）
CLI_TOKEN_AUTH_HEADER = "X-XAI-Token-Auth"
CLI_TOKEN_AUTH_VALUE = "xai-grok-cli"
CLI_VERSION_HEADER = "x-grok-client-version"
# 与 https://x.ai/cli/stable 当前版本保持同步
CLI_VERSION_VALUE = "0.2.93"
CLI_USER_AGENT = f"grok-pager/{CLI_VERSION_VALUE} grok-shell/{CLI_VERSION_VALUE} (macos; aarch64)"

# SSO device flow 模拟浏览器
_SSO_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_SSO_CONVERT_TIMEOUT = 75.0

# 套餐月度上限（美分），用于识别订阅档位
_SUPERGROK_LIMIT_CENTS = 15_000
_SUPERGROK_HEAVY_LIMIT_CENTS = 150_000

DEFAULT_MODELS = [
    "grok-4.5",
    "grok-4.3",
    "grok-build-0.1",
    "grok-composer-2.5-fast",
    "grok-4.20-0309-reasoning",
    "grok-4.20-0309-non-reasoning",
    "grok-4.20-multi-agent-0309",
    "grok-imagine",
    "grok-imagine-image",
    "grok-imagine-image-quality",
    "grok-imagine-edit",
    "grok-imagine-video",
    "grok-imagine-video-1.5",
]

# Imagine 媒体模型前缀。grok-imagine-video* 走视频端点，其余 grok-imagine* 走图片端点
_MEDIA_MODEL_PREFIX = "grok-imagine"

# 视频生成轮询参数
_VIDEO_POLL_INTERVAL = 4.0
_VIDEO_POLL_TIMEOUT = 480.0

# 媒体请求允许透传的额外参数（从 request 对象额外字段提取）
_IMAGE_PARAM_FIELDS = ("n", "size", "aspect_ratio", "resolution", "output_format", "quality")
_VIDEO_PARAM_FIELDS = ("duration", "aspect_ratio", "resolution", "image_url", "n")


# 被动采集时保留的响应头（与 sub2api quotaHeaderAllowlist 对齐）
_QUOTA_HEADER_ALLOWLIST = (
    "x-ratelimit-limit-requests",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-reset-requests",
    "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-tokens",
    "x-ratelimit-reset-tokens",
    "retry-after",
    "x-subscription-tier",
    "xai-subscription-tier",
    "x-entitlement-status",
    "xai-entitlement-status",
)


# ============================================================
# 响应头被动额度解析
# ============================================================

def _percentage(remaining: str | None, limit: str | None) -> float | None:
    """计算 ratelimit 剩余额度百分比。"""
    if remaining is None or limit is None:
        return None
    try:
        value = float(remaining) / float(limit) * 100
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return round(max(0.0, min(100.0, value)), 1)


def _parse_ratelimit_headers(headers) -> dict | None:
    """从 xAI 响应头提取统一 quota 数据（requests/tokens 双窗口 + 订阅信息）。"""
    if not headers:
        return None

    lowered = {}
    for k, v in headers.items():
        kl = k.lower() if isinstance(k, str) else str(k).lower()
        lowered[kl] = v

    result: dict[str, Any] = {}

    quota_inner = _percentage(
        lowered.get("x-ratelimit-remaining-requests"),
        lowered.get("x-ratelimit-limit-requests"),
    )
    quota_outer = _percentage(
        lowered.get("x-ratelimit-remaining-tokens"),
        lowered.get("x-ratelimit-limit-tokens"),
    )
    if quota_inner is not None:
        result["quota_inner"] = quota_inner
    if quota_outer is not None:
        result["quota_outer"] = quota_outer

    raw = {name: lowered[name] for name in _QUOTA_HEADER_ALLOWLIST if lowered.get(name) is not None}
    if raw:
        result["raw"] = raw

    return result if result else None


def _get_quota_context_from_request() -> tuple[str, str] | None:
    """从当前请求上下文读取 OAuth 渠道名和原始 key_id。"""
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


def _store_quota_from_headers(headers) -> None:
    """把响应头中的 quota 数据写入 OAuthManager 内存缓存（被动采集）。"""
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
        _store_quota_from_headers(getattr(response, "headers", None))
        return response

    def stream(self, *args, **kwargs):
        return _QuotaCapturingStreamContext(self._client.stream(*args, **kwargs))


# ============================================================
# Billing 主动探测解析
# ============================================================

def _parse_cent_value(value) -> float | None:
    """解析 monthlyLimit/used 字段（可能是数字、字符串或 {\"val\": n} 对象）。"""
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("val")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _resolve_plan(monthly_limit_cents: float | None) -> str:
    """按月度上限识别订阅档位。

    实测（2026-07-27，免费账号）：monthlyLimit=0；SuperGrok=$150、SuperGrok Heavy=$1500。
    """
    if monthly_limit_cents is None:
        return ""
    limit = round(monthly_limit_cents)
    if limit == 0:
        return "Free"
    if limit == _SUPERGROK_LIMIT_CENTS:
        return "SuperGrok"
    if limit == _SUPERGROK_HEAVY_LIMIT_CENTS:
        return "SuperGrok Heavy"
    return ""


def _build_billing_quota(weekly_body: dict | None, monthly_body: dict | None) -> dict | None:
    """把 weekly/monthly billing 响应合并为统一 quota 结构。"""
    weekly_config = (weekly_body or {}).get("config") or {}
    monthly_config = (monthly_body or {}).get("config") or {}
    if not weekly_config and not monthly_config:
        return None

    result: dict[str, Any] = {}
    raw: dict[str, Any] = {}

    # weekly：creditUsagePercent 是已用百分比，转换为剩余百分比
    weekly_used_pct = weekly_config.get("creditUsagePercent")
    if weekly_used_pct is not None:
        try:
            result["quota_inner"] = round(max(0.0, min(100.0, 100.0 - float(weekly_used_pct))), 1)
            raw["weekly_usage_percent"] = float(weekly_used_pct)
        except (TypeError, ValueError):
            pass
    period = weekly_config.get("currentPeriod") or {}
    if isinstance(period, dict):
        if period.get("start"):
            raw["weekly_period_start"] = period.get("start")
        if period.get("end"):
            raw["weekly_period_end"] = period.get("end")
    product_usage = weekly_config.get("productUsage")
    if isinstance(product_usage, list) and product_usage:
        raw["product_usage"] = [
            {"product": p.get("product"), "usage_percent": p.get("usagePercent")}
            for p in product_usage if isinstance(p, dict)
        ]

    # monthly：monthlyLimit/used 是美分
    monthly_limit = _parse_cent_value(monthly_config.get("monthlyLimit"))
    monthly_used = _parse_cent_value(monthly_config.get("used"))
    if monthly_limit is not None:
        raw["monthly_limit_cents"] = monthly_limit
    if monthly_used is not None:
        raw["monthly_used_cents"] = monthly_used
    if monthly_limit and monthly_limit > 0 and monthly_used is not None:
        included = min(monthly_used, monthly_limit)
        raw["monthly_used_percent"] = round(included / monthly_limit * 100, 1)
        result["quota_outer"] = round(max(0.0, 100.0 - raw["monthly_used_percent"]), 1)
    plan = _resolve_plan(monthly_limit)
    if plan:
        raw["plan"] = plan
    if monthly_config.get("billingPeriodStart"):
        raw["billing_period_start"] = monthly_config.get("billingPeriodStart")
    if monthly_config.get("billingPeriodEnd"):
        raw["billing_period_end"] = monthly_config.get("billingPeriodEnd")

    if raw:
        result["raw"] = raw
    return result if result else None


# ============================================================
# JWT 工具
# ============================================================

def _decode_jwt_payload(token: str | None) -> dict[str, Any]:
    """不验签解码 JWT payload。"""
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


# ============================================================
# SSO cookie 转换
# ============================================================

def normalize_sso_token(value: str | None) -> str:
    """从多种粘贴形态中提取 sso cookie 值。

    支持："sso:xxx"、"sso=xxx"、"sso-rw=xxx"、整段 Cookie 头、裸 token。
    """
    text = (value or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered.startswith("cookie:"):
        text = text[len("cookie:"):].strip()
    elif lowered.startswith("sso-rw:"):
        text = text[len("sso-rw:"):].strip()
    elif lowered.startswith("sso:"):
        text = text[len("sso:"):].strip()
    for part in text.split(";"):
        name, sep, token = part.strip().partition("=")
        if not sep:
            continue
        if name.strip().lower() in ("sso", "sso-rw"):
            return re.sub(r"[\r\n\x00]", "", token.strip())
    # 裸 token（可能后面带 ; 其他 cookie）
    head = text.split(";", 1)[0].strip()
    return re.sub(r"[\r\n\x00]", "", head)


def looks_like_sso_cookie(value: str | None) -> bool:
    """判断 refresh_token 字段是否像网页 sso cookie 而不是 xAI refresh_token。

    显式形态：sso:/sso=/sso-rw=/cookie: 前缀，或含分号的 Cookie 头。
    """
    text = (value or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if lowered.startswith(("sso:", "sso=", "sso-rw=", "cookie:")):
        return True
    if ";" in text and ("sso=" in lowered or "sso-rw=" in lowered):
        return True
    return False


async def _convert_sso_to_token(sso_token: str, proxy: str | None = None) -> dict:
    """把 Grok 网页版 sso cookie 通过 device authorization flow 转换为 at/rt。

    流程（与 sub2api sso_device.go 对齐）：
    accounts.x.ai 验证登录态 → device/code 申请 → 带 cookie 自动完成 verify/approve → 轮询 token。
    """
    sso_token = normalize_sso_token(sso_token)
    if not sso_token:
        raise ValueError("sso cookie is empty")

    async with httpx.AsyncClient(
        timeout=30,
        proxy=proxy,
        follow_redirects=True,
        max_redirects=8,
        headers={
            "User-Agent": _SSO_BROWSER_UA,
            "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    ) as client:
        client.cookies.set("sso", sso_token, domain=".x.ai")
        client.cookies.set("sso-rw", sso_token, domain=".x.ai")

        # 1. 验证 SSO 登录态
        resp = await client.get(SSO_ACCOUNTS_URL)
        if resp.status_code == 401 or "sign-in" in str(resp.url) or "sign-up" in str(resp.url):
            raise ValueError("sso cookie 无效或已过期（accounts.x.ai 未登录）")
        if resp.status_code >= 400:
            raise ValueError(f"验证 sso 登录态失败: HTTP {resp.status_code}")

        # 2. 申请 device code
        resp = await client.post(
            DEVICE_CODE_URL,
            data={"client_id": CLIENT_ID, "scope": SSO_SCOPE},
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        )
        if resp.status_code >= 400:
            raise ValueError(f"device code 申请失败: HTTP {resp.status_code} {resp.text[:200]}")
        device = resp.json()
        device_code = device.get("device_code")
        user_code = device.get("user_code")
        verification_url = device.get("verification_uri_complete")
        if not device_code or not user_code or not verification_url:
            raise ValueError("device code 响应不完整")
        interval = max(int(device.get("interval") or 5), 1)

        # 3. 打开验证页（携带 sso cookie，建立授权上下文）
        resp = await client.get(verification_url)
        if resp.status_code >= 400:
            raise ValueError(f"打开 device 验证页失败: HTTP {resp.status_code}")

        # 4. 提交 user_code，应到达 consent 页
        resp = await client.post(
            DEVICE_VERIFY_URL,
            data={"user_code": user_code},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code >= 400:
            raise ValueError(f"device 验证失败: HTTP {resp.status_code}")
        if "consent" not in str(resp.url):
            raise ValueError("device 验证未到达授权确认页（sso cookie 可能已失效）")

        # 5. 自动批准授权
        resp = await client.post(
            DEVICE_APPROVE_URL,
            data={"user_code": user_code, "action": "allow", "principal_type": "User", "principal_id": ""},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code >= 400:
            raise ValueError(f"device 授权批准失败: HTTP {resp.status_code}")

        # 6. 轮询换 token
        deadline = time.monotonic() + _SSO_CONVERT_TIMEOUT
        while time.monotonic() < deadline:
            await asyncio_sleep(interval)
            resp = await client.post(
                DEFAULT_TOKEN_URL,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": CLIENT_ID,
                    "device_code": device_code,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            )
            try:
                payload = resp.json()
            except Exception:
                payload = {}
            if 200 <= resp.status_code < 300 and payload.get("access_token"):
                return payload
            error = payload.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            if error in ("access_denied", "expired_token"):
                raise ValueError("device 授权被拒绝或已过期")
            if resp.status_code >= 400:
                raise ValueError(f"token 轮询失败: HTTP {resp.status_code} {payload.get('error_description') or error or ''}")
        raise ValueError("sso 转换超时（device flow 轮询超时）")


async def asyncio_sleep(seconds: float) -> None:
    """隔离 asyncio import，方便测试替换。"""
    import asyncio

    await asyncio.sleep(seconds)


# ============================================================
# Grok OAuth Provider
# ============================================================

class GrokProvider(OAuthProvider):
    """管理 xAI Grok OAuth token 的刷新、授权码交换、SSO 转换和额度查询。"""

    redirect_mode = "manual"
    localhost_redirect_uri = DEFAULT_REDIRECT_URI

    def __init__(self):
        self._config_getter = None

    def set_config_getter(self, config_getter):
        """设置运行时配置读取函数。"""
        self._config_getter = config_getter

    def set_oauth_manager(self, oauth_manager):
        """绑定共享 OAuthManager，供被动额度采集使用。"""
        global _oauth_manager
        _oauth_manager = oauth_manager

    def _get_runtime_config(self) -> dict:
        if not self._config_getter:
            return {}
        try:
            config = self._config_getter()
        except Exception:
            return {}
        return config if isinstance(config, dict) else {}

    def _find_provider_config(self, config: dict | None = None) -> dict:
        """找到当前 engine=grok 的 provider 配置。"""
        runtime_config = config if config is not None else self._get_runtime_config()
        for provider in (runtime_config or {}).get("providers", []):
            if isinstance(provider, dict) and provider.get("engine") == "grok":
                return provider
        return {}

    def _resolve_token_url(self, config: dict | None = None) -> str:
        provider = self._find_provider_config(config)
        custom = provider.get("token_url")
        if isinstance(custom, str) and custom.strip():
            custom = custom.strip()
            if "/oauth2/token" in custom:
                return custom
            return custom.rstrip("/") + "/oauth2/token"
        return DEFAULT_TOKEN_URL

    def _resolve_proxy(self, config: dict | None = None) -> str | None:
        provider = self._find_provider_config(config)
        proxy = provider.get("preferences", {}).get("proxy")
        if isinstance(proxy, str) and proxy.strip():
            return proxy.strip()
        runtime_config = config if config is not None else self._get_runtime_config()
        global_proxy = (runtime_config or {}).get("preferences", {}).get("proxy")
        if isinstance(global_proxy, str) and global_proxy.strip():
            return global_proxy.strip()
        return None

    def _resolve_base_url(self, config: dict | None = None) -> str:
        provider = self._find_provider_config(config)
        custom = provider.get("base_url")
        if isinstance(custom, str) and custom.strip():
            return custom.strip().rstrip("/")
        return DEFAULT_BASE_URL

    @staticmethod
    def _generate_pkce() -> tuple[str, str]:
        verifier = secrets.token_urlsafe(32)
        digest = hashlib.sha256(verifier.encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        return verifier, challenge

    def build_auth_url(self, state: str, redirect_uri: str = DEFAULT_REDIRECT_URI) -> tuple[str, str]:
        """构建 xAI OAuth 授权 URL（PKCE S256），返回 (url, verifier)。"""
        verifier, challenge = self._generate_pkce()
        params = {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": redirect_uri or DEFAULT_REDIRECT_URI,
            "scope": SCOPE,
            "state": state,
            "nonce": secrets.token_hex(16),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{AUTHORIZE_URL}?{urlencode(params)}", verifier

    async def refresh_token(self, credential: dict, config: dict | None = None) -> dict:
        """刷新 token；refresh_token 字段为 sso cookie 时自动走 device flow 转换。"""
        refresh_token = (credential.get("refresh_token") or "").strip()
        sso_token = (credential.get("sso_token") or "").strip()
        proxy = self._resolve_proxy(config)

        if sso_token or looks_like_sso_cookie(refresh_token):
            token_response = await _convert_sso_to_token(sso_token or refresh_token, proxy=proxy)
            return self._build_credential(credential, token_response)

        if not refresh_token:
            raise ValueError("refresh_token is required")

        data = {
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": refresh_token,
        }
        token_response = await self._post_token(data, config=config)
        return self._build_credential(credential, token_response)

    async def exchange_code(
        self,
        code: str,
        redirect_uri: str = DEFAULT_REDIRECT_URI,
        code_verifier: str | None = None,
        config: dict | None = None,
    ) -> dict:
        """用授权码和 PKCE verifier 换取 xAI token。"""
        if not code_verifier:
            raise ValueError("code_verifier is required")
        # 兼容粘贴完整 callback URL 或 query 串
        code = _parse_authorization_code(code)

        data = {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": code,
            "redirect_uri": redirect_uri or DEFAULT_REDIRECT_URI,
            "code_verifier": code_verifier,
        }
        token_response = await self._post_token(data, config=config)
        return self._build_credential({}, token_response)

    def get_default_base_url(self) -> str:
        return DEFAULT_BASE_URL

    async def fetch_quota(self, credential: dict, config: dict | None = None) -> dict | None:
        """通过 CLI 网关 /billing 端点查询周配额和月额度。"""
        access_token = credential.get("access_token")
        if not access_token:
            return None

        base_url = self._resolve_base_url(config)
        proxy = self._resolve_proxy(config)
        headers = _cli_headers(access_token)

        weekly_body = None
        monthly_body = None
        try:
            async with httpx.AsyncClient(timeout=20, proxy=proxy) as client:
                weekly_resp = await client.get(f"{base_url}/billing?format=credits", headers=headers)
                if weekly_resp.status_code < 400:
                    weekly_body = weekly_resp.json()
                monthly_resp = await client.get(f"{base_url}/billing", headers=headers)
                if monthly_resp.status_code < 400:
                    monthly_body = monthly_resp.json()
        except Exception:
            return None

        return _build_billing_quota(weekly_body, monthly_body)

    async def _post_token(self, data: dict, config: dict | None = None) -> dict:
        """向 xAI OAuth token endpoint 提交 form 请求。"""
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
        access_token = token_response.get("access_token")
        if not access_token:
            raise ValueError("Token response missing access_token")

        updated = dict(original or {})
        # sso 一次性转换字段不保留到凭据里
        updated.pop("sso_token", None)
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
        if expires_in <= 0:
            # xAI 偶尔缺省 expires_in，按 6 小时默认 TTL 处理
            expires_in = 6 * 3600
        updated["expires_at"] = time.time() + expires_in

        # 从 id_token / access_token claims 提取身份信息
        for token in (updated.get("id_token"), access_token):
            claims = _decode_jwt_payload(token)
            if not claims:
                continue
            if claims.get("email") and not updated.get("email"):
                updated["email"] = str(claims["email"])
            if claims.get("sub") and not updated.get("account_id"):
                updated["account_id"] = str(claims["sub"])
            if claims.get("team_id") and not updated.get("team_id"):
                updated["team_id"] = str(claims["team_id"])

        updated.setdefault("base_url", DEFAULT_BASE_URL)
        return updated


def _parse_authorization_code(raw: str) -> str:
    """从完整 callback URL / query 串 / 裸 code 中提取授权码。"""
    text = (raw or "").strip()
    if not text:
        return text
    from urllib.parse import urlparse, parse_qs

    if text.startswith("http://") or text.startswith("https://"):
        parsed = urlparse(text)
        code = parse_qs(parsed.query).get("code", [""])[0]
        if code:
            return code.strip()
    query = text.lstrip("?")
    if "=" in query:
        code = parse_qs(query).get("code", [""])[0]
        if code:
            return code.strip()
    return text


def _cli_headers(access_token: str) -> dict:
    """Grok CLI 身份头（billing 强制，对话降风控）。"""
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        CLI_TOKEN_AUTH_HEADER: CLI_TOKEN_AUTH_VALUE,
        CLI_VERSION_HEADER: CLI_VERSION_VALUE,
        "User-Agent": CLI_USER_AGENT,
    }


# ============================================================
# Imagine 媒体模型：请求构建与响应转换
# ============================================================

def _is_media_model(model: str | None) -> bool:
    return bool(model) and str(model).lower().startswith(_MEDIA_MODEL_PREFIX)


def _is_video_model(model: str | None) -> bool:
    return _is_media_model(model) and "video" in str(model).lower()


def _extract_prompt_and_images(request) -> tuple:
    """从 messages 中提取 prompt 文本和图片附件（最后一条 user 消息）。"""
    prompt = ""
    images = []
    for msg in reversed(request.messages):
        if msg.role != "user":
            continue
        if isinstance(msg.content, str):
            prompt = msg.content
        elif isinstance(msg.content, list):
            text_parts = []
            for item in msg.content:
                item_type = getattr(item, "type", None)
                if item_type == "text" and getattr(item, "text", None):
                    text_parts.append(item.text)
                elif item_type == "image_url" and getattr(item, "image_url", None):
                    image_url = item.image_url
                    url = image_url.url if hasattr(image_url, "url") else str(image_url)
                    images.append(url)
            prompt = "\n".join(text_parts)
        break
    return prompt.strip(), images


def _extract_media_params(request, fields) -> dict:
    """从 request 对象额外字段提取媒体 API 参数。"""
    try:
        request_dict = request.model_dump(exclude_unset=True)
    except Exception:
        request_dict = {}
    params = {}
    for field in fields:
        value = request_dict.get(field)
        if value is not None:
            params[field] = value
    return params


def _normalize_media_base_url(provider) -> str:
    """媒体端点 base_url：provider 未显式覆盖时固定为 api.x.ai。

    provider base_url 是 cli-chat-proxy（对话网关），媒体请求必须改打官方 API。
    用户显式配置了非 cli-chat-proxy 的 base_url 时尊重该配置（反代场景）。
    """
    custom = (provider.get("base_url") or "").strip().rstrip("/")
    if custom and "cli-chat-proxy" not in custom:
        return custom
    return MEDIA_BASE_URL


def _media_headers(api_key: str | None) -> dict:
    """官方 API 媒体端点请求头。不带 CLI 伪装头（api.x.ai 不校验）。"""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _build_media_payload(request, provider, api_key) -> tuple:
    """为 grok-imagine* 模型构建媒体请求 (url, headers, payload)。

    图片：有附件 → /images/edits，无附件 → /images/generations。
    视频：→ /videos/generations（异步任务，响应适配器轮询）。
    """
    from ..utils import get_model_dict, resolve_base_url

    model_dict = get_model_dict(provider)
    original_model = model_dict[request.model]
    base_url = _normalize_media_base_url(provider)
    headers = _media_headers(api_key)

    prompt, images = _extract_prompt_and_images(request)
    if not prompt:
        prompt = "Generate an image" if not _is_video_model(original_model) else "Generate a video"

    if _is_video_model(original_model):
        url = resolve_base_url(base_url, "/videos/generations")
        payload = {"model": original_model, "prompt": prompt}
        payload.update(_extract_media_params(request, _VIDEO_PARAM_FIELDS))
        # 有图片附件 → 图生视频首帧
        if images and "image_url" not in payload:
            payload["image_url"] = images[0]
    else:
        is_edit = bool(images) or "edit" in str(original_model).lower()
        url = resolve_base_url(base_url, "/images/edits" if is_edit else "/images/generations")
        payload = {"model": original_model, "prompt": prompt}
        payload.update(_extract_media_params(request, _IMAGE_PARAM_FIELDS))
        if is_edit and images:
            payload["images"] = [{"image_url": image} for image in images]

    return url, headers, payload


def _image_content_items(response_json: dict, payload: dict) -> list:
    """把 images API 响应转换为 Chat Completions content item 列表。"""
    import base64 as _b64

    data_list = response_json.get("data", [])
    content_items = []
    output_format = payload.get("output_format", "png")
    mime = {"jpeg": "image/jpeg", "webp": "image/webp"}.get(output_format, "image/png")

    for item in data_list:
        if not isinstance(item, dict):
            continue
        b64 = item.get("b64_json", "")
        image_url = item.get("url", "")
        if b64:
            content_items.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            })
        elif image_url:
            content_items.append({
                "type": "image_url",
                "image_url": {"url": image_url},
            })
        revised = item.get("revised_prompt")
        if revised:
            content_items.append({
                "type": "text",
                "text": f"*Revised prompt: {revised}*",
            })
    return content_items


def _video_status_done(status: str | None) -> bool:
    return (status or "").lower() in ("completed", "done", "succeeded", "success")


def _video_status_failed(status: str | None) -> bool:
    return (status or "").lower() in ("failed", "error", "expired", "cancelled", "canceled")


async def _poll_video_task(client, base_url: str, request_id: str, headers: dict, timeout) -> dict:
    """轮询 xAI 视频生成任务，返回完成后的任务 JSON。

    成功时返回含 video.url 的任务对象；失败/超时抛出 ValueError。
    """
    import asyncio as _asyncio

    deadline = time.monotonic() + min(float(timeout or _VIDEO_POLL_TIMEOUT), _VIDEO_POLL_TIMEOUT)
    poll_url = f"{base_url}/videos/{request_id}"
    while time.monotonic() < deadline:
        await _asyncio.sleep(_VIDEO_POLL_INTERVAL)
        response = await client.get(poll_url, headers=headers)
        _store_quota_from_headers(getattr(response, "headers", None))
        if response.status_code >= 400:
            raise ValueError(f"视频任务查询失败: HTTP {response.status_code} {response.text[:200]}")
        task = response.json()
        status = task.get("status")
        if _video_status_done(status):
            video_url = ((task.get("video") or {}).get("url") or "").strip()
            if not video_url:
                raise ValueError("视频任务完成但未返回 video.url")
            return task
        if _video_status_failed(status):
            reason = task.get("error") or task.get("error_message") or status
            raise ValueError(f"视频生成失败: {reason}")
    raise ValueError(f"视频生成超时（{int(_VIDEO_POLL_TIMEOUT)}s）")


def _chat_completion_result(content, model: str, usage: dict | None = None) -> dict:
    """构建 Chat Completions 格式响应。"""
    import random
    import string
    from datetime import datetime

    timestamp = int(datetime.timestamp(datetime.now()))
    random.seed(timestamp)
    random_str = "".join(random.choices(string.ascii_letters + string.digits, k=29))
    return {
        "id": f"chatcmpl-{random_str}",
        "object": "chat.completion",
        "created": timestamp,
        "model": model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": content,
                "refusal": None,
            },
            "logprobs": None,
            "finish_reason": "stop",
        }],
        "usage": usage,
        "system_fingerprint": "fp_grok_media",
    }


async def fetch_grok_media_response(client, url, headers, payload, model, timeout):
    """处理 grok-imagine* 的非流式响应，转换为 Chat Completions 格式。"""
    import asyncio as _asyncio

    from ..json_utils import json_loads, json_dumps_text
    from ..response import check_response
    from ..response_context import mark_adapter_metrics_managed, mark_content_start, merge_usage

    json_payload = await _asyncio.to_thread(json_dumps_text, payload)
    response = await client.post(url, headers=headers, content=json_payload, timeout=timeout)
    _store_quota_from_headers(getattr(response, "headers", None))

    error_message = await check_response(response, "fetch_grok_media_response")
    if error_message:
        yield error_message
        return

    response_bytes = await response.aread()
    response_json = await _asyncio.to_thread(json_loads, response_bytes)
    mark_adapter_metrics_managed()

    if _is_video_model(payload.get("model")):
        # 视频：POST 只返回 request_id，轮询任务直到完成
        request_id = (response_json.get("request_id") or "").strip()
        if not request_id:
            yield "data: [DONE]\n\n"
            return
        base_url = url.rsplit("/videos/generations", 1)[0]
        try:
            task = await _poll_video_task(client, base_url, request_id, headers, timeout)
        except ValueError as exc:
            yield _chat_completion_result(str(exc), model)
            return
        video_url = ((task.get("video") or {}).get("url") or "").strip()
        content = f"[下载视频]({video_url})"
        result = _chat_completion_result(content, model, usage=None)
        mark_content_start()
        yield result
        return

    # 图片：data[] 转 content items
    content_items = _image_content_items(response_json, payload)
    usage = response_json.get("usage") or {}
    if usage:
        merge_usage(
            prompt_tokens=usage.get("input_tokens", 0),
            completion_tokens=usage.get("output_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
        )
    if content_items:
        mark_content_start()
    yield _chat_completion_result(content_items or None, model, usage=response_json.get("usage"))


async def fetch_grok_media_stream(client, url, headers, payload, model, timeout):
    """媒体端点不支持流式，拿到完整结果后转成 SSE 分块。"""
    from ..utils import generate_sse_response, end_of_line

    payload.pop("stream", None)

    result = None
    async for chunk in fetch_grok_media_response(client, url, headers, payload, model, timeout):
        result = chunk
    if not isinstance(result, dict):
        if result:
            yield result
        else:
            yield "data: [DONE]" + end_of_line
        return

    from datetime import datetime

    timestamp = int(datetime.timestamp(datetime.now()))
    yield await generate_sse_response(timestamp, model, role="assistant")
    mark = result["choices"][0]["message"].get("content")
    if mark:
        yield await generate_sse_response(timestamp, model, content=mark)
    yield await generate_sse_response(timestamp, model, stop="stop")
    yield "data: [DONE]" + end_of_line


# ============================================================
# 请求 / 响应适配器
# ============================================================

async def get_grok_payload(request, engine, provider, api_key=None):
    """构建请求 payload：grok-imagine* 走媒体端点，其余走 Responses 端点。"""
    from ..utils import get_model_dict

    model_dict = get_model_dict(provider)
    original_model = model_dict[request.model]
    if _is_media_model(original_model):
        return _build_media_payload(request, provider, api_key)

    url, headers, payload = await get_responses_payload(request, "openai-responses", provider, api_key)
    headers["Authorization"] = f"Bearer {api_key}"
    headers[CLI_TOKEN_AUTH_HEADER] = CLI_TOKEN_AUTH_VALUE
    headers[CLI_VERSION_HEADER] = CLI_VERSION_VALUE
    headers["User-Agent"] = CLI_USER_AGENT
    return url, headers, payload


async def get_grok_passthrough_meta(request, engine, provider, api_key=None):
    """透传模式构建 Responses API URL + CLI 伪装头。"""
    from ..utils import resolve_base_url

    base_url = provider.get("base_url") or DEFAULT_BASE_URL
    url = resolve_base_url(base_url, "/responses")
    headers = {
        "content-type": "application/json",
        "Authorization": f"Bearer {api_key}",
        CLI_TOKEN_AUTH_HEADER: CLI_TOKEN_AUTH_VALUE,
        CLI_VERSION_HEADER: CLI_VERSION_VALUE,
        "User-Agent": CLI_USER_AGENT,
    }
    return url, headers, {}


async def fetch_grok_response_stream(client, url, headers, payload, model, timeout):
    """流式响应适配：grok-imagine* 走媒体转换，其余走 Responses adapter + 被动采集 quota。"""
    if _is_media_model(payload.get("model")):
        async for chunk in fetch_grok_media_stream(client, url, headers, payload, model, timeout):
            yield chunk
        return
    capturing_client = _QuotaCapturingClient(client)
    async for chunk in fetch_responses_stream(capturing_client, url, headers, payload, model, timeout):
        yield chunk


async def fetch_grok_response(client, url, headers, payload, model, timeout):
    """非流式响应适配：grok-imagine* 走媒体转换，其余走 Responses adapter + 被动采集 quota。"""
    if _is_media_model(payload.get("model")):
        async for chunk in fetch_grok_media_response(client, url, headers, payload, model, timeout):
            yield chunk
        return
    capturing_client = _QuotaCapturingClient(client)
    async for chunk in fetch_responses_response(capturing_client, url, headers, payload, model, timeout):
        yield chunk


async def fetch_grok_models(client, provider):
    """获取 Grok 模型列表：对话模型来自 CLI 网关 /models，Imagine 媒体模型追加静态列表。"""
    from ..utils import resolve_base_url

    base_url = provider.get("base_url") or DEFAULT_BASE_URL
    api_key = provider.get("api")
    if isinstance(api_key, list):
        api_key = api_key[0] if api_key else None

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers[CLI_TOKEN_AUTH_HEADER] = CLI_TOKEN_AUTH_VALUE
        headers[CLI_VERSION_HEADER] = CLI_VERSION_VALUE
        headers["User-Agent"] = CLI_USER_AGENT

    chat_models = []
    models_url = resolve_base_url(base_url, "/models")
    try:
        response = await client.get(models_url, headers=headers)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and "data" in data:
            chat_models = [m.get("id") for m in data["data"] if isinstance(m, dict) and m.get("id")]
        elif isinstance(data, list):
            chat_models = [m.get("id") if isinstance(m, dict) else m for m in data]
    except Exception:
        pass

    if not chat_models:
        return list(DEFAULT_MODELS)

    # CLI 网关 /models 不含 Imagine 媒体模型，静态补齐（去重）
    models = list(chat_models)
    for model_id in DEFAULT_MODELS:
        if _is_media_model(model_id) and model_id not in models:
            models.append(model_id)
    return models


# ============================================================
# 前端 quota_display 插槽
# ============================================================

GROK_QUOTA_DISPLAY = """
export default function render(ctx) {
    ctx = ctx || {};
    const { el, data } = ctx;
    if (!el) return;
    const mode = ctx.context?.mode || ctx.mode || 'row';

    const raw = data?.raw || {};
    const plan = raw.plan || raw['xai-subscription-tier'] || raw['x-subscription-tier'] || '';
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
        } else if (plan) {
            el.style.display = '';
            el.textContent = plan;
            el.title = plan;
            el.className = 'text-[8px] font-semibold leading-none text-violet-500 truncate max-w-[50px]';
        } else {
            el.textContent = '';
            el.removeAttribute('title');
            el.style.display = 'none';
        }
        return;
    }

    const parts = [];
    if (plan) parts.push(plan);
    if (minPct != null) parts.push(minPct + '%');

    if (parts.length) {
        const colorCls = minPct == null ? 'bg-violet-500/15 text-violet-500' : minPct >= 50 ? 'bg-emerald-500/15 text-emerald-500' : minPct >= 20 ? 'bg-amber-500/15 text-amber-600' : 'bg-red-500/15 text-red-500';
        el.style.display = '';
        el.textContent = parts.join(' ');
        const tip = [];
        if (plan) tip.push(plan);
        if (qInner != null) tip.push('weekly ' + Math.round(qInner) + '%');
        if (qOuter != null) tip.push('monthly ' + Math.round(qOuter) + '%');
        el.title = tip.join(' · ');
        el.className = 'flex-shrink-0 text-[10px] font-semibold font-mono px-1.5 py-0.5 rounded relative z-[2] cursor-default ' + colorCls;
    } else {
        el.textContent = '';
        el.removeAttribute('title');
        el.style.display = 'none';
    }
}
""".strip()


# ============================================================
# 注册
# ============================================================

def register():
    """注册 Grok (xAI) OAuth 渠道。"""
    from .registry import register_channel

    register_channel(
        id="grok",
        type_name="openai-responses",
        default_base_url=DEFAULT_BASE_URL,
        default_token_url=OAUTH_ISSUER,
        auth_header="Authorization: Bearer {api_key}",
        description="xAI Grok (OAuth, Responses API, CLI 网关)",
        request_adapter=get_grok_payload,
        passthrough_adapter=get_grok_passthrough_meta,
        response_adapter=fetch_grok_response,
        stream_adapter=fetch_grok_response_stream,
        models_adapter=fetch_grok_models,
        is_oauth=True,
        oauth_provider=GrokProvider(),
        ui_slots={
            "quota_display": GROK_QUOTA_DISPLAY,
            "import_placeholder": "refresh_token 或 sso cookie",
        },
        source="builtin",
    )


def register_oauth_provider(oauth_manager):
    """兼容旧入口：向 OAuthManager 注册 Grok provider。"""
    provider = GrokProvider()
    provider.set_oauth_manager(oauth_manager)
    oauth_manager.register_provider("grok", provider)
