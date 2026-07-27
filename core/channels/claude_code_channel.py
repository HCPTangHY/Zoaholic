"""Claude Code OAuth 渠道适配器。

本文件自包含 Claude Code OAuth provider、渠道注册和响应头额度采集逻辑。
复用 claude_channel 的 request/response adapter，只处理 OAuth 认证和额度采集。

OAuth 流程参考 sub2api 的 internal/pkg/oauth/ 实现：
- Auth URL: https://claude.ai/oauth/authorize
- Token URL: https://platform.claude.com/v1/oauth/token
- Client ID: 9d1c250a-e61b-44d9-88ed-5944d1962f5e
- Redirect: https://platform.claude.com/oauth/code/callback
- Scope: org:create_api_key user:profile user:inference user:sessions:claude_code ...
- PKCE: S256
- Token exchange/refresh 用 JSON body（不是 form-urlencoded）
"""

import asyncio
import gzip
import hashlib
import httpx
import json
import secrets
import uuid
import time
from base64 import urlsafe_b64encode
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from core.oauth.providers.base import OAuthProvider
from core.channels.claude_channel import (
    fetch_claude_response_stream,
    fetch_claude_response,
    get_claude_payload,
    get_claude_passthrough_meta,
)


_oauth_manager = None

# ═══════════════════════════════════════════════════════════════════
# OAuth 常量
# ═══════════════════════════════════════════════════════════════════

CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
AUTH_URL = "https://claude.ai/oauth/authorize"
# 修改原因：Anthropic 已将 OAuth token 端点从 api.anthropic.com 迁移到 platform.claude.com，旧地址返回 405。
# 修改方式：对齐 sub2api 的 oauth.TokenURL 和 oauth.RedirectURI。
# 目的：修复 CC OAuth 登录 token exchange 405 报错。
DEFAULT_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
DEFAULT_REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
SCOPES = "org:create_api_key user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload"

DEFAULT_BASE_URL = "https://api.anthropic.com"

CLAUDE_REFRESH_MIN_BACKOFF = 5
CLAUDE_REFRESH_MAX_BACKOFF = 300
CLAUDE_REFRESH_MAX_RETRIES = 3
# 修改原因：Anthropic 会根据 User-Agent 的产品名+版本号判定是否为官方 CLI 请求，
#   旧值 claude-code/2.1.97 已被识别为第三方。
# 修改方式：对齐 sub2api constants.go 中 CLICurrentVersion 和 DefaultHeaders["User-Agent"]。
# 目的：降低被判定 third-party 而走 extra usage 的概率。
CLAUDE_CODE_CLI_VERSION = "2.1.161"
CLAUDE_CODE_USER_AGENT = f"claude-cli/{CLAUDE_CODE_CLI_VERSION} (external, cli)"

# ── Session ID 缓存（per api_key，1 小时 TTL） ──
_session_id_cache: dict[str, tuple[str, float]] = {}
_SESSION_TTL = 3600  # 1 hour

# ── 响应头网关指纹前缀（Layer 7+ 清洗） ──
_GATEWAY_HEADER_PREFIXES = (
    "x-litellm-", "helicone-", "x-portkey-",
    "cf-aig-", "x-kong-", "x-bt-",
)


def _get_session_id(api_key: str) -> str:
    """获取 per-apiKey 的稳定 session UUID（TTL=1h）。"""
    now = time.monotonic()
    cached = _session_id_cache.get(api_key)
    if cached and (now - cached[1]) < _SESSION_TTL:
        return cached[0]
    sid = str(uuid.uuid4())
    _session_id_cache[api_key] = (sid, now)
    return sid


def _parse_version_from_ua(ua: str) -> str:
    """从 User-Agent 解析 CC 版本号，如 'claude-code/2.1.97' → '2.1.97'。"""
    if not ua:
        return _BILLING_CC_VERSION
    for part in ua.split():
        if part.startswith("claude-code/"):
            ver = part.split("/", 1)[1].split(" ")[0]
            if ver:
                return ver
    return _BILLING_CC_VERSION


def _parse_entrypoint_from_ua(ua: str) -> str:
    """从 User-Agent 解析 entrypoint，如 'claude-code/2.1.97 vscode' → 'vscode'。"""
    if not ua:
        return _BILLING_ENTRYPOINT
    # CPA 格式: claude-code/VERSION ENTRYPOINT ...
    parts = ua.split()
    for i, part in enumerate(parts):
        if part.startswith("claude-code/") and i + 1 < len(parts):
            ep = parts[i + 1].lower()
            if ep in ("cli", "vscode", "local-agent", "jetbrains", "emacs", "vim"):
                return ep
    return _BILLING_ENTRYPOINT


def _strip_gateway_headers(headers: dict) -> dict:
    """清洗响应头中的网关/代理指纹前缀。"""
    return {
        k: v for k, v in headers.items()
        if not any(k.lower().startswith(p) for p in _GATEWAY_HEADER_PREFIXES)
    }
# 修改原因：anthropic-beta 头的完整集合是 Anthropic 判定请求来源的关键维度之一，
#   缺少任何"官方 CLI 请求才带"的 beta 都会被降级到第三方额度。
# 修改方式：对齐 sub2api FullClaudeCodeMimicryBetas() 的完整列表及顺序。
# 目的：确保与真实 Claude Code CLI 流量一致，避免 third-party 降级。
CLAUDE_CODE_ANTHROPIC_BETA = (
    "claude-code-20250219,oauth-2025-04-20,interleaved-thinking-2025-05-14,"
    "prompt-caching-scope-2026-01-05,effort-2025-11-24,"
    "context-management-2025-06-27,extended-cache-ttl-2025-04-11"
)

# 修改原因：CPA 在 refresh 遇到 429 时会按 refresh_token 记录 Retry-After 阻塞窗口。
# 修改方式：模块级字典保存每个 refresh_token 的 blocked_until epoch，供 refresh 前快速拒绝。
# 目的：避免同一失效或限流凭据在 Retry-After 窗口内反复打到 Anthropic token endpoint。
_claude_refresh_blocked_until: dict[str, float] = {}


# ═══════════════════════════════════════════════════════════════════
# PKCE
# ═══════════════════════════════════════════════════════════════════

def _generate_pkce():
    """生成 PKCE code_verifier + code_challenge (S256)。"""
    raw = secrets.token_bytes(96)
    code_verifier = urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


class _RefreshHTTPError(RuntimeError):
    """Claude refresh HTTP 错误，携带是否可重试的信息。"""

    def __init__(self, status_code: int, message: str, retryable: bool):
        # 修改原因：CPA 的 RefreshTokensWithRetry 只重试网络错误或 5xx，不重试 429 等显式阻塞错误。
        # 修改方式：自定义异常保存 status_code 和 retryable，供 refresh 重试循环判断。
        # 目的：让 Python 实现具备与 CPA 等价的刷新退避语义。
        self.status_code = status_code
        self.retryable = retryable
        super().__init__(f"token refresh failed with status {status_code}: {message}")


def _format_rfc3339(epoch: float | None = None) -> str:
    """把 epoch 秒格式化为 RFC3339 UTC 字符串。"""
    # 修改原因：CPA 的 ClaudeTokenData.expired 和 last_refresh 使用 RFC3339 字符串，而本地还需要 expires_at 数值。
    # 修改方式：保留 expires_at 的同时新增 RFC3339 字段，统一以 UTC Z 结尾输出。
    # 目的：兼容 OAuthManager 的刷新判断，并保存 CPA TokenStorage 所需的时间字段。
    value = time.time() if epoch is None else epoch
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def _clamp_refresh_backoff(seconds: float) -> float:
    """按 CPA 的 5 秒到 5 分钟范围裁剪 Retry-After。"""
    return max(CLAUDE_REFRESH_MIN_BACKOFF, min(CLAUDE_REFRESH_MAX_BACKOFF, seconds))


