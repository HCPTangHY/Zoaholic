"""
Gemini 空内容自动重试插件

当 Gemini 返回空内容或图片生成失败时，
通过返回错误触发上层的自动重试机制，走完整的渠道 key 轮换流程。

使用场景：
1. 图像生成模型（如 gemini-2.5-flash-image）有思维链但没生成图片
2. 响应被截断（finish_reason 为 MAX_TOKENS/length），图片被截断
3. 普通聊天模型返回空内容
4. 某些情况下 Gemini 返回空响应

使用方式：
在渠道配置的 enabled_plugins 中添加：
- "gemini_empty_retry" - 使用默认重试次数（3次）
- "gemini_empty_retry:5" - 最多重试 5 次

工作原理：
1. 使用请求拦截器在 request_info 中记录配置
2. 包装 Gemini 渠道的响应适配器
3. 对于图像生成模型：必须有图片才算成功，有思维链没图片会重试
4. 对于普通模型：有文本或思维链就算成功
5. 如果被截断（MAX_TOKENS）也会触发重试
6. 重试次数用完后，返回原始响应

注意：
- 对于流式请求，启用此插件会增加首字节时间（TTFB），因为需要先收集完整响应
- 图像生成模型必须返回图片才算成功（有思维链没图片 = 需要重试）
- 建议用于图像生成场景，避免图片被截断或生成失败

配置示例：
```yaml
providers:
  - provider: gemini-main
    base_url: https://generativelanguage.googleapis.com/v1beta
    api: YOUR_API_KEY
    model:
      - gemini-2.5-flash
    preferences:
      enabled_plugins:
        - "gemini_empty_retry:3"  # 最多重试 3 次
```
"""

import json
import asyncio
from typing import Any, Dict, List, Optional, Tuple
from functools import wraps

from core.log_config import logger
from core.middleware import request_info
from core.plugins import (
    register_request_interceptor,
    unregister_request_interceptor,
    get_plugin_options,
    is_plugin_enabled,
)
from core.channels import get_channel, register_channel


# 插件元信息
PLUGIN_INFO = {
    "name": "gemini_empty_retry",
    "version": "1.1.0",
    "description": "Gemini 空响应自动重试 — 检测到空内容、图片生成失败或响应被截断时自动重试，走完整的 key 轮换流程。适用于图像生成模型（有思维链但没图片）和普通模型的空响应场景。注意：流式请求会增加 TTFB。",
    "author": "Zoaholic Team",
    "dependencies": [],
    "metadata": {
        "category": "interceptors",
        "tags": ["gemini", "retry", "empty-content"],
        "params_hint": "填写最大重试次数（数字），如 3 或 5。默认 3 次。",
    },
}

# 声明提供的扩展
EXTENSIONS = [
    "interceptors:gemini_empty_retry_request",
]

# 默认重试次数
DEFAULT_MAX_RETRIES = 3

# 保存原始适配器的引用
_original_stream_adapter = None
_original_response_adapter = None
_original_channel_registered = False

# 图像生成模型的标识符
IMAGE_MODEL_PATTERNS = [
    "-image",
    "image-generation",
    "image",
]


def parse_retry_options(options: Optional[str]) -> int:
    """
    解析插件选项，获取最大重试次数
    
    Args:
        options: 插件选项字符串，如 "3" 或 "5"
        
    Returns:
        最大重试次数
    """
    if not options:
        return DEFAULT_MAX_RETRIES
    
    try:
        max_retries = int(options.strip())
        if max_retries < 1:
            return 1
        if max_retries > 10:
            return 10  # 限制最大重试次数
        return max_retries
    except ValueError:
        return DEFAULT_MAX_RETRIES


def is_image_generation_model(model: str) -> bool:
    """
    判断是否为图像生成模型
    
    Args:
        model: 模型名称
        
    Returns:
        是否为图像生成模型
    """
    if not model:
        return False
    model_lower = model.lower()
    return any(pattern in model_lower for pattern in IMAGE_MODEL_PATTERNS)


