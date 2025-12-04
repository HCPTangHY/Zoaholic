"""
OpenAI Reasoning 模式插件

功能：
1. 识别以 -high, -medium, -low, -minimal 结尾的模型名称
2. 自动设置 OpenAI reasoning_effort 和相关参数
3. 去掉后缀并发送请求

使用方式：
- 请求模型名为 gpt-5-high 时自动设置 reasoning_effort="high"
- 请求模型名为 o1-preview-medium 时自动设置 reasoning_effort="medium"
- 会自动去掉后缀，添加 reasoning 参数
"""

from typing import Any, Dict, Optional, Tuple

from core.log_config import logger
from core.plugins import (
    register_request_interceptor,
    unregister_request_interceptor,
)


# 插件元信息
PLUGIN_INFO = {
    "name": "oai_reasoning",
    "version": "1.0.0",
    "description": "OpenAI Reasoning 模式插件 - 支持 -high/-medium/-low/-minimal 后缀模型的思考参数设置",
    "author": "Zoaholic Team",
    "dependencies": [],
    "metadata": {
        "category": "interceptors",
        "tags": ["openai", "reasoning", "gpt-5"],
    },
}

# 声明提供的扩展
EXTENSIONS = [
    "interceptors:oai_reasoning_request",
]

# 支持的 reasoning effort 级别
REASONING_EFFORT_LEVELS = {
    "-high": "high",
    "-medium": "medium",
    "-low": "low",
    "-minimal": "minimal",
}


def get_reasoning_effort_suffix(model: Any) -> Optional[str]:
    """
    检查模型名是否有 reasoning effort 后缀
    
    Args:
        model: 模型名称
        
    Returns:
        匹配到的后缀，如 "-high"，未匹配返回 None
    """
    if not isinstance(model, str):
        return None
    
    model_lower = model.lower()
    for suffix in REASONING_EFFORT_LEVELS.keys():
        if model_lower.endswith(suffix):
            return suffix
    
    return None


def is_openai_reasoning_model(model: Any, engine: str) -> bool:
    """
    检查是否为需要处理的 OpenAI reasoning 模型
    
    支持的引擎：openai, azure, openrouter
    
    Args:
        model: 模型名称
        engine: 引擎类型
        
    Returns:
        是否需要处理
    """
    if not isinstance(model, str):
        return False
    
    # 只处理 OpenAI 兼容的引擎
    supported_engines = {"openai", "azure", "openrouter"}
    if engine.lower() not in supported_engines:
        return False
    
    # 检查是否有 reasoning effort 后缀
    return get_reasoning_effort_suffix(model) is not None


def set_reasoning_parameters(payload: Dict[str, Any], effort: str) -> None:
    """
    设置 reasoning 相关参数
    
    支持多种参数格式以兼容不同的 API 版本：
    - reasoning_effort: Chat Completions API 格式
    - reasoningEffort: camelCase 格式（部分代理/封装可能使用）
    - reasoning: Responses API 格式（包含 effort 和 summary）
    
    Args:
        payload: 请求 payload
        effort: reasoning effort 级别 (high/medium/low/minimal)
    """
    # Chat Completions API 格式 (snake_case)
    payload["reasoning_effort"] = effort
    
    # camelCase 格式（兼容某些代理）
    payload["reasoningEffort"] = effort
    
    # Responses API 格式（如果有 reasoning 对象则合并，否则创建）
    if "reasoning" not in payload or not isinstance(payload.get("reasoning"), dict):
        payload["reasoning"] = {}
    
    reasoning = payload["reasoning"]
    reasoning["effort"] = effort
    
    # 设置 reasoning_summary 为 auto（自动生成推理摘要）
    if "summary" not in reasoning:
        reasoning["summary"] = "auto"
    
    # 顶层也设置一个 reasoning_summary（兼容某些实现）
    if "reasoning_summary" not in payload:
        payload["reasoning_summary"] = "auto"


# ==================== 请求拦截器 ====================

async def oai_reasoning_request_interceptor(
    request: Any,
    engine: str,
    provider: Dict[str, Any],
    api_key: Optional[str],
    url: str,
    headers: Dict[str, Any],
    payload: Dict[str, Any],
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """
    OpenAI Reasoning 请求拦截器
    
    处理 -high/-medium/-low/-minimal 后缀的模型请求
    """
    model = payload.get("model", "")
    
    # 早期退出：不是 reasoning 模型直接返回
    if not is_openai_reasoning_model(model, engine):
        return url, headers, payload
    
    # 获取后缀
    suffix = get_reasoning_effort_suffix(model)
    if not suffix:
        return url, headers, payload
    
    effort = REASONING_EFFORT_LEVELS[suffix]
    
    logger.info(f"[oai_reasoning] Processing reasoning model: {model}, effort={effort}")
    
    # 去掉后缀
    original_model = model
    # 保持原始大小写，只移除后缀
    payload["model"] = model[:-len(suffix)]
    
    # 设置 reasoning 参数
    set_reasoning_parameters(payload, effort)
    
    logger.debug(f"[oai_reasoning] Modified payload: model={payload['model']}, "
                 f"reasoning_effort={effort}, reasoning={payload.get('reasoning')}")
    
    return url, headers, payload


# ==================== 插件生命周期 ====================

def setup(manager):
    """
    插件初始化
    """
    logger.info(f"[{PLUGIN_INFO['name']}] 正在初始化...")
    
    # 注册请求拦截器
    register_request_interceptor(
        interceptor_id="oai_reasoning_request",
        callback=oai_reasoning_request_interceptor,
        priority=50,  # 较高优先级，在其他拦截器之前处理
        plugin_name=PLUGIN_INFO["name"],
        metadata={"description": "OpenAI Reasoning 请求处理"},
    )
    
    logger.info(f"[{PLUGIN_INFO['name']}] 已注册请求拦截器")


def teardown(manager):
    """
    插件清理
    """
    logger.info(f"[{PLUGIN_INFO['name']}] 正在清理...")
    
    # 注销拦截器
    unregister_request_interceptor("oai_reasoning_request")
    
    logger.info(f"[{PLUGIN_INFO['name']}] 已清理完成")


def unload():
    """
    插件卸载回调
    """
    logger.debug(f"[{PLUGIN_INFO['name']}] 模块即将卸载")