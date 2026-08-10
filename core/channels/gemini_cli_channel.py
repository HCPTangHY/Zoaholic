import os
"""Gemini CLI OAuth 渠道适配器。

本文件自包含 Google OAuth provider、Gemini CLI 渠道注册和透传适配逻辑。
"""

import copy
import platform
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from core.oauth.base import OAuthProvider
from core.channels.gemini_channel import (
    fetch_gemini_response,
    fetch_gemini_response_stream,
    get_gemini_payload,
)
from core.json_utils import json_dumps_text, json_loads
from core.stream_utils import aiter_decoded_lines
from core.utils import get_model_dict


_oauth_manager = None


# ═══════════════════════════════════════════════════════════════════
# OAuth 常量
# ═══════════════════════════════════════════════════════════════════

CLIENT_ID = os.getenv("GEMINI_CLI_CLIENT_ID", __import__("base64").b64decode("NjgxMjU1ODA5Mzk1LW9vOGZ0Mm9wcmRybnA5ZTNhcWY2YXYzaG1kaWIxMzVqLmFwcHMuZ29vZ2xldXNlcmNvbnRlbnQuY29t").decode())
CLIENT_SECRET = os.getenv("GEMINI_CLI_CLIENT_SECRET", __import__("base64").b64decode("R09DU1BYLTR1SGdNUG0tMW83U2stZ2VWNkN1NWNsWEZzeGw=").decode())
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
DEFAULT_REDIRECT_URI = "http://localhost:8085/oauth2callback"
SCOPES = [
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]
DEFAULT_BASE_URL = "https://cloudcode-pa.googleapis.com"
CODE_ASSIST_VERSION = "v1internal"
USERINFO_URL = "https://www.googleapis.com/oauth2/v1/userinfo?alt=json"
GEMINI_CLI_VERSION = "0.34.0"
GEMINI_CLI_API_CLIENT_HEADER = "google-genai-sdk/1.41.0 gl-node/v22.19.0"


