"""
Claude Tools 插件

功能：
1. 处理原生 Claude 渠道的模型后缀
2. 支持 -thinking[-N], -search, -code 等后缀
3. 自动设置对应的 Claude API 参数

支持的后缀：
- -thinking: 启用 extended thinking 模式（默认 budget_tokens=16384）
- -thinking-N: 启用 extended thinking 模式，指定 budget_tokens=N
- -search: 启用 web_search 工具
- -code: 启用 code_execution 工具
- -computer: 启用 computer_use 工具（beta）
- -artifacts: 启用 artifacts 工具
- -fast: 启用 fast mode（speed=fast + beta header）

使用方式：
- claude-sonnet-4-thinking → 启用思考模式
- claude-sonnet-4-thinking-32768 → 启用思考模式，budget=32768
- claude-sonnet-4-search → 启用搜索
- claude-sonnet-4-thinking-search → 同时启用思考和搜索
- claude-opus-4-fast → 启用 fast mode
- claude-sonnet-4-thinking-fast → 思考 + fast mode
"""

import re
from typing import Any, Dict, List, Optional, Tuple, Set

from core.log_config import logger
from core.plugins import (
    register_request_interceptor,
    unregister_request_interceptor,
)


# 插件元信息
PLUGIN_INFO = {
    "name": "claude_tools",
    "version": "1.1.0",
    "description": "Claude 后缀工具 + 自动缓存。模型名后缀 -thinking/-search/-code/-fast 等自动注入 API 参数；cache 参数自动注入 prompt caching（客户端已带则跳过）。",
    "author": "Zoaholic Team",
    "dependencies": [],
    "metadata": {
        "category": "interceptors",
        "tags": ["claude", "anthropic", "thinking", "tools", "cache"],
        "params_hint": "cache=5m | cache=1h（自动注入 prompt caching，客户端自带则跳过）。模型后缀无需在此配置，直接写在模型名后即可。",
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
            },
        ],
    },
}

# 声明提供的扩展
EXTENSIONS = [
    "interceptors:claude_tools_request",
]

# 支持的后缀及其处理器
SUPPORTED_SUFFIXES = {
    "-thinking": "thinking",
    "-search": "search",
    "-code": "code",
    "-computer": "computer",
    "-artifacts": "artifacts",
    "-fast": "fast",
}

# 默认的 thinking budget tokens
DEFAULT_THINKING_BUDGET = 16384

# thinking 后缀正则（支持 -thinking / -thinking-N / -thinking-{effort} 格式）
# effort 级别: max, xhigh, high, medium, low
THINKING_PATTERN = re.compile(r"-thinking(?:-(\d+|max|xhigh|high|medium|low))?$", re.IGNORECASE)
_VALID_EFFORTS = {"max", "xhigh", "high", "medium", "low"}


def parse_model_suffixes(model: str) -> Tuple[str, Set[str], Optional[int]]:
    """
    解析模型名称中的后缀

    Args:
        model: 原始模型名称

    Returns:
        Tuple[base_model, enabled_features, thinking_budget]
        - base_model: 去除后缀后的基础模型名
        - enabled_features: 启用的功能集合 {"thinking", "search", "code", ...}
        - thinking_budget: thinking 的 budget_tokens（仅当启用 thinking 时）
    """
    if not isinstance(model, str):
        return model, set(), None

    enabled_features: Set[str] = set()
    thinking_budget: Optional[int] = None
    remaining = model

    # 循环检测所有后缀（从右到左）
    found = True
    while found:
        found = False

        # 首先检查 thinking 后缀（因为它可能带数字）
        thinking_match = THINKING_PATTERN.search(remaining)
        if thinking_match:
            enabled_features.add("thinking")
            param = thinking_match.group(1)
            if param and param.lower() in _VALID_EFFORTS:
                # effort 级别名 — 存负数作为标记，apply 时识别
                thinking_budget = -1  # 哨兵值，表示用 effort 名
                enabled_features.add(f"effort:{param.lower()}")
            elif param:
                thinking_budget = int(param)
            else:
                thinking_budget = DEFAULT_THINKING_BUDGET
            # 移除 thinking 后缀
            remaining = remaining[:thinking_match.start()]
            found = True
            continue

        # 检查其他后缀
        for suffix, feature in SUPPORTED_SUFFIXES.items():
            if suffix == "-thinking":
                continue  # thinking 已经单独处理
            if remaining.lower().endswith(suffix):
                enabled_features.add(feature)
                remaining = remaining[:-len(suffix)]
                found = True
                break

    return remaining, enabled_features, thinking_budget


