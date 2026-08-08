"""Antigravity OAuth 渠道适配器。

本文件自包含 Google Antigravity OAuth provider、Cloud Code Assist 请求伪装、
OpenAI Chat Completions 到 Gemini 再到 Antigravity 的请求转换，以及上游响应解包逻辑。
"""

import asyncio
import copy
import hashlib
import random
import re
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import httpx

from core.channels.gemini_channel import (
    _is_image_model,
    fetch_gemini_response,
    fetch_gemini_response_stream,
    get_gemini_payload,
)
from core.json_utils import json_dumps_text, json_loads
from core.oauth.providers.base import OAuthProvider
from core.stream_utils import aiter_decoded_lines
from core.utils import get_model_dict


# ═══════════════════════════════════════════════════════════════════
# OAuth 与上游协议常量
# ═══════════════════════════════════════════════════════════════════

CLIENT_ID = "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"
CLIENT_SECRET = "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"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo?alt=json"
DEFAULT_REDIRECT_URI = "http://localhost:8085/oauth2callback"
SCOPES = [
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/cclog",
    "https://www.googleapis.com/auth/experimentsandconfigs",
]

DEFAULT_BASE_URL = "https://daily-cloudcode-pa.googleapis.com"
FALLBACK_BASE_URL = "https://cloudcode-pa.googleapis.com"
CODE_ASSIST_VERSION = "v1internal"
STREAM_ACTION = "streamGenerateContent"
GENERATE_ACTION = "generateContent"
COUNT_TOKENS_ACTION = "countTokens"
LOAD_CODE_ASSIST_ACTION = "loadCodeAssist"
FETCH_AVAILABLE_MODELS_ACTION = "fetchAvailableModels"

RELEASES_URL = "https://antigravity-hub-auto-updater-974169037036.us-central1.run.app/manifest/latest-arm64-mac.yml"
DEFAULT_ANTIGRAVITY_VERSION = "2.2.1"
VERSION_CACHE_TTL_SECONDS = 6 * 60 * 60
ANTIGRAVITY_API_CLIENT_HEADER = "gl-node/22.21.1"
TOKEN_USER_AGENT = "Go-http-client/2.0"
ONBOARD_USER_ACTION = "onboardUser"
ONBOARD_MAX_ATTEMPTS = 5
ONBOARD_POLL_INTERVAL = 2.0

_PROJECT_ADJECTIVES = ["useful", "bright", "swift", "calm", "bold"]
_PROJECT_NOUNS = ["fuze", "wave", "spark", "flow", "core"]
_ALLOWED_GENERATION_HEADERS = {"content-type", "authorization", "user-agent", "connection"}
_TRANSPORT_HEADERS = {"host", "content-length"}

_version_cache: dict[str, Any] = {"version": DEFAULT_ANTIGRAVITY_VERSION, "expires_at": 0.0}
_version_lock = asyncio.Lock()
_version_background_task: asyncio.Task | None = None


async def _async_return(value):
    """返回一个可 await 的固定值，供测试替换异步版本解析函数。"""
    # 修改原因：单元测试需要把 get_antigravity_version 替换为轻量可 await 对象。
    # 修改方式：提供一个极小的 async helper，测试中可用 lambda 返回它。
    # 目的：不访问真实 updater 服务也能覆盖 User-Agent 拼装逻辑。
    return value


# ═══════════════════════════════════════════════════════════════════
# 版本管理
# ═══════════════════════════════════════════════════════════════════


def _extract_version_from_releases_payload(payload: Any) -> str:
    """从 updater 响应中提取版本号。"""
    # 修改原因：Antigravity updater 的响应格式可能随服务端调整，渠道不能依赖单一 JSON 形态。
    # 修改方式：递归扫描常见字段和字符串内容，提取第一个 x.y.z 版本号。
    # 目的：保证版本拉取失败或格式轻微变化时仍能回退到固定版本，不影响请求路径。
    version_re = re.compile(r"\b\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?\b")

    def _scan(value: Any) -> str:
        if isinstance(value, str):
            match = version_re.search(value)
            return match.group(0) if match else ""
        if isinstance(value, dict):
            for key in ("version", "latest", "name", "tag", "tag_name"):
                found = _scan(value.get(key))
                if found:
                    return found
            for key in ("releases", "items", "data"):
                found = _scan(value.get(key))
                if found:
                    return found
            for item in value.values():
                found = _scan(item)
                if found:
                    return found
        if isinstance(value, list):
            for item in value:
                found = _scan(item)
                if found:
                    return found
        return ""

    return _scan(payload)


def _extract_version_from_yaml_manifest(text: str) -> str:
    """从 YAML manifest 中提取 version 字段。"""
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith("version:"):
            value = line.split(":", 1)[1].strip().strip('"').strip("'")
            if re.match(r"^\d+\.\d+\.\d+$", value):
                return value
    return ""


async def _fetch_antigravity_version() -> str:
    """请求 Antigravity Hub updater manifest 获取最新版本号。"""
    async with httpx.AsyncClient(timeout=10, http2=False) as client:
        response = await client.get(
            RELEASES_URL,
            headers={
                "User-Agent": "electron-builder",
                "Cache-Control": "no-cache",
            },
        )
        response.raise_for_status()
        text = response.text
    # 优先按 YAML manifest 解析
    version = _extract_version_from_yaml_manifest(text)
    if version:
        return version
    # 兼容旧 JSON 格式
    try:
        payload = json_loads(text)
    except Exception:
        payload = text
    version = _extract_version_from_releases_payload(payload)
    if not version:
        raise ValueError("Antigravity updater response did not contain a version")
    return version


async def _version_refresh_loop() -> None:
    """后台定期刷新 Antigravity 版本缓存。"""
    # 修改原因：懒加载只能保证请求时更新，长时间运行的服务应尽量提前刷新 UA 版本。
    # 修改方式：后台任务每 6 小时强制刷新一次缓存，异常时保留旧版本并继续下一轮。
    # 目的：减少每个请求路径上的 updater 访问，同时保持版本指纹接近真实客户端。
    while True:
        await asyncio.sleep(VERSION_CACHE_TTL_SECONDS)
        try:
            await get_antigravity_version(force=True)
        except Exception:
            pass


def _ensure_version_background_task() -> None:
    """在已有事件循环中启动版本刷新后台任务。"""
    # 修改原因：channels 包可能在无事件循环的导入期加载，不能无条件 create_task。
    # 修改方式：只有检测到运行中的事件循环时才启动守护任务，且全局只启动一次。
    # 目的：满足后台刷新需求，同时避免导入时抛出 RuntimeError 或制造重复任务。
    global _version_background_task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _version_background_task is None or _version_background_task.done():
        _version_background_task = loop.create_task(_version_refresh_loop())


async def get_antigravity_version(force: bool = False) -> str:
    """返回缓存的 Antigravity 版本号，必要时刷新。"""
    # 修改原因：Antigravity User-Agent 要使用实时版本，但每次请求 updater 会增加延迟和故障面。
    # 修改方式：用 6 小时 TTL 缓存版本；刷新失败时继续返回最后成功值或 fallback。
    # 目的：保证渠道始终可用，并尽量模拟真实 Antigravity 客户端版本。
    _ensure_version_background_task()
    now = time.time()
    if not force and now < float(_version_cache.get("expires_at") or 0):
        return str(_version_cache.get("version") or DEFAULT_ANTIGRAVITY_VERSION)

    async with _version_lock:
        now = time.time()
        if not force and now < float(_version_cache.get("expires_at") or 0):
            return str(_version_cache.get("version") or DEFAULT_ANTIGRAVITY_VERSION)
        try:
            version = await _fetch_antigravity_version()
        except Exception:
            version = str(_version_cache.get("version") or DEFAULT_ANTIGRAVITY_VERSION)
        _version_cache["version"] = version or DEFAULT_ANTIGRAVITY_VERSION
        _version_cache["expires_at"] = now + VERSION_CACHE_TTL_SECONDS
        return str(_version_cache["version"])


# ═══════════════════════════════════════════════════════════════════
# OAuth Provider
# ═══════════════════════════════════════════════════════════════════