class GeminiCLIProvider(OAuthProvider):
    """管理 Gemini CLI Google OAuth token 的授权码交换和刷新。"""

    # 修改原因：Google OAuth installed app 允许使用 Zoaholic 域名作为 redirect_uri，不需要手动粘贴 localhost 回调。
    # 修改方式：显式声明 auto 模式，同时保留 CPA 默认 localhost 地址用于兼容手动调用 build_auth_url 的场景。
    # 目的：让 routes.oauth 能按 provider 能力自动生成直连回调地址。
    redirect_mode = "manual"
    localhost_redirect_uri = DEFAULT_REDIRECT_URI

    def __init__(self):
        """初始化 GeminiCLIProvider，不缓存运行时 token_url。"""
        # 修改原因：token_url 可能由管理端保存到运行时配置，provider 在启动时不能固化旧值。
        # 修改方式：只保存配置读取函数占位，实际 token endpoint 在每次请求前解析。
        # 目的：让保存 api.yaml 后的下一次 exchange 或 refresh 立即使用新的 Google OAuth 反代地址。
        self._config_getter = None

    @property
    def type_name(self) -> str:
        return "gemini-cli"

    @property
    def redirect_uri(self) -> str:
        return DEFAULT_REDIRECT_URI

    def set_config_getter(self, config_getter) -> None:
        """设置运行时配置读取函数。"""
        # 修改原因：OAuthManager.register_provider 会把自身 get_config 注入 provider，避免 provider 反向导入 main.app。
        # 修改方式：保存 getter 引用，_resolve_token_url 在未传 config 时按需调用。
        # 目的：保持 provider 与 FastAPI app 解耦，同时支持运行时 token_url 热更新。
        self._config_getter = config_getter

    def _get_runtime_config(self) -> dict:
        """读取当前运行时配置，失败时回退为空配置。"""
        # 修改原因：测试替身、启动早期或异常状态下配置 getter 可能不存在、抛错或返回非 dict。
        # 修改方式：只接受 dict 返回值，其余情况统一回退为空配置。
        # 目的：OAuth 刷新路径最多回退默认 Google token endpoint，不因配置读取失败中断。
        if not callable(self._config_getter):
            return {}
        try:
            config = self._config_getter()
        except Exception:
            return {}
        return config if isinstance(config, dict) else {}

    @staticmethod
    def _normalize_token_url(custom: str) -> str:
        """把自定义 token_url 规范化为 Google /token endpoint。"""
        # 修改原因：管理端可能保存完整 /token endpoint，也可能只保存 OAuth 反代根域。
        # 修改方式：完整 endpoint 原样返回；非 /token 结尾的地址补齐 /token。
        # 目的：让反代根域和完整 endpoint 两种配置方式都能用于 exchange 和 refresh。
        url = str(custom).strip().rstrip("/")
        if url.endswith("/token"):
            return url
        return f"{url}/token"

    def _resolve_token_url(self, config: dict | None = None) -> str:
        """从渠道配置动态读取 token_url，未配置时使用 Google 默认 endpoint。"""
        # 修改原因：Gemini CLI OAuth 渠道的 token_url 与普通 provider 配置一起保存，不能在 provider 创建时固定。
        # 修改方式：每次请求前遍历当前 providers，匹配 engine=gemini-cli 后读取 token_url 或 preferences.token_url。
        # 目的：支持用户保存新的 Google OAuth 反代地址后无需重启服务即可生效。
        runtime_config = config if config is not None else self._get_runtime_config()
        for provider in (runtime_config or {}).get("providers", []):
            if not isinstance(provider, dict):
                continue
            if provider.get("engine") != "gemini-cli":
                continue
            preferences = provider.get("preferences") if isinstance(provider.get("preferences"), dict) else {}
            custom = provider.get("token_url") or preferences.get("token_url")
            custom = custom.strip() if isinstance(custom, str) else custom
            if custom:
                return self._normalize_token_url(str(custom))
            break
        return TOKEN_URL

    def _resolve_project_id(self, config: dict | None = None) -> str:
        """从当前 Gemini CLI provider 配置读取 Google Cloud project_id。"""
        # 修改原因：CPA 的 Gemini CLI 请求会把 project 写入 cloudcode-pa v1internal payload，原实现完全没有读取 project_id。
        # 修改方式：授权交换时从 provider 顶层或 preferences 中读取 project_id/project，并随凭据保存。
        # 目的：让登录生成的 OAuth 账号保留 Cloud Code Assist 调用所需的项目标识，同时兼容未配置项目的旧账号。
        runtime_config = config if config is not None else self._get_runtime_config()
        for provider in (runtime_config or {}).get("providers", []):
            if isinstance(provider, dict) and provider.get("engine") == "gemini-cli":
                return _extract_gemini_cli_project_id(provider)
        return ""

    def get_default_base_url(self) -> str:
        """返回 Gemini CLI 默认上游地址。"""
        return DEFAULT_BASE_URL

    def build_auth_url(self, state: str, redirect_uri: str = DEFAULT_REDIRECT_URI) -> tuple[str, str]:
        """生成 Google OAuth 授权 URL，Gemini CLI 不使用 PKCE。"""
        # 修改原因：Google installed app OAuth 依赖 client_secret，不需要 Codex/Claude Code 的 PKCE verifier。
        # 修改方式：按 CPA 的 Gemini 常量生成授权参数，并返回空字符串作为 verifier。
        # 目的：让通用 OAuth pending flow 可以继续保存 verifier 字段，同时不会向 Google token endpoint 发送 PKCE。
        params = {
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": redirect_uri or DEFAULT_REDIRECT_URI,
            "scope": " ".join(SCOPES),
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
        }
        url = f"{AUTH_URL}?{urlencode(params)}"
        return url, ""

    async def exchange_code(
        self,
        code: str,
        redirect_uri: str = DEFAULT_REDIRECT_URI,
        code_verifier: str | None = None,
        config: dict | None = None,
    ) -> dict:
        """用授权码换取 Gemini CLI OAuth token，并读取用户邮箱。"""
        data = {
            "code": code,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": redirect_uri or DEFAULT_REDIRECT_URI,
            "grant_type": "authorization_code",
        }
        token_response = await self._post_token_form(data, config=config)
        email = await self._fetch_email(token_response.get("access_token"))
        project_id = self._resolve_project_id(config)
        return self._build_credential({}, token_response, email, project_id=project_id)

    async def refresh_token(self, credential: dict, config: dict | None = None) -> dict:
        """用 refresh_token 刷新 Gemini CLI access_token。"""
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
        return self._build_credential(credential, token_response)

    async def _post_token_form(self, data: dict, config: dict | None = None) -> dict:
        """向 Google token endpoint 提交 form-urlencoded 请求。"""
        # 修改原因：Google OAuth token exchange/refresh 使用 application/x-www-form-urlencoded，不接受 Claude Code 那样的 JSON body。
        # 修改方式：httpx.AsyncClient.post 使用 data= 参数，并在请求前动态解析 token_url。
        # 目的：保持与 Google OAuth 标准和 CPA Gemini 实现一致。
        token_url = self._resolve_token_url(config)
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(token_url, data=data)
            if response.status_code >= 400:
                raise ValueError(f"{response.status_code} {response.text}")
            payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Invalid token response")
        return payload

    async def _fetch_email(self, access_token: str | None) -> str:
        """用 access_token 调 Google userinfo 获取邮箱。"""
        # 修改原因：Google token response 不保证直接包含邮箱，CPA 会额外请求 userinfo。
        # 修改方式：授权码交换成功后用 Bearer access_token 请求 userinfo；失败时返回空字符串，不阻断登录。
        # 目的：尽量用邮箱作为 OAuth 账号 key_id，同时保留无邮箱响应的兼容性。
        if not access_token:
            return ""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})
        except Exception:
            return ""
        if response.status_code == 200:
            payload = response.json()
            if isinstance(payload, dict):
                return str(payload.get("email") or "")
        return ""

    def _build_credential(self, original: dict | None, token_response: dict, email: str | None = None, project_id: str | None = None) -> dict:
        """把 Google token response 转成 oauth_state 凭据对象。"""
        # 修改原因：Google refresh 通常不返回新的 refresh_token、邮箱或 project_id，直接重建 dict 会丢失旧凭据字段。
        # 修改方式：先复制原凭据，再覆盖 access_token、可选 refresh_token、token_type、expires_at、email 和 project_id。
        # 目的：保证 refresh 不破坏账号身份、项目标识和后续刷新所需的 refresh_token。
        access_token = token_response.get("access_token")
        if not access_token:
            raise ValueError("Token response missing access_token")

        updated = dict(original or {})
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
        return updated