def _parse_retry_after(headers: Any) -> float:
    """解析 Retry-After / Retry-After-Ms 响应头。"""
    # 修改原因：CPA 同时支持 Retry-After 秒数、HTTP 日期和 Retry-After-Ms 毫秒数。
    # 修改方式：按大小写不敏感方式读取响应头，并将结果裁剪到 CPA 的 backoff 范围。
    # 目的：让 Anthropic 429 限流时的本地阻塞窗口与 CPA 保持一致。
    if not headers:
        return CLAUDE_REFRESH_MIN_BACKOFF

    retry_after = headers.get("Retry-After") or headers.get("retry-after")
    if retry_after is not None:
        raw = str(retry_after).strip()
        try:
            return _clamp_refresh_backoff(float(raw))
        except (TypeError, ValueError):
            try:
                parsed = parsedate_to_datetime(raw)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return _clamp_refresh_backoff(parsed.timestamp() - time.time())
            except (TypeError, ValueError, IndexError, OverflowError):
                pass

    retry_after_ms = headers.get("Retry-After-Ms") or headers.get("retry-after-ms")
    if retry_after_ms is not None:
        try:
            return _clamp_refresh_backoff(float(str(retry_after_ms).strip()) / 1000.0)
        except (TypeError, ValueError):
            pass

    return CLAUDE_REFRESH_MIN_BACKOFF


def _decode_gzip_if_needed(data: bytes) -> bytes:
    """对缺失 Content-Encoding 的 gzip 响应做 magic-byte 解压。"""
    # 修改原因：CPA 对 Claude 响应做 magic-byte 检测，处理上游返回 gzip 但缺失 Content-Encoding 的情况。
    # 修改方式：只在响应体以 gzip magic bytes 开头时尝试 gzip.decompress，失败则保留原始字节。
    # 目的：避免 Claude Code 非流式响应因未声明压缩而在 JSON 解析阶段失败。
    if len(data) >= 2 and data[0] == 0x1F and data[1] == 0x8B:
        try:
            return gzip.decompress(data)
        except OSError:
            return data
    return data


class _GzipAwareResponse:
    """包装 httpx.Response，补齐缺失 Content-Encoding 时的 gzip 解压。"""

    def __init__(self, response):
        self._response = response

    def __getattr__(self, name):
        return getattr(self._response, name)

    async def aread(self):
        # 修改原因：fetch_claude_response 和 check_response 都通过 aread 读取完整响应体。
        # 修改方式：读取原响应体后执行 magic-byte gzip 检测并返回解压后的字节。
        # 目的：在不复制 Claude 普通响应解析逻辑的前提下修复缺失 gzip 响应头的兼容问题。
        data = await self._response.aread()
        return _decode_gzip_if_needed(data)

    async def aiter_bytes(self):
        # 修改原因：流式路径按字节迭代响应；如果上游错误地压缩 SSE 且不声明响应头，逐行解析会失败。
        # 修改方式：先缓存到足够判断 gzip magic bytes；命中 gzip 时读完并解压，否则继续原样流式转发。
        # 目的：兼顾正常 SSE 的低延迟与异常 gzip 响应的可解析性。
        iterator = self._response.aiter_bytes()
        buffered: list[bytes] = []
        probe = b""
        async for chunk in iterator:
            if not chunk:
                continue
            buffered.append(chunk)
            probe += chunk
            if len(probe) >= 2:
                break

        if not buffered:
            return

        if len(probe) >= 2 and probe[0] == 0x1F and probe[1] == 0x8B:
            chunks = list(buffered)
            async for chunk in iterator:
                if chunk:
                    chunks.append(chunk)
            yield _decode_gzip_if_needed(b"".join(chunks))
            return

        for chunk in buffered:
            yield chunk
        async for chunk in iterator:
            yield chunk


class _GzipAwareStreamContext:
    """包装 httpx stream context，让进入上下文后返回 gzip-aware response。"""

    def __init__(self, inner_context):
        self._inner_context = inner_context

    async def __aenter__(self):
        response = await self._inner_context.__aenter__()
        return _GzipAwareResponse(response)

    async def __aexit__(self, exc_type, exc, tb):
        return await self._inner_context.__aexit__(exc_type, exc, tb)


class _GzipAwareClient:
    """代理 httpx.AsyncClient，只为 Claude Code 响应补 gzip magic-byte 解压。"""

    def __init__(self, client):
        self._client = client

    def __getattr__(self, name):
        return getattr(self._client, name)

    async def post(self, *args, **kwargs):
        response = await self._client.post(*args, **kwargs)
        return _GzipAwareResponse(response)

    def stream(self, *args, **kwargs):
        return _GzipAwareStreamContext(self._client.stream(*args, **kwargs))


# ═══════════════════════════════════════════════════════════════════
# Claude Code OAuth Provider
# ═══════════════════════════════════════════════════════════════════