class AntigravityProvider(OAuthProvider):
    """管理 Antigravity Google OAuth token 的授权码交换、刷新和额度查询。"""

    redirect_mode = "manual"
    localhost_redirect_uri = DEFAULT_REDIRECT_URI

    def __init__(self):
        """初始化 provider，不固化运行时配置。"""
        # 修改原因：token_url 和 base_url 可能由管理端热更新，provider 在启动时不能缓存旧值。
        # 修改方式：保存配置 getter 占位，在每次 token 或 loadCodeAssist 请求前动态解析。
        # 目的：用户保存 OAuth 反代或 Cloud Code 反代后无需重启即可生效。
        self._config_getter = None

    @property
    def type_name(self) -> str:
        return "antigravity"

    @property
    def redirect_uri(self) -> str:
        return DEFAULT_REDIRECT_URI

    def set_config_getter(self, config_getter) -> None:
        """设置运行时配置读取函数。"""
        # 修改原因：provider 不应反向导入 FastAPI app，否则会形成循环依赖。
        # 修改方式：由 OAuthManager.register_provider 注入自身 get_config 方法。
        # 目的：保持 OAuth 子模块解耦，并支持运行时配置热更新。
        self._config_getter = config_getter

    def _get_runtime_config(self) -> dict:
        """读取当前运行时配置，失败时回退为空配置。"""
        # 修改原因：测试替身、启动早期或配置异常都不应阻断 token refresh。
        # 修改方式：只接受 dict 返回值，异常和非 dict 统一按空配置处理。
        # 目的：最坏情况下回退到 Google 默认 token endpoint 和官方 Cloud Code endpoint。
        if not callable(self._config_getter):
            return {}
        try:
            config = self._config_getter()
        except Exception:
            return {}
        return config if isinstance(config, dict) else {}

    @staticmethod
    def _normalize_token_url(custom: str) -> str:
        """把自定义 token_url 规范化为 /token endpoint。"""
        # 修改原因：管理端可能保存 OAuth 反代根域，也可能保存完整 token endpoint。
        # 修改方式：完整 /token 原样返回，否则补齐 /token 后缀。
        # 目的：兼容两种常见配置写法。
        url = str(custom).strip().rstrip("/")
        if url.endswith("/token"):
            return url
        return f"{url}/token"

    def _resolve_provider_config(self, config: dict | None = None) -> dict:
        """从完整配置或单个 provider 配置中找到 Antigravity provider。"""
        # 修改原因：OAuthManager 在 refresh/exchange 时传完整配置，在 fetch_quota 时传当前渠道配置。
        # 修改方式：先识别单 provider dict，再兼容 providers 和 api_config.providers 两种列表位置。
        # 目的：让所有 OAuth 调用路径都能使用同一套 token_url/base_url/project 解析逻辑。
        runtime_config = config if isinstance(config, dict) else self._get_runtime_config()
        if isinstance(runtime_config, dict) and runtime_config.get("engine") == "antigravity":
            return runtime_config
        providers = runtime_config.get("providers") if isinstance(runtime_config, dict) else None
        if not isinstance(providers, list):
            api_config = runtime_config.get("api_config") if isinstance(runtime_config, dict) else None
            providers = api_config.get("providers") if isinstance(api_config, dict) and isinstance(api_config.get("providers"), list) else []
        for provider in providers:
            if isinstance(provider, dict) and provider.get("engine") == "antigravity":
                return provider
        return {}

    def _resolve_token_url(self, config: dict | None = None) -> str:
        """从运行时配置读取 token endpoint，未配置时使用 Google 默认值。"""
        provider = self._resolve_provider_config(config)
        preferences = provider.get("preferences") if isinstance(provider.get("preferences"), dict) else {}
        custom = provider.get("token_url") or preferences.get("token_url")
        custom = custom.strip() if isinstance(custom, str) else custom
        if custom:
            return self._normalize_token_url(str(custom))
        return TOKEN_URL

    def _resolve_base_url(self, config: dict | None = None) -> str:
        """从运行时配置读取 Cloud Code Assist base_url。"""
        provider = self._resolve_provider_config(config)
        preferences = provider.get("preferences") if isinstance(provider.get("preferences"), dict) else {}
        custom = provider.get("base_url") or preferences.get("base_url")
        custom = custom.strip() if isinstance(custom, str) else custom
        return str(custom).rstrip("/") if custom else DEFAULT_BASE_URL

    def get_default_base_url(self) -> str:
        """返回 Antigravity 默认上游地址。"""
        return DEFAULT_BASE_URL

    def build_auth_url(self, state: str, redirect_uri: str = DEFAULT_REDIRECT_URI) -> tuple[str, str]:
        """生成 Google OAuth 授权 URL，Antigravity 不使用 PKCE。"""
        # 修改原因：Antigravity 使用 Google installed app OAuth 和 CPA 提取出的固定 scope 集合。
        # 修改方式：构造 authorization_code URL，并返回空 verifier 表示不启用 PKCE。
        # 目的：让通用 OAuth pending flow 可以复用，同时不向 Google token endpoint 发送错误的 PKCE 参数。
        params = {
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": redirect_uri or DEFAULT_REDIRECT_URI,
            "scope": " ".join(SCOPES),
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
        }
        return f"{AUTH_URL}?{urlencode(params)}", ""

    async def exchange_code(
        self,
        code: str,
        redirect_uri: str = DEFAULT_REDIRECT_URI,
        code_verifier: str | None = None,
        config: dict | None = None,
    ) -> dict:
        """用授权码换取 Antigravity OAuth token，并加载用户邮箱和 Cloud Code project。"""
        data = {
            "code": code,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": redirect_uri or DEFAULT_REDIRECT_URI,
            "grant_type": "authorization_code",
        }
        token_response = await self._post_token_form(data, config=config)
        access_token = token_response.get("access_token")
        email = await self._fetch_email(access_token)
        load_payload = await self._load_code_assist(access_token, config=config)
        project_id = _extract_project_from_load_code_assist(load_payload) or _extract_antigravity_project_id(self._resolve_provider_config(config), include_context=False)
        if not project_id and access_token:
            tier_id = _default_tier_id_from_load_code_assist(load_payload)
            project_id = await self._onboard_user(access_token, tier_id, config=config)
        return self._build_credential({}, token_response, email=email, project_id=project_id, load_code_assist=load_payload)

    async def refresh_token(self, credential: dict, config: dict | None = None) -> dict:
        """用 refresh_token 刷新 Antigravity access_token。"""
        refresh = credential.get("refresh_token")
        if not refresh:
            raise ValueError("refresh_token is required")
        data = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        }
        token_response = await self._post_token_form(data, config=config)
        updated = self._build_credential(credential, token_response)
        if not updated.get("project_id"):
            access_token = updated.get("access_token")
            load_payload = await self._load_code_assist(access_token, config=config)
            project_id = _extract_project_from_load_code_assist(load_payload)
            if not project_id and access_token:
                tier_id = _default_tier_id_from_load_code_assist(load_payload)
                project_id = await self._onboard_user(access_token, tier_id, config=config)
            updated = self._build_credential(updated, {}, project_id=project_id, load_code_assist=load_payload)
        return updated

    async def fetch_quota(self, credential: dict, config: dict | None = None) -> dict | None:
        """通过 fetchAvailableModels 获取各模型组 quota + loadCodeAssist 获取 credits。"""
        # 修改原因：loadCodeAssist 只返回 paidTier/credits，不含各模型组的 remainingFraction。
        # 修改方式：主调 fetchAvailableModels 获取按模型的 quotaInfo，辅调 loadCodeAssist 获取 credits。
        # 目的：让前端能按模型组画半圆弧，同时兼容有 credits 池的账号。
        access_token = credential.get("access_token")
        raw: dict[str, Any] = {}

        # 1. fetchAvailableModels — 各模型独立 quota
        models_payload = await self._fetch_available_models(access_token, config=config)
        model_quotas = _extract_model_quotas_from_available_models(models_payload)
        if model_quotas:
            raw["modelQuotas"] = model_quotas

        # 2. loadCodeAssist — paidTier + credits
        load_payload = await self._load_code_assist(access_token, config=config)
        if isinstance(load_payload, dict):
            paid_tier = load_payload.get("paidTier")
            if isinstance(paid_tier, dict):
                raw["paidTier"] = paid_tier
                if paid_tier.get("availableCredits") is not None:
                    raw["availableCredits"] = paid_tier.get("availableCredits")
            project_id = _extract_project_from_load_code_assist(load_payload)
            if project_id:
                raw["cloudaicompanionProject"] = project_id

        if not raw:
            return None

        # 修改原因：Antigravity 前端把上弧定义为 Gemini 组最低值、下弧定义为 Claude/GPT 外部模型组最低值。
        # 修改方式：优先从 raw.modelQuotas 按 provider 计算 quota_inner/quota_outer；无法识别 provider 时再回退到旧的全模型最低值。
        # 目的：/v1/channels/balance、OAuth 缓存和管理端 Key 行使用同一套分组语义。
        quota_result: dict[str, Any] = {"raw": raw}
        provider_quota = _compute_antigravity_provider_quota_percentages(raw.get("modelQuotas", []))
        if provider_quota:
            quota_result.update(provider_quota)
        else:
            fractions = [
                mq["remainingFraction"]
                for mq in raw.get("modelQuotas", [])
                if isinstance(mq.get("remainingFraction"), (int, float))
            ]
            if fractions:
                min_pct = min(fractions) * 100.0
                quota_result["quota_inner"] = min_pct
                quota_result["quota_outer"] = min_pct
            else:
                # fallback: 从 credits 提取
                credits_list = raw.get("availableCredits")
                if isinstance(credits_list, list):
                    for credit in credits_list:
                        if not isinstance(credit, dict):
                            continue
                        try:
                            amount = float(credit.get("creditAmount", 0))
                            if amount > 0:
                                quota_result["quota_inner"] = min(amount / 10.0, 100.0)
                                quota_result["quota_outer"] = quota_result["quota_inner"]
                                break
                        except (TypeError, ValueError):
                            continue
        return quota_result

    async def _post_token_form(self, data: dict, config: dict | None = None) -> dict:
        """向 Google token endpoint 提交 form-urlencoded 请求。"""
        # 修改原因：CPA 提取显示 Antigravity token refresh 使用 Go-http-client/2.0 指纹，并且必须禁用 HTTP/2。
        # 修改方式：每次请求动态解析 token_url，使用 httpx data= 生成 form body，只显式发送特殊 User-Agent。
        # 目的：保持 Google OAuth 标准格式，同时模拟 Antigravity executor 的 token refresh 行为。
        token_url = self._resolve_token_url(config)
        async with httpx.AsyncClient(timeout=30, http2=False) as client:
            response = await client.post(token_url, data=data, headers={"User-Agent": TOKEN_USER_AGENT})
            if response.status_code >= 400:
                raise ValueError(f"{response.status_code} {response.text}")
            payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Invalid token response")
        return payload

    async def _fetch_email(self, access_token: str | None) -> str:
        """用 access_token 调 Google userinfo 获取邮箱。"""
        # 修改原因：Google token response 不保证包含邮箱，账号列表需要稳定 key_id 供管理端选择。
        # 修改方式：授权码交换成功后请求 userinfo；失败时返回空字符串，不阻断登录。
        # 目的：尽量用邮箱标识 OAuth 账号，同时兼容 userinfo 暂时不可用的情况。
        if not access_token:
            return ""
        try:
            async with httpx.AsyncClient(timeout=15, http2=False) as client:
                response = await client.get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})
        except Exception:
            return ""
        if response.status_code == 200:
            payload = response.json()
            if isinstance(payload, dict):
                return str(payload.get("email") or "")
        return ""

    async def _fetch_available_models(self, access_token: str | None, config: dict | None = None) -> dict:
        """调用 Antigravity fetchAvailableModels，获取各模型 quotaInfo。"""
        # 修改原因：loadCodeAssist 不返回各模型组的 remainingFraction，前端无法按模型画 quota 弧。
        # 修改方式：新增调用 fetchAvailableModels 端点，返回 models 字典（含 quotaInfo）。
        # 目的：让 fetch_quota 能拿到按模型粒度的 quota 数据供 QUOTA_UI 渲染。
        if not access_token:
            return {}
        version = await get_antigravity_version()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": _antigravity_load_code_assist_user_agent(version),
            "X-Goog-Api-Client": ANTIGRAVITY_API_CLIENT_HEADER,
        }
        # 修改原因：部分反代不支持 fetchAvailableModels 路径，返回 403。
        # 修改方式：优先试官方直连域名，再 fallback 到用户配置的反代。
        # 目的：确保 quota 查询不受反代路径限制影响。
        bases = [DEFAULT_BASE_URL, FALLBACK_BASE_URL, self._resolve_base_url(config)]
        seen: set[str] = set()
        for base in bases:
            normalized_base = str(base or "").rstrip("/")
            if not normalized_base or normalized_base in seen:
                continue
            seen.add(normalized_base)
            url = _build_antigravity_url_from_base(normalized_base, FETCH_AVAILABLE_MODELS_ACTION)
            try:
                async with httpx.AsyncClient(timeout=30, http2=False) as client:
                    response = await client.post(url, json={}, headers=headers)
                if response.status_code < 400:
                    payload = response.json()
                    return payload if isinstance(payload, dict) else {}
            except Exception:
                continue
        return {}

    async def _load_code_assist(self, access_token: str | None, config: dict | None = None) -> dict:
        """调用 Antigravity loadCodeAssist，提取 IDE project 和 credits 信息。"""
        # 修改原因：Antigravity 请求体中的 project 应尽量来自真实 loadCodeAssist，而不是随机占位。
        # 修改方式：用 Antigravity 专用 UA 和 X-Goog-Api-Client 调用 primary base_url，失败时尝试 fallback。
        # 目的：保持 IDE 启动流程指纹，并为后续请求保存 cloudaicompanionProject。
        if not access_token:
            return {}
        version = await get_antigravity_version()
        body = {"metadata": {"ideType": "ANTIGRAVITY"}}
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": _antigravity_load_code_assist_user_agent(version),
            "X-Goog-Api-Client": ANTIGRAVITY_API_CLIENT_HEADER,
        }
        bases = [self._resolve_base_url(config), FALLBACK_BASE_URL]
        seen: set[str] = set()
        for base in bases:
            normalized_base = str(base or "").rstrip("/")
            if not normalized_base or normalized_base in seen:
                continue
            seen.add(normalized_base)
            url = _build_antigravity_url_from_base(normalized_base, LOAD_CODE_ASSIST_ACTION)
            try:
                async with httpx.AsyncClient(timeout=30, http2=False) as client:
                    response = await client.post(url, json=body, headers=headers)
                if response.status_code < 400:
                    payload = response.json()
                    return payload if isinstance(payload, dict) else {}
            except Exception:
                continue
        return {}

    async def _onboard_user(self, access_token: str, tier_id: str = "free-tier", config: dict | None = None) -> str:
        """loadCodeAssist 未返回 project 时，通过 onboardUser 初始化项目。"""
        if not access_token:
            return ""
        version = await get_antigravity_version()
        body = {
            "tier_id": tier_id,
            "metadata": {"ide_type": "ANTIGRAVITY", "ide_version": version, "ide_name": "antigravity"},
        }
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "*/*",
            "Content-Type": "application/json",
            "User-Agent": f"{_antigravity_user_agent(version)} google-api-nodejs-client/10.3.0",
            "X-Goog-Api-Client": ANTIGRAVITY_API_CLIENT_HEADER,
        }
        url = _build_antigravity_url_from_base(DEFAULT_BASE_URL, ONBOARD_USER_ACTION)
        for attempt in range(ONBOARD_MAX_ATTEMPTS):
            try:
                async with httpx.AsyncClient(timeout=30, http2=False) as client:
                    response = await client.post(url, json=body, headers=headers)
                if response.status_code != 200:
                    return ""
                payload = response.json()
                if not isinstance(payload, dict):
                    return ""
                if payload.get("done") is True:
                    resp_data = payload.get("response")
                    if isinstance(resp_data, dict):
                        project = _extract_project_from_load_code_assist(resp_data)
                        if project:
                            return project
                    return ""
                await asyncio.sleep(ONBOARD_POLL_INTERVAL)
            except Exception:
                return ""
        return ""

    def _build_credential(
        self,
        original: dict | None,
        token_response: dict,
        email: str | None = None,
        project_id: str | None = None,
        load_code_assist: dict | None = None,
    ) -> dict:
        """把 Google token response 转成 oauth_state 凭据对象。"""
        # 修改原因：Google refresh 通常不返回新的 refresh_token、邮箱或 project_id，直接重建会丢失关键字段。
        # 修改方式：复制原凭据后只覆盖 access_token、可选 refresh_token、token_type、expires_at、email、project_id 和 credits raw。
        # 目的：保证刷新不会破坏账号身份、项目标识和后续 refresh 所需凭据。
        updated = dict(original or {})
        access_token = token_response.get("access_token") or updated.get("access_token")
        if not access_token:
            raise ValueError("Token response missing access_token")
        updated["access_token"] = access_token

        if token_response.get("refresh_token"):
            updated["refresh_token"] = token_response["refresh_token"]
        if token_response.get("token_type"):
            updated["token_type"] = token_response["token_type"]
        try:
            expires_in = int(token_response.get("expires_in") or 0)
        except (TypeError, ValueError):
            expires_in = 0
        if expires_in > 0:
            updated["expires_at"] = time.time() + expires_in
        if email:
            updated["email"] = email
        if project_id:
            updated["project_id"] = project_id

        quota_raw = _extract_quota_raw_from_load_code_assist(load_code_assist)
        if quota_raw:
            updated["quota_raw"] = quota_raw
        return updated