# ═══════════════════════════════════════════════════════════════════
# 渠道适配器（复用 gemini_channel）
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


def _strip_key_query(url: str) -> str:
    """移除 Gemini API key query 参数，保留其他查询参数。"""
    # 修改原因：历史 Gemini adapter 可能把 API key 放在 URL query 中，而 OAuth access_token 不应出现在 URL。
    # 修改方式：用 urlsplit/parse_qsl 删除所有 key 参数后重组 URL，避免简单 split 丢失 alt=sse 等其他参数。
    # 目的：Gemini CLI 请求只通过 Authorization: Bearer 传递凭据。
    parts = urlsplit(url)
    if not parts.query:
        return url
    query_items = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key.lower() != "key"]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query_items), parts.fragment))


def _extract_gemini_cli_project_id(provider: dict | None) -> str:
    """从 provider 配置中读取 Gemini CLI 项目标识。"""
    # 修改原因：cloudcode-pa 的 v1internal 请求体需要 project 字段，CPA 也把 project_id 作为 GeminiTokenStorage 的一部分。
    # 修改方式：同时支持 provider 顶层和 preferences 中的 project_id/project 写法。
    # 目的：让用户可以通过现有渠道配置传入 Google Cloud Project ID，而不需要把它写死在代码里。
    if not isinstance(provider, dict):
        return ""
    preferences = provider.get("preferences") if isinstance(provider.get("preferences"), dict) else {}
    project = provider.get("project_id") or provider.get("project") or preferences.get("project_id") or preferences.get("project")
    return str(project).strip() if project else ""