class ClaudeCodeProvider(OAuthProvider):
    """Anthropic Claude Code OAuth provider。

    跟 Codex 最大的区别：
    1. token endpoint 用 JSON body 不是 form-urlencoded
    2. refresh 也用 JSON body
    3. 响应里有 organization + account 结构
    4. 不需要 id_token 解析（邮箱直接在 account.email_address 里）
    """

    # 修改原因：Anthropic OAuth 回调地址已迁移到 platform.claude.com/oauth/code/callback，不再用 localhost。
    # 修改方式：声明新的回调地址，路由层读取后传给 authorize 和 exchange 两步。
    # 目的：保证与 Anthropic 白名单一致。
    localhost_redirect_uri = DEFAULT_REDIRECT_URI

    @property
    def type_name(self) -> str:
        return "claude-code"

    @property
    def redirect_mode(self) -> str:
        return "manual"

    @property
    def redirect_uri(self) -> str:
        return DEFAULT_REDIRECT_URI

    def get_default_base_url(self) -> str:
        return DEFAULT_BASE_URL

    def build_auth_url(self, state: str, redirect_uri: str = DEFAULT_REDIRECT_URI) -> tuple[str, str]:
        """生成 Claude OAuth 授权 URL，返回 (auth_url, code_verifier)。"""
        code_verifier, code_challenge = _generate_pkce()
        params = {
            "code": "true",
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": redirect_uri or DEFAULT_REDIRECT_URI,
            "scope": SCOPES,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        from urllib.parse import urlencode as _urlencode
        url = f"{AUTH_URL}?{_urlencode(params)}"
        return url, code_verifier

    async def exchange_code(
        self,
        code: str,
        redirect_uri: str = DEFAULT_REDIRECT_URI,
        code_verifier: str | None = None,
        config: dict | None = None,
    ) -> dict:
        """用授权码 + PKCE verifier 换 token。"""
        if not code_verifier:
            raise ValueError("code_verifier is required for Claude Code OAuth")

        parsed_code, parsed_state = self._parse_code_and_state(code)

        data = {
            "code": parsed_code,
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "redirect_uri": redirect_uri or DEFAULT_REDIRECT_URI,
            "code_verifier": code_verifier,
        }
        if parsed_state:
            # 修改原因：CPA 的 parseCodeAndState 会把 code 中的 #fragment 作为 state 覆盖进 token exchange body。
            # 修改方式：只在 fragment 存在时加入 state 字段，避免本地路由未传 state 时制造空字段。
            # 目的：兼容 Anthropic 授权码中携带 fragment state 的返回形式。
            data["state"] = parsed_state
        token_response = await self._post_token_json(data, config=config)
        return self._build_credential({}, token_response)

    async def refresh_token(self, credential: dict, config: dict | None = None) -> dict:
        """用 refresh_token 刷新 access_token。"""
        refresh = credential.get("refresh_token")
        if not refresh:
            raise ValueError("refresh_token is required")

        self._raise_if_refresh_blocked(refresh)
        data = {
            "client_id": CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh,
        }
        token_response = await self._post_token_json(data, config=config, refresh_token=refresh)
        # 修改原因：CPA refresh 成功后会 clearClaudeRefreshBlockedUntil，避免旧 429 阻塞影响后续正常刷新。
        # 修改方式：刷新成功后删除当前 refresh_token 的 blocked_until 记录。
        # 目的：让临时限流恢复后账号可以重新进入 active 状态。
        _claude_refresh_blocked_until.pop(refresh, None)
        return self._build_credential(credential, token_response)

    async def fetch_quota(self, credential: dict, config: dict | None = None) -> dict | None:
        """调用 Anthropic OAuth usage 端点获取 Claude Code 配额。"""
        access_token = credential.get("access_token")
        if not access_token:
            return None

        # 从渠道 base_url 推导 usage 端点（走反代），fallback 到官方直连
        usage_url = "https://api.anthropic.com/api/oauth/usage"
        if config and isinstance(config, dict):
            base = config.get("base_url", "") or ""
            if base:
                from urllib.parse import urlparse
                host = urlparse(base).netloc.lower()
                if host and "anthropic.com" not in host:
                    # base_url 的域名不是 anthropic → 是反代 → 去掉末尾的 /v1 再拼 usage 路径
                    import re
                    base_stripped = re.sub(r'/v\d+/?$', '', base.rstrip("/").rstrip("#"))
                    usage_url = base_stripped + "/api/oauth/usage"

        headers = {
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Content-Type": "application/json",
            "User-Agent": CLAUDE_CODE_USER_AGENT,
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    usage_url,
                    headers=headers,
                )
                if resp.status_code != 200:
                    # 修改原因：此前上游 HTTP 错误被转成 None，路由层无法把具体失败原因返回给前端。
                    # 修改方式：非 200 响应直接抛出包含状态码和响应正文片段的 ValueError。
                    # 目的：让 Claude Code usage 接口失败时，管理员能在前端控制台看到可排查的上游错误。
                    raise ValueError(f"upstream {resp.status_code}: {resp.text[:500]}")
                data = resp.json()
        except Exception:
            # 修改原因：此前异常被静默吞掉，导致上层只能得到 Quota not available。
            # 修改方式：保留异常原样向上抛出，不在 provider 层改写为 None。
            # 目的：让路由层统一生成带错误详情的 JSON 响应。
            raise

        result = {}
        # five_hour → 5h 窗口
        fh = data.get("five_hour")
        if fh and isinstance(fh, dict):
            result["quota_inner"] = round(100 - (fh.get("utilization") or 0), 1)
            result["quota_inner_resets_at"] = fh.get("resets_at")
        # seven_day → 7d 窗口
        sd = data.get("seven_day")
        if sd and isinstance(sd, dict):
            result["quota_outer"] = round(100 - (sd.get("utilization") or 0), 1)
            result["quota_outer_resets_at"] = sd.get("resets_at")
        # model-specific weekly
        for key, val in data.items():
            if key.startswith("seven_day_") and isinstance(val, dict):
                model_tag = key[len("seven_day_"):]
                result[f"quota_outer_{model_tag}"] = round(100 - (val.get("utilization") or 0), 1)
                result[f"quota_outer_{model_tag}_resets_at"] = val.get("resets_at")
        # extra_usage
        eu = data.get("extra_usage")
        if eu and isinstance(eu, dict) and eu.get("is_enabled"):
            result["extra_usage_enabled"] = True
            result["extra_usage_monthly_limit"] = eu.get("monthly_limit")
            result["extra_usage_used"] = eu.get("used_credits")
            result["extra_usage_utilization"] = eu.get("utilization")

        # 修改原因：usage 接口未必返回订阅类型，但前端刷新 quota 后仍需要在 raw 数据中读到 tier。
        # 修改方式：从已保存 credential 中取 subscription_type 并补入 fetch_quota 结果。
        # 目的：让 quota_display 既能从 account 读 tier，也能从 data.raw 读到同一字段。
        if credential.get("subscription_type"):
            result["subscription_type"] = credential["subscription_type"]

        return result if result else None

    # ── 内部方法 ──

    @staticmethod
    def _parse_code_and_state(code: str) -> tuple[str, str]:
        """按 CPA parseCodeAndState 语义拆分 code 与 fragment state。"""
        # 修改原因：Anthropic 回调中的 code 可能包含 #fragment，CPA 会把 fragment 当作 state 传给 token endpoint。
        # 修改方式：只按第一个 # 拆分，保留前半段为真实授权码，后半段为可选 state。
        # 目的：避免 token exchange 把 fragment 一起当作 code，或丢失 Anthropic 返回的 state。
        parsed_code, sep, parsed_state = str(code or "").partition("#")
        return parsed_code, parsed_state if sep else ""

    @staticmethod
    def _raise_if_refresh_blocked(refresh_token: str) -> None:
        """如果 refresh_token 仍处于 Retry-After 阻塞窗口，则直接拒绝刷新。"""
        blocked_until = _claude_refresh_blocked_until.get(refresh_token, 0)
        if blocked_until > time.time():
            raise _RefreshHTTPError(
                429,
                f"refresh temporarily blocked until {_format_rfc3339(blocked_until)}",
                retryable=False,
            )

    def _resolve_token_url(self, config: dict | None = None) -> str:
        """动态读取 token_url，支持反代。"""
        if config and isinstance(config, dict):
            providers = config.get("providers", [])
            for p in providers:
                if isinstance(p, dict) and p.get("engine") == "claude-code":
                    custom = p.get("token_url") or p.get("preferences", {}).get("token_url")
                    if custom:
                        # 修改原因：token_url 可能配置为根域、/v1 或完整 /v1/oauth/token，不能简单重复拼 /v1。
                        # 修改方式：先去掉尾斜杠，再分别识别完整 endpoint、/v1 前缀和根域三种形式。
                        # 目的：让前端保存反代地址后，exchange 与 refresh 都能请求正确 endpoint。
                        url = str(custom).strip().rstrip("/")
                        if url.endswith("/oauth/token"):
                            return url
                        if url.endswith("/v1"):
                            return f"{url}/oauth/token"
                        return f"{url}/v1/oauth/token"
        return DEFAULT_TOKEN_URL

    async def _post_token_json(
        self,
        data: dict,
        config: dict | None = None,
        refresh_token: str | None = None,
    ) -> dict:
        """向 token endpoint 发 JSON POST（Claude 用 JSON 不用 form）。"""
        token_url = self._resolve_token_url(config)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "axios/1.13.6",
        }
        attempts = CLAUDE_REFRESH_MAX_RETRIES if refresh_token else 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            if refresh_token:
                self._raise_if_refresh_blocked(refresh_token)
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    response = await client.post(token_url, json=data, headers=headers)
                if response.status_code >= 400:
                    message = getattr(response, "text", "")
                    if refresh_token and response.status_code == 429:
                        retry_after = _parse_retry_after(getattr(response, "headers", None))
                        _claude_refresh_blocked_until[refresh_token] = time.time() + retry_after
                        raise _RefreshHTTPError(response.status_code, message, retryable=False)
                    if refresh_token:
                        raise _RefreshHTTPError(
                            response.status_code,
                            message,
                            retryable=response.status_code >= 500,
                        )
                    raise RuntimeError(f"token request failed with status {response.status_code}: {message}")
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("Invalid token response")
                return payload
            except _RefreshHTTPError as exc:
                last_error = exc
                if not refresh_token or not exc.retryable or attempt >= attempts - 1:
                    raise
            except httpx.HTTPError as exc:
                # 修改原因：CPA 的 RefreshTokensWithRetry 会对临时网络错误做指数退避式重试。
                # 修改方式：仅 refresh 路径重试 httpx 网络异常；授权码交换仍立即失败，避免重复消费 code。
                # 目的：提升后台刷新抗瞬时网络故障能力，同时不破坏 authorization_code 一次性语义。
                last_error = exc
                if not refresh_token or attempt >= attempts - 1:
                    raise

            await asyncio.sleep(attempt + 1)
        raise RuntimeError(f"token refresh failed after {attempts} attempts: {last_error}")

    def _build_credential(self, original: dict, token_response: dict) -> dict:
        """把 token endpoint 响应转成 oauth_state 凭据对象。"""
        # 修改原因：CPA 的 tokenResponse 解析出 access_token、refresh_token、token_type、expires_in、account 和 organization。
        # 修改方式：在原有扁平 oauth_state 上补齐 token_type、expires_at、expired、last_refresh、账号和组织字段。
        # 目的：既满足 OAuthManager 的本地刷新判断，也保存 CPA ClaudeTokenStorage 中需要的身份与过期信息。
        access_token = token_response.get("access_token")
        if not access_token:
            raise ValueError("Token response missing access_token")

        updated = dict(original or {})
        updated["access_token"] = access_token

        # refresh_token rotation — 新的覆盖旧的
        if token_response.get("refresh_token"):
            updated["refresh_token"] = token_response["refresh_token"]

        if token_response.get("token_type"):
            updated["token_type"] = token_response["token_type"]

        # 修改原因：Claude Code token response 会返回 subscriptionType 和 rateLimitTier，前端 tier 标签需要从 oauth_state 读取这些非敏感字段。
        # 修改方式：兼容 camelCase 与 snake_case 两种字段名，并保存为本地统一的 snake_case 字段。
        # 目的：让 /v1/oauth/accounts 能把订阅 tier 透传给 quota_display 插槽。
        sub_type = token_response.get("subscriptionType") or token_response.get("subscription_type")
        if sub_type:
            updated["subscription_type"] = sub_type

        rate_tier = token_response.get("rateLimitTier") or token_response.get("rate_limit_tier")
        if rate_tier:
            updated["rate_limit_tier"] = rate_tier

        try:
            expires_in = int(token_response.get("expires_in") or 0)
        except (TypeError, ValueError):
            expires_in = 0
        if expires_in > 0:
            expires_at = time.time() + expires_in
            updated["expires_at"] = expires_at
            updated["expired"] = _format_rfc3339(expires_at)

        updated["last_refresh"] = _format_rfc3339()

        account = token_response.get("account", {})
        if isinstance(account, dict):
            email = account.get("email_address")
            if email:
                updated["email"] = email
            account_uuid = account.get("uuid")
            if account_uuid:
                updated["account_id"] = account_uuid

        org = token_response.get("organization", {})
        if isinstance(org, dict):
            org_uuid = org.get("uuid")
            if org_uuid:
                updated["organization_id"] = org_uuid
            org_name = org.get("name")
            if org_name:
                updated["organization_name"] = org_name
            # organizationType: "claude_max" / "claude_pro" / "claude_team" 等
            org_type = org.get("organizationType") or org.get("organization_type") or org.get("type")
            if org_type:
                updated["organization_type"] = org_type
            # organizationRateLimitTier: "default_claude_max_20x" 等
            org_rate_tier = org.get("organizationRateLimitTier") or org.get("organization_rate_limit_tier") or org.get("rateLimitTier")
            if org_rate_tier:
                updated["rate_limit_tier"] = org_rate_tier
                # 从 rate_limit_tier 反推 subscription_type（比 subscriptionType 字段更靠谱）
                # "default_claude_max_20x" → "max", "default_claude_pro" → "pro"
                if not updated.get("subscription_type") or updated.get("subscription_type") == "Claude API":
                    tier_str = org_rate_tier.lower()
                    if "max" in tier_str:
                        updated["subscription_type"] = "max"
                    elif "pro" in tier_str:
                        updated["subscription_type"] = "pro"
                    elif "team" in tier_str:
                        updated["subscription_type"] = "team"
                    elif "enterprise" in tier_str:
                        updated["subscription_type"] = "enterprise"

        return updated