# ═══════════════════════════════════════════════════════════════════
# 请求体转换与伪装
# ═══════════════════════════════════════════════════════════════════


def _antigravity_user_agent(version: str) -> str:
    """生成 Antigravity Hub 生成接口 User-Agent。"""
    return f"antigravity/hub/{version} darwin/arm64"


def _antigravity_load_code_assist_user_agent(version: str) -> str:
    """生成 Antigravity loadCodeAssist User-Agent。"""
    return f"{_antigravity_user_agent(version)} google-api-nodejs-client/10.3.0"


def _build_generation_headers(access_token: str | None, version: str) -> dict:
    """构建 Antigravity 生成接口请求头白名单。"""
    # 修改原因：Antigravity 封控对请求头很敏感，普通 httpx/Gemini adapter 的 Accept、x-goog-api-key 等头不能泄露。
    # 修改方式：只返回 Content-Type、Authorization、User-Agent 和 Connection: close 四个应用层请求头。
    # 目的：让后续响应 adapter 即使使用共享 httpx client，也能从源头固定渠道级 header 集合。
    headers = {
        "Content-Type": "application/json",
        "User-Agent": _antigravity_user_agent(version),
        "Connection": "close",
    }
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    return headers


def _build_antigravity_url_from_base(base_url: str, action: str) -> str:
    """基于 base_url 拼接 Antigravity v1internal URL。"""
    # 修改原因：用户可能配置根域、带路径反代、完整 v1internal endpoint 或精确 URL。
    # 修改方式：保留根路径并剥掉已有 v1internal action，再拼接目标 action；流式请求补 alt=sse。
    # 目的：让默认官方地址和自定义反代地址都能稳定命中 Cloud Code API。
    base_url = str(base_url or DEFAULT_BASE_URL).strip()
    if base_url.endswith("#"):
        return base_url[:-1].rstrip("/")

    parts = urlsplit(base_url.rstrip("/"))
    root_path = parts.path.rstrip("/")
    if f"/{CODE_ASSIST_VERSION}:" in root_path:
        root_path = root_path.split(f"/{CODE_ASSIST_VERSION}:", 1)[0]
    elif root_path.endswith(f"/{CODE_ASSIST_VERSION}"):
        root_path = root_path[: -len(f"/{CODE_ASSIST_VERSION}")]
    endpoint_path = f"{root_path}/{CODE_ASSIST_VERSION}:{action}" if root_path else f"/{CODE_ASSIST_VERSION}:{action}"

    query_items = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key.lower() != "key"]
    if action == STREAM_ACTION and not any(key.lower() == "alt" for key, _ in query_items):
        query_items.append(("alt", "sse"))
    return urlunsplit((parts.scheme, parts.netloc, endpoint_path, urlencode(query_items), parts.fragment))


def _build_antigravity_url(provider: dict | None, action: str) -> str:
    """按 provider 配置构建 Antigravity URL。"""
    provider_copy = provider if isinstance(provider, dict) else {}
    base_url = str(provider_copy.get("base_url") or DEFAULT_BASE_URL).strip()
    return _build_antigravity_url_from_base(base_url, action)


def _resolve_original_model(request: Any, provider: dict | None) -> str:
    """按 Zoaholic 模型映射解析上游模型名。"""
    # 修改原因：Antigravity API 的 model 位于 JSON 顶层，不在 Gemini /models URL 中。
    # 修改方式：复用 get_model_dict，把请求模型别名还原为真实上游模型名。
    # 目的：确保模型前缀和别名配置在 Antigravity 渠道中继续生效。
    provider_copy = provider or {}
    requested_model = getattr(request, "model", "")
    model_dict = get_model_dict(provider_copy)
    return model_dict.get(requested_model, requested_model)