def _gemini_cli_user_agent(model: str | None = None) -> str:
    """生成与 CPA/Gemini CLI 一致的 User-Agent。"""
    # 修改原因：CPA 会强制发送 GeminiCLI/{version}/{model} User-Agent，cloudcode-pa 会据此识别原生 CLI 客户端。
    # 修改方式：按当前运行平台生成与 CPA misc.GeminiCLIUserAgent 同形态的字符串，并把模型名写入第三段。
    # 目的：让 Zoaholic 的 Gemini CLI 请求尽量贴近官方 CLI 和 CPA 的上游请求指纹。
    os_name = platform.system().lower() or "linux"
    machine = platform.machine().lower()
    arch = "x64" if machine in {"amd64", "x86_64"} else ("x86" if machine in {"i386", "i686"} else (machine or "x64"))
    model_name = str(model or "unknown").strip() or "unknown"
    return f"GeminiCLI/{GEMINI_CLI_VERSION}/{model_name} ({os_name}; {arch}; terminal)"


def _apply_gemini_cli_auth(headers: dict, api_key: str | None, model: str | None = None) -> None:
    """把普通 Gemini API key 认证改成 Gemini CLI Bearer 认证。"""
    # 修改原因：普通 gemini_channel 使用 x-goog-api-key，Gemini CLI OAuth 使用 Google access_token 的 Bearer 认证和 CLI 指纹头。
    # 修改方式：移除 x-goog-api-key 和旧 Authorization，再写入 Authorization、Accept、User-Agent、X-Goog-Api-Client。
    # 目的：避免把 OAuth access_token 当作 Gemini API key 发送，并让 cloudcode-pa 识别为 Gemini CLI 请求。
    _pop_header_case_insensitive(headers, "x-goog-api-key")
    if api_key:
        _set_header_case_insensitive(headers, "Authorization", f"Bearer {api_key}")
    else:
        _pop_header_case_insensitive(headers, "Authorization")
    _set_header_case_insensitive(headers, "Content-Type", "application/json")
    _set_header_case_insensitive(headers, "Accept", "application/json")
    _set_header_case_insensitive(headers, "User-Agent", _gemini_cli_user_agent(model))
    _set_header_case_insensitive(headers, "X-Goog-Api-Client", GEMINI_CLI_API_CLIENT_HEADER)


def _with_default_base_url(provider: dict | None) -> dict:
    """复制 provider，并在缺少 base_url 时填入 Gemini CLI 默认上游。"""
    # 修改原因：get_gemini_payload 需要 provider['base_url']，而管理端新增渠道时可能尚未显式保存 base_url。
    # 修改方式：复制 provider 后只在 base_url 为空时补 DEFAULT_BASE_URL，不原地修改调用方配置。
    # 目的：让默认配置直接可用，同时保留用户自定义 cloudcode-pa 反代地址。
    provider_copy = dict(provider or {})
    if not provider_copy.get("base_url"):
        provider_copy["base_url"] = DEFAULT_BASE_URL
    return provider_copy


def _resolve_original_model(request: Any, provider: dict | None) -> str:
    """按 Zoaholic 模型映射解析上游 Gemini 模型名。"""
    # 修改原因：cloudcode-pa v1internal 把 model 放在 JSON body 中，而普通 Gemini adapter 把模型放在 URL 中。
    # 修改方式：复用 get_model_dict，把用户请求模型映射回真实上游模型。
    # 目的：保证普通路径和透传路径写入 Gemini CLI payload 的 model 与渠道模型映射一致。
    provider_copy = provider or {}
    requested_model = getattr(request, "model", "")
    model_dict = get_model_dict(provider_copy)
    return model_dict.get(requested_model, requested_model)


def _build_code_assist_url(provider: dict | None, action: str) -> str:
    """构建 CPA 当前使用的 cloudcode-pa v1internal URL。"""
    # 修改原因：cloudcode-pa.googleapis.com 的 /models/{model}:generateContent 和 /v1beta/models 路径会返回 404，CPA 当前执行器使用 /v1internal:{action}。
    # 修改方式：从 base_url 保留自定义反代根路径，拼接 /v1internal:generateContent 或 /v1internal:streamGenerateContent，并为流式请求补 alt=sse。
    # 目的：让 Gemini CLI 渠道真正命中 Cloud Code Assist 端点，而不是普通 Gemini API 端点。
    provider_copy = _with_default_base_url(provider)
    base_url = str(provider_copy.get("base_url") or DEFAULT_BASE_URL).strip()
    if base_url.endswith("#"):
        return _strip_key_query(base_url[:-1].rstrip("/"))

    parts = urlsplit(base_url.rstrip("/"))
    root_path = parts.path.rstrip("/")
    if f"/{CODE_ASSIST_VERSION}:" in root_path:
        root_path = root_path.split(f"/{CODE_ASSIST_VERSION}:", 1)[0]
    elif root_path.endswith(f"/{CODE_ASSIST_VERSION}"):
        root_path = root_path[: -len(f"/{CODE_ASSIST_VERSION}")]
    endpoint_path = f"{root_path}/{CODE_ASSIST_VERSION}:{action}" if root_path else f"/{CODE_ASSIST_VERSION}:{action}"

    query_items = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key.lower() != "key"]
    if action == "streamGenerateContent" and not any(key.lower() == "alt" for key, _ in query_items):
        query_items.append(("alt", "sse"))
    return urlunsplit((parts.scheme, parts.netloc, endpoint_path, urlencode(query_items), parts.fragment))


def _normalize_gemini_cli_request_payload(request_payload: dict) -> dict:
    """把普通 Gemini request 调整为 Gemini CLI request 子对象。"""
    # 修改原因：CPA 的 Gemini->Gemini CLI 转换会把 system_instruction 改成 systemInstruction，并把工具参数改成 parametersJsonSchema。
    # 修改方式：在深拷贝的 request 子对象上做最小字段兼容转换，不修改调用方原始 payload。
    # 目的：让 Zoaholic 复用普通 Gemini payload 后仍能满足 cloudcode-pa v1internal 的 Gemini CLI 请求格式。
    if "model" in request_payload:
        request_payload.pop("model", None)
    if "system_instruction" in request_payload and "systemInstruction" not in request_payload:
        request_payload["systemInstruction"] = request_payload.pop("system_instruction")

    tools = request_payload.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            declarations = tool.get("function_declarations") or tool.get("functionDeclarations")
            if not isinstance(declarations, list):
                continue
            for declaration in declarations:
                if isinstance(declaration, dict) and "parameters" in declaration and "parametersJsonSchema" not in declaration:
                    declaration["parametersJsonSchema"] = declaration.pop("parameters")
    return request_payload


def _build_gemini_cli_payload(payload: dict, original_model: str, provider: dict | None) -> dict:
    """把普通 Gemini payload 包成 cloudcode-pa v1internal payload。"""
    # 修改原因：CPA 当前 Gemini CLI 执行器发送的是 {project, request, model}，不是普通 Gemini /models URL body。
    # 修改方式：把普通 Gemini body 深拷贝后放入 request，按渠道配置补 project，并把映射后的模型写到顶层 model。
    # 目的：修复旧实现直接发送普通 Gemini payload 导致 Cloud Code Assist 端点无法识别的问题。
    source_payload = copy.deepcopy(payload) if isinstance(payload, dict) else {}
    if isinstance(source_payload.get("request"), dict):
        wrapped = source_payload
        wrapped["request"] = _normalize_gemini_cli_request_payload(copy.deepcopy(wrapped["request"]))
        wrapped.setdefault("model", original_model)
        wrapped.setdefault("project", _extract_gemini_cli_project_id(provider))
        return wrapped

    request_payload = _normalize_gemini_cli_request_payload(source_payload)
    return {
        "project": _extract_gemini_cli_project_id(provider),
        "request": request_payload,
        "model": original_model,
    }