# ═══════════════════════════════════════════════════════════════════
# 渠道适配器（复用 claude_channel）

# ═══════════════════════════════════════════════════════════════════
# Plan Billing 清洗（绕过 Anthropic 4 层第三方检测）
# ═══════════════════════════════════════════════════════════════════

import re as _re
from contextvars import ContextVar, Token

# 请求级别的反向映射表：sanitize 时存入，response 时读取
_reverse_tool_map: ContextVar[dict[str, str]] = ContextVar("_reverse_tool_map", default={})
_reverse_prop_map: ContextVar[dict[str, str]] = ContextVar("_reverse_prop_map", default={})
# 修改原因：反向映射通过 ContextVar 保存，请求结束后需要用 set 返回的 token 显式恢复旧值。
# 修改方式：额外保存 tool/property 两个 ContextVar token，并由响应 adapter 的 finally 统一 reset。
# 目的：保留流式响应期间的反向映射能力，同时避免请求结束后 ContextVar 值残留。
_reverse_tool_map_token: ContextVar[Token | None] = ContextVar("_reverse_tool_map_token", default=None)
_reverse_prop_map_token: ContextVar[Token | None] = ContextVar("_reverse_prop_map_token", default=None)


def _reset_one_reverse_map(map_var, token_var) -> None:
    token = token_var.get(None)
    if token is None:
        map_var.set({})
        return
    try:
        map_var.reset(token)
    except (LookupError, RuntimeError, ValueError):
        # 修改原因：部分异步框架可能在复制后的 Context 中消费响应，旧 token 不能跨 Context reset。
        # 修改方式：token reset 失败时退回为空映射，保证当前 Context 不再持有请求级映射。
        # 目的：在不影响响应反向映射的前提下，尽量释放 ContextVar 对请求数据的引用。
        map_var.set({})
    finally:
        token_var.set(None)


def _set_reverse_tool_map(value: dict[str, str]) -> None:
    _reset_one_reverse_map(_reverse_tool_map, _reverse_tool_map_token)
    token = _reverse_tool_map.set(value)
    _reverse_tool_map_token.set(token)


def _set_reverse_prop_map(value: dict[str, str]) -> None:
    _reset_one_reverse_map(_reverse_prop_map, _reverse_prop_map_token)
    token = _reverse_prop_map.set(value)
    _reverse_prop_map_token.set(token)


def _reset_reverse_maps() -> None:
    # 修改原因：tool/property 反向映射只在当前请求响应转换期间有效。
    # 修改方式：按设置的反向顺序 reset property 和 tool 两个 ContextVar token。
    # 目的：让响应结束或异常中断后都能释放请求级映射字典。
    _reset_one_reverse_map(_reverse_prop_map, _reverse_prop_map_token)
    _reset_one_reverse_map(_reverse_tool_map, _reverse_tool_map_token)

# Layer 2: 第三方特征串（大小写不敏感匹配）
_THIRD_PARTY_PATTERNS = _re.compile(
    r"(?i)"
    r"(?:sessions_spawn|sessions_list|sessions_history|sessions_send|sessions_yield"
    r"|sessions_store|sessions_yield_interrupt"
    r"|HEARTBEAT_OK|HEARTBEAT"
    r"|clawhub|clawd|openclaw|open.?claw|cline|continue\.dev|lossless-claw"
    r"|running\s+inside|prometheus|skillhub"
    r"|roo.?code|windsurf|cursor|aider"
    r"|billing.?proxy|routing.?layer)"
)

# Layer 3: Tool name 重命名（第三方小写 → CC PascalCase）
_TOOL_RENAME_MAP: dict[str, str] = {
    "bash": "Bash",
    "read": "Read",
    "write": "Write",
    "edit": "Edit",
    "glob": "Glob",
    "grep": "Grep",
    "task": "Task",
    "webfetch": "WebFetch",
    "web_fetch": "WebFetch",
    "web_search": "WebSearch",
    "todowrite": "TodoWrite",
    "todoread": "TodoRead",
    "question": "Question",
    "skill": "Skill",
    "ls": "LS",
    "notebookedit": "NotebookEdit",
    "exec": "Bash",
    "process": "BashSession",
    "browser": "BrowserControl",
    "message": "SendMessage",
    "agents_list": "AgentList",
    "list_tasks": "TaskList",
    "get_history": "TaskHistory",
    "send_to_task": "TaskSend",
    "create_task": "TaskCreate",
    "subagents": "AgentControl",
    "session_status": "StatusCheck",
    "pdf": "PdfParse",
    "image_generate": "ImageCreate",
    "memory_search": "KnowledgeSearch",
    "memory_get": "KnowledgeGet",
    "lcm_expand_query": "ContextQuery",
    "lcm_grep": "ContextGrep",
    "lcm_describe": "ContextDescribe",
    "lcm_expand": "ContextExpand",
    "yield_task": "TaskYield",
    "task_store": "TaskStore",
    "task_yield_interrupt": "TaskYieldInterrupt",
}