def _first_text_from_content(content: Any) -> str:
    """从 OpenAI message content 中提取文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text" and item.get("text"):
                    parts.append(str(item.get("text")))
            elif getattr(item, "type", None) == "text" and getattr(item, "text", None):
                parts.append(str(getattr(item, "text")))
        return "".join(parts)
    return ""


def _first_user_message_text(request: Any) -> str:
    """提取第一条 user message 文本，用于稳定 sessionId。"""
    # 修改原因：Antigravity sessionId 由首条用户消息派生，需要跨重试保持稳定。
    # 修改方式：优先扫描 role=user 的第一条文本，找不到时退回第一条有文本的消息。
    # 目的：同一会话请求在上游侧获得一致 sessionId，而不是每次随机。
    messages = getattr(request, "messages", None) or []
    fallback = ""
    for msg in messages:
        text = _first_text_from_content(getattr(msg, "content", None) if not isinstance(msg, dict) else msg.get("content"))
        role = getattr(msg, "role", None) if not isinstance(msg, dict) else msg.get("role")
        if text and not fallback:
            fallback = text
        if role == "user" and text:
            return text
    prompt = getattr(request, "prompt", None)
    if isinstance(prompt, str) and prompt:
        return prompt
    return fallback or "No messages"


def _build_session_id(text: str) -> str:
    """按 Antigravity 规则从首条用户文本生成 -{int64} sessionId。"""
    # 修改原因：真实 Antigravity 会为 request.request.sessionId 生成稳定负数 ID，不能用随机值。
    # 修改方式：取 sha256 前 8 字节，裁剪到 int64 正数范围后加负号前缀。
    # 目的：同一首条用户消息在多次请求中得到相同 sessionId，减少上游异常指纹。
    digest = hashlib.sha256(str(text or "").encode("utf-8")).digest()
    value = int.from_bytes(digest[:8], "big", signed=False) & ((1 << 63) - 1)
    return f"-{value}"


def _random_project_id() -> str:
    """生成 Antigravity fallback project_id。"""
    # 修改原因：loadCodeAssist 失败或手动导入旧凭据时可能没有真实 project_id，但上游请求体仍要求 project。
    # 修改方式：按 CPA 提取的 adj-noun-5char 形态生成随机项目名。
    # 目的：保证请求体结构完整，同时避免使用明显的占位字符串。
    return f"{random.choice(_PROJECT_ADJECTIVES)}-{random.choice(_PROJECT_NOUNS)}-{uuid4().hex[:5]}"


def _extract_context_project_id() -> str:
    """从当前请求上下文中的 OAuth metadata 读取 project_id。"""
    # 修改原因：OAuthManager.resolve 只把 access_token 传给 adapter，project_id 通过 request_info 的安全 metadata 传递。
    # 修改方式：尝试读取 _oauth_credential_metadata 中的 project_id/project 字段，失败时返回空字符串。
    # 目的：让请求体优先使用登录时 loadCodeAssist 获得的真实 Cloud Code project。
    try:
        from core.middleware import request_info

        current_info = request_info.get()
    except Exception:
        return ""
    if not isinstance(current_info, dict):
        return ""
    metadata = current_info.get("_oauth_credential_metadata")
    if not isinstance(metadata, dict):
        return ""
    project = metadata.get("project_id") or metadata.get("project") or metadata.get("cloudaicompanionProject")
    return str(project).strip() if project else ""


def _extract_antigravity_project_id(provider: dict | None, include_context: bool = True) -> str:
    """从 OAuth metadata 或 provider 配置中读取 project_id。"""
    if include_context:
        context_project = _extract_context_project_id()
        if context_project:
            return context_project
    if not isinstance(provider, dict):
        return ""
    preferences = provider.get("preferences") if isinstance(provider.get("preferences"), dict) else {}
    project = (
        provider.get("project_id")
        or provider.get("project")
        or provider.get("cloudaicompanionProject")
        or preferences.get("project_id")
        or preferences.get("project")
        or preferences.get("cloudaicompanionProject")
    )
    return str(project).strip() if project else ""


def _default_tier_id_from_load_code_assist(payload: dict | None) -> str:
    """从 loadCodeAssist 响应中提取 default tier ID，用于 onboardUser。"""
    if not isinstance(payload, dict):
        return "free-tier"
    allowed_tiers = payload.get("allowedTiers")
    if isinstance(allowed_tiers, list):
        for tier in allowed_tiers:
            if isinstance(tier, dict) and tier.get("isDefault") is True:
                tid = str(tier.get("id", "")).strip()
                if tid:
                    return tid
    current_tier = payload.get("currentTier")
    if isinstance(current_tier, dict):
        tid = str(current_tier.get("id", "")).strip()
        if tid:
            return tid
    return "free-tier"


def _extract_project_from_load_code_assist(payload: dict | None) -> str:
    """从 loadCodeAssist 响应中提取 cloudaicompanionProject。"""
    if not isinstance(payload, dict):
        return ""
    project = payload.get("cloudaicompanionProject")
    if not project and isinstance(payload.get("metadata"), dict):
        project = payload["metadata"].get("cloudaicompanionProject")
    return str(project).strip() if project else ""


def _extract_quota_raw_from_load_code_assist(payload: dict | None) -> dict:
    """从 loadCodeAssist 响应中提取 credits 原始字段。"""
    if not isinstance(payload, dict):
        return {}
    raw: dict[str, Any] = {}
    paid_tier = payload.get("paidTier")
    if isinstance(paid_tier, dict):
        raw["paidTier"] = paid_tier
        if paid_tier.get("availableCredits") is not None:
            raw["availableCredits"] = paid_tier.get("availableCredits")
    project = _extract_project_from_load_code_assist(payload)
    if project:
        raw["cloudaicompanionProject"] = project
    return raw


def _coerce_remaining_fraction(value: Any) -> float | None:
    """把 remainingFraction 清洗为 0 到 1 之间的浮点数。"""
    # 修改原因：fetchAvailableModels 的 remainingFraction 可能来自 JSON 数字，也可能经过反代或缓存后变成字符串。
    # 修改方式：统一尝试转 float，非法值返回 None，合法值裁剪到 0..1。
    # 目的：后端兼容 quota_inner/quota_outer 计算，前端 QUOTA_UI 也能收到稳定 number。
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, number))


def _classify_antigravity_quota_provider(model_quota: dict[str, Any]) -> str | None:
    """把 Antigravity modelProvider 归类为 gemini 或 external。"""
    # 修改原因：UI 上弧和下弧不再表示 5h/7d 时间窗，而是 Gemini 与 Claude/GPT 两类 provider 的剩余额度。
    # 修改方式：优先识别官方 MODEL_PROVIDER_*，并兼容旧缓存中的 google/gemini/anthropic/openai 字符串和模型名前缀。
    # 目的：让真实响应、历史缓存和单元测试替身都能稳定得到同一套分组结果。
    provider = str(model_quota.get("modelProvider") or "").upper()
    model = str(model_quota.get("model") or "").lower()
    if model.startswith(("tab_", "chat_")):
        return None
    if provider == "MODEL_PROVIDER_GOOGLE" or "GOOGLE" in provider or "GEMINI" in provider or model.startswith("gemini-"):
        return "gemini"
    if (
        provider == "MODEL_PROVIDER_ANTHROPIC"
        or provider == "MODEL_PROVIDER_OPENAI"
        or "ANTHROPIC" in provider
        or "OPENAI" in provider
        or model.startswith("claude-")
        or model.startswith("gpt-")
    ):
        return "external"
    return None


def _compute_antigravity_provider_quota_percentages(model_quotas: Any) -> dict[str, float]:
    """按 Gemini 与外部模型 provider 计算 Antigravity 上下弧百分比。"""
    # 修改原因：旧实现把所有模型 remainingFraction 取一个最低值，无法让前端复用 QuotaBorderOverlay 分别绘制 Gemini 和 External。
    # 修改方式：过滤 tab_*、chat_* 后按 provider 归类，Gemini 最低值写入 quota_inner，Claude/GPT 最低值写入 quota_outer。
    # 目的：balance 接口、OAuth 缓存和前端标签使用一致的数据语义。
    if not isinstance(model_quotas, list):
        return {}
    grouped: dict[str, list[float]] = {"gemini": [], "external": []}
    for model_quota in model_quotas:
        if not isinstance(model_quota, dict):
            continue
        provider_kind = _classify_antigravity_quota_provider(model_quota)
        if provider_kind not in grouped:
            continue
        fraction = _coerce_remaining_fraction(model_quota.get("remainingFraction"))
        if fraction is None:
            continue
        grouped[provider_kind].append(fraction)
    result: dict[str, float] = {}
    if grouped["gemini"]:
        result["quota_inner"] = min(grouped["gemini"]) * 100.0
    if grouped["external"]:
        result["quota_outer"] = min(grouped["external"]) * 100.0
    return result


def _model_identifier_from_info(model_info: dict, fallback: str = "") -> str:
    """从 fetchAvailableModels 的单个模型对象中提取模型 ID。"""
    # 修改原因：上游返回形态可能是 {models:{id:info}}，也可能是 {models:[{name/id/model...}]}。
    # 修改方式：优先使用 fallback key，再读取常见模型标识字段并去空白。
    # 目的：同一套解析同时服务 quota_raw.modelQuotas 和 fetch_models 列表。
    candidates = [
        fallback,
        model_info.get("name"),
        model_info.get("id"),
        model_info.get("model"),
        model_info.get("modelId"),
        model_info.get("model_id"),
    ]
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text:
            return text
    return ""


def _iter_available_model_items(payload: Any) -> list[tuple[str, dict[str, Any]]]:
    """把 fetchAvailableModels 的 dict/list 形态归一为 (model_id, model_info) 列表。"""
    # 修改原因：当前实现只处理 models 字典，真实接口或反代可能返回 models 数组，导致 modelQuotas 和模型列表断裂。
    # 修改方式：支持 payload.models、payload.availableModels、payload.modelQuotas 和顶层 dict/list，并为 list 项读取 name/id 字段。
    # 目的：让后端 quota 和 fetch_models 不再依赖单一上游 JSON 形态。
    if isinstance(payload, dict):
        raw_models = (
            payload.get("models")
            if payload.get("models") is not None
            else payload.get("availableModels")
            if payload.get("availableModels") is not None
            else payload.get("modelQuotas")
            if payload.get("modelQuotas") is not None
            else payload
        )
    else:
        raw_models = payload

    items: list[tuple[str, dict[str, Any]]] = []
    if isinstance(raw_models, dict):
        for model_id, model_info in raw_models.items():
            if not isinstance(model_info, dict):
                continue
            resolved_id = _model_identifier_from_info(model_info, str(model_id))
            if resolved_id:
                items.append((resolved_id, model_info))
    elif isinstance(raw_models, list):
        for model_info in raw_models:
            if not isinstance(model_info, dict):
                continue
            resolved_id = _model_identifier_from_info(model_info)
            if resolved_id:
                items.append((resolved_id, model_info))
    return items


def _extract_model_quotas_from_available_models(payload: Any) -> list[dict[str, Any]]:
    """从 fetchAvailableModels 响应中提取前端需要的 modelQuotas。"""
    # 修改原因：QUOTA_UI 只读取 raw.modelQuotas，后端必须在各种上游形态下稳定生成这个数组。
    # 修改方式：复用 _iter_available_model_items，并兼容 quotaInfo 内层或顶层 remainingFraction 字段。
    # 目的：OAuthManager.update_quota 落盘后，/v1/oauth/accounts 可以直接给前端渲染完整额度。
    model_quotas: list[dict[str, Any]] = []
    for model_id, model_info in _iter_available_model_items(payload):
        quota_info = model_info.get("quotaInfo") if isinstance(model_info.get("quotaInfo"), dict) else model_info
        remaining_fraction = _coerce_remaining_fraction(quota_info.get("remainingFraction"))
        if remaining_fraction is None:
            continue
        model_quotas.append({
            "model": model_id,
            "displayName": model_info.get("displayName") or model_info.get("display_name") or model_id,
            "modelProvider": model_info.get("modelProvider") or model_info.get("provider") or "unknown",
            "remainingFraction": remaining_fraction,
            "resetTime": quota_info.get("resetTime"),
            "isExhausted": bool(quota_info.get("isExhausted", remaining_fraction <= 0)),
        })
    return model_quotas


def _extract_model_names_from_available_models(payload: Any) -> list[str]:
    """从 fetchAvailableModels 响应中提取模型名列表。"""
    # 修改原因：管理端“获取模型”和 quota 查询使用同一个上游接口，不能一个支持 list 一个不支持。
    # 修改方式：复用归一化后的模型项，按出现顺序去重。
    # 目的：让 fetch_antigravity_models 签名保持 (client, provider) 的同时稳定返回 List[str]。
    names: list[str] = []
    seen: set[str] = set()
    for model_id, _model_info in _iter_available_model_items(payload):
        if model_id in seen:
            continue
        seen.add(model_id)
        names.append(model_id)
    return names


def _normalize_tool_config(tool_config: Any) -> dict | None:
    """把 Gemini snake_case tool_config 转为 Antigravity camelCase toolConfig。"""
    # 修改原因：Zoaholic 的 Gemini adapter 为兼容 AI Studio 会输出 tool_config/function_calling_config，Antigravity 需要 camelCase。
    # 修改方式：只转换 toolConfig 已知键，不递归改动函数参数 JSON Schema 中的用户字段名。
    # 目的：避免工具配置字段名错误，同时不破坏工具参数属性名称。
    if not isinstance(tool_config, dict):
        return None
    function_calling = tool_config.get("functionCallingConfig") or tool_config.get("function_calling_config")
    result: dict[str, Any] = {}
    if isinstance(function_calling, dict):
        fc: dict[str, Any] = {}
        if function_calling.get("mode") is not None:
            fc["mode"] = function_calling.get("mode")
        allowed = function_calling.get("allowedFunctionNames") or function_calling.get("allowed_function_names")
        if allowed is not None:
            fc["allowedFunctionNames"] = allowed
        result["functionCallingConfig"] = fc
    return result or None


def _normalize_tools(tools: Any) -> Any:
    """把 Gemini tools 中的 function_declarations 转为 functionDeclarations。"""
    # 修改原因：Antigravity 请求体采用 camelCase Gemini JSON 形态，不能把 snake_case 工具声明直接发给上游。
    # 修改方式：只处理工具层的 function_declarations 键，函数声明内部原样保留。
    # 目的：兼容 Gemini adapter 的输出，同时保护 JSON Schema 属性名不被误转换。
    if not isinstance(tools, list):
        return tools
    normalized = []
    for tool in tools:
        if not isinstance(tool, dict):
            normalized.append(tool)
            continue
        item = dict(tool)
        declarations = item.pop("function_declarations", None)
        if declarations is not None and "functionDeclarations" not in item:
            item["functionDeclarations"] = declarations
        normalized.append(item)
    return normalized


def _normalize_antigravity_request_payload(payload: dict) -> dict:
    """把 Gemini payload 调整为 Antigravity request 子对象。"""
    # 修改原因：普通 Gemini adapter 会混入 URL 模型、snake_case tool_config 等字段，Antigravity 外层已单独携带 model。
    # 修改方式：深拷贝后移除 model，并把 system/toolConfig 等关键字段规范为 camelCase。
    # 目的：让请求体与 CPA 的 geminiToAntigravity 结构保持一致。
    request_payload = copy.deepcopy(payload) if isinstance(payload, dict) else {}
    request_payload.pop("model", None)

    if "system_instruction" in request_payload and "systemInstruction" not in request_payload:
        request_payload["systemInstruction"] = request_payload.pop("system_instruction")

    raw_tool_config = request_payload.pop("tool_config", None)
    if raw_tool_config is None:
        raw_tool_config = request_payload.get("toolConfig")
    normalized_tool_config = _normalize_tool_config(raw_tool_config)
    if normalized_tool_config:
        request_payload["toolConfig"] = normalized_tool_config

    if "tools" in request_payload:
        request_payload["tools"] = _normalize_tools(request_payload.get("tools"))

    return request_payload


def _apply_claude_validated_tool_config(request_payload: dict, model: str) -> None:
    """Claude 模型强制使用 VALIDATED 工具调用模式。"""
    # 修改原因：CPA 提取显示 Antigravity 在 Claude 模型上需要 functionCallingConfig.mode=VALIDATED。
    # 修改方式：无论原 Gemini adapter 输出 AUTO/ANY/NONE，都覆盖为 VALIDATED，并保留 allowedFunctionNames 等其他字段。
    # 目的：避免 Claude 工具调用协议不匹配导致上游拒绝或行为异常。
    if "claude" not in str(model or "").lower():
        return
    tool_config = request_payload.get("toolConfig")
    if not isinstance(tool_config, dict):
        tool_config = {}
    function_calling = tool_config.get("functionCallingConfig")
    if not isinstance(function_calling, dict):
        function_calling = {}
    function_calling["mode"] = "VALIDATED"
    tool_config["functionCallingConfig"] = function_calling
    request_payload["toolConfig"] = tool_config


def _extract_enabled_credit_types(provider: dict | None) -> list[str]:
    """从 provider 配置读取 Antigravity enabledCreditTypes。"""
    # 修改原因：Antigravity 支持在请求体注入 GOOGLE_ONE_AI credits，但不应无条件改变用户额度来源。
    # 修改方式：兼容顶层和 preferences 中的 enabledCreditTypes/enabled_credit_types/use_paid_credits 配置。
    # 目的：需要时可以显式启用付费 credits，默认保持官方请求的最小字段集合。
    if not isinstance(provider, dict):
        return []
    preferences = provider.get("preferences") if isinstance(provider.get("preferences"), dict) else {}
    value = (
        provider.get("enabledCreditTypes")
        or provider.get("enabled_credit_types")
        or preferences.get("enabledCreditTypes")
        or preferences.get("enabled_credit_types")
    )
    if value is None and (provider.get("use_paid_credits") or preferences.get("use_paid_credits")):
        value = ["GOOGLE_ONE_AI"]
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if item]
    return []


def _build_request_id(is_image: bool) -> str:
    """生成 Antigravity requestId。"""
    if is_image:
        return f"image_gen/{int(time.time() * 1000)}/{uuid4()}/12"
    return f"agent-{uuid4()}"


# gemini-3.1-pro-high 上游已不接受作为模型名，需拆为 base + thinkingLevel
_THINKING_SUFFIX_MAP = {
    "high": ("low", "high"),   # -high → 发 -low，注入 thinkingLevel=high
    "medium": ("low", "medium"),
}
# 模型前缀白名单：只对这些前缀做后缀拆分
_THINKING_SUFFIX_MODEL_PREFIXES = ("gemini-3.1-pro-", "gemini-3-pro-")

_GEMINI_REASONING_PREFIXES = (
    "gemini-3.1-pro-high", "gemini-3-pro-high", "gemini-3-pro-preview",
    "gemini-2.5-flash-thinking",
)


def _is_gemini_reasoning_model(model: str) -> bool:
    lower = str(model or "").lower()
    return any(lower.startswith(p) for p in _GEMINI_REASONING_PREFIXES)


def _rewrite_thinking_suffix(model: str) -> tuple[str, str | None]:
    """拆分 -high/-medium 后缀：返回 (upstream_model, thinkingLevel or None)。"""
    lower = str(model or "").lower()
    for prefix in _THINKING_SUFFIX_MODEL_PREFIXES:
        if lower.startswith(prefix):
            suffix = lower[len(prefix):]
            if suffix in _THINKING_SUFFIX_MAP:
                base_suffix, level = _THINKING_SUFFIX_MAP[suffix]
                return prefix + base_suffix, level
    return model, None


def _sanitize_reasoning_model_payload(request_payload: dict, model: str, thinking_level: str | None) -> None:
    """reasoning 模型：移除不允许的参数，注入 thinkingLevel。"""
    is_reasoning = _is_gemini_reasoning_model(model)
    if not is_reasoning and not thinking_level:
        return
    gen_config = request_payload.get("generationConfig")
    if isinstance(gen_config, dict):
        if is_reasoning:
            for forbidden in ("temperature", "topP", "top_p", "topK", "top_k", "stopSequences", "stop_sequences"):
                gen_config.pop(forbidden, None)
        if thinking_level:
            thinking_config = gen_config.setdefault("thinkingConfig", {})
            thinking_config["thinkingLevel"] = thinking_level
            thinking_config.setdefault("includeThoughts", True)
    elif thinking_level:
        request_payload["generationConfig"] = {"thinkingConfig": {"thinkingLevel": thinking_level, "includeThoughts": True}}
    if is_reasoning and "toolConfig" in request_payload:
        tools = request_payload.get("tools")
        if not tools or (isinstance(tools, list) and len(tools) == 0):
            request_payload.pop("toolConfig", None)


def _build_antigravity_payload(gemini_payload: dict, original_model: str, provider: dict | None, request: Any) -> dict:
    """把 Gemini payload 包成 Antigravity v1internal payload。"""
    upstream_model, thinking_level = _rewrite_thinking_suffix(original_model)
    is_image = _is_image_model(original_model)
    request_payload = _normalize_antigravity_request_payload(gemini_payload)
    request_payload["sessionId"] = _build_session_id(_first_user_message_text(request))
    _apply_claude_validated_tool_config(request_payload, upstream_model)
    _sanitize_reasoning_model_payload(request_payload, upstream_model, thinking_level)

    project = _extract_antigravity_project_id(provider) or _random_project_id()
    body = {
        "model": upstream_model,
        "userAgent": "antigravity",
        "requestType": "image_gen" if is_image else "agent",
        "project": project,
        "requestId": _build_request_id(is_image),
        "request": request_payload,
    }
    credit_types = _extract_enabled_credit_types(provider)
    if credit_types:
        body["enabledCreditTypes"] = credit_types
    return body


async def get_antigravity_payload(request, engine, provider, api_key=None):
    """构建 Antigravity 请求 URL、白名单 headers 和包裹后的 payload。"""
    # 修改原因：Antigravity 内容格式接近 Gemini，但认证、URL、User-Agent、请求头和外层 body 完全不同。
    # 修改方式：先用 Gemini adapter 完成 OpenAI 消息与工具转换，再丢弃 Gemini URL/headers，重建 Antigravity 指纹。
    # 目的：复用成熟的 Gemini 转换逻辑，同时满足 Antigravity Cloud Code API 的精确伪装要求。
    provider_copy = dict(provider or {})
    gemini_provider = dict(provider_copy)
    gemini_provider["base_url"] = "https://generativelanguage.googleapis.com/v1beta"
    _, _gemini_headers, gemini_payload = await get_gemini_payload(request, "gemini", gemini_provider, api_key)

    original_model = _resolve_original_model(request, provider_copy)
    action = STREAM_ACTION if getattr(request, "stream", False) else GENERATE_ACTION
    version = await get_antigravity_version()
    url = _build_antigravity_url(provider_copy, action)
    headers = _build_generation_headers(api_key, version)
    payload = _build_antigravity_payload(gemini_payload, original_model, provider_copy, request)
    return url, headers, payload


async def get_antigravity_passthrough_meta(request, engine, provider, api_key=None):
    """透传模式下只构建 Antigravity URL 和白名单请求头。"""
    # 修改原因：Gemini 方言透传时 payload 已经是入口原生 Gemini body，不能再运行 OpenAI 到 Gemini 转换。
    # 修改方式：这里只返回 Antigravity v1internal URL 和认证头，实际包裹交给 passthrough_payload_adapter。
    # 目的：让透传路径也能使用同一套 Antigravity OAuth 和请求伪装。
    provider_copy = dict(provider or {})
    action = STREAM_ACTION if getattr(request, "stream", False) else GENERATE_ACTION
    version = await get_antigravity_version()
    return _build_antigravity_url(provider_copy, action), _build_generation_headers(api_key, version), {}


async def patch_antigravity_passthrough_payload(payload: dict, modifications: dict, request, engine: str, provider: dict, api_key=None) -> dict:
    """把 Gemini 原生透传 payload 包成 Antigravity payload。"""
    # 修改原因：透传入口保留普通 Gemini body，而 Cloud Code API 仍要求 Antigravity 外层字段。
    # 修改方式：按模型映射、project 和 sessionId 调用同一个 _build_antigravity_payload。
    # 目的：保持普通请求与透传请求的上游协议一致。
    original_model = _resolve_original_model(request, provider)
    return _build_antigravity_payload(payload if isinstance(payload, dict) else {}, original_model, provider, request)


# ═══════════════════════════════════════════════════════════════════
# HTTP/1.1 请求头清理与响应解包
# ═══════════════════════════════════════════════════════════════════


def _strip_httpx_generated_headers(headers) -> None:
    """移除 httpx 默认合并进来的非 Antigravity 头。"""
    # 修改原因：ClientManager 默认带 Accept、Accept-Encoding 等通用头，Antigravity 要求生成请求头白名单。
    # 修改方式：在 build_request 之后、send 之前删除白名单外应用层头，仅保留 Host/Content-Length 等传输必需头。
    # 目的：既能复用代理和连接管理，又尽量贴近真实 Antigravity 的请求头集合。
    for key in list(headers.keys()):
        lowered = str(key).lower()
        if lowered not in _ALLOWED_GENERATION_HEADERS and lowered not in _TRANSPORT_HEADERS:
            del headers[key]


class _AntigravityHTTP11Client:
    """代理 httpx.AsyncClient，按请求级清理 headers 并发送 Connection: close。"""

    def __init__(self, client):
        # 修改原因：响应 adapter 收到的是共享 client，不能直接修改 client.headers 影响其他并发请求。
        # 修改方式：包装 post/stream，在每次 build_request 后清理该请求自己的 headers。
        # 目的：实现 Antigravity 请求头白名单，同时保留共享 client 的代理配置和连接池。
        self._client = client

    def __getattr__(self, name):
        return getattr(self._client, name)

    async def post(self, url, headers=None, content=None, timeout=None, **kwargs):
        request = self._client.build_request("POST", url, headers=headers, content=content, timeout=timeout, **kwargs)
        _strip_httpx_generated_headers(request.headers)
        return await self._client.send(request, stream=False)

    def stream(self, method, url, headers=None, content=None, timeout=None, **kwargs):
        return _AntigravityHTTP11StreamContext(self._client, method, url, headers, content, timeout, kwargs)


class _AntigravityHTTP11StreamContext:
    """用 build_request/send(stream=True) 实现可清理 header 的 stream context。"""

    def __init__(self, client, method, url, headers, content, timeout, kwargs):
        self._client = client
        self._method = method
        self._url = url
        self._headers = headers
        self._content = content
        self._timeout = timeout
        self._kwargs = kwargs
        self._response = None

    async def __aenter__(self):
        request = self._client.build_request(
            self._method,
            self._url,
            headers=self._headers,
            content=self._content,
            timeout=self._timeout,
            **self._kwargs,
        )
        _strip_httpx_generated_headers(request.headers)
        self._response = await self._client.send(request, stream=True)
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        if self._response is not None:
            await self._response.aclose()
        return False


def _unwrap_antigravity_response_payload(payload: Any) -> Any:
    """去掉 Cloud Code API 响应中的 response 外壳。"""
    # 修改原因：Antigravity 响应形如 {response:{...}, traceId:"..."}，Gemini parser 只识别 candidates 顶层。
    # 修改方式：dict 命中 response 时返回内层；list 则逐项处理。
    # 目的：复用现有 Gemini 到 OpenAI 响应转换，不复制复杂的工具、图片和 usage 解析逻辑。
    if isinstance(payload, dict) and isinstance(payload.get("response"), dict):
        return payload.get("response")
    if isinstance(payload, list):
        changed = False
        items = []
        for item in payload:
            if isinstance(item, dict) and isinstance(item.get("response"), dict):
                changed = True
                items.append(item.get("response"))
            else:
                items.append(item)
        return items if changed else payload
    return payload


def _unwrap_antigravity_response_bytes(response_bytes: bytes) -> bytes:
    """把 Antigravity 非流式响应字节转换为普通 Gemini 响应字节。"""
    try:
        payload = json_loads(response_bytes)
    except Exception:
        return response_bytes
    return json_dumps_text(_unwrap_antigravity_response_payload(payload)).encode("utf-8")


def _parse_antigravity_stream_json_line(line: str) -> dict | None:
    """解析 Antigravity JSON Lines 或兼容的 data: 行，并返回内层 Gemini chunk。"""
    # 修改原因：Antigravity streamGenerateContent 返回 JSON Lines，不是标准 SSE；部分反代又可能补 data: 前缀。
    # 修改方式：同时兼容裸 JSON 行和 data: JSON 行，解析后去掉 response 外壳。
    # 目的：让思考内容、正文、工具调用和 usage 都能继续交给 Gemini 流式解析器处理。
    stripped = str(line or "").strip()
    if not stripped or stripped == "[DONE]":
        return None
    if stripped.startswith("data:"):
        stripped = stripped[5:].strip()
    if not stripped or stripped == "[DONE]":
        return None
    try:
        payload = json_loads(stripped)
    except Exception:
        return None
    unwrapped = _unwrap_antigravity_response_payload(payload)
    return unwrapped if isinstance(unwrapped, dict) else None


class _AntigravityResponseUnwrapClient:
    """包装 httpx client，在复用 Gemini parser 前解包 Antigravity 响应。"""

    def __init__(self, client):
        # 修改原因：Antigravity 成功响应需要去掉 response 外壳，错误响应必须保留原始体给 check_response。
        # 修改方式：成功 post/stream 包装响应体，非 2xx 原样返回。
        # 目的：让错误处理仍能看到上游真实错误，同时成功路径复用 Gemini adapter。
        self._client = client

    def __getattr__(self, name):
        return getattr(self._client, name)

    async def post(self, *args, **kwargs):
        response = await self._client.post(*args, **kwargs)
        if not (200 <= response.status_code < 300):
            return response
        content = _unwrap_antigravity_response_bytes(await response.aread())
        response_kwargs = {
            "status_code": response.status_code,
            "headers": response.headers,
            "content": content,
            "extensions": getattr(response, "extensions", None) or {},
        }
        request = getattr(response, "request", None)
        if request is not None:
            response_kwargs["request"] = request
        return httpx.Response(**response_kwargs)

    def stream(self, *args, **kwargs):
        return _AntigravityStreamContext(self._client.stream(*args, **kwargs))


class _AntigravityStreamContext:
    """包装 Antigravity 流式响应，成功时返回可解包 response。"""

    def __init__(self, context_manager):
        self._context_manager = context_manager

    async def __aenter__(self):
        response = await self._context_manager.__aenter__()
        if not (200 <= response.status_code < 300):
            return response
        return _AntigravityStreamResponse(response)

    async def __aexit__(self, exc_type, exc, tb):
        return await self._context_manager.__aexit__(exc_type, exc, tb)


class _AntigravityStreamResponse:
    """成功流式响应代理，把 JSON Lines 转为 Gemini SSE data 行。"""

    def __init__(self, response):
        self._response = response
        self.status_code = response.status_code
        self.headers = response.headers

    async def aread(self):
        return _unwrap_antigravity_response_bytes(await self._response.aread())

    async def aiter_bytes(self):
        async for line in aiter_decoded_lines(self._response.aiter_bytes()):
            parsed = _parse_antigravity_stream_json_line(line)
            if parsed is None:
                continue
            yield f"data: {json_dumps_text(parsed)}\n\n".encode("utf-8")

    async def aiter_text(self):
        async for chunk in self.aiter_bytes():
            yield chunk.decode("utf-8", errors="replace")


async def fetch_antigravity_passthrough_stream(client, url, headers, payload, model, timeout):
    """透传流式 Antigravity 响应适配器。"""
    # 修改原因：Gemini 方言透传会绕过普通响应 adapter，导致共享 client 的默认头和 Antigravity response 外壳泄露给客户端。
    # 修改方式：发送前使用 _AntigravityHTTP11Client 清理请求头，成功响应逐行去掉 response 外壳并补成标准 SSE data 行。
    # 目的：让 Gemini 原生客户端在透传模式下也看到普通 Gemini streamGenerateContent 形态，而不是 Cloud Code 外壳。
    from core.response import _log_upstream_request, check_response

    _log_upstream_request(url, payload)
    wrapped_client = _AntigravityHTTP11Client(client)
    stream_timeout = httpx.Timeout(
        connect=15.0,
        read=None,
        write=300.0,
        pool=10.0,
    )
    json_payload = await asyncio.to_thread(json_dumps_text, payload)
    async with wrapped_client.stream("POST", url, headers=headers, content=json_payload, timeout=stream_timeout) as response:
        error_message = await check_response(response, "fetch_antigravity_passthrough_stream")
        if error_message:
            yield error_message
            return

        async for line in aiter_decoded_lines(response.aiter_bytes()):
            parsed = _parse_antigravity_stream_json_line(line)
            if parsed is None:
                continue
            yield f"data: {json_dumps_text(parsed)}\n\n"


async def fetch_antigravity_passthrough_response(client, url, headers, payload, model, timeout):
    """透传非流式 Antigravity 响应适配器。"""
    # 修改原因：非流式透传默认会把 {response:{...}, traceId:"..."} 原样返回，Gemini 方言客户端无法按 candidates 顶层解析。
    # 修改方式：使用同一套 HTTP/1.1/header 清理 wrapper 发送请求，成功响应只解包 response 字段，非 2xx 保持 check_response 原始错误处理。
    # 目的：让透传非流式输出恢复为普通 Gemini generateContent JSON，同时不改变上游错误体的可观测性。
    from core.response import _log_upstream_request, check_response

    _log_upstream_request(url, payload)
    wrapped_client = _AntigravityHTTP11Client(client)
    request_timeout = httpx.Timeout(
        connect=15.0,
        read=timeout,
        write=300.0,
        pool=10.0,
    )
    json_payload = await asyncio.to_thread(json_dumps_text, payload)
    response = await wrapped_client.post(url, headers=headers, content=json_payload, timeout=request_timeout)
    error_message = await check_response(response, "fetch_antigravity_passthrough_response")
    if error_message:
        yield error_message
        return

    response_bytes = await response.aread()
    yield _unwrap_antigravity_response_bytes(response_bytes).decode("utf-8", errors="replace")


async def fetch_antigravity_response(client, url, headers, payload, model, timeout):
    """非流式 Antigravity 响应适配器。"""
    # 修改原因：Antigravity 非流式响应比 Gemini 多 response 外壳，同时请求必须清理 httpx 默认头。
    # 修改方式：先用 HTTP/1.1/header 清理 wrapper 发送，再用响应解包 wrapper 复用 Gemini parser。
    # 目的：输出标准 OpenAI chat completions，并保持上游请求指纹最小化。
    wrapped_client = _AntigravityResponseUnwrapClient(_AntigravityHTTP11Client(client))
    async for item in fetch_gemini_response(wrapped_client, url, headers, payload, model, timeout):
        yield item


async def fetch_antigravity_response_stream(client, url, headers, payload, model, timeout):
    """流式 Antigravity 响应适配器。"""
    # 修改原因：Antigravity 流式返回 JSON Lines 而不是 data: SSE，普通 Gemini parser 需要标准 data 行。
    # 修改方式：逐行解析 JSON、去掉 response 外壳、补成 data: Gemini chunk 后交给 Gemini 流式 parser。
    # 目的：保留思考内容 thought、thoughtSignature、usage 和工具调用的既有转换能力。
    wrapped_client = _AntigravityResponseUnwrapClient(_AntigravityHTTP11Client(client))
    async for item in fetch_gemini_response_stream(wrapped_client, url, headers, payload, model, timeout):
        yield item


# ═══════════════════════════════════════════════════════════════════
# 前端额度渲染脚本（内联 JS，由 UI 插槽机制动态加载）
# ═══════════════════════════════════════════════════════════════════

# 修改原因：fetchAvailableModels 返回各模型组独立 quota，需要按 modelProvider 分组展示 remainingFraction。
# 修改方式：内联 JS 按 provider 分组取最低百分比，显示总览标签 + 点击弹出详细 quota。
# 目的：key 行显示最低 quota，点击展开按模型组的额度列表。
QUOTA_UI = """
export default function render(ctx) {
    ctx = ctx || {};
    const { el, data } = ctx;
    if (!el) return;
    const mode = ctx.context?.mode || ctx.mode || 'row';
    const raw = data?.raw || {};
    // 修改原因：Antigravity quota_display 同时挂载到完整行和机房卡片，机房卡片不能再通过 DOM 宽度猜测布局。
    // 修改方式：脚本读取 ctx.context.mode；rack 模式只输出短百分比或 tier 缩写，row 模式保留完整标签和点击详情。
    // 目的：让上下边框复用现有组件，同时避免圆环中心显示长 tier 或 credits 金额而溢出。
    const modelQuotas = Array.isArray(raw?.modelQuotas) ? raw.modelQuotas : [];
    const credits = Array.isArray(raw?.availableCredits) ? raw.availableCredits : [];
    const paidTier = raw?.paidTier;
    const initialQuota5h = typeof data?.quota_inner === 'number' && Number.isFinite(data.quota_inner) ? data.quota_inner : undefined;
    const initialQuota7d = typeof data?.quota_outer === 'number' && Number.isFinite(data.quota_outer) ? data.quota_outer : undefined;

    const normalizeFraction = value => {
        if (value == null || value === '') return null;
        const n = Number(value);
        if (!Number.isFinite(n)) return null;
        return Math.max(0, Math.min(1, n));
    };
    const percentFromFraction = value => {
        const fraction = normalizeFraction(value);
        return fraction == null ? null : fraction * 100;
    };
    const isIgnoredModel = model => {
        const id = String(model || '').toLowerCase();
        return id.startsWith('tab_') || id.startsWith('chat_');
    };
    const providerKindOf = modelQuota => {
        const provider = String(modelQuota?.modelProvider || '').toUpperCase();
        const model = String(modelQuota?.model || '').toLowerCase();
        if (isIgnoredModel(model)) return null;
        if (provider === 'MODEL_PROVIDER_GOOGLE' || provider.includes('GOOGLE') || provider.includes('GEMINI') || model.startsWith('gemini-')) return 'gemini';
        if (provider === 'MODEL_PROVIDER_ANTHROPIC' || provider === 'MODEL_PROVIDER_OPENAI' || provider.includes('ANTHROPIC') || provider.includes('OPENAI') || model.startsWith('claude-') || model.startsWith('gpt-')) return 'external';
        return null;
    };
    const colorOf = pct => pct >= 50 ? 'bg-emerald-500/15 text-emerald-500' : pct >= 20 ? 'bg-amber-500/15 text-amber-600' : 'bg-red-500/15 text-red-500';
    const textColorOf = pct => pct >= 50 ? 'text-emerald-600' : pct >= 20 ? 'text-amber-600' : 'text-red-500';
    const strokeColorOf = pct => pct >= 50 ? '#10b981' : pct >= 20 ? '#f59e0b' : '#ef4444';
    const shortProviderName = providerKind => providerKind === 'external' ? 'Claude + GPT' : 'Gemini';
    const prettyModelName = model => {
        const base = String(model || 'quota')
            .replace(/-(low|high|thinking|agent|medium)$/g, '')
            .replace(/-/g, ' ')
            .replace(/\\b\\w/g, ch => ch.toUpperCase())
            .replace(/Gpt/g, 'GPT');
        return base || 'Quota';
    };
    const geminiSeriesName = model => {
        const id = String(model || '').toLowerCase();
        if (id.startsWith('gemini-3.1-pro')) return 'Gemini 3.1 Pro';
        if (id.startsWith('gemini-3.1-flash-lite') || id.startsWith('gemini-3.1-image')) return 'Gemini 3.1 Flash Lite / Image';
        if (id.startsWith('gemini-3-pro')) return 'Gemini 3 Pro';
        if (id.startsWith('gemini-3-flash') || id.includes('gemini-3') && id.endsWith('-agent')) return 'Gemini 3 Flash';
        if (id.startsWith('gemini-2.5-pro')) return 'Gemini 2.5 Pro';
        if (id.startsWith('gemini-2.5-flash')) return 'Gemini 2.5 Flash';
        return prettyModelName(model);
    };
    const groupLabelOf = modelQuota => {
        const providerKind = providerKindOf(modelQuota);
        if (providerKind === 'external') return 'Claude + GPT';
        if (providerKind === 'gemini') return geminiSeriesName(modelQuota?.model);
        return prettyModelName(modelQuota?.model) || shortProviderName(providerKind);
    };
    const formatResetDuration = resetTime => {
        if (!resetTime) return 'unknown';
        const target = new Date(resetTime).getTime();
        if (!Number.isFinite(target)) return 'unknown';
        const diffMs = Math.max(0, target - Date.now());
        const totalMinutes = Math.ceil(diffMs / 60000);
        if (totalMinutes <= 0) return 'now';
        const days = Math.floor(totalMinutes / 1440);
        const hours = Math.floor((totalMinutes % 1440) / 60);
        const minutes = totalMinutes % 60;
        if (days > 0) return `${days}d ${hours}h`;
        if (hours > 0) return `${hours}h ${minutes}m`;
        return `${minutes}m`;
    };
    const groupModels = modelQuotaList => {
        // 修改原因：CPA 的 quota 是 shared group，不应在点击气泡里逐模型铺开。
        // 修改方式：过滤 tab_* 和 chat_*，Gemini 按系列与 reset/fraction 分组，Claude/GPT 外部模型统一合并。
        // 目的：让气泡行数对应真实 quota 池，避免同一 shared group 被多个模型重复显示。
        const groups = {};
        for (const modelQuota of modelQuotaList) {
            const model = String(modelQuota?.model || '');
            if (isIgnoredModel(model)) continue;
            const providerKind = providerKindOf(modelQuota);
            if (providerKind !== 'gemini' && providerKind !== 'external') continue;
            const fraction = normalizeFraction(modelQuota?.remainingFraction);
            if (fraction == null) continue;
            const label = groupLabelOf(modelQuota);
            const resetTime = modelQuota?.resetTime || '';
            const key = providerKind === 'external'
                ? `external|${resetTime}|${fraction}`
                : `gemini|${label}|${resetTime}|${fraction}`;
            if (!groups[key]) {
                groups[key] = { label, providerKind, fraction, resetTime, models: [] };
            }
            groups[key].models.push(model || String(modelQuota?.displayName || label));
        }
        return Object.values(groups).sort((a, b) => {
            if (a.providerKind !== b.providerKind) return a.providerKind === 'gemini' ? -1 : 1;
            return a.label.localeCompare(b.label);
        });
    };

    const filteredQuotas = modelQuotas.filter(modelQuota => !isIgnoredModel(modelQuota?.model));
    const geminiPcts = filteredQuotas.filter(modelQuota => providerKindOf(modelQuota) === 'gemini').map(modelQuota => percentFromFraction(modelQuota?.remainingFraction)).filter(value => value != null);
    const externalPcts = filteredQuotas.filter(modelQuota => providerKindOf(modelQuota) === 'external').map(modelQuota => percentFromFraction(modelQuota?.remainingFraction)).filter(value => value != null);
    const geminiPct = geminiPcts.length ? Math.min(...geminiPcts) : undefined;
    const externalPct = externalPcts.length ? Math.min(...externalPcts) : undefined;
    if (geminiPct != null) data.quota_inner = geminiPct;
    if (externalPct != null) data.quota_outer = externalPct;

    const fallbackPcts = [geminiPct, externalPct, initialQuota5h, initialQuota7d].filter(value => typeof value === 'number' && Number.isFinite(value));
    const minPct = fallbackPcts.length ? Math.round(Math.min(...fallbackPcts)) : null;
    // 修改原因：Python 三引号字符串会解析反斜杠，JavaScript 正则中的 \\s 必须在源码中写成双反斜杠。
    // 修改方式：tier 清洗正则统一使用 \\s，运行到浏览器后仍按空白字符匹配。
    // 目的：消除 py_compile 的无效转义告警，并保持 tier 名称清洗行为不变。
    const tierName = paidTier?.name
        ? String(paidTier.name).replace(/Google\\s*(AI\\s*)?/i, '').replace(/Gemini Code Assist in /i, '').trim()
        : '';
    const shortTierName = value => {
        const cleaned = String(value || '').replace(/Google One /i, '').replace(/Google\\s*(AI\\s*)?/i, '').replace(/Gemini Code Assist in /i, '').trim();
        if (!cleaned) return '';
        const mapped = { 'Standard': 'Std', 'Enterprise': 'Ent', 'Professional': 'Pro' }[cleaned];
        return mapped || (cleaned.length > 6 ? cleaned.slice(0, 6) : cleaned);
    };
    if (mode === 'rack') {
        // 修改原因：机房卡片圆环中心空间很小，Antigravity 的 tier 和 credits 金额都可能溢出。
        // 修改方式：rack 模式优先只显示最低百分比；没有百分比时才显示 tier 缩写，并且不渲染 credits 金额。
        // 目的：保持圆环中心可读，同时让完整行继续展示完整 quota 和 credits 信息。
        const rackTier = shortTierName(tierName || paidTier?.name || '');
        if (minPct != null) {
            el.style.display = '';
            el.textContent = `${minPct}%`;
            el.removeAttribute('title');
            el.className = `text-[9px] font-bold font-mono leading-none cursor-pointer ${textColorOf(minPct)}`;
        } else if (rackTier) {
            el.style.display = '';
            el.textContent = rackTier;
            el.title = tierName || String(paidTier?.name || '');
            el.className = 'text-[8px] font-semibold leading-none text-blue-500 truncate max-w-[50px] cursor-pointer';
        } else {
            el.textContent = '';
            el.removeAttribute('title');
            el.style.display = 'none';
            if (typeof el.__antigravityQuotaCleanup === 'function') {
                el.__antigravityQuotaCleanup();
            }
            return;
        }
    } else if (minPct != null) {
        el.style.display = '';
        el.textContent = tierName ? `${tierName} ${minPct}%` : `${minPct}%`;
        el.className = `flex-shrink-0 text-[10px] font-semibold font-mono px-1.5 py-0.5 rounded relative z-[2] cursor-pointer ${colorOf(minPct)}`;
    } else if (paidTier?.name) {
        el.style.display = '';
        el.textContent = String(paidTier.name).replace(/Gemini Code Assist in /i, '').replace(/Google One /i, '');
        el.className = 'flex-shrink-0 text-[10px] font-medium px-1.5 py-0.5 rounded bg-blue-500/15 text-blue-500 relative z-[2] cursor-pointer';
    } else if (credits.length) {
        const amount = Number.parseFloat(credits[0]?.creditAmount || 0);
        const displayAmount = Number.isFinite(amount) ? (amount >= 1000 ? `${(amount / 1000).toFixed(1)}k` : amount.toFixed(0)) : '0';
        el.style.display = '';
        el.textContent = `${displayAmount} cr`;
        el.className = `flex-shrink-0 text-[10px] font-semibold font-mono px-1.5 py-0.5 rounded relative z-[2] cursor-pointer ${colorOf(amount >= 500 ? 80 : amount >= 100 ? 30 : 5)}`;
    } else {
        el.style.display = '';
        el.textContent = 'N/A';
        el.className = 'flex-shrink-0 text-[10px] font-medium px-1.5 py-0.5 rounded bg-muted text-muted-foreground relative z-[2] cursor-default';
        // 修改原因：如果额度数据消失，旧 tooltip 不能继续悬挂在 document.body 中。
        // 修改方式：无可展示标签时显式执行旧清理函数，再退出本轮 render。
        // 目的：避免异常数据让已存在的气泡和事件监听残留。
        if (typeof el.__antigravityQuotaCleanup === 'function') {
            el.__antigravityQuotaCleanup();
        }
        return;
    }

    const nextTooltipState = { groups: groupModels(modelQuotas), credits, paidTier };
    if (el.__agTooltipOpen && el.__agQuotaState?.update) {
        // 修改原因：React 轮询刷新 quota 时会重新调用 render，直接 cleanup 会移除用户已打开的气泡。
        // 修改方式：打开状态下跳过 DOM 重建，只刷新标签文本、颜色、quota 回写值和现有气泡内容。
        // 目的：让点击打开的 tooltip 稳定保留，直到用户点击外部、再次点击标签或滚动关闭。
        el.__agQuotaState.update(nextTooltipState);
        if (typeof el.__agQuotaState.placeTooltip === 'function') {
            el.__agQuotaState.placeTooltip();
        }
        return;
    }

    if (typeof el.__antigravityQuotaCleanup === 'function') {
        el.__antigravityQuotaCleanup();
    }

    let tooltipState = nextTooltipState;
    const tooltip = document.createElement('div');
    tooltip.className = 'absolute z-[9999] hidden min-w-[280px] max-w-[360px] bg-popover border-border text-foreground rounded-lg border p-3 text-xs shadow-lg';
    tooltip.style.background = 'hsl(var(--popover, 0 0% 100%))';
    tooltip.style.color = 'hsl(var(--popover-foreground, 222.2 84% 4.9%))';
    tooltip.style.borderColor = 'hsl(var(--border, 214.3 31.8% 91.4%))';
    tooltip.style.pointerEvents = 'auto';
    tooltip.style.zIndex = '9999';

    const renderTooltip = (nextState) => {
        // 修改原因：打开状态下 render 会复用同一个 tooltip DOM，闭包中的旧数据必须被替换。
        // 修改方式：renderTooltip 接收最新分组、credits 和 paidTier，并写入 tooltipState 后重绘内容。
        // 目的：React 重渲染时不关闭 tooltip，也能显示最新 quota 明细。
        if (nextState) tooltipState = nextState;
        const activeGroups = Array.isArray(tooltipState?.groups) ? tooltipState.groups : [];
        const activeCredits = Array.isArray(tooltipState?.credits) ? tooltipState.credits : [];
        const activePaidTier = tooltipState?.paidTier;
        tooltip.textContent = '';
        const title = document.createElement('div');
        title.className = 'mb-2 font-semibold text-foreground';
        title.textContent = activePaidTier?.name || 'Antigravity quota';
        tooltip.appendChild(title);

        if (!activeGroups.length) {
            const empty = document.createElement('div');
            empty.className = 'text-muted-foreground';
            empty.textContent = activeCredits.length ? `Credits: ${activeCredits.map(c => c?.creditAmount).filter(Boolean).join(', ')}` : 'No model quota details';
            tooltip.appendChild(empty);
            return;
        }

        for (const group of activeGroups) {
            const pct = Math.round(group.fraction * 100);
            const row = document.createElement('div');
            row.className = 'flex items-center justify-between gap-3 py-1';
            row.title = group.models.join(', ');

            const left = document.createElement('div');
            left.className = 'flex min-w-0 items-center gap-2';
            const arcSvg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
            arcSvg.setAttribute('width', '16');
            arcSvg.setAttribute('height', '10');
            arcSvg.setAttribute('viewBox', '0 0 16 10');
            arcSvg.style.flexShrink = '0';
            const arcPath = document.createElementNS('http://www.w3.org/2000/svg', 'path');
            arcPath.setAttribute('d', 'M 1 9 A 7 7 0 0 1 15 9');
            arcPath.setAttribute('fill', 'none');
            arcPath.setAttribute('stroke', strokeColorOf(pct));
            arcPath.setAttribute('stroke-width', '2');
            arcPath.setAttribute('stroke-linecap', 'round');
            arcPath.setAttribute('pathLength', '100');
            arcPath.style.strokeDasharray = `${pct} 100`;
            arcSvg.appendChild(arcPath);
            const name = document.createElement('span');
            name.className = 'truncate';
            name.textContent = group.label;
            left.appendChild(arcSvg);
            left.appendChild(name);

            const right = document.createElement('div');
            right.className = 'flex flex-shrink-0 items-center gap-2 font-mono';
            const percent = document.createElement('span');
            percent.textContent = `${pct}%`;
            const reset = document.createElement('span');
            reset.className = 'text-muted-foreground';
            reset.textContent = `resets in ${formatResetDuration(group.resetTime)}`;
            right.appendChild(percent);
            right.appendChild(reset);

            row.appendChild(left);
            row.appendChild(right);
            tooltip.appendChild(row);
        }
    };

    let open = false;
    const placeTooltip = () => {
        const rect = el.getBoundingClientRect();
        const tw = tooltip.offsetWidth || 280;
        const left = Math.max(8, Math.min(window.scrollX + rect.right - tw, window.scrollX + window.innerWidth - tw - 8));
        tooltip.style.left = `${left}px`;
        tooltip.style.top = `${window.scrollY + rect.bottom + 4}px`;
    };
    const show = () => {
        if (open) return;
        if (!tooltip.isConnected) document.body.appendChild(tooltip);
        renderTooltip();
        tooltip.style.display = 'block';
        placeTooltip();
        open = true;
        el.__agTooltipOpen = true;
    };
    const hide = () => {
        if (!open) return;
        tooltip.style.display = 'none';
        open = false;
        el.__agTooltipOpen = false;
    };
    const onElClick = (e) => {
        e.stopPropagation();
        e.preventDefault();
        open ? hide() : show();
    };
    const onOutsideClick = (e) => {
        if (open && !tooltip.contains(e.target) && !el.contains(e.target)) hide();
    };
    const onScroll = () => { if (open) hide(); };
    // 修改原因：QUOTA_UI 插槽会被 React 轮询刷新重复调用，hover 和延时关闭容易在刷新时误关或重建气泡。
    // 修改方式：只保留标签点击切换、document 外部点击关闭和滚动关闭，不再注册鼠标移入移出监听或延时关闭。
    // 目的：让用户显式点击打开的 quota 明细稳定保留，直到用户明确关闭或页面滚动。
    el.__agQuotaState = { update: renderTooltip, placeTooltip };
    el.addEventListener('click', onElClick);
    document.addEventListener('click', onOutsideClick);
    window.addEventListener('scroll', onScroll, true);
    el.__antigravityQuotaCleanup = () => {
        el.removeEventListener('click', onElClick);
        document.removeEventListener('click', onOutsideClick);
        window.removeEventListener('scroll', onScroll, true);
        open = false;
        el.__agTooltipOpen = false;
        el.__agQuotaState = null;
        tooltip.remove();
    };
}
""".strip()


# 修改原因：QuotaBorderOverlay 读取 OAuth 缓存中的 quota_inner/quota_outer，可能落后于 QUOTA_UI 从 raw.modelQuotas 算出的实时百分比。
# 修改方式：新增 key_border 插槽脚本，直接从 data.raw.modelQuotas 分组计算 Gemini 与 External 最低额度，并在插槽 div 内绘制 SVG 上下弧。
# 目的：让 Antigravity Key 行的边框弧和额度气泡使用同一个实时数据源，避免上弧或下弧停留在 100%。
AG_KEY_BORDER_UI = """
export default function render(ctx) {
    const { el, data } = ctx || {};
    if (!el) return;

    // 修改原因：UiSlot 会在 React 重渲染和额度轮询时重复调用，旧的 SVG 和 ResizeObserver 不能残留。
    // 修改方式：每轮渲染开始前执行上一次注册的清理函数，再根据最新 data.raw.modelQuotas 重建 DOM。
    // 目的：避免重复观察器、旧弧线和旧 title 在同一个 Key 行上叠加。
    if (typeof el.__agBorderCleanup === 'function') {
        el.__agBorderCleanup();
    }

    const raw = data?.raw || {};
    const modelQuotas = Array.isArray(raw?.modelQuotas) ? raw.modelQuotas : [];

    const normalizePercent = value => {
        if (value == null || value === '') return null;
        const n = Number(value);
        if (!Number.isFinite(n)) return null;
        return Math.max(0, Math.min(100, n));
    };
    const percentFromFraction = value => {
        if (value == null || value === '') return null;
        const n = Number(value);
        if (!Number.isFinite(n)) return null;
        return normalizePercent(n * 100);
    };
    const isIgnored = model => {
        const id = String(model || '').toLowerCase();
        return id.startsWith('tab_') || id.startsWith('chat_');
    };
    const providerOf = mq => {
        const p = String(mq?.modelProvider || '').toUpperCase();
        const m = String(mq?.model || '').toLowerCase();
        if (isIgnored(m)) return null;
        if (p.includes('GOOGLE') || p.includes('GEMINI') || m.startsWith('gemini-')) return 'gemini';
        if (p.includes('ANTHROPIC') || p.includes('OPENAI') || m.startsWith('claude-') || m.startsWith('gpt-')) return 'external';
        return null;
    };

    let geminiMin = null;
    let externalMin = null;
    for (const mq of modelQuotas) {
        const pct = percentFromFraction(mq?.remainingFraction);
        if (pct == null) continue;
        const kind = providerOf(mq);
        if (kind === 'gemini') geminiMin = geminiMin == null ? pct : Math.min(geminiMin, pct);
        if (kind === 'external') externalMin = externalMin == null ? pct : Math.min(externalMin, pct);
    }

    const q5 = normalizePercent(geminiMin ?? data?.quota_inner);
    const q7 = normalizePercent(externalMin ?? data?.quota_outer);
    if (q5 == null && q7 == null) return;

    const buildTopHalfPath = (x, y, bw, bh, r) => {
        // 修改原因：Antigravity 自定义边框必须与 Channels.tsx 的 buildTopHalfPath 几何路径一致。
        // 修改方式：使用同样的左中点到右中点顺时针路径，并使用 SVG A 圆角命令而不是另造 Q 曲线。
        // 目的：替换默认 QuotaBorderOverlay 时，弧线位置、长度和圆角视觉保持一致。
        const my = y + bh / 2;
        return [
            `M ${x} ${my}`,
            `L ${x} ${y + r}`,
            `A ${r} ${r} 0 0 1 ${x + r} ${y}`,
            `L ${x + bw - r} ${y}`,
            `A ${r} ${r} 0 0 1 ${x + bw} ${y + r}`,
            `L ${x + bw} ${my}`,
        ].join(' ');
    };
    const buildBottomHalfPath = (x, y, bw, bh, r) => {
        // 修改原因：下弧必须复用 Channels.tsx 的 buildBottomHalfPath 方向，否则 strokeDasharray 百分比会从错误位置增长。
        // 修改方式：同样从左中点出发，沿左下圆角、下边、右下圆角到右中点，保留 A 命令 sweep 参数。
        // 目的：确保 External 百分比缩短时与通用 QuotaBorderOverlay 的表现一致。
        const my = y + bh / 2;
        return [
            `M ${x} ${my}`,
            `L ${x} ${y + bh - r}`,
            `A ${r} ${r} 0 0 0 ${x + r} ${y + bh}`,
            `L ${x + bw - r} ${y + bh}`,
            `A ${r} ${r} 0 0 0 ${x + bw} ${y + bh - r}`,
            `L ${x + bw} ${my}`,
        ].join(' ');
    };

    let ro = null;
    const draw = () => {
        const w = el.offsetWidth;
        const h = el.offsetHeight;
        el.innerHTML = '';
        if (w <= 0 || h <= 0) return;

        const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
        svg.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;overflow:visible;pointer-events:none;';

        const title = document.createElementNS('http://www.w3.org/2000/svg', 'title');
        title.textContent = `Gemini: ${q5 != null ? Math.round(q5) + '%' : '?'} · External: ${q7 != null ? Math.round(q7) + '%' : '?'}`;
        svg.appendChild(title);

        const x = 1;
        const y = 1;
        const bw = w - 2;
        const bh = h - 2;
        const r = 7;

        if (q5 != null) {
            const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
            path.setAttribute('d', buildTopHalfPath(x, y, bw, bh, r));
            path.setAttribute('pathLength', '100');
            path.setAttribute('fill', 'none');
            path.setAttribute('stroke', '#3b82f6');
            path.setAttribute('stroke-width', '2');
            path.setAttribute('stroke-linecap', 'round');
            path.style.strokeDasharray = `${q5} 100`;
            path.style.strokeDashoffset = '0';
            path.style.transition = 'stroke-dasharray 0.5s ease';
            svg.appendChild(path);
        }

        if (q7 != null) {
            const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
            path.setAttribute('d', buildBottomHalfPath(x, y, bw, bh, r));
            path.setAttribute('pathLength', '100');
            path.setAttribute('fill', 'none');
            path.setAttribute('stroke', '#8b5cf6');
            path.setAttribute('stroke-width', '2');
            path.setAttribute('stroke-linecap', 'round');
            path.style.strokeDasharray = `${q7} 100`;
            path.style.strokeDashoffset = '0';
            path.style.transition = 'stroke-dasharray 0.5s ease';
            svg.appendChild(path);
        }

        el.appendChild(svg);
    };

    draw();
    if (typeof ResizeObserver !== 'undefined') {
        ro = new ResizeObserver(draw);
        ro.observe(el);
    }
    el.__agBorderCleanup = () => {
        if (ro) ro.disconnect();
        el.innerHTML = '';
        el.__agBorderCleanup = null;
    };
}
""".strip()


# 修改原因：Antigravity 的请求体参数覆写需要嵌套在 request 下，通用前端不能硬编码这个渠道专属格式提醒。
# 修改方式：注册 override_hint 插槽脚本，只通过 ctx.el.textContent 写入纯提示文本。
# 目的：让 Antigravity 编辑面板显示参数覆写格式提醒，未注册该插槽的渠道不显示提示。
AG_OVERRIDE_HINT = """
export default function render(ctx) {
    ctx.el.textContent = '⚠️ 反重力参数覆写需嵌套在 request: 下，如 {"all": {"request": {"generationConfig": {"temperature": 0.7}}}}';
}
""".strip()


# ═══════════════════════════════════════════════════════════════════
# 模型列表适配器
# ═══════════════════════════════════════════════════════════════════


async def fetch_antigravity_models(client, provider):
    """通过 fetchAvailableModels 获取 Antigravity 可用模型列表。"""
    # OAuth resolve 已在路由层完成，provider["api"] 已是 access_token
    access_token = provider.get("api")
    if isinstance(access_token, list):
        access_token = access_token[0] if access_token else None
    if not access_token:
        return []

    version = await get_antigravity_version()
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": f"antigravity/{version} darwin/arm64 google-api-nodejs-client/10.3.0",
        "X-Goog-Api-Client": ANTIGRAVITY_API_CLIENT_HEADER,
    }
    base_url = provider.get("base_url") or DEFAULT_BASE_URL
    bases = [DEFAULT_BASE_URL, FALLBACK_BASE_URL, str(base_url).rstrip("/")]
    seen: set[str] = set()
    for base in bases:
        normalized = str(base or "").rstrip("/")
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        url = _build_antigravity_url_from_base(normalized, FETCH_AVAILABLE_MODELS_ACTION)
        try:
            async with httpx.AsyncClient(timeout=30, http2=False) as _client:
                response = await _client.post(url, json={}, headers=headers)
            if response.status_code < 400:
                payload = response.json()
                models = _extract_model_names_from_available_models(payload)
                if models:
                    return models
        except Exception:
            continue
    return []

# ═══════════════════════════════════════════════════════════════════
# 注册
# ═══════════════════════════════════════════════════════════════════


def register():
    """注册 Antigravity OAuth 渠道。"""
    from .registry import register_channel

    provider = AntigravityProvider()
    register_channel(
        id="antigravity",
        type_name="gemini",
        default_base_url=DEFAULT_BASE_URL,
        default_token_url=TOKEN_URL,
        auth_header="Authorization: Bearer {api_key}",
        description="Google Antigravity (Google OAuth subscription)",
        request_adapter=get_antigravity_payload,
        passthrough_adapter=get_antigravity_passthrough_meta,
        passthrough_payload_adapter=patch_antigravity_passthrough_payload,
        # 修改原因：透传路径不会调用普通 response_adapter，必须单独声明 Antigravity 的请求头清理和响应解包处理器。
        # 修改方式：把流式和非流式透传 adapter 注册到 ChannelDefinition，供 core.passthrough 优先调用。
        # 目的：让 Gemini 方言透传 Antigravity 时也保持 HTTP/1.1 伪装并去掉 Cloud Code response 外壳。
        passthrough_stream_adapter=fetch_antigravity_passthrough_stream,
        passthrough_response_adapter=fetch_antigravity_passthrough_response,
        response_adapter=fetch_antigravity_response,
        stream_adapter=fetch_antigravity_response_stream,
        models_adapter=fetch_antigravity_models,
        is_oauth=True,
        oauth_provider=provider,
        # 修改原因：Antigravity 的 key_border 应复用前端内建 QuotaBorderOverlay，避免继续维护渠道侧手写 SVG 边框。
        # 修改方式：这里只注册 quota_display 和 override_hint，故意不注册 key_border，让前端走默认 fallback。
        # 目的：让 Antigravity、CC 和 Codex 三个 OAuth 渠道使用统一的额度边框实现。
        ui_slots={
            "quota_display": QUOTA_UI,
            "override_hint": AG_OVERRIDE_HINT,
            "import_placeholder": "1//0xxxxxxxx...",
        },
        source="builtin",
    )


def register_oauth_provider(oauth_manager) -> None:
    """兼容旧入口：向 OAuthManager 注册 Antigravity provider。"""
    # 修改原因：旧测试或外部集成可能仍直接调用渠道模块的 register_oauth_provider。
    # 修改方式：创建同一个 provider 类型并注册到 OAuthManager 的 antigravity 名称下。
    # 目的：在 registry 自动扫描之外保留向后兼容入口。
    oauth_manager.register_provider("antigravity", AntigravityProvider())
