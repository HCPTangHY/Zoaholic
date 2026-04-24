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

使用方式：
- claude-sonnet-4-thinking → 启用思考模式
- claude-sonnet-4-thinking-32768 → 启用思考模式，budget=32768
- claude-sonnet-4-search → 启用搜索
- claude-sonnet-4-thinking-search → 同时启用思考和搜索
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
    "version": "1.0.0",
    "description": "Claude 后缀工具插件 — 在模型名后追加 -thinking/-search/-code/-computer/-artifacts 等后缀，自动注入对应的原生 Claude API 参数。后缀可自由组合，如 claude-sonnet-4-thinking-search。",
    "author": "Zoaholic Team",
    "dependencies": [],
    "metadata": {
        "category": "interceptors",
        "tags": ["claude", "anthropic", "thinking", "tools"],
        "params_hint": "无需参数。后缀直接写在模型名后: -thinking[-N] / -search / -code / -computer / -artifacts",
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
}

# 默认的 thinking budget tokens
DEFAULT_THINKING_BUDGET = 16384

# thinking 后缀的正则（支持 -thinking 和 -thinking-N 格式）
THINKING_PATTERN = re.compile(r"-thinking(?:-(\d+))?$", re.IGNORECASE)


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
            if thinking_match.group(1):
                thinking_budget = int(thinking_match.group(1))
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
    检查是否为 Claude 引擎

    Args:
        engine: 引擎类型

    Returns:
        是否为 Claude 引擎
    """
    if not isinstance(engine, str):
        return False

    claude_engines = {"claude", "anthropic", "vertex-claude", "aws"}
    return engine.lower() in claude_engines


def apply_thinking_config(payload: Dict[str, Any], budget_tokens: int) -> None:
    """
    应用 thinking 配置到 payload

    Claude 原生 thinking 格式：
    {
        "thinking": {
            "type": "enabled",
            "budget_tokens": 10240
        }
    }

    Args:
        payload: 请求 payload
        budget_tokens: thinking budget tokens
    """
    payload["thinking"] = {
        "type": "enabled",
        "budget_tokens": budget_tokens
    }

    # thinking 模式要求 temperature=1，且不能有 top_p/top_k
    payload["temperature"] = 1
    payload.pop("top_p", None)
    payload.pop("top_k", None)

    logger.debug(f"[claude_tools] Applied thinking config: budget_tokens={budget_tokens}")


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
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 5,  # 限制每次请求最多搜索次数
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
        # 检查是否已存在相同类型的工具
        existing_types = {t.get("type") for t in payload["tools"] if isinstance(t, dict)}
        if tool_config["type"] not in existing_types:
            payload["tools"].append(tool_config.copy())
            logger.debug(f"[claude_tools] Added server tool: {tool_config['type']}")


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
    }

    for feature in features:
        beta = feature_beta_mapping.get(feature)
        if beta and beta not in beta_features:
            beta_features.append(beta)

    if beta_features:
        headers["anthropic-beta"] = ",".join(beta_features)


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

    处理 -thinking/-search/-code 等后缀的模型请求
    """
    model = payload.get("model", "")

    # 早期退出：不是 Claude 引擎
    if not is_claude_engine(engine):
        return url, headers, payload

    # 解析后缀
    base_model, features, thinking_budget = parse_model_suffixes(model)

    # 早期退出：没有识别到任何后缀
    if not features:
        return url, headers, payload

    logger.info(f"[claude_tools] Processing model: {model}, features: {features}")

    # 更新模型名（去除后缀）
    payload["model"] = base_model

    # 应用 thinking 配置
    if "thinking" in features and thinking_budget:
        apply_thinking_config(payload, thinking_budget)

    # 应用工具配置
    for feature in features:
        if feature != "thinking":
            apply_tool_config(payload, feature)

    # 更新 anthropic-beta header
    update_anthropic_beta_header(headers, features)

    logger.debug(f"[claude_tools] Modified payload model: {payload['model']}, "
                 f"thinking: {'thinking' in features}, tools: {payload.get('tools', [])}")

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