def is_content_empty(
    content: str, 
    reasoning_content: str, 
    function_call_name: Optional[str], 
    has_image: bool,
    is_image_model: bool,
    finish_reason: Optional[str] = None
) -> bool:
    """
    判断响应内容是否为空（需要重试）
    
    Args:
        content: 文本内容
        reasoning_content: 思考内容
        function_call_name: 函数调用名称
        has_image: 是否有图片
        is_image_model: 是否为图像生成模型
        finish_reason: 结束原因（如 stop, length, MAX_TOKENS 等）
        
    Returns:
        是否为空（需要重试）
    """
    # 有函数调用，不为空
    if function_call_name:
        return False
    
    # 图像生成模型的判断逻辑：
    # - 必须有图片才算有效响应
    # - 有思维链但没图片 = 图片生成失败，需要重试
    # - 被截断（MAX_TOKENS/length）= 图片可能被截断，需要重试
    if is_image_model:
        # 检查是否被截断
        truncated_reasons = {"max_tokens", "length", "MAX_TOKENS"}
        if finish_reason and finish_reason in truncated_reasons:
            # 被截断了，需要重试
            return True
        
        # 有图片就不为空
        if has_image:
            return False
        
        # 没有图片（不管有没有思维链/文字），都视为空
        return True
    
    # 普通模型的判断逻辑：
    # 有文本内容，不为空
    if content and content.strip():
        return False
    
    # 有思考内容，不为空
    if reasoning_content and reasoning_content.strip():
        return False
    
    # 什么都没有，为空
    return True


def get_retry_state() -> Dict[str, Any]:
    """
    获取当前请求的重试状态
    
    Returns:
        重试状态字典
    """
    try:
        current_info = request_info.get()
        return current_info.get("_gemini_empty_retry_state", {})
    except Exception:
        return {}


def set_retry_state(state: Dict[str, Any]) -> None:
    """
    设置当前请求的重试状态
    
    Args:
        state: 重试状态字典
    """
    try:
        current_info = request_info.get()
        current_info["_gemini_empty_retry_state"] = state
    except Exception as e:
        logger.error(f"[gemini_empty_retry] Failed to set retry state: {e}")


def increment_retry_count() -> int:
    """
    增加并返回当前重试计数
    
    Returns:
        更新后的重试计数
    """
    state = get_retry_state()
    retry_count = state.get("retry_count", 0) + 1
    state["retry_count"] = retry_count
    set_retry_state(state)
    return retry_count


def is_plugin_active_for_request() -> Tuple[bool, int, bool]:
    """
    检查插件是否对当前请求生效
    
    Returns:
        (是否生效, 最大重试次数, 是否为图像模型)
    """
    try:
        current_info = request_info.get()
        enabled = current_info.get("_gemini_empty_retry_enabled", False)
        max_retries = current_info.get("_gemini_empty_retry_max", DEFAULT_MAX_RETRIES)
        is_image_model = current_info.get("_gemini_empty_retry_is_image_model", False)
        return enabled, max_retries, is_image_model
    except Exception:
        return False, DEFAULT_MAX_RETRIES, False


# ==================== 包装后的响应适配器 ====================