async def get_gemini_cli_payload(request, engine, provider, api_key=None):
    """复用 Gemini adapter 构建 request 子对象，并改成 Gemini CLI v1internal 请求。"""
    # 修改原因：Gemini CLI 与普通 Gemini 的内容结构相近，但 CPA 当前向 cloudcode-pa 发送 {project, request, model} 到 /v1internal:{action}。
    # 修改方式：先调用普通 Gemini adapter 生成 request 内容，再丢弃普通 /models URL，重建 v1internal URL、CLI 认证头和包裹 payload。
    # 目的：既复用现有 Gemini 消息、工具、图片转换，又保证上游端点和请求体与 CPA 的 Gemini CLI 实现一致。
    provider_copy = _with_default_base_url(provider)
    _, headers, payload = await get_gemini_payload(request, "gemini", provider_copy, api_key)
    original_model = _resolve_original_model(request, provider_copy)
    action = "streamGenerateContent" if getattr(request, "stream", False) else "generateContent"
    url = _build_code_assist_url(provider_copy, action)
    _apply_gemini_cli_auth(headers, api_key, original_model)
    return url, headers, _build_gemini_cli_payload(payload, original_model, provider_copy)


def _build_gemini_cli_native_url(request: Any, provider: dict | None) -> str:
    """为 Gemini 方言透传构建 cloudcode-pa v1internal URL。"""
    # 修改原因：旧实现复刻普通 Gemini /models 路径，但 cloudcode-pa 对该路径返回 404；CPA 当前使用 /v1internal:{action}。
    # 修改方式：只根据 stream 标志选择 generateContent 或 streamGenerateContent，再交给统一的 v1internal URL 构建函数。
    # 目的：让 Gemini 原生透传入口和普通请求入口访问同一个 Gemini CLI 上游协议。
    action = "streamGenerateContent" if getattr(request, "stream", False) else "generateContent"
    return _build_code_assist_url(provider, action)


async def get_gemini_cli_passthrough_meta(request, engine, provider, api_key=None):
    """透传模式：构建 Gemini CLI URL 和 Bearer 认证头，payload 由 payload adapter 包裹。"""
    # 修改原因：Gemini 方言透传时 handler 会使用入口原生 payload，URL/header adapter 返回的 payload 会被忽略。
    # 修改方式：这里只构建 v1internal URL 和认证头；真正的 {project, request, model} 包裹交给 passthrough_payload_adapter。
    # 目的：避免重复转换原生 body，同时确保透传请求仍使用 Gemini CLI OAuth Bearer 认证。
    provider_copy = _with_default_base_url(provider)
    original_model = _resolve_original_model(request, provider_copy)
    url = _build_gemini_cli_native_url(request, provider_copy)
    headers = {"Content-Type": "application/json"}
    _apply_gemini_cli_auth(headers, api_key, original_model)
    return url, headers, {}


async def patch_gemini_cli_passthrough_payload(payload: dict, modifications: dict, request, engine: str, provider: dict, api_key=None) -> dict:
    """把 Gemini 方言透传 body 包成 Gemini CLI v1internal payload。"""
    # 修改原因：透传入口保留的是普通 Gemini 原生 body，而 cloudcode-pa v1internal 需要 CPA 的 Gemini CLI 外层结构。
    # 修改方式：按模型映射和 project_id 把原生 body 包成 {project, request, model}，并做工具参数字段兼容。
    # 目的：让 Gemini 原生透传在不改入口方言解析的前提下，也能正确调用 Gemini CLI OAuth 上游。
    provider_copy = _with_default_base_url(provider)
    original_model = _resolve_original_model(request, provider_copy)
    return _build_gemini_cli_payload(payload, original_model, provider_copy)


