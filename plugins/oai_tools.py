"""
OpenAI Tools 插件

统一处理 OpenAI 系模型名后缀，自动注入对应的 API 参数。

支持的后缀（可组合使用）：
- Reasoning effort:
  -high, -medium, -low, -minimal, -none, -xhigh
  → 自动设置 reasoning_effort / reasoning.effort

- Image generation:
  -image
  → 自动注入 {type: "image_generation"} tool（仅 openai-responses 引擎）

组合示例：
- gpt-4o-image        → gpt-4o + image_generation tool
- gpt-5-high          → gpt-5 + reasoning_effort=high
- gpt-4o-image-high   → gpt-4o + image_generation tool + reasoning_effort=high
- o3-low              → o3 + reasoning_effort=low

注意：-image 后缀仅对 openai-responses 引擎生效（Responses API 才支持 image_generation tool）。
对其他引擎的 -image 后缀会被忽略并记录警告。
"""

from typing import Any, Dict, List, Optional, Set, Tuple

from core.log_config import logger
from core.plugins import (
    register_request_interceptor,
    unregister_request_interceptor,
)


# 插件元信息
PLUGIN_INFO = {
    "name": "oai_tools",
    "version": "2.0.0",
    "description": "OpenAI 后缀工具插件 — 在模型名后追加 -high/-medium/-low/-image 等后缀，自动设置 reasoning_effort 或注入 image_generation tool。后缀可组合，如 gpt-4o-image-high。注意: -image 仅对 openai-responses 引擎生效。",
    "author": "Zoaholic Team",
    "dependencies": [],
    "metadata": {
        "category": "interceptors",
        "tags": ["openai", "reasoning", "image", "tools", "gpt-5"],
        "params_hint": "无需参数。后缀直接写在模型名后: -high / -medium / -low / -minimal / -none / -xhigh / -image",
    },
}

# 声明提供的扩展
EXTENSIONS = [
    "interceptors:oai_tools_request",
]

# 支持的引擎
SUPPORTED_ENGINES = {"openai", "azure", "openrouter", "openai-responses"}

# Reasoning effort 后缀 → effort 值
REASONING_EFFORT_SUFFIXES = {
    "-high": "high",
    "-medium": "medium",
    "-low": "low",
    "-minimal": "minimal",
    "-none": "none",
    "-xhigh": "xhigh",
}

# Tool 后缀 → tool 注入配置
TOOL_SUFFIXES = {
    "-image": {
        "tool": {"type": "image_generation"},
        "engines": {"openai-responses"},  # 仅 Responses API 支持
    },
}


# ============================================================
# 后缀解析
# ============================================================


def parse_oai_suffixes(model: str) -> Tuple[str, Optional[str], Set[str]]:
    """
    解析模型名中的所有 OAI 后缀（从右到左）。

    Returns:
        (base_model, reasoning_effort, tool_features)
        - base_model: 去除所有后缀后的模型名
        - reasoning_effort: reasoning effort 值（如 "high"），没有则 None
        - tool_features: 启用的 tool 功能集合（如 {"image"}）
    """
    if not isinstance(model, str):
        return model, None, set()

    remaining = model
    reasoning_effort: Optional[str] = None
    tool_features: Set[str] = set()

    # 从右向左循环剥离后缀
    found = True
    while found:
        found = False
        remaining_lower = remaining.lower()

        # 检查 reasoning effort 后缀
        for suffix, effort in REASONING_EFFORT_SUFFIXES.items():
            if remaining_lower.endswith(suffix):
                reasoning_effort = effort
                remaining = remaining[:-len(suffix)]
                found = True
                break

        if found:
            continue

        # 检查 tool 后缀
        for suffix, config in TOOL_SUFFIXES.items():
            if remaining_lower.endswith(suffix):
                # 提取 feature 名（去掉开头的 -）
                feature_name = suffix.lstrip("-")
                tool_features.add(feature_name)
                remaining = remaining[:-len(suffix)]
                found = True
                break

    return remaining, reasoning_effort, tool_features


# ============================================================
# 参数设置
# ============================================================