def is_claude_engine(engine: str) -> bool:
    """
    检查是否为 Claude 引擎。
    通过渠道注册表的 type_name 动态判断，不再硬编码白名单。
    AWS Bedrock 跑的也是 Claude，需要识别。
    """
    if not isinstance(engine, str):
        return False
    engine_lower = engine.lower()
    # 直接匹配
    if engine_lower in ("claude", "anthropic", "aws"):
        return True
    # 查注册表：type_name 含 "claude" 即视为 Claude 系
    try:
        from core.channels.registry import get_channel
        ch = get_channel(engine_lower)
        if ch and "claude" in ch.type_name.lower():
            return True
    except Exception:
        pass
    return False


def _needs_legacy_thinking(model: str) -> bool:
    """判断模型是否只支持旧版 enabled + budget_tokens 格式。
    
    Claude 3.x 系列只支持 type: enabled。
    4.x 及以后全部支持 adaptive，直接用 adaptive + effort。
    """
    model_lower = model.lower() if model else ""
    return "claude-3" in model_lower


def apply_thinking_config(payload: Dict[str, Any], budget_tokens: int, model: str = "") -> None:
    """
    应用 thinking 配置到 payload

    Claude 4.x+: thinking.type = "adaptive" + output_config.effort
    Claude 3.x: thinking.type = "enabled" + budget_tokens

    Args:
        payload: 请求 payload
        budget_tokens: thinking budget tokens
        model: 模型名，用于判断用哪种格式
    """
    if not _needs_legacy_thinking(model):
        # Claude 4.x+ 统一用 adaptive + effort
        payload["thinking"] = {"type": "adaptive"}
        # 从 features 里找 effort 级别，没有则默认 max
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

    # thinking 模式要求 temperature=1，且不能有 top_p/top_k
    payload["temperature"] = 1
    payload.pop("top_p", None)
    payload.pop("top_k", None)


def apply_tool_config(payload: Dict[str, Any], tool_type: str) -> None:
    """
    应用工具配置到 payload

    Claude 服务器端工具格式（server_tool_use）：
    {
        "tools": [
            {"type": "web_search_20250305", "name": "web_search", "max_uses": 5},
            {"type": "code_execution_20250522", "name": "code_execution"},
            ...
        ]
    }

    这些是服务器端工具，Claude API 会自动执行，不会返回 tool_calls 给客户端。

    Args:
        payload: 请求 payload
        tool_type: 工具类型 (search/code/computer/artifacts)
    """
    if "tools" not in payload:
        payload["tools"] = []

    # Claude 服务器端工具配置
    # 注意：type 必须包含版本日期后缀
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
        # 检查是否已存在相同 type 或 name 的工具（Anthropic 要求 name 唯一）
        existing_types = {t.get("type") for t in payload["tools"] if isinstance(t, dict)}
        existing_names = {t.get("name") for t in payload["tools"] if isinstance(t, dict)}
        if tool_config["type"] not in existing_types and tool_config.get("name") not in existing_names:
            payload["tools"].append(tool_config.copy())
            logger.debug(f"[claude_tools] Added server tool: {tool_config['type']}")

        # web_search_20260209 内置 dynamic filtering，API 会自动注入 code_execution
        # 不需要也不应该手动添加，否则与 API 自动注入的或客户端已有的冲突
        # 参考：https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-reference


def _has_cache_control(payload: Dict[str, Any]) -> bool:
    """检测请求体里是否已有 cache_control（客户端自己管缓存）。"""
    # 顶层
    if "cache_control" in payload:
        return True
    # system 里
    system = payload.get("system")
    if isinstance(system, list):
        for block in system:
            if isinstance(block, dict) and "cache_control" in block:
                return True
    # messages 里
    for msg in payload.get("messages", []):
        if isinstance(msg, dict):
            if "cache_control" in msg:
                return True
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and "cache_control" in block:
                        return True
    # tools 里
    for tool in payload.get("tools", []):
        if isinstance(tool, dict) and "cache_control" in tool:
            return True
    return False


def _inject_auto_cache(payload: Dict[str, Any], provider: Dict[str, Any]) -> None:
    """自动注入顶层 cache_control（Anthropic 自动滚动缓存）+ system 尾块锚点。客户端自带则跳过。"""
    if _has_cache_control(payload):
        return
    # 从 provider preferences 的 enabled_plugins 参数读 TTL
    # 格式：claude_tools:cache=1h 或 claude_tools:cache=5m（兼容 : 和 , 分隔）
    ttl = None
    for plugin_entry in provider.get("preferences", {}).get("enabled_plugins", []):
        if isinstance(plugin_entry, str) and plugin_entry.startswith("claude_tools"):
            for part in re.split(r"[:;,]", plugin_entry)[1:]:
                part = part.strip()
                if part.startswith("cache="):
                    ttl = part[6:].strip()  # "1h" 或 "5m"
                    break
            break
    if not ttl:
        return  # 没配 cache 参数，不注入
    cc = {"type": "ephemeral"}
    if ttl == "1h":
        cc["ttl"] = "1h"
    # 1. 固定锚点：在 system 最后一个非空文本块打上 cache_control，确保跨会话/压缩后 system+tools 固定命中
    system = payload.get("system")
    if isinstance(system, list) and system:
        last_sys = system[-1]
        if isinstance(last_sys, dict) and last_sys.get("type") == "text" and last_sys.get("text"):
            last_sys["cache_control"] = dict(cc)
    # 2. 滚动锚点：顶层 cache_control，Anthropic 自动将断点挂在最后一块可缓存消息并随轮次前移
    payload["cache_control"] = dict(cc)
    logger.debug(f"[claude_tools] Injected auto cache_control: {cc}")


