"""
Claude Tools 插件

功能：
1. 处理原生 Claude 渠道的模型后缀（-thinking[-N], -search, -code, -computer, -artifacts, -fast）
2. 自动注入 Prompt Caching（cache=5m | cache=1h）
3. 剥离 Claude Code 客户端私有指纹（strip_cc=true）：
   - 剥离顶层 system 最前面的 `x-anthropic-billing-header:...` 行（兼容字符串与数组首元素，空串自动移除元素或顶层 system）
   - 剥离请求体中的 `metadata`、`context_management` 及 `anthropic_beta` 中的私有指纹段
   - 剥离请求头中的 `x-stainless-*`、`anthropic-dangerous-direct-browser-access`、`x-app`、Claude Code `User-Agent` 及 `anthropic-beta` 中的私有段
"""

import re
from typing import Any, Dict, List, Optional, Tuple, Set

from core.log_config import logger
from core.plugins import (
    get_plugin_options,
    register_request_interceptor,
    unregister_request_interceptor,
)


# 插件元信息
PLUGIN_INFO = {
    "name": "claude_tools",
    "version": "1.2.0",
    "description": "Claude 后缀工具 + 自动缓存 + Claude Code 指纹剥离。支持 -thinking/-search/-code/-fast 后缀、cache=5m/1h 自动缓存、strip_cc=true 剥离客户端指纹。",
    "author": "Zoaholic Team",
    "dependencies": [],
    "metadata": {
        "category": "interceptors",
        "tags": ["claude", "anthropic", "thinking", "tools", "cache", "fingerprint"],
        "params_hint": "cache=5m | cache=1h（自动缓存）；strip_cc=true（剥离 Claude Code 指纹，含 billing-header/metadata/context_management/私有 headers）。",
        "params_schema": [
            {
                "key": "cache",
                "label": "Prompt Caching",
                "type": "select",
                "options": [
                    {"value": "", "label": "不启用"},
                    {"value": "5m", "label": "5 分钟 (写入1.25x, 读取0.1x)"},
                    {"value": "1h", "label": "1 小时 (写入2x, 读取0.1x)"},
                ],
                "default": "",
                "serialize": "key_value",
            },
            {
                "key": "strip_cc",
                "label": "剥离 Claude Code 指纹",
                "type": "toggle",
                "default": False,
                "serialize": "key_value",
            },
        ],
    },
}

# 声明提供的扩展
EXTENSIONS = [
    "interceptors:claude_tools_request",
]

SUPPORTED_SUFFIXES = {
    "-thinking": "thinking",
    "-search": "search",
    "-code": "code",
    "-computer": "computer",
    "-artifacts": "artifacts",
    "-fast": "fast",
}

DEFAULT_THINKING_BUDGET = 16384

THINKING_PATTERN = re.compile(r"-thinking(?:-(\d+|max|xhigh|high|medium|low))?$", re.IGNORECASE)
_VALID_EFFORTS = {"max", "xhigh", "high", "medium", "low"}

# 匹配最开头的 x-anthropic-billing-header:...（末尾可能有 \r\n、\n、\r，也可能没有换行符）
_BILLING_HEADER_FIRST_LINE_RE = re.compile(r"^\s*x-anthropic-billing-header:[^\r\n]*(?:\r?\n|\r)?")

_STRIP_HEADER_EXACT = {
    "anthropic-dangerous-direct-browser-access",
    "x-app",
}
_STRIP_HEADER_PREFIXES = ("x-stainless-", "x-claude-code-")
_STRIP_CC_BETA_PREFIXES = ("oauth-", "prompt-caching-scope-", "claude-code-")


def _parse_claude_tools_options(provider: Dict[str, Any]) -> Dict[str, Any]:
    """解析 provider 的 enabled_plugins 中针对 claude_tools 的参数（兼容 dict 与 key=value 字符串）。"""
    opts: Dict[str, Any] = {"cache": "", "strip_cc": False}
    if not isinstance(provider, dict):
        return opts
    raw = get_plugin_options("claude_tools", provider)
    if isinstance(raw, dict):
        if raw.get("cache") in ("5m", "1h"):
            opts["cache"] = raw["cache"]
        sc = raw.get("strip_cc")
        if isinstance(sc, bool):
            opts["strip_cc"] = sc
        elif isinstance(sc, str) and sc.strip().lower() in ("true", "1", "yes", "on"):
            opts["strip_cc"] = True
        return opts
    if isinstance(raw, str) and raw.strip():
        for part in re.split(r"[,:]", raw):
            part = part.strip()
            if not part:
                continue
            if part.startswith("cache="):
                val = part[6:].strip()
                if val in ("5m", "1h"):
                    opts["cache"] = val
            elif part.startswith("strip_cc="):
                val = part[9:].strip().lower()
                opts["strip_cc"] = val in ("true", "1", "yes", "on")
            elif part in ("strip_cc", "strip_fingerprint"):
                opts["strip_cc"] = True
    return opts


def _strip_billing_header_from_text(text: str) -> str:
    """剥离文本最前面的一行 x-anthropic-billing-header:...（兼容有无换行符及 \r\n / \n）。"""
    if not isinstance(text, str):
        return text
    if not text.lstrip().startswith("x-anthropic-billing-header:"):
        return text
    return _BILLING_HEADER_FIRST_LINE_RE.sub("", text, count=1)


def strip_system_billing_header(payload: Dict[str, Any]) -> None:
    """剥离顶层 system 字段最前面的 x-anthropic-billing-header:...。

    - 若 system 为字符串：剥离后若 trim 为空串，直接移除顶层 system 字段。
    - 若 system 为数组：只取第一个元素 system[0] 处理（不遍历后续元素）；
      若剥离后第一个元素的文本 trim 为空串，移除该元素；若数组因此变空，移除顶层 system 字段。
    """
    if not isinstance(payload, dict) or "system" not in payload:
        return
    system = payload.get("system")
    if isinstance(system, str):
        cleaned = _strip_billing_header_from_text(system)
        if not cleaned.strip():
            payload.pop("system", None)
        elif cleaned != system:
            payload["system"] = cleaned
    elif isinstance(system, list):
        if not system:
            payload.pop("system", None)
            return
        first = system[0]
        if isinstance(first, dict) and isinstance(first.get("text"), str):
            orig_text = first["text"]
            cleaned = _strip_billing_header_from_text(orig_text)
            if not cleaned.strip():
                system.pop(0)
            elif cleaned != orig_text:
                first["text"] = cleaned
        elif isinstance(first, str):
            cleaned = _strip_billing_header_from_text(first)
            if not cleaned.strip():
                system.pop(0)
            elif cleaned != first:
                system[0] = cleaned
        if not system:
            payload.pop("system", None)


def strip_claude_code_fingerprint(headers: Dict[str, Any], payload: Dict[str, Any], engine: str = "") -> None:
    """剥离 Claude Code 客户端的私有指纹（System billing-header + Body 字段 + Request Headers）。"""
    # 1. 剥离顶层 system 最前面的 x-anthropic-billing-header:...
    if isinstance(payload, dict):
        strip_system_billing_header(payload)
        # 2. 剥离 Body 中的客户端指纹字段
        payload.pop("metadata", None)
        payload.pop("context_management", None)
    # 3. 剥离 Headers 中的通用 Claude Code 客户端私有指纹（不含渠道特定 beta 过滤）
    if isinstance(headers, dict):
        for k in list(headers.keys()):
            kl = k.lower()
            if kl in _STRIP_HEADER_EXACT or kl.startswith(_STRIP_HEADER_PREFIXES):
                del headers[k]
                continue
            if kl == "user-agent":
                ua_val = str(headers.get(k) or "").lower()
                if any(fp in ua_val for fp in ("claude-cli", "anthropic", "claude-code")):
                    del headers[k]
                continue
            if kl == "anthropic-beta":
                raw_beta = str(headers.get(k) or "")
                segments = [
                    seg.strip() for seg in raw_beta.split(",")
                    if seg.strip() and not seg.strip().startswith(_STRIP_CC_BETA_PREFIXES)
                ]
                if segments:
                    headers[k] = ",".join(segments)
                else:
                    del headers[k]


def parse_model_suffixes(model: str) -> Tuple[str, Set[str], Optional[int]]:
    if not isinstance(model, str):
        return model, set(), None

    enabled_features: Set[str] = set()
    thinking_budget: Optional[int] = None
    remaining = model

    found = True
    while found:
        found = False

        thinking_match = THINKING_PATTERN.search(remaining)
        if thinking_match:
            enabled_features.add("thinking")
            param = thinking_match.group(1)
            if param and param.lower() in _VALID_EFFORTS:
                thinking_budget = -1
                enabled_features.add(f"effort:{param.lower()}")
            elif param:
                thinking_budget = int(param)
            else:
                thinking_budget = DEFAULT_THINKING_BUDGET
            remaining = remaining[:thinking_match.start()]
            found = True
            continue

        for suffix, feature in SUPPORTED_SUFFIXES.items():
            if suffix == "-thinking":
                continue
            if remaining.lower().endswith(suffix):
                enabled_features.add(feature)
                remaining = remaining[:-len(suffix)]
                found = True
                break

    return remaining, enabled_features, thinking_budget


def is_claude_engine(engine: str) -> bool:
    if not isinstance(engine, str):
        return False
    engine_lower = engine.lower()
    if engine_lower in ("claude", "anthropic", "aws", "vertex-claude"):
        return True
    try:
        from core.channels.registry import get_channel
        ch = get_channel(engine_lower)
        if ch and "claude" in ch.type_name.lower():
            return True
    except Exception:
        pass
    return False


def _needs_legacy_thinking(model: str) -> bool:
    model_lower = model.lower() if model else ""
    return "claude-3" in model_lower


def apply_thinking_config(payload: Dict[str, Any], budget_tokens: int, model: str = "") -> None:
    if not _needs_legacy_thinking(model):
        payload["thinking"] = {"type": "adaptive"}
        effort = "max"
        features = payload.pop("_thinking_features", None) or set()
        for f in features:
            if f.startswith("effort:"):
                effort = f.split(":", 1)[1]
                break
        payload.setdefault("output_config", {})["effort"] = effort
        logger.debug(f"[claude_tools] Applied adaptive thinking: effort={effort}")
    else:
        payload["thinking"] = {
            "type": "enabled",
            "budget_tokens": budget_tokens
        }
        logger.debug(f"[claude_tools] Applied thinking config: budget_tokens={budget_tokens}")

    model_l = (model or "").lower()
    if not any(k in model_l for k in ("-5", "4-7", "4-8", "4.7", "4.8")):
        payload["temperature"] = 1
    else:
        payload.pop("temperature", None)
    payload.pop("top_p", None)
    payload.pop("top_k", None)

    # 后缀开启 thinking 发生在 payload 构建之后，这里需要再处理一次 tool_choice
    from core.channels.claude_channel import downgrade_tool_choice_for_thinking
    downgrade_tool_choice_for_thinking(payload)


def apply_tool_config(payload: Dict[str, Any], tool_type: str) -> None:
    if "tools" not in payload:
        payload["tools"] = []

    tool_mapping = {
        "search": {
            "type": "web_search_20260209",
            "name": "web_search",
            "max_uses": 5,
        },
        "code": {
            "type": "code_execution_20250522",
            "name": "code_execution",
        },
        "computer": {
            "type": "computer_20250124",
            "name": "computer",
            "display_width_px": 1024,
            "display_height_px": 768,
            "display_number": 1,
        },
        "artifacts": {
            "type": "text_editor_20250429",
            "name": "str_replace_based_edit_tool",
        },
    }

    tool_config = tool_mapping.get(tool_type)
    if tool_config:
        existing_types = {t.get("type") for t in payload["tools"] if isinstance(t, dict)}
        existing_names = {t.get("name") for t in payload["tools"] if isinstance(t, dict)}
        if tool_config["type"] not in existing_types and tool_config.get("name") not in existing_names:
            payload["tools"].append(tool_config.copy())
            logger.debug(f"[claude_tools] Added server tool: {tool_config['type']}")


def _has_cache_control(payload: Dict[str, Any]) -> bool:
    if "cache_control" in payload:
        return True
    system = payload.get("system")
    if isinstance(system, list):
        for block in system:
            if isinstance(block, dict) and "cache_control" in block:
                return True
    for msg in payload.get("messages", []):
        if isinstance(msg, dict):
            if "cache_control" in msg:
                return True
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and "cache_control" in block:
                        return True
    for tool in payload.get("tools", []):
        if isinstance(tool, dict) and "cache_control" in tool:
            return True
    return False


def _inject_auto_cache(payload: Dict[str, Any], ttl: str) -> None:
    """自动注入顶层 cache_control，走 Anthropic 自动缓存。客户端自带则跳过。"""
    if not ttl or _has_cache_control(payload):
        return
    cc = {"type": "ephemeral"}
    if ttl == "1h":
        cc["ttl"] = "1h"
    payload["cache_control"] = cc
    logger.debug(f"[claude_tools] Injected auto cache_control: {cc}")