def set_reasoning_parameters(payload: Dict[str, Any], effort: str, engine: str) -> None:
    """
    设置 reasoning 相关参数。

    根据不同引擎设置不同格式：
    - openai-responses: 只设置 reasoning 对象
    - openai/azure/openrouter: 设置 reasoning_effort 和兼容格式
    """
    # Responses API 格式
    if "reasoning" not in payload or not isinstance(payload.get("reasoning"), dict):
        payload["reasoning"] = {}

    reasoning = payload["reasoning"]
    reasoning["effort"] = effort

    if "summary" not in reasoning:
        reasoning["summary"] = "auto"

    # OpenAI Responses API 只支持 reasoning 对象格式
    if engine.lower() == "openai-responses":
        return

    # Chat Completions API 格式
    payload["reasoning_effort"] = effort
    payload["reasoningEffort"] = effort

    if "reasoning_summary" not in payload:
        payload["reasoning_summary"] = "auto"


def inject_image_generation_tool(payload: Dict[str, Any]) -> None:
    """
    注入 image_generation tool 到 Responses API payload。

    Responses API 的 image_generation tool 格式：
    {"type": "image_generation"}

    可选参数（通过 overrides 或用户传参设置）：
    - background: "transparent" | "opaque" | "auto"
    - input_image_mask: object
    - quality: "low" | "medium" | "high" | "auto"
    - size: "auto" | "1024x1024" | ...
    """
    if "tools" not in payload:
        payload["tools"] = []

    # 检查是否已有 image_generation tool
    for tool in payload["tools"]:
        if isinstance(tool, dict) and tool.get("type") == "image_generation":
            return  # 已存在，不重复添加

    payload["tools"].append({"type": "image_generation"})


# ============================================================
# 请求拦截器
# ============================================================


async def oai_tools_request_interceptor(
    request: Any,
    engine: str,
    provider: Dict[str, Any],
    api_key: Optional[str],
    url: str,
    headers: Dict[str, Any],
    payload: Dict[str, Any],
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """
    OpenAI Tools 请求拦截器

    统一处理 -high/-medium/-low/-image 等后缀
    """
    model = payload.get("model", "")

    if not isinstance(model, str):
        return url, headers, payload

    # 早期退出：不是支持的引擎
    if engine.lower() not in SUPPORTED_ENGINES:
        return url, headers, payload

    # 解析后缀
    base_model, reasoning_effort, tool_features = parse_oai_suffixes(model)

    # 早期退出：没有识别到任何后缀
    if reasoning_effort is None and not tool_features:
        return url, headers, payload

    logger.info(
        f"[oai_tools] Processing model: {model}, "
        f"reasoning_effort={reasoning_effort}, tools={tool_features}"
    )

    # 更新模型名（去除所有后缀）
    payload["model"] = base_model

    # 应用 reasoning effort
    if reasoning_effort is not None:
        set_reasoning_parameters(payload, reasoning_effort, engine)

    # 应用 tool 注入
    for feature in tool_features:
        suffix_key = f"-{feature}"
        tool_config = TOOL_SUFFIXES.get(suffix_key)
        if not tool_config:
            continue

        allowed_engines = tool_config.get("engines", SUPPORTED_ENGINES)
        if engine.lower() not in allowed_engines:
            logger.warning(
                f"[oai_tools] -{feature} suffix only supported on engines: "
                f"{allowed_engines}, current engine: {engine}. Ignoring."
            )
            continue

        if feature == "image":
            inject_image_generation_tool(payload)

    logger.debug(
        f"[oai_tools] Modified payload: model={payload['model']}, "
        f"reasoning={payload.get('reasoning')}, "
        f"tools={[t.get('type') for t in payload.get('tools', []) if isinstance(t, dict)]}"
    )

    return url, headers, payload


# ============================================================
# 插件生命周期
# ============================================================


def setup(manager):
    """
    插件初始化
    """
    logger.info(f"[{PLUGIN_INFO['name']}] 正在初始化...")

    # 注册请求拦截器
    register_request_interceptor(
        interceptor_id="oai_tools_request",
        callback=oai_tools_request_interceptor,
        priority=50,  # 与原 oai_reasoning 相同优先级
        plugin_name=PLUGIN_INFO["name"],
        metadata={"description": "OpenAI 后缀处理（reasoning effort + image generation）"},
    )

    logger.info(f"[{PLUGIN_INFO['name']}] 已注册请求拦截器")


def teardown(manager):
    """
    插件清理
    """
    logger.info(f"[{PLUGIN_INFO['name']}] 正在清理...")

    unregister_request_interceptor("oai_tools_request")

    logger.info(f"[{PLUGIN_INFO['name']}] 已清理完成")


def unload():
    """
    插件卸载回调
    """
    logger.debug(f"[{PLUGIN_INFO['name']}] 模块即将卸载")