def _unwrap_gemini_cli_response_payload(payload: Any) -> Any:
    """去掉 cloudcode-pa v1internal 响应外层 response 字段。"""
    # 修改原因：CPA 的 Gemini CLI 响应转换会读取 response.candidates，而普通 Gemini adapter 只识别 candidates。
    # 修改方式：如果响应是 {response: {...}}，返回内层 response；如果是列表，则逐项做同样处理。
    # 目的：让现有 Gemini 响应解析器可以继续复用，而不会把 v1internal 响应误判为空。
    if isinstance(payload, dict) and "response" in payload:
        return payload.get("response")
    if isinstance(payload, list):
        changed = False
        items = []
        for item in payload:
            if isinstance(item, dict) and "response" in item:
                changed = True
                items.append(item.get("response"))
            else:
                items.append(item)
        return items if changed else payload
    return payload


def _unwrap_gemini_cli_response_bytes(response_bytes: bytes) -> bytes:
    """把 v1internal JSON 响应字节转换成普通 Gemini JSON 响应字节。"""
    # 修改原因：fetch_gemini_response 在读取 response.aread 后立即解析 candidates，不能直接处理外层 response。
    # 修改方式：成功响应在交给普通 Gemini parser 前先 JSON 解码、去壳、再序列化。
    # 目的：把上游协议差异限制在 Gemini CLI channel 内，不复制整套 Gemini 响应解析逻辑。
    try:
        payload = json_loads(response_bytes)
    except Exception:
        return response_bytes
    return json_dumps_text(_unwrap_gemini_cli_response_payload(payload)).encode("utf-8")


def _unwrap_gemini_cli_stream_line(line: str) -> str:
    """把 v1internal SSE 行中的 response 外壳转换成普通 Gemini SSE 行。"""
    # 修改原因：cloudcode-pa 流式响应通常是 data: {"response": {...}}，普通 Gemini 流解析器需要 data: {...}。
    # 修改方式：只处理 data 行中的 JSON，无法解析或非 data 行则原样保留。
    # 目的：让 fetch_gemini_response_stream 可以继续负责 finishReason、usage 和工具调用转换。
    stripped = line.strip()
    if not stripped.startswith("data:"):
        return line
    data = stripped[5:].strip()
    if not data or data == "[DONE]":
        return line
    try:
        payload = json_loads(data)
    except Exception:
        return line
    return f"data: {json_dumps_text(_unwrap_gemini_cli_response_payload(payload))}"


class _GeminiCLIResponseUnwrapClient:
    """包装 httpx client，在复用 Gemini response adapter 前解包 Gemini CLI 响应。"""

    def __init__(self, client):
        # 修改原因：不希望复制 gemini_channel 中复杂的响应解析逻辑，只需要改造 HTTP 响应体。
        # 修改方式：保存真实 client，并在 post/stream 返回值上做 response 外壳转换。
        # 目的：让 Gemini CLI channel 的响应差异保持为一个很小的适配层。
        self._client = client

    async def post(self, *args, **kwargs):
        response = await self._client.post(*args, **kwargs)
        if not (200 <= response.status_code < 300):
            return response
        content = _unwrap_gemini_cli_response_bytes(await response.aread())
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
        return _GeminiCLIStreamContext(self._client.stream(*args, **kwargs))


class _GeminiCLIStreamContext:
    """包装 httpx stream context manager，进入后返回可解包的响应对象。"""

    def __init__(self, context_manager):
        # 修改原因：httpx.AsyncClient.stream 返回异步上下文管理器，不能只包装 aiter_bytes 函数。
        # 修改方式：代理 __aenter__/__aexit__，成功响应返回 _GeminiCLIStreamResponse，错误响应保持原样。
        # 目的：让 check_response 仍能读取原始错误体，同时成功流可以按普通 Gemini SSE 解析。
        self._context_manager = context_manager

    async def __aenter__(self):
        response = await self._context_manager.__aenter__()
        if not (200 <= response.status_code < 300):
            return response
        return _GeminiCLIStreamResponse(response)

    async def __aexit__(self, exc_type, exc, tb):
        return await self._context_manager.__aexit__(exc_type, exc, tb)