async def wrapped_fetch_gemini_response(client, url, headers, payload, model, timeout):
    """
    包装后的非流式响应处理
    
    检查响应是否为空，如果为空且重试次数未用完，返回错误触发重试
    """
    from core.utils import safe_get
    
    # 检查插件是否生效
    active, max_retries, is_image_model = is_plugin_active_for_request()
    
    # 如果插件未生效，直接使用原始适配器
    if not active:
        async for chunk in _original_response_adapter(client, url, headers, payload, model, timeout):
            yield chunk
        return
    
    # 获取当前重试状态
    state = get_retry_state()
    current_retry = state.get("retry_count", 0)
    
    # 收集原始响应
    response_chunks = []
    async for chunk in _original_response_adapter(client, url, headers, payload, model, timeout):
        response_chunks.append(chunk)
    
    # 检查是否有错误响应
    if response_chunks and isinstance(response_chunks[0], dict) and "error" in response_chunks[0]:
        # 原始适配器已经返回错误，直接透传
        for chunk in response_chunks:
            yield chunk
        return
    
    # 检查响应内容是否为空
    if response_chunks:
        # 非流式响应通常只有一个 chunk（完整的 OpenAI 格式响应）
        response = response_chunks[0]
        if isinstance(response, dict):
            # 检查 choices 中的 content
            choices = response.get("choices", [])
            if choices:
                message = choices[0].get("message", {})
                content = message.get("content", "")
                reasoning_content = message.get("reasoning_content", "")
                
                # 检查是否有工具调用
                tool_calls = message.get("tool_calls")
                has_tool_calls = bool(tool_calls)
                
                # 检查是否有图片（在非流式响应中，图片通常已处理为 URL）
                # 检查 content 中是否包含图片 markdown
                has_image = content and "![image](" in content
                
                # 获取 finish_reason
                finish_reason = choices[0].get("finish_reason")
                
                # 判断是否为空
                is_empty = is_content_empty(
                    content, 
                    reasoning_content, 
                    "function" if has_tool_calls else None,
                    has_image,
                    is_image_model,
                    finish_reason
                )
                
                if is_empty and current_retry < max_retries:
                    # 增加重试计数
                    new_count = increment_retry_count()
                    
                    logger.warning(
                        f"[gemini_empty_retry] Empty content detected "
                        f"(retry {new_count}/{max_retries}), triggering retry"
                    )
                    
                    # 返回错误触发重试
                    yield {
                        "error": f"Gemini returned empty content (attempt {new_count}/{max_retries})",
                        "status_code": 502,
                        "details": {
                            "reason": "empty_content",
                            "content": content[:100] if content else None,
                            "retry_count": new_count,
                            "max_retries": max_retries,
                        }
                    }
                    return
    
    # 内容不为空或重试次数已用完，返回原始响应
    for chunk in response_chunks:
        yield chunk