def update_anthropic_beta_header(headers: Dict[str, Any], features: Set[str]) -> None:
    """
    更新 anthropic-beta header 以启用相应功能

    注意：web_search 是正式功能，不需要 beta header

    Args:
        headers: 请求头
        features: 启用的功能集合
    """
    beta_features = []

    # 现有的 beta header
    existing_beta = headers.get("anthropic-beta", "")
    if existing_beta:
        beta_features.extend(existing_beta.split(","))

    # 根据功能添加 beta features
    # 注意：web_search 已是正式功能，不需要 beta header
    feature_beta_mapping = {
        "thinking": "interleaved-thinking-2025-05-14",
        # "search": 不需要 beta header，已是正式功能
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


# ==================== 辅助：模型名提取 ====================

def _extract_model_from_url(url: str) -> str:
    """从 Bedrock 风格 URL 提取模型名: .../model/{model_id}/invoke..."""
    if "/model/" not in url:
        return ""
    try:
        return url.split("/model/")[1].split("/")[0]
    except (IndexError, ValueError):
        return ""


def _replace_model_in_url(url: str, old: str, new: str) -> str:
    """替换 URL 中的模型名段"""
    if old and new and old != new and old in url:
        return url.replace(old, new, 1)
    return url


# ==================== 请求拦截器 ====================

async def claude_tools_request_interceptor(
    request: Any,
    engine: str,
    provider: Dict[str, Any],
    api_key: Optional[str],
    url: str,
    headers: Dict[str, Any],
    payload: Dict[str, Any],
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """
    Claude Tools 请求拦截器

    处理 -thinking/-search/-code 等后缀的模型请求。
    支持两种渠道格式：
      - 标准 Claude：model 在 payload 里
      - AWS Bedrock：model 在 URL 路径里（payload 不含 model 字段）
    """
    if not is_claude_engine(engine):
        return url, headers, payload

    # ── 1. 确定 model 来源 ──
    is_aws = (engine.lower() == "aws")
    url_model = _extract_model_from_url(url) if is_aws else ""

    if is_aws:
        # AWS: payload 没有 model 字段，从 URL 取
        model = url_model
    else:
        # 标准 Claude: 从 payload 取
        model = payload.get("model", "")

    if not model:
        return url, headers, payload

    # ── 2. 解析后缀 ──
    base_model, features, thinking_budget = parse_model_suffixes(model)
    if not features:
        # 修复：无后缀模型（如 claude-opus-5-5 / claude-opus-4-6）也必须执行自动 prompt caching 注入
        _inject_auto_cache(payload, provider)
        return url, headers, payload

    logger.info(f"[claude_tools] model={model}, features={features}, engine={engine}")

    # ── 3. 剥后缀：payload 和 URL 各管各的 ──
    if is_aws:
        # AWS: 只改 URL 里的模型名，payload 没有 model 字段
        url = _replace_model_in_url(url, url_model, base_model)
    else:
        # 标准 Claude: 改 payload 里的 model
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

    update_anthropic_beta_header(headers, features)

    # ── 5. 自动 prompt caching ──
    _inject_auto_cache(payload, provider)

    return url, headers, payload


# ==================== 插件生命周期 ====================

def setup(manager):
    """
    插件初始化
    """
    logger.info(f"[{PLUGIN_INFO['name']}] 正在初始化...")

    # 注册请求拦截器
    register_request_interceptor(
        interceptor_id="claude_tools_request",
        callback=claude_tools_request_interceptor,
        priority=45,  # 比 claude_thinking 优先级稍高
        plugin_name=PLUGIN_INFO["name"],
        metadata={"description": "Claude 工具后缀请求处理"},
    )

    logger.info(f"[{PLUGIN_INFO['name']}] 已注册请求拦截器")


def teardown(manager):
    """
    插件清理
    """
    logger.info(f"[{PLUGIN_INFO['name']}] 正在清理...")

    # 注销拦截器
    unregister_request_interceptor("claude_tools_request")

    logger.info(f"[{PLUGIN_INFO['name']}] 已清理完成")


def unload():
    """
    插件卸载回调
    """
    logger.debug(f"[{PLUGIN_INFO['name']}] 模块即将卸载")