# Layer 4: System prompt 中需要 strip 的配置节 section header
_SYSTEM_CONFIG_SECTIONS = _re.compile(
    r"(?:^|\\n|\n)"
    r"##\s*(?:Tooling|Workspace|Messaging|Reply|Configuration|Sessions?|Scheduling|Browser)"
    r"(?:\s|\\n|\n|$)",
    _re.MULTILINE,
)

# Layer 6: Property name 重命名（第三方工具 schema 里的特征属性名）
_PROP_RENAME_MAP: dict[str, str] = {
    "session_id": "thread_id",
    "conversation_id": "thread_ref",
    "summaryIds": "chunk_ids",
    "summary_id": "chunk_id",
    "system_event": "event_text",
    "agent_id": "worker_id",
    "wake_at": "trigger_at",
    "wake_event": "trigger_event",
}

# CC 工具桩：注入到 tools 数组，让工具集更像真 CC session
_CC_TOOL_STUBS: list[dict] = [
    {"name": "Glob", "description": "Find files by pattern", "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
    {"name": "Grep", "description": "Search file contents", "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}}, "required": ["pattern"]}},
    {"name": "Agent", "description": "Launch a subagent", "input_schema": {"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]}},
    {"name": "NotebookEdit", "description": "Edit notebook cells", "input_schema": {"type": "object", "properties": {"notebook_path": {"type": "string"}, "cell_index": {"type": "integer"}}, "required": ["notebook_path"]}},
    {"name": "TodoRead", "description": "Read task list", "input_schema": {"type": "object", "properties": {}}},
]




# ── Billing Header 构建（内置，不依赖 claude_code_compat 插件） ──

_BILLING_HEADER_PREFIX = "x-anthropic-billing-header:"
_BILLING_SALT = "59cf53e54c78"
_BILLING_SAMPLE_INDEXES = (4, 7, 20)
_BILLING_CC_VERSION = CLAUDE_CODE_CLI_VERSION
_BILLING_ENTRYPOINT = "cli"


def _sample_js_code_unit(text: str, idx: int) -> str:
    """按 JavaScript UTF-16 code unit 语义采样单个字符。"""
    if not isinstance(text, str) or idx < 0:
        return "0"
    utf16_le = text.encode("utf-16-le")
    start = idx * 2
    end = start + 2
    if end > len(utf16_le):
        return "0"
    return utf16_le[start:end].decode("utf-16-le", errors="replace")


def _first_user_message_text(messages: list) -> str:
    """提取第一条 user 消息中的首个文本内容。"""
    for msg in (messages or []):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    return item.get("text", "")
        return ""
    return ""


def _build_billing_header(messages: list, version: str = "", entrypoint: str = "") -> str:
    """构造 x-anthropic-billing-header 文本。"""
    ver = version or _BILLING_CC_VERSION
    ep = entrypoint or _BILLING_ENTRYPOINT
    sampled = "".join(
        _sample_js_code_unit(_first_user_message_text(messages), idx)
        for idx in _BILLING_SAMPLE_INDEXES
    )
    digest = hashlib.sha256(
        f"{_BILLING_SALT}{sampled}{ver}".encode()
    ).hexdigest()[:3]
    # 修改原因：新版 Claude Code CLI 已取消 cch=... 签名字段（sub2api issue #3358），
    #   继续注入它反而会让伪装请求偏离真实 CLI 流量。
    # 修改方式：移除 cch=00000 字段，仅保留 cc_version + cc_entrypoint。
    # 目的：与真实 CLI 流量一致。
    return (
        f"{_BILLING_HEADER_PREFIX} cc_version={ver}.{digest}; "
        f"cc_entrypoint={ep};"
    )


def _has_billing_header(system) -> bool:
    """检查 system 中是否已有 billing header。"""
    if isinstance(system, str):
        return system.strip().startswith(_BILLING_HEADER_PREFIX)
    if isinstance(system, list) and system:
        first = system[0]
        if isinstance(first, dict):
            return (first.get("text") or "").strip().startswith(_BILLING_HEADER_PREFIX)
        if isinstance(first, str):
            return first.strip().startswith(_BILLING_HEADER_PREFIX)
    return False


def _sanitize_for_plan_billing(payload: dict, headers: dict | None = None) -> dict:
    """清洗 payload 绕过 Anthropic 第三方检测，使请求走 plan limits 而非 extra usage。

    Layer 1: 确保 system prompt 存在
    Layer 2: 清除第三方特征串
    Layer 3: Tool name 重命名为 CC PascalCase
    Layer 4: Strip system prompt 中的配置节结构
    """
    if not isinstance(payload, dict):
        return payload

    # 修改原因：同一异步上下文可能在异常或重试后复用，旧反向映射不应影响新请求。
    # 修改方式：每次清洗 payload 前先清空上一轮保存在 ContextVar 中的映射和 token。
    # 目的：保证本次请求只使用本次 sanitize 产生的反向映射。
    _reset_reverse_maps()

    # ── Layer 1: system prompt 重写 ──
    # 修改原因：Anthropic 要求 system 块以 billing header 开头才能走 plan limits，
    #   但用户自定义 system prompt 降级到 messages 后会显著削弱指令遵循。
    # 修改方式：system 块保持 [billing_block, CC身份前缀, 原始system] 顺序拼接，
    #   不再把原始 system prompt 转移到 messages。
    # 目的：billing 头仍在首位保证计费判定，原始指令保持 system 优先级生效。
    _CC_IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."
    system = payload.get("system")
    _ua = ""
    if headers:
        _ua_key, _ua_val = _get_header_case_insensitive(headers, "User-Agent")
        _ua = str(_ua_val or "")
    billing_text = _build_billing_header(
        payload.get("messages", []),
        version=_parse_version_from_ua(_ua),
        entrypoint=_parse_entrypoint_from_ua(_ua),
    )
    billing_block = {"type": "text", "text": billing_text}
    cc_identity_block = {"type": "text", "text": _CC_IDENTITY}

    # 提取原始 system prompt 文本
    original_system_text = ""
    if isinstance(system, str):
        original_system_text = system.strip()
    elif isinstance(system, list):
        parts = []
        for blk in system:
            if isinstance(blk, dict):
                t = (blk.get("text") or "").strip()
                if t and not t.startswith(_BILLING_HEADER_PREFIX) and t != _CC_IDENTITY:
                    parts.append(t)
            elif isinstance(blk, str) and blk.strip():
                parts.append(blk.strip())
        original_system_text = "\n\n".join(parts)

    # system 块按伪装头在前、原始 prompt 在后的顺序拼接
    payload["system"] = [billing_block, cc_identity_block]
    if original_system_text and original_system_text != _CC_IDENTITY:
        payload["system"].append({"type": "text", "text": original_system_text})

    # ── Layer 3: Tool name 重命名 ──
    # 修改原因：原有的 _TOOL_RENAME_MAP 只覆盖了40个通用名，Clonoth 等客户端特有的
    #   snake_case 工具名（list_dir, save_memory, discord_manage 等）不在映射表中，
    #   原样发给上游会被 Anthropic 判定为第三方。
    # 修改方式：先查映射表，未命中且包含下划线的名称自动转 PascalCase。
    # 目的：确保所有工具名都以 PascalCase 形式发往上游，响应侧再反向还原。
    renamed: dict[str, str] = {}
    tools = payload.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, dict):
                name = tool.get("name", "")
                lower = name.lower()
                if lower in _TOOL_RENAME_MAP and name != _TOOL_RENAME_MAP[lower]:
                    new_name = _TOOL_RENAME_MAP[lower]
                    renamed[name] = new_name
                    tool["name"] = new_name
                elif "_" in name or "-" in name:
                    # 自动 snake_case/kebab-case → PascalCase
                    new_name = "".join(
                        seg.capitalize() for seg in name.replace("-", "_").split("_") if seg
                    )
                    if new_name and new_name != name:
                        renamed[name] = new_name
                        tool["name"] = new_name

    # messages 中的 tool_use / tool_result 也要同步重命名
    if renamed:
        messages = payload.get("messages")
        if isinstance(messages, list):
            _rename_tools_in_messages(messages, renamed)

    # 保存反向映射到 ContextVar，供响应侧 Layer 7 使用
    if renamed:
        _set_reverse_tool_map({v: k for k, v in renamed.items()})

    # ── Layer 5: Tool description strip ──
    # 清空 tool schema 的 description 内容（保留 key），减少指纹信号
    tools = payload.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, dict):
                if "description" in tool:
                    tool["description"] = ""
                # 也清理嵌套的 input_schema.properties 里的 description
                schema = tool.get("input_schema")
                if isinstance(schema, dict):
                    props = schema.get("properties")
                    if isinstance(props, dict):
                        for prop_val in props.values():
                            if isinstance(prop_val, dict) and "description" in prop_val:
                                prop_val["description"] = ""

    # ── Layer 5b: 注入 CC 工具桩 ──
    # 让 tools 数组包含真 CC 的标准工具，减少被检测概率
    if isinstance(tools, list):
        existing_names = {t.get("name") for t in tools if isinstance(t, dict)}
        for stub in _CC_TOOL_STUBS:
            if stub["name"] not in existing_names:
                tools.insert(0, dict(stub))

    # ── Layer 6: Property name 重命名 ──
    if isinstance(tools, list):
        for tool in tools:
            schema = tool.get("input_schema") if isinstance(tool, dict) else None
            if isinstance(schema, dict):
                props = schema.get("properties")
                if isinstance(props, dict):
                    for old_name, new_name in _PROP_RENAME_MAP.items():
                        if old_name in props:
                            props[new_name] = props.pop(old_name)
                # required 列表也要同步
                required = schema.get("required")
                if isinstance(required, list):
                    schema["required"] = [
                        _PROP_RENAME_MAP.get(r, r) for r in required
                    ]

    # 保存 property 反向映射
    _set_reverse_prop_map({v: k for k, v in _PROP_RENAME_MAP.items()})

    # ── Layer 2 + 4: 字符串级清洗（system + messages 中的文本） ──
    _sanitize_text_blocks(payload)

    return payload


def _rename_tools_in_messages(messages: list, renamed: dict[str, str]) -> None:
    """遍历 messages，重命名 tool_use/tool_result 中的 tool name。"""
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "tool_use":
                    name = block.get("name", "")
                    if name in renamed:
                        block["name"] = renamed[name]
                elif btype == "tool_result":
                    name = block.get("name", "")
                    if name in renamed:
                        block["name"] = renamed[name]


def _sanitize_text_blocks(payload: dict) -> None:
    """Layer 2 + 4: 清洗 system 和 messages 中的文本内容。"""
    # system
    system = payload.get("system")
    if isinstance(system, str):
        payload["system"] = _clean_text(system)
    elif isinstance(system, list):
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                block["text"] = _clean_text(text)

    # messages 中的 text 内容（只清 user/system role，不碰 assistant 的 thinking blocks）
    messages = payload.get("messages")
    if isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "")
            if role == "assistant":
                continue  # 不碰 assistant 消息（可能含 thinking/signature）
            content = msg.get("content")
            if isinstance(content, str):
                msg["content"] = _clean_text(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        block["text"] = _clean_text(block.get("text", ""))


def _clean_text(text: str) -> str:
    """对单个文本执行 Layer 2（特征串）+ Layer 4（配置节）清洗。"""
    # Layer 2: strip 第三方特征串
    text = _THIRD_PARTY_PATTERNS.sub("", text)
    # Layer 4: strip 配置节 section headers（但保留内容，只去 ## 行）
    text = _SYSTEM_CONFIG_SECTIONS.sub("\n", text)
    return text


# ═══════════════════════════════════════════════════════════════════

def _pop_header_case_insensitive(headers: dict, name: str):
    """按大小写不敏感方式移除请求头。"""
    target = name.lower()
    for key in list(headers.keys()):
        if str(key).lower() == target:
            return headers.pop(key)
    return None


def _set_header_case_insensitive(headers: dict, name: str, value: str) -> None:
    """按大小写不敏感方式设置请求头。"""
    _pop_header_case_insensitive(headers, name)
    headers[name] = value


def _get_header_case_insensitive(headers: dict, name: str):
    """按大小写不敏感方式读取请求头。"""
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target:
            return key, value
    return None, None


def _merge_anthropic_beta(headers: dict) -> None:
    """把 CPA Claude Code OAuth beta 集合合并进 anthropic-beta。"""
    # 修改原因：普通 claude_channel 只设置模型相关 beta，CPA Claude Code 请求还固定包含 oauth-2025-04-20 等 beta。
    # 修改方式：按逗号拆分现有值和 CPA 默认值，去重后写回 anthropic-beta。
    # 目的：让 Claude Code OAuth 请求头既保留原 adapter beta，又带上 OAuth 必需 beta。
    existing_key, existing_value = _get_header_case_insensitive(headers, "anthropic-beta")
    beta_values: list[str] = []
    for raw in (existing_value or "", CLAUDE_CODE_ANTHROPIC_BETA):
        for item in str(raw).split(","):
            beta = item.strip()
            if beta and beta not in beta_values:
                beta_values.append(beta)
    if existing_key and existing_key != "anthropic-beta":
        headers.pop(existing_key, None)
    headers["anthropic-beta"] = ",".join(beta_values)


def _apply_claude_code_headers(headers: dict, api_key: str | None) -> None:
    """把普通 Claude 请求头改成完整的 Claude Code OAuth 请求头。"""
    _pop_header_case_insensitive(headers, "x-api-key")
    _set_header_case_insensitive(headers, "Authorization", f"Bearer {api_key}")
    _merge_anthropic_beta(headers)

    if _get_header_case_insensitive(headers, "X-App")[0] is None:
        headers["X-App"] = "cli"
    if _get_header_case_insensitive(headers, "User-Agent")[0] is None:
        headers["User-Agent"] = CLAUDE_CODE_USER_AGENT

    # X-Claude-Code-Session-Id — per apiKey stable UUID (TTL=1h)
    if api_key and _get_header_case_insensitive(headers, "X-Claude-Code-Session-Id")[0] is None:
        headers["X-Claude-Code-Session-Id"] = _get_session_id(api_key)

    # x-client-request-id — per request UUID
    if _get_header_case_insensitive(headers, "x-client-request-id")[0] is None:
        headers["x-client-request-id"] = str(uuid.uuid4())

    # anthropic-version — 固定值，官方 CLI 始终携带
    if _get_header_case_insensitive(headers, "anthropic-version")[0] is None:
        headers["anthropic-version"] = "2023-06-01"

    # Anthropic-Dangerous-Direct-Browser-Access — 官方 CLI 固定携带
    if _get_header_case_insensitive(headers, "Anthropic-Dangerous-Direct-Browser-Access")[0] is None:
        headers["Anthropic-Dangerous-Direct-Browser-Access"] = "true"

    # X-Stainless SDK 头 — 完整模拟 Node.js/@anthropic-ai/sdk 指纹
    # 修改原因：sub2api 注入 8 个 X-Stainless 头，我们之前只有 4 个，缺失的头会被判第三方。
    # 修改方式：对齐 sub2api DefaultHeaders 的完整列表。
    # 目的：请求头指纹与真实 Claude CLI 流量一致。
    for hdr, val in [
        ("X-Stainless-Retry-Count", "0"),
        ("X-Stainless-Runtime", "node"),
        ("X-Stainless-Runtime-Version", "v24.3.0"),
        ("X-Stainless-Lang", "js"),
        ("X-Stainless-Package-Version", "0.94.0"),
        ("X-Stainless-OS", "Linux"),
        ("X-Stainless-Arch", "arm64"),
        ("X-Stainless-Timeout", "600"),
    ]:
        if _get_header_case_insensitive(headers, hdr)[0] is None:
            headers[hdr] = val


async def get_claude_code_payload(request, engine, provider, api_key=None):
    """复用 Claude adapter 构建 payload，覆盖为 Bearer 认证 + plan billing 清洗。"""
    url, headers, payload = await get_claude_payload(request, "claude", provider, api_key)
    _apply_claude_code_headers(headers, api_key)
    payload = _sanitize_for_plan_billing(payload, headers=headers)
    return url, headers, payload


async def get_claude_code_passthrough_meta(request, engine, provider, api_key=None):
    """透传模式：复用 Claude passthrough adapter + 注入 CC 伪装头。

    真 CC CLI 客户端自带完整请求头，后续 apply_custom_headers(original_headers)
    会用客户端值覆盖这里的默认值。第三方客户端（Cline/Continue 等）不带
    CC 特有头，这里注入的默认值就作为 fallback 保留。
    """
    url, headers, payload = await get_claude_passthrough_meta(request, "claude", provider, api_key)

    # 完整 CC 伪装（Bearer + Session-Id + request-id + Stainless + beta flags）
    # 第三方客户端：这些默认值保留
    # 真 CC 客户端：后续 original_headers 覆盖为真实值
    _apply_claude_code_headers(headers, api_key)

    return url, headers, payload


async def fetch_claude_code_response_stream(client, url, headers, payload, model, timeout):
    """包装 Claude 流式 adapter，补齐 gzip + Layer 7 反向映射。"""
    try:
        async for chunk in fetch_claude_response_stream(_GzipAwareClient(client), url, headers, payload, model, timeout):
            if isinstance(chunk, str):
                yield _reverse_map_chunk(chunk)
            else:
                yield chunk
    finally:
        # 修改原因：流式响应可能正常结束、异常中断或被客户端提前关闭，ContextVar 都需要清理。
        # 修改方式：在 async generator 的 finally 中 reset sanitize 阶段保存的 token。
        # 目的：避免 reverse tool/property map 在请求结束后继续占用上下文。
        _reset_reverse_maps()


async def fetch_claude_code_response(client, url, headers, payload, model, timeout):
    """包装 Claude 非流式 adapter，补齐 gzip + Layer 7 反向映射。"""
    try:
        async for chunk in fetch_claude_response(_GzipAwareClient(client), url, headers, payload, model, timeout):
            if isinstance(chunk, str):
                yield _reverse_map_chunk(chunk)
            else:
                yield chunk
    finally:
        # 修改原因：非流式响应同样依赖 sanitize 阶段的 ContextVar 反向映射。
        # 修改方式：响应 adapter 迭代结束后 reset 保存的 token。
        # 目的：确保一次请求的映射不会残留到后续请求上下文。
        _reset_reverse_maps()


# ═══════════════════════════════════════════════════════════════════
# 注册
# ═══════════════════════════════════════════════════════════════════



def _reverse_map_chunk(chunk: str) -> str:
    """Layer 7: 对响应 SSE chunk 做反向映射 — 把 CC PascalCase tool name 改回客户端原始名。"""
    reverse_tools = _reverse_tool_map.get({})
    reverse_props = _reverse_prop_map.get({})
    if not reverse_tools and not reverse_props:
        return chunk

    # 反向 tool name：只替换 JSON 值位置（"name":"Bash" → "name":"exec"）
    for cc_name, orig_name in reverse_tools.items():
        chunk = chunk.replace(f'"name":"{cc_name}"', f'"name":"{orig_name}"')
        chunk = chunk.replace(f'"name": "{cc_name}"', f'"name": "{orig_name}"')

    # 反向 property name
    for renamed, orig in reverse_props.items():
        chunk = chunk.replace(f'"{renamed}"', f'"{orig}"')

    return chunk


async def _passthrough_sanitize(payload, modifications, request, engine, provider, api_key):
    """透传模式下的 plan billing 清洗。"""
    # 透传模式下从 request 获取原始 headers 用于 UA 解析
    original_headers = {}
    if hasattr(request, '_passthrough_headers'):
        original_headers = request._passthrough_headers or {}
    return _sanitize_for_plan_billing(payload, headers=original_headers)


# 修改原因：Claude Code 的 extra_usage 可视化属于渠道专属 UI，不能继续由 Channels.tsx 写死计算和样式。
# 修改方式：在渠道文件中注册 key_background、quota_display、balance_summary 三个内联 JS 插槽，旧金额脚本常量仅保留给外部兼容。
# 目的：前端只提供通用挂载点，CC 的额度条、金额标签和余额汇总都随渠道元数据下发。
CC_KEY_BACKGROUND_UI = """
export default function render(ctx) {
  const { el, account } = ctx;
  const mode = ctx.context?.mode || ctx.mode || 'row';
  // 修改原因：Claude Code 背景条同一个脚本会挂载到完整行和机房卡片，机房卡片需要纵向填充才不会遮挡圆环内容。
  // 修改方式：从 ctx.context.mode 读取布局；rack 模式改为 bottom-up 高度填充，row 模式保持原横向宽度填充。
  // 目的：保留完整行的横向金额背景，同时让机房卡片中的背景装饰适配小卡片布局。
  if (!account?.extra_usage_enabled) {
    el.style.width = mode === 'rack' ? '100%' : '0%';
    el.style.height = '0%';
    el.style.background = 'transparent';
    return;
  }
  const limit = account.extra_usage_limit ?? account.extra_usage_monthly_limit ?? 0;
  const used = account.extra_usage_used ?? 0;
  const pct = limit > 0 ? Math.max(1, ((limit - used) / limit) * 100) : 0;
  const colors = { green: 'rgba(34,197,94,0.08)', yellow: 'rgba(234,179,8,0.08)', red: 'rgba(239,68,68,0.08)' };
  const color = pct >= 50 ? colors.green : pct >= 20 ? colors.yellow : colors.red;
  el.style.background = color;
  if (mode === 'rack') {
    el.style.position = 'absolute';
    el.style.top = 'auto';
    el.style.right = '0';
    el.style.bottom = '0';
    el.style.left = '0';
    el.style.width = '100%';
    el.style.height = pct + '%';
  } else {
    el.style.top = '';
    el.style.right = '';
    el.style.bottom = '';
    el.style.left = '';
    el.style.height = '';
    el.style.width = pct + '%';
  }
}
""".strip()

CC_QUOTA_LABEL_UI = """
export default function render(ctx) {
  const { el, account } = ctx;
  // 修改原因：$remaining / $limit 是 Claude Code extra_usage 的专属金额标签，通用前端不应计算。
  // 修改方式：在插槽脚本中读取账号透传字段，计算剩余额度并写入标签文本和 Tailwind 类。
  // 目的：让 CC 金额标签可以独立演进，不影响其他 OAuth 渠道。
  if (!account?.extra_usage_enabled) { el.style.display = 'none'; return; }
  el.style.display = '';
  const limit = account.extra_usage_limit ?? account.extra_usage_monthly_limit ?? 0;
  const used = account.extra_usage_used ?? 0;
  const remaining = Math.max(0, limit - used);
  const pct = limit > 0 ? (remaining / limit) * 100 : 0;
  const cls = pct >= 50 ? 'bg-emerald-500/15 text-emerald-500' : pct >= 20 ? 'bg-amber-500/15 text-amber-600' : 'bg-red-500/15 text-red-500';
  el.textContent = '$' + remaining.toFixed(0) + ' / $' + limit.toFixed(0);
  el.className = 'flex-shrink-0 text-[10px] font-semibold font-mono px-1.5 py-0.5 rounded relative z-[2] ' + cls;
}
""".strip()

# 修改原因：Claude Code 的 quota_display 需要同时承接订阅层级、quota 百分比和 extra_usage 金额。
# 修改方式：从 account/data.raw 读取 subscription_type 和标准 quota，从 account 读取 extra_usage，并组合为单个标签。
# 目的：前端删除独立标签挂载点后仍能在 quota_display 位置显示完整额度信息。
CC_QUOTA_DISPLAY = """
export default function render(ctx) {
    ctx = ctx || {};
    const { el, data, account } = ctx;
    if (!el) return;
    const mode = ctx.context?.mode || ctx.mode || 'row';

    // 修改原因：合并后单一 quota_display 同时服务完整行和机房卡片，extra_usage 金额在圆环中心会溢出。
    // 修改方式：rack 模式只输出百分比或 tier 缩写；row 模式继续组合 tier、百分比和 extra_usage 金额。
    // 目的：完整行保留 Claude Code 的完整额度信息，机房卡片中心只保留可读的短文本。
    const subType = account?.subscription_type || account?.subscriptionType || data?.raw?.subscription_type || '';
    const tierMap = { 'pro': 'Pro', 'max': 'Max', 'team': 'Team', 'enterprise': 'Enterprise' };
    const tierLabel = tierMap[subType.toLowerCase()] || (subType ? subType.charAt(0).toUpperCase() + subType.slice(1) : '');
    const shortTierLabel = tierLabel === 'Enterprise' ? 'Ent' : tierLabel;
    const q5 = typeof data?.quota_inner === 'number' ? data.quota_inner : null;
    const q7 = typeof data?.quota_outer === 'number' ? data.quota_outer : null;
    const pcts = [q5, q7].filter(v => v != null);
    const minPct = pcts.length ? Math.round(Math.min(...pcts)) : null;

    if (mode === 'rack') {
        if (minPct != null) {
            el.style.display = '';
            el.textContent = minPct + '%';
            el.removeAttribute('title');
            const colorCls = minPct >= 50 ? 'text-emerald-600' : minPct >= 20 ? 'text-amber-600' : 'text-red-500';
            el.className = 'text-[9px] font-bold font-mono leading-none ' + colorCls;
        } else if (shortTierLabel) {
            el.style.display = '';
            el.textContent = shortTierLabel;
            el.title = tierLabel;
            el.className = 'text-[8px] font-semibold leading-none text-blue-500 truncate max-w-[50px]';
        } else {
            el.textContent = '';
            el.removeAttribute('title');
            el.style.display = 'none';
        }
        return;
    }

    const quotaLabel = minPct != null ? (tierLabel ? tierLabel + ' ' + minPct + '%' : minPct + '%') : tierLabel;
    const limit = account?.extra_usage_enabled ? (account.extra_usage_limit ?? account.extra_usage_monthly_limit ?? 0) : 0;
    const used = account?.extra_usage_enabled ? (account.extra_usage_used ?? 0) : 0;
    const remaining = Math.max(0, limit - used);
    const extraPct = account?.extra_usage_enabled && limit > 0 ? (remaining / limit) * 100 : null;
    const extraUsageLabel = account?.extra_usage_enabled ? '$' + remaining.toFixed(0) + ' / $' + limit.toFixed(0) : '';
    const parts = [quotaLabel, extraUsageLabel].filter(Boolean);

    if (parts.length) {
        const scores = [minPct, extraPct].filter(v => typeof v === 'number');
        const colorBasis = scores.length ? Math.min(...scores) : null;
        const colorCls = colorBasis == null ? 'bg-blue-500/15 text-blue-500' : colorBasis >= 50 ? 'bg-emerald-500/15 text-emerald-500' : colorBasis >= 20 ? 'bg-amber-500/15 text-amber-600' : 'bg-red-500/15 text-red-500';
        el.style.display = '';
        el.textContent = parts.join(' · ');
        el.title = parts.join(' · ');
        el.className = 'flex-shrink-0 text-[10px] font-semibold font-mono px-1.5 py-0.5 rounded relative z-[2] cursor-default ' + colorCls;
    } else {
        el.textContent = '';
        el.removeAttribute('title');
        el.style.display = 'none';
    }
}
""".strip()

CC_BALANCE_SUMMARY_UI = """
export default function render(ctx) {
  const { el, accounts } = ctx;
  // 修改原因：Claude Code 余额按钮需要汇总所有开启 extra_usage 的账号，通用前端不应读取这些字段。
  // 修改方式：从 balance_summary 插槽上下文中的 accounts 聚合 limit/used，并写入按钮内联文本。
  // 目的：保持余额按钮平台化，同时让 CC 渠道自行定义汇总文案和 title。
  if (!accounts) return;
  const accts = Object.values(accounts).filter(a => a?.extra_usage_enabled);
  if (!accts.length) return;
  const totalLimit = accts.reduce((s, a) => s + (a.extra_usage_limit ?? a.extra_usage_monthly_limit ?? 0), 0);
  const totalUsed = accts.reduce((s, a) => s + (a.extra_usage_used ?? 0), 0);
  if (totalLimit <= 0) return;
  const r = totalLimit - totalUsed;
  el.innerHTML = '余额 <span class="font-mono">$' + r.toFixed(1) + '</span>';
  el.title = '总额 $' + totalLimit.toFixed(0) + ' / 已用 $' + totalUsed.toFixed(1);
}
""".strip()


# 修改原因：Claude Code Extra Usage 的充值入口是渠道专属提示，通用前端不能硬编码外部链接。



def register():
    """注册 Claude Code OAuth 渠道。"""
    from .registry import register_channel

    register_channel(
        id="claude-code",
        type_name="claude",
        default_base_url=DEFAULT_BASE_URL,
        default_token_url=DEFAULT_TOKEN_URL,
        auth_header="Authorization: Bearer {api_key}",
        description="Claude Code (OAuth subscription)",
        request_adapter=get_claude_code_payload,
        passthrough_adapter=get_claude_code_passthrough_meta,
        passthrough_payload_adapter=_passthrough_sanitize,
        response_adapter=fetch_claude_code_response,
        stream_adapter=fetch_claude_code_response_stream,
        is_oauth=True,
        # 修改原因：Claude Code 的 extra_usage 背景、金额标签、按钮汇总和订阅 tier 标签都属于渠道专属 UI。
        # 修改方式：注册 key_background、balance_summary 和合并后的 quota_display 三个展示插槽，不注册 key_border。
        # 目的：让前端通用挂载点加载 CC 专属脚本，同时继续使用默认 QuotaBorderOverlay 绘制 5h/7d 弧线。
        ui_slots={
            "key_background": CC_KEY_BACKGROUND_UI,
            "balance_summary": CC_BALANCE_SUMMARY_UI,
            "quota_display": CC_QUOTA_DISPLAY,
            "import_placeholder": "sk-ant-oat01-xxxxxxxx...",
        },
        # 修改原因：OAuth provider 注册要从 main.py 硬编码迁移到渠道注册声明。
        # 修改方式：在 Claude Code 渠道定义中直接传入 ClaudeCodeProvider 实例。
        # 目的：启动流程扫描 registry 时即可自动注册 Claude Code provider，插件渠道也可照此声明。
        oauth_provider=ClaudeCodeProvider(),
        source="builtin",
    )


def register_oauth_provider(oauth_manager, providers: list | None = None):
    """向 OAuthManager 注册 Claude Code provider。"""
    global _oauth_manager
    _oauth_manager = oauth_manager
    provider = ClaudeCodeProvider()
    oauth_manager.register_provider("claude-code", provider)