async def wrapped_fetch_gemini_response_stream(client, url, headers, payload, model, timeout):
    """
    包装后的流式响应处理
    
    先收集完整响应，检查是否为空，然后决定是重试还是重放
    注意：这会增加首字节时间（TTFB）
    """
    # 检查插件是否生效
    active, max_retries, is_image_model = is_plugin_active_for_request()
    
    # 如果插件未生效，直接使用原始适配器
    if not active:
        async for chunk in _original_stream_adapter(client, url, headers, payload, model, timeout):
            yield chunk
        return
    
    # 获取当前重试状态
    state = get_retry_state()
    current_retry = state.get("retry_count", 0)
    
    # 收集所有响应 chunks
    collected_chunks: List[str] = []
    has_error = False
    error_chunk = None
    
    # 用于检测内容的变量
    total_content = ""
    total_reasoning = ""
    has_function_call = False
    has_image = False
    finish_reason = None
    
    try:
        async for chunk in _original_stream_adapter(client, url, headers, payload, model, timeout):
            # 检查是否是错误响应
            if isinstance(chunk, dict) and "error" in chunk:
                has_error = True
                error_chunk = chunk
                break
            
            collected_chunks.append(chunk)
            
            # 解析 SSE 数据，提取内容用于空检测
            if isinstance(chunk, str):
                # 检查是否有图片 URL（Gemini 流式返回的图片会被转换成 markdown）
                if "![image](" in chunk:
                    has_image = True
                
                for line in chunk.split("\n"):
                    line = line.strip()
                    if line.startswith("data: ") and not line.endswith("[DONE]"):
                        try:
                            data = json.loads(line[6:])
                            choices = data.get("choices", [])
                            if choices:
                                delta = choices[0].get("delta", {})
                                if "content" in delta and delta["content"]:
                                    total_content += delta["content"]
                                if "reasoning_content" in delta and delta["reasoning_content"]:
                                    total_reasoning += delta["reasoning_content"]
                                if "tool_calls" in delta or "function_call" in delta:
                                    has_function_call = True
                                # 获取 finish_reason
                                fr = choices[0].get("finish_reason")
                                if fr:
                                    finish_reason = fr
                        except json.JSONDecodeError:
                            pass
            
    except Exception as e:
        logger.error(f"[gemini_empty_retry] Error collecting stream: {e}")
        # 出错时直接透传已收集的内容
        for chunk in collected_chunks:
            yield chunk
        raise
    
    # 如果原始适配器返回了错误，直接透传
    if has_error and error_chunk:
        yield error_chunk
        return
    
    # 检查收集到的内容是否为空
    is_empty = is_content_empty(
        total_content,
        total_reasoning,
        "function" if has_function_call else None,
        has_image,
        is_image_model,
        finish_reason
    )
    
    if is_empty and current_retry < max_retries:
        # 增加重试计数
        new_count = increment_retry_count()
        
        logger.warning(
            f"[gemini_empty_retry] Empty content detected in stream "
            f"(retry {new_count}/{max_retries}), triggering retry"
        )
        
        # 返回错误触发重试
        yield {
            "error": f"Gemini returned empty content (attempt {new_count}/{max_retries})",
            "status_code": 502,
            "details": {
                "reason": "empty_content",
                "content": total_content[:100] if total_content else None,
                "retry_count": new_count,
                "max_retries": max_retries,
            }
        }
        return
    
    # 内容不为空或重试次数已用完，重放收集的响应
    for chunk in collected_chunks:
        yield chunk


# ==================== 请求拦截器 ====================