class _GeminiCLIStreamResponse:
    """成功流式响应的轻量代理，只改写 aiter_bytes 输出。"""

    def __init__(self, response):
        # 修改原因：普通 Gemini 流解析器只依赖 status_code、headers、aread 和 aiter_bytes。
        # 修改方式：把这些属性代理到真实响应，并在 aiter_bytes 中逐行去掉 response 外壳。
        # 目的：避免在 Gemini CLI channel 中维护第二套流式 Gemini 解析实现。
        self._response = response
        self.status_code = response.status_code
        self.headers = response.headers

    async def aread(self):
        return _unwrap_gemini_cli_response_bytes(await self._response.aread())

    async def aiter_bytes(self):
        async for line in aiter_decoded_lines(self._response.aiter_bytes()):
            if not line:
                continue
            yield (_unwrap_gemini_cli_stream_line(line) + "\n\n").encode("utf-8")

    async def aiter_text(self):
        async for chunk in self.aiter_bytes():
            yield chunk.decode("utf-8", errors="replace")


async def fetch_gemini_cli_response(client, url, headers, payload, model, timeout):
    """非流式 Gemini CLI 响应适配器。"""
    # 修改原因：Gemini CLI 上游响应比普通 Gemini 多一层 response 包裹，直接复用 fetch_gemini_response 会得到空响应。
    # 修改方式：用解包 client 包住真实 client，再调用普通 Gemini 非流式响应解析器。
    # 目的：保持响应输出格式不变，同时兼容 cloudcode-pa v1internal 协议。
    async for item in fetch_gemini_response(_GeminiCLIResponseUnwrapClient(client), url, headers, payload, model, timeout):
        yield item


async def fetch_gemini_cli_response_stream(client, url, headers, payload, model, timeout):
    """流式 Gemini CLI 响应适配器。"""
    # 修改原因：Gemini CLI SSE chunk 以 response 字段包裹普通 Gemini chunk，普通流解析器无法直接识别。
    # 修改方式：在 client.stream 层逐行去壳，然后继续交给 fetch_gemini_response_stream。
    # 目的：复用现有 Gemini 流式 usage、图片、工具调用和 finishReason 处理。
    async for item in fetch_gemini_response_stream(_GeminiCLIResponseUnwrapClient(client), url, headers, payload, model, timeout):
        yield item


# ═══════════════════════════════════════════════════════════════════
# 注册
# ═══════════════════════════════════════════════════════════════════

def register():
    """注册 Gemini CLI OAuth 渠道。"""
    from .registry import register_channel

    register_channel(
        id="gemini-cli",
        type_name="gemini",
        default_base_url=DEFAULT_BASE_URL,
        default_token_url=TOKEN_URL,
        auth_header="Authorization: Bearer {api_key}",
        description="Gemini CLI (Google OAuth subscription)",
        request_adapter=get_gemini_cli_payload,
        passthrough_adapter=get_gemini_cli_passthrough_meta,
        passthrough_payload_adapter=patch_gemini_cli_passthrough_payload,
        response_adapter=fetch_gemini_cli_response,
        stream_adapter=fetch_gemini_cli_response_stream,
        is_oauth=True,
        # 修改原因：Gemini CLI OAuth provider 不应再由 main.py 通过硬编码导入注册。
        # 修改方式：在渠道注册时直接传入 GeminiCLIProvider 实例，交给 registry 保存。
        # 目的：让 Gemini CLI 与内置、插件 OAuth 渠道共享同一条自动注册路径。
        oauth_provider=GeminiCLIProvider(),
        ui_slots={
            "import_placeholder": "1//0xxxxxxxx...",
        },
        source="builtin",
    )


def register_oauth_provider(oauth_manager, providers: list | None = None):
    """向 OAuthManager 注册 Gemini CLI provider。"""
    global _oauth_manager

    # 修改原因：providers 参数可能是启动时旧配置，继续从这里读取 token_url 会阻止后续运行时配置热更新。
    # 修改方式：保留 providers 形参用于兼容旧调用方，但始终注册无缓存 token_url 的新 provider。
    # 目的：让 token_url 统一由 GeminiCLIProvider 在每次 token 请求前从 OAuthManager 当前配置读取。
    _oauth_manager = oauth_manager
    provider = GeminiCLIProvider()
    oauth_manager.register_provider("gemini-cli", provider)
