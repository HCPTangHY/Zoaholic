"""
JetBrains Grazie Cloud API 代理插件 (jb_grazie)

功能：
在请求发出前注入 Grazie Cloud 所需的请求头，
并对 Gemini 渠道做路径改写。
响应由上游原样透传，不需要响应拦截器。

支持渠道：
- OpenAI 系 (openai, openai-responses, azure, openrouter)
- Claude/Anthropic 系 (claude, vertex-claude)
- Gemini 系 (gemini, vertex-gemini, firebaseVertex, gemini-interactions)

路径改写（仅 Gemini 渠道需要）：
  /v1beta/models/{model}:{method}
  → /v1/projects/jetbrains-grazie/locations/global/publishers/google/models/{model}:{method}

Claude、OpenAI 的路径（/v1/messages、/v1/chat/completions、/v1/responses）
在 Grazie 端点上原样可用，无需改写。

使用方式：
  base_url 直接写目标地址（Grazie 端点或中转代理），
  插件只负责路径改写和头注入，不会覆盖 base_url。

  配置示例：
  ```yaml
  # Gemini 直连 Grazie
  engine: gemini
  base_url: https://ingrazzio-cloud-prod.labs.jb.gg/v1beta
  api: your-grazie-key
  preferences:
    enabled_plugins:
      - jb_grazie

  # Claude 直连 Grazie
  engine: claude
  base_url: https://ingrazzio-cloud-prod.labs.jb.gg/v1
  api: your-grazie-key
  preferences:
    enabled_plugins:
      - jb_grazie

  # OpenAI 直连 Grazie
  engine: openai
  base_url: https://ingrazzio-cloud-prod.labs.jb.gg/v1
  api: your-grazie-key
  preferences:
    enabled_plugins:
      - jb_grazie

  # 通过自建代理中转到 Grazie
  engine: gemini
  base_url: https://my-proxy.example.com/v1beta
  api: your-grazie-key
  preferences:
    enabled_plugins:
      - jb_grazie
  ```

  如果你已经部署了 CF Worker 做中转（Worker 本身处理路由和头注入），
  那直接把 base_url 写 CF Worker 地址即可，不需要启用本插件。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from core.log_config import logger
from core.plugins import (
    register_request_interceptor,
    unregister_request_interceptor,
)

# ==================== 插件元信息 ====================

PLUGIN_INFO = {
    "name": "jb_grazie",
    "version": "1.1.0",
    "description": "JetBrains Grazie Cloud API 代理：注入 Grazie 请求头 + Gemini 路径改写",
    "author": "Zoaholic",
    "dependencies": [],
    "metadata": {
        "category": "interceptors",
        "tags": ["grazie", "jetbrains", "proxy", "junie"],
    },
}

EXTENSIONS = [
    "interceptors:jb_grazie_request",
]

# ==================== 常量 ====================

GRAZIE_AGENT_DEFAULT = '{"name":"junie:cli","version":"888.219"}'

# engine → X-LLM-Model 映射
_ENGINE_TO_LLM_MODEL: Dict[str, str] = {
    "openai": "openai",
    "openai-responses": "openai",
    "azure": "openai",
    "openrouter": "openai",
    "claude": "anthropic",
    "vertex-claude": "anthropic",
    "gemini": "google",
    "vertex-gemini": "google",
    "firebaseVertex": "google",
    "gemini-interactions": "google",
}

# Gemini 类渠道（需要做路径改写）
_GEMINI_ENGINES = frozenset({
    "gemini", "vertex-gemini", "firebaseVertex", "gemini-interactions",
})

# Anthropic 类渠道（需要额外的 anthropic-* 头）
_ANTHROPIC_ENGINES = frozenset({"claude", "vertex-claude"})

# Gemini 路径匹配: /v1beta/models/{suffix} 或 /v1/models/{suffix}
_GEMINI_MODELS_RE = re.compile(r"^(.*?)/v1(?:beta)?/models/(.+)$")


# ==================== 工具函数 ====================


def _detect_llm_model(engine: str) -> str:
    """根据 engine 识别 X-LLM-Model 值。"""
    llm = _ENGINE_TO_LLM_MODEL.get(engine)
    if llm:
        return llm
    # 名称推断
    el = engine.lower()
    if "gemini" in el or "google" in el:
        return "google"
    if "claude" in el or "anthropic" in el:
        return "anthropic"
    if "openai" in el or "gpt" in el:
        return "openai"
    # CF Worker 的默认值
    return "anthropic"


def _rewrite_gemini_path(url: str, path: str) -> str:
    """将 Gemini 标准路径改写为 Grazie 路由格式。

    输入:  /v1beta/models/gemini-2.5-flash:streamGenerateContent
    输出:  /v1/projects/jetbrains-grazie/.../models/gemini-2.5-flash:streamGenerateContent

    支持带前缀路径:
    输入:  /graziexxxxx/v1beta/models/gemini-2.5-flash:streamGenerateContent
    输出:  /graziexxxxx/v1/projects/jetbrains-grazie/.../models/gemini-2.5-flash:streamGenerateContent

    不匹配时原样返回。
    """
    m = _GEMINI_MODELS_RE.match(path)
    if m:
        prefix, model_suffix = m.group(1), m.group(2)
        return (
            f"{prefix}/v1/projects/jetbrains-grazie/locations/global"
            f"/publishers/google/models/{model_suffix}"
        )
    return path


# ==================== 请求拦截器 ====================


async def jb_grazie_request_interceptor(
    request: Any,
    engine: str,
    provider: Dict[str, Any],
    api_key: Optional[str],
    url: str,
    headers: Dict[str, Any],
    payload: Dict[str, Any],
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """请求拦截器：注入 Grazie 请求头，Gemini 渠道做路径改写。

    不替换 host——base_url 写什么就发到哪里。
    """
    parsed = urlparse(url)
    target_host = parsed.hostname or ""
    original_path = parsed.path
    original_query = parsed.query

    # 确定 LLM 模型类型
    llm_model = _detect_llm_model(engine)
    is_anthropic = (engine in _ANTHROPIC_ENGINES) or (llm_model == "anthropic")
    is_gemini = (engine in _GEMINI_ENGINES) or (llm_model == "google")

    # Gemini 路径改写（其他渠道路径在 Grazie 上原样可用）
    if is_gemini:
        new_path = _rewrite_gemini_path(url, original_path)
    else:
        new_path = original_path

    # 重建 URL（保留 scheme、host、port、query，只改 path）
    new_url = f"{parsed.scheme}://{parsed.netloc}{new_path}"
    if original_query:
        new_url += f"?{original_query}"

    # 提取 API Key
    actual_key = api_key or ""
    if not actual_key:
        actual_key = (
            headers.get("x-api-key", "")
            or headers.get("x-goog-api-key", "")
            or headers.get("Authorization", "").removeprefix("Bearer ").strip()
            or headers.get("authorization", "").removeprefix("Bearer ").strip()
            or ""
        )

    # 保留原始 Content-Type
    content_type = (
        headers.get("Content-Type")
        or headers.get("content-type")
        or "application/json"
    )

    # 构建新请求头（完全替换，与 CF Worker 对齐）
    new_headers: Dict[str, Any] = {
        "Accept":               "text/event-stream,application/json",
        "Accept-Charset":       "UTF-8",
        "Accept-Encoding":      "identity",
        "Authorization":        f"Bearer {actual_key}",
        "Content-Type":         content_type,
        "Grazie-Agent":         headers.get("Grazie-Agent")
                                or headers.get("grazie-agent")
                                or GRAZIE_AGENT_DEFAULT,
        "Host":                 target_host,
        "User-Agent":           "ktor-client",
        "X-Accept-EAP-License": "false",
        "X-Free-Google-Api":    "true",
        "X-Keep-Path":          "true",
        "X-LLM-Model":          llm_model,
    }

    # Anthropic 专属头
    if is_anthropic:
        new_headers["anthropic-beta"] = (
            headers.get("anthropic-beta") or "prompt-caching-2024-07-31"
        )
        new_headers["anthropic-version"] = (
            headers.get("anthropic-version") or "2023-06-01"
        )

    # Claude/Anthropic 不允许同时传 temperature 和 top_p，
    # 当两者都存在时自动移除 top_p，避免上游返回 400 错误。
    if is_anthropic and isinstance(payload, dict):
        if "temperature" in payload and "top_p" in payload:
            removed_top_p = payload.pop("top_p")
            logger.info(
                f"[jb_grazie] Removed top_p={removed_top_p} from Claude payload "
                f"(temperature and top_p cannot coexist)"
            )

    logger.info(
        f"[jb_grazie] {engine} → {target_host}, "
        f"llm_model={llm_model}, path={new_path}"
    )

    return new_url, new_headers, payload


# ==================== 插件生命周期 ====================


def setup(manager):
    """插件初始化：注册请求拦截器。"""
    register_request_interceptor(
        interceptor_id="jb_grazie_request",
        callback=jb_grazie_request_interceptor,
        priority=50,
        plugin_name=PLUGIN_INFO["name"],
        metadata={"description": "注入 Grazie 请求头 + Gemini 路径改写"},
    )
    logger.info(f"[{PLUGIN_INFO['name']}] Request interceptor registered.")


def teardown(manager):
    """插件清理：注销请求拦截器。"""
    unregister_request_interceptor("jb_grazie_request")
    logger.info(f"[{PLUGIN_INFO['name']}] Request interceptor unregistered.")