async def gemini_empty_retry_request_interceptor(
    request: Any,
    engine: str,
    provider: Dict[str, Any],
    api_key: Optional[str],
    url: str,
    headers: Dict[str, Any],
    payload: Dict[str, Any],
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """
    请求拦截器：检测插件是否启用，设置配置到 request_info
    
    注意：此拦截器可能在重试时被多次调用，需要保持状态
    """
    # 只处理 Gemini 引擎
    if engine != "gemini":
        return url, headers, payload
    
    # 检查插件是否在此 provider 上启用
    if not is_plugin_enabled(PLUGIN_INFO["name"], provider):
        return url, headers, payload
    
    try:
        current_info = request_info.get()
        
        # 检查是否已经初始化过（重试场景）
        if current_info.get("_gemini_empty_retry_enabled"):
            # 已初始化，不重置状态，保持重试计数
            logger.debug(f"[gemini_empty_retry] Already initialized, keeping state")
            return url, headers, payload
        
        # 首次初始化
        # 获取插件选项（重试次数）
        options = get_plugin_options(PLUGIN_INFO["name"], provider)
        max_retries = parse_retry_options(options)
        
        # 获取模型名称，判断是否为图像生成模型
        # 注意：Gemini 格式的 payload 中没有 model 字段（model 在 URL 中）
        # 需要从原始请求对象获取，或者从 URL 中解析
        model = ""
        
        # 方式1：从 request 对象获取（OpenAI 格式的原始请求）
        if hasattr(request, "model") and request.model:
            model = request.model
        # 方式2：从 payload 获取（某些情况下可能有）
        elif isinstance(payload, dict) and payload.get("model"):
            model = payload.get("model", "")
        # 方式3：从 URL 中解析（Gemini 格式：/models/{model}:generateContent）
        elif url:
            import re
            match = re.search(r"/models/([^/:]+)", url)
            if match:
                model = match.group(1)
        
        is_image_model = is_image_generation_model(model)
        
        # 在 request_info 中设置标记
        current_info["_gemini_empty_retry_enabled"] = True
        current_info["_gemini_empty_retry_max"] = max_retries
        current_info["_gemini_empty_retry_is_image_model"] = is_image_model
        current_info["_gemini_empty_retry_state"] = {"retry_count": 0}
        
        logger.debug(
            f"[gemini_empty_retry] Enabled for request, model={model}, "
            f"max_retries={max_retries}, is_image_model={is_image_model}"
        )
    except Exception as e:
        logger.error(f"[gemini_empty_retry] Failed to set request_info: {e}")
    
    return url, headers, payload


# ==================== 渠道包装 ====================

def wrap_gemini_channel():
    """
    包装 Gemini 渠道的响应适配器
    """
    global _original_stream_adapter, _original_response_adapter, _original_channel_registered
    
    channel = get_channel("gemini")
    if not channel:
        logger.error("[gemini_empty_retry] Gemini channel not found")
        return False
    
    # 保存原始适配器
    _original_stream_adapter = channel.stream_adapter
    _original_response_adapter = channel.response_adapter
    
    # 重新注册渠道，使用包装后的适配器
    register_channel(
        id="gemini",
        type_name=channel.type_name,
        default_base_url=channel.default_base_url,
        auth_header=channel.auth_header,
        description=channel.description,
        request_adapter=channel.request_adapter,
        passthrough_adapter=channel.passthrough_adapter,
        stream_adapter=wrapped_fetch_gemini_response_stream,
        response_adapter=wrapped_fetch_gemini_response,
        models_adapter=channel.models_adapter,
        overwrite=True,
    )
    
    _original_channel_registered = True
    logger.info("[gemini_empty_retry] Wrapped Gemini channel adapters")
    return True


def unwrap_gemini_channel():
    """
    恢复 Gemini 渠道的原始适配器
    """
    global _original_stream_adapter, _original_response_adapter, _original_channel_registered
    
    if not _original_channel_registered:
        return
    
    channel = get_channel("gemini")
    if not channel:
        return
    
    if _original_stream_adapter and _original_response_adapter:
        # 恢复原始适配器
        register_channel(
            id="gemini",
            type_name=channel.type_name,
            default_base_url=channel.default_base_url,
            auth_header=channel.auth_header,
            description=channel.description,
            request_adapter=channel.request_adapter,
            passthrough_adapter=channel.passthrough_adapter,
            stream_adapter=_original_stream_adapter,
            response_adapter=_original_response_adapter,
            models_adapter=channel.models_adapter,
            overwrite=True,
        )
        
        logger.info("[gemini_empty_retry] Restored original Gemini channel adapters")
    
    _original_stream_adapter = None
    _original_response_adapter = None
    _original_channel_registered = False


# ==================== 插件生命周期 ====================

def setup(manager):
    """
    插件初始化
    """
    logger.info(f"[{PLUGIN_INFO['name']}] 正在初始化...")
    
    # 注册请求拦截器
    register_request_interceptor(
        interceptor_id="gemini_empty_retry_request",
        callback=gemini_empty_retry_request_interceptor,
        priority=150,  # 较低优先级，在其他拦截器之后处理
        plugin_name=PLUGIN_INFO["name"],
        metadata={"description": "Gemini 空内容检测配置"},
    )
    
    # 包装 Gemini 渠道
    wrap_gemini_channel()
    
    logger.info(f"[{PLUGIN_INFO['name']}] 已初始化完成")


def teardown(manager):
    """
    插件清理
    """
    logger.info(f"[{PLUGIN_INFO['name']}] 正在清理...")
    
    # 注销拦截器
    unregister_request_interceptor("gemini_empty_retry_request")
    
    # 恢复 Gemini 渠道
    unwrap_gemini_channel()
    
    logger.info(f"[{PLUGIN_INFO['name']}] 已清理完成")


def unload():
    """
    插件卸载回调
    """
    logger.debug(f"[{PLUGIN_INFO['name']}] 模块即将卸载")