def update_anthropic_beta_header(headers: Dict[str, Any], features: Set[str]) -> None:
    beta_features = []
    existing_beta = headers.get("anthropic-beta", "")
    if existing_beta:
        beta_features.extend(existing_beta.split(","))

    feature_beta_mapping = {
        "thinking": "interleaved-thinking-2025-05-14",
        "code": "code-execution-2025-05-22",
        "computer": "computer-use-2025-01-24",
        "fast": "fast-mode-2026-02-01",
    }

    for feature in features:
        beta = feature_beta_mapping.get(feature)
        if beta and beta not in beta_features:
            beta_features.append(beta)

    if beta_features:
        headers["anthropic-beta"] = ",".join(beta_features)


def _extract_model_from_url(url: str) -> str:
    """从 Bedrock (.../model/{id}/invoke...) 或 Vertex (.../models/{id}:...) URL 提取模型名。"""
    if "/model/" in url:
        try:
            return url.split("/model/")[1].split("/")[0]
        except (IndexError, ValueError):
            return ""
    if "/models/" in url:
        try:
            return url.split("/models/")[1].split(":")[0].split("/")[0]
        except (IndexError, ValueError):
            return ""
    return ""


def _replace_model_in_url(url: str, old: str, new: str) -> str:
    if old and new and old != new and old in url:
        return url.replace(old, new, 1)
    return url


async def claude_tools_request_interceptor(
    request: Any,
    engine: str,
    provider: Dict[str, Any],
    api_key: Optional[str],
    url: str,
    headers: Dict[str, Any],
    payload: Dict[str, Any],
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    if not is_claude_engine(engine):
        return url, headers, payload

    opts = _parse_claude_tools_options(provider)
    engine_l = (engine or "").lower()

    # ── 0. 指纹剥离与自动缓存（不依赖模型后缀，无后缀模型也必须执行） ──
    if opts["strip_cc"]:
        strip_claude_code_fingerprint(headers, payload, engine=engine_l)

    if opts["cache"] and engine_l != "vertex-claude":
        _inject_auto_cache(payload, opts["cache"])

    # ── 1. 确定 model 来源 ──
    is_url_model = engine_l in ("aws", "vertex-claude")
    url_model = _extract_model_from_url(url) if is_url_model else ""
    model = url_model if is_url_model else (payload.get("model", "") if isinstance(payload, dict) else "")
    if not model and request is not None:
        model = str(getattr(request, "model", "") or "")

    if not model:
        return url, headers, payload

    # ── 2. 解析后缀 ──
    base_model, features, thinking_budget = parse_model_suffixes(model)
    if not features:
        return url, headers, payload

    logger.info(f"[claude_tools] model={model}, features={features}, engine={engine}")

    # ── 3. 剥后缀：payload 和 URL 各管各的 ──
    if is_url_model:
        url = _replace_model_in_url(url, url_model, base_model)
    elif isinstance(payload, dict) and "model" in payload:
        payload["model"] = base_model

    # ── 4. 注入功能配置 ──
    if "thinking" in features and thinking_budget and "thinking" not in payload:
        payload["_thinking_features"] = features
        apply_thinking_config(payload, thinking_budget, model=base_model)

    if "fast" in features:
        payload["speed"] = "fast"

    for feature in features:
        if feature not in ("thinking", "fast") and not feature.startswith("effort:"):
            apply_tool_config(payload, feature)

    if engine_l != "vertex-claude":
        update_anthropic_beta_header(headers, features)

    return url, headers, payload


def setup(manager):
    logger.info(f"[{PLUGIN_INFO['name']}] 正在初始化...")
    register_request_interceptor(
        interceptor_id="claude_tools_request",
        callback=claude_tools_request_interceptor,
        priority=45,
        plugin_name=PLUGIN_INFO["name"],
        metadata={"description": "Claude 工具后缀 + 自动缓存 + 指纹剥离请求处理"},
        overwrite=True,
    )
    logger.info(f"[{PLUGIN_INFO['name']}] 已注册请求拦截器")


def teardown(manager):
    logger.info(f"[{PLUGIN_INFO['name']}] 正在清理...")
    unregister_request_interceptor("claude_tools_request")
    logger.info(f"[{PLUGIN_INFO['name']}] 已清理完成")


def unload():
    logger.debug(f"[{PLUGIN_INFO['name']}] 模块即将卸载")
