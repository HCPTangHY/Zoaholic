"""
请求处理模块

包含 process_request 函数和 ModelRequestHandler 类，
负责向 provider 发送请求、处理响应、错误重试等逻辑。
"""

import json
import asyncio
from collections import defaultdict
from urllib.parse import urlparse
from typing import Dict, Union, Optional, Any, Callable, TYPE_CHECKING

import httpx
from fastapi import HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from starlette.responses import Response
from starlette.responses import StreamingResponse as StarletteStreamingResponse

from core.log_config import logger
from core.request import get_payload
from core.response import fetch_response, fetch_response_stream
from core.models import (
    RequestModel,
    ImageGenerationRequest,
    AudioTranscriptionRequest,
    ModerationRequest,
    EmbeddingRequest,
)
from core.utils import get_engine, provider_api_circular_list
from core.routing import get_right_order_providers
from utils import safe_get, error_handling_wrapper

if TYPE_CHECKING:
    from fastapi import FastAPI

# 默认超时时间
DEFAULT_TIMEOUT = 100

# 调试模式标志
is_debug = False


def set_debug_mode(debug: bool):
    """设置调试模式"""
    global is_debug
    is_debug = debug


def get_preference_value(provider_timeouts: Dict[str, Any], original_model: str) -> Optional[int]:
    """
    根据模型名获取偏好值（如超时时间）
    
    Args:
        provider_timeouts: 偏好配置字典
        original_model: 原始模型名
        
    Returns:
        偏好值，如果未找到则返回 None
    """
    timeout_value = None
    original_model = original_model.lower()
    if original_model in provider_timeouts:
        timeout_value = provider_timeouts[original_model]
    else:
        # 尝试模糊匹配模型
        for timeout_model in provider_timeouts:
            if timeout_model != "default" and timeout_model.lower() in original_model.lower():
                timeout_value = provider_timeouts[timeout_model]
                break
        else:
            # 如果模糊匹配失败，使用渠道的默认值
            timeout_value = provider_timeouts.get("default", None)
    return timeout_value


def get_preference(
    preference_config: Dict[str, Any],
    channel_id: str,
    original_request_model: tuple,
    default_value: int
) -> int:
    """
    获取偏好配置值（如超时时间、keepalive 间隔）
    
    按照 channel_id -> request_model_name -> original_model -> global default 的顺序查找
    
    Args:
        preference_config: 偏好配置字典
        channel_id: 渠道 ID
        original_request_model: (original_model, request_model_name) 元组
        default_value: 默认值
        
    Returns:
        偏好配置值
    """
    original_model, request_model_name = original_request_model
    provider_timeouts = safe_get(preference_config, channel_id, default=preference_config["global"])
    timeout_value = get_preference_value(provider_timeouts, request_model_name)
    if timeout_value is None:
        timeout_value = get_preference_value(provider_timeouts, original_model)
    if timeout_value is None:
        timeout_value = get_preference_value(preference_config["global"], original_model)
    if timeout_value is None:
        timeout_value = preference_config["global"].get("default", default_value)
    return timeout_value


async def process_request(
    request: Union[RequestModel, ImageGenerationRequest, AudioTranscriptionRequest, ModerationRequest, EmbeddingRequest],
    provider: Dict[str, Any],
    background_tasks: BackgroundTasks,
    app: "FastAPI",
    request_info_getter: Callable[[], Dict[str, Any]],
    update_channel_stats_func: Callable,
    endpoint: Optional[str] = None,
    role: Optional[str] = None,
    timeout_value: int = DEFAULT_TIMEOUT,
    keepalive_interval: Optional[int] = None
) -> Response:
    """
    向单个 provider 发送请求并处理响应
    
    Args:
        request: 请求对象
        provider: provider 配置
        background_tasks: 后台任务
        app: FastAPI 应用实例
        request_info_getter: 获取当前请求信息的函数
        update_channel_stats_func: 更新渠道统计的函数
        endpoint: 请求端点
        role: 用户角色
        timeout_value: 超时时间
        keepalive_interval: keepalive 间隔
        
    Returns:
        响应对象
        
    Raises:
        Exception: 请求失败时抛出异常
    """
    timeout_value = int(timeout_value)
    model_dict = provider["_model_dict_cache"]
    original_model = model_dict[request.model]
    
    if provider['provider'].startswith("sk-"):
        api_key = provider['provider']
    elif provider.get("api"):
        api_key = await provider_api_circular_list[provider['provider']].next(original_model)
    else:
        api_key = None

    engine, stream_mode = get_engine(provider, endpoint, original_model)

    if stream_mode is not None:
        request.stream = stream_mode

    channel_id = f"{provider['provider']}"
    if engine != "moderation":
        logger.info(f"provider: {channel_id[:11]:<11} model: {request.model:<22} engine: {engine[:13]:<13} role: {role}")

    last_message_role = safe_get(request, "messages", -1, "role", default=None)
    
    url, headers, payload = await get_payload(request, engine, provider, api_key)
    headers.update(safe_get(provider, "preferences", "headers", default={}))  # add custom headers
    
    if is_debug:
        logger.info(url)
        logger.info(json.dumps(headers, indent=4, ensure_ascii=False))
        logger.info(json.dumps({k: v for k, v in payload.items() if k != 'file'}, indent=4, ensure_ascii=False))

    current_info = request_info_getter()
    # 确保日志中一定记录模型名（使用当前请求对象上的 model）
    if hasattr(request, "model") and getattr(request, "model", None):
        current_info["model"] = request.model

    proxy = safe_get(app.state.config, "preferences", "proxy", default=None)  # global proxy
    proxy = safe_get(provider, "preferences", "proxy", default=proxy)  # provider proxy
    
    # 获取该渠道启用的插件列表
    enabled_plugins = safe_get(provider, "preferences", "enabled_plugins", default=None)

    try:
        async with app.state.client_manager.get_client(url, proxy) as client:
            if request.stream:
                generator = fetch_response_stream(client, url, headers, payload, engine, original_model, timeout_value, enabled_plugins=enabled_plugins)
                wrapped_generator, first_response_time = await error_handling_wrapper(
                    generator, channel_id, engine, request.stream,
                    app.state.error_triggers, keepalive_interval=keepalive_interval,
                    last_message_role=last_message_role
                )
                response = StarletteStreamingResponse(wrapped_generator, media_type="text/event-stream")
            else:
                generator = fetch_response(client, url, headers, payload, engine, original_model, timeout_value)
                wrapped_generator, first_response_time = await error_handling_wrapper(
                    generator, channel_id, engine, request.stream, 
                    app.state.error_triggers, keepalive_interval=keepalive_interval, 
                    last_message_role=last_message_role
                )

                # 处理音频和其他二进制响应
                if endpoint == "/v1/audio/speech":
                    if isinstance(wrapped_generator, bytes):
                        response = Response(content=wrapped_generator, media_type="audio/mpeg")
                else:
                    first_element = await anext(wrapped_generator)
                    first_element = first_element.lstrip("data: ")
                    decoded_element = await asyncio.to_thread(json.loads, first_element)
                    encoded_element = await asyncio.to_thread(json.dumps, decoded_element)
                    response = StarletteStreamingResponse(iter([encoded_element]), media_type="application/json")

            # 更新成功计数和首次响应时间
            background_tasks.add_task(
                update_channel_stats_func, 
                current_info["request_id"], channel_id, request.model, 
                current_info["api_key"], success=True, provider_api_key=api_key
            )
            current_info["first_response_time"] = first_response_time
            current_info["success"] = True
            current_info["provider"] = channel_id
            return response

    except (Exception, HTTPException, asyncio.CancelledError, httpx.ReadError,
            httpx.RemoteProtocolError, httpx.LocalProtocolError, httpx.ReadTimeout,
            httpx.ConnectError) as e:
        background_tasks.add_task(
            update_channel_stats_func, 
            current_info["request_id"], channel_id, request.model, 
            current_info["api_key"], success=False, provider_api_key=api_key
        )
        raise e


class ModelRequestHandler:
    """
    模型请求处理器
    
    负责根据配置选择 provider、发送请求、处理错误和重试逻辑。
    """
    
    def __init__(
        self,
        app: "FastAPI",
        request_info_getter: Callable[[], Dict[str, Any]],
        update_channel_stats_func: Callable,
        default_timeout: int = DEFAULT_TIMEOUT
    ):
        """
        初始化处理器
        
        Args:
            app: FastAPI 应用实例
            request_info_getter: 获取当前请求信息的函数
            update_channel_stats_func: 更新渠道统计的函数
            default_timeout: 默认超时时间
        """
        self.app = app
        self.request_info_getter = request_info_getter
        self.update_channel_stats_func = update_channel_stats_func
        self.default_timeout = default_timeout
        self.last_provider_indices = defaultdict(lambda: -1)
        self.locks = defaultdict(asyncio.Lock)

    async def request_model(
        self,
        request_data: Union[RequestModel, ImageGenerationRequest, AudioTranscriptionRequest, ModerationRequest, EmbeddingRequest],
        api_index: int,
        background_tasks: BackgroundTasks,
        endpoint: Optional[str] = None
    ) -> Response:
        """
        处理模型请求
        
        Args:
            request_data: 请求数据
            api_index: API key 索引
            background_tasks: 后台任务
            endpoint: 请求端点
            
        Returns:
            响应对象
        """
        config = self.app.state.config
        request_model_name = request_data.model
        
        if not safe_get(config, 'api_keys', api_index, 'model'):
            raise HTTPException(status_code=404, detail=f"No matching model found: {request_model_name}")

        scheduling_algorithm = safe_get(
            config, 'api_keys', api_index, "preferences", "SCHEDULING_ALGORITHM", 
            default="fixed_priority"
        )

        # 估算请求 token 数
        request_total_tokens = 0
        if request_data and isinstance(request_data, RequestModel):
            for message in request_data.messages:
                if message.content and isinstance(message.content, str):
                    request_total_tokens += len(message.content)
        request_total_tokens = int(request_total_tokens / 4)

        matching_providers = await get_right_order_providers(
            request_model_name, config, api_index, scheduling_algorithm, 
            self.app, request_total_tokens=request_total_tokens
        )
        num_matching_providers = len(matching_providers)

        status_code = 500
        error_message = None

        start_index = 0
        if scheduling_algorithm != "fixed_priority":
            async with self.locks[request_model_name]:
                self.last_provider_indices[request_model_name] = (
                    self.last_provider_indices[request_model_name] + 1
                ) % num_matching_providers
                start_index = self.last_provider_indices[request_model_name]

        auto_retry = safe_get(config, 'api_keys', api_index, "preferences", "AUTO_RETRY", default=True)
        role = safe_get(
            config, 'api_keys', api_index, "role", 
            default=safe_get(config, 'api_keys', api_index, "api", default="None")[:8]
        )

        index = 0
        if num_matching_providers == 1:
            count = provider_api_circular_list[matching_providers[0]['provider']].get_items_count()
            if count > 1:
                retry_count = count
            else:
                retry_count = 1
        else:
            tmp_retry_count = sum(
                provider_api_circular_list[provider['provider']].get_items_count() 
                for provider in matching_providers
            ) * 2
            retry_count = min(tmp_retry_count, 10)

        while True:
            if index > num_matching_providers + retry_count:
                break
            current_index = (start_index + index) % num_matching_providers
            index += 1
            provider = matching_providers[current_index]

            provider_name = provider['provider']

            # 检查是否所有 API 密钥都被速率限制
            model_dict = provider["_model_dict_cache"]
            original_model = model_dict[request_model_name]
            if await provider_api_circular_list[provider_name].is_all_rate_limited(original_model):
                error_message = "All API keys are rate limited and stop auto retry!"
                if num_matching_providers == 1:
                    break
                else:
                    continue

            original_request_model = (original_model, request_data.model)
            
            # 处理本地 sk- 代理
            if provider_name.startswith("sk-") and provider_name in self.app.state.api_list:
                local_provider_api_index = self.app.state.api_list.index(provider_name)
                local_provider_scheduling_algorithm = safe_get(
                    config, 'api_keys', local_provider_api_index, "preferences", 
                    "SCHEDULING_ALGORITHM", default="fixed_priority"
                )
                local_provider_matching_providers = await get_right_order_providers(
                    request_model_name, config, local_provider_api_index, 
                    local_provider_scheduling_algorithm, self.app, 
                    request_total_tokens=request_total_tokens
                )
                local_timeout_value = 0
                for local_provider in local_provider_matching_providers:
                    local_provider_name = local_provider['provider']
                    if not local_provider_name.startswith("sk-"):
                        local_timeout_value += get_preference(
                            self.app.state.provider_timeouts, local_provider_name, 
                            original_request_model, self.default_timeout
                        )
                local_provider_num_matching_providers = len(local_provider_matching_providers)
            else:
                local_timeout_value = get_preference(
                    self.app.state.provider_timeouts, provider_name, 
                    original_request_model, self.default_timeout
                )
                local_provider_num_matching_providers = 1

            local_timeout_value = local_timeout_value * local_provider_num_matching_providers

            keepalive_interval = get_preference(
                self.app.state.keepalive_interval, provider_name, 
                original_request_model, 99999
            )
            if keepalive_interval > local_timeout_value:
                keepalive_interval = None
            if provider_name.startswith("sk-"):
                keepalive_interval = None

            try:
                response = await process_request(
                    request_data, provider, background_tasks, self.app,
                    self.request_info_getter, self.update_channel_stats_func,
                    endpoint, role, local_timeout_value, keepalive_interval
                )
                return response
            except (Exception, HTTPException, asyncio.CancelledError, httpx.ReadError, 
                    httpx.RemoteProtocolError, httpx.LocalProtocolError, httpx.ReadTimeout, 
                    httpx.ConnectError) as e:

                # 根据异常类型设置状态码和错误消息
                if isinstance(e, httpx.ReadTimeout):
                    status_code = 504  # Gateway Timeout
                    timeout_value = e.request.extensions.get('timeout', {}).get('read', -1)
                    error_message = f"Request timed out after {timeout_value} seconds"
                elif isinstance(e, httpx.ConnectError):
                    status_code = 503  # Service Unavailable
                    error_message = "Unable to connect to service"
                elif isinstance(e, httpx.ReadError):
                    status_code = 502  # Bad Gateway
                    error_message = "Network read error"
                elif isinstance(e, httpx.RemoteProtocolError):
                    status_code = 502  # Bad Gateway
                    error_message = "Remote protocol error"
                elif isinstance(e, httpx.LocalProtocolError):
                    status_code = 502  # Bad Gateway
                    error_message = "Local protocol error"
                elif isinstance(e, asyncio.CancelledError):
                    status_code = 499  # Client Closed Request
                    error_message = "Request was cancelled"
                elif isinstance(e, HTTPException):
                    status_code = e.status_code
                    error_message = str(e.detail)
                else:
                    status_code = 500  # Internal Server Error
                    error_message = str(e) or f"Unknown error: {e.__class__.__name__}"

                exclude_error_rate_limit = [
                    "BrokenResourceError",
                    "Proxy connection timed out",
                    "Unknown error: EndOfStream",
                    "'status': 'INVALID_ARGUMENT'",
                    "Unable to connect to service",
                    "Connection closed unexpectedly",
                    "Invalid JSON payload received. Unknown name ",
                    "User location is not supported for the API use",
                    "The model is overloaded. Please try again later.",
                    "[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE] sslv3 alert handshake failure (_ssl.c:1007)",
                    "<title>Worker exceeded resource limits",
                ]

                channel_id = provider['provider']

                if (self.app.state.channel_manager.cooldown_period > 0 
                    and num_matching_providers > 1
                    and all(error not in error_message for error in exclude_error_rate_limit)):
                    await self.app.state.channel_manager.exclude_model(channel_id, request_model_name)
                    matching_providers = await get_right_order_providers(
                        request_model_name, config, api_index, scheduling_algorithm, 
                        self.app, request_total_tokens=request_total_tokens
                    )
                    last_num_matching_providers = num_matching_providers
                    num_matching_providers = len(matching_providers)
                    if num_matching_providers != last_num_matching_providers:
                        index = 0

                cooling_time = safe_get(provider, "preferences", "api_key_cooldown_period", default=0)
                api_key_count = provider_api_circular_list[channel_id].get_items_count()
                current_api = await provider_api_circular_list[channel_id].after_next_current()

                if (cooling_time > 0 and api_key_count > 1
                    and all(error not in error_message for error in exclude_error_rate_limit)):
                    await provider_api_circular_list[channel_id].set_cooling(current_api, cooling_time=cooling_time)

                # 有些错误并没有请求成功，所以需要删除请求记录
                if (current_api 
                    and any(error in error_message for error in exclude_error_rate_limit) 
                    and provider_api_circular_list[provider_name].requests[current_api][original_model]):
                    provider_api_circular_list[provider_name].requests[current_api][original_model].pop()

                # 根据错误消息调整状态码
                if "string_above_max_length" in error_message:
                    status_code = 413
                if "must be less than max_seq_len" in error_message:
                    status_code = 413
                if "Please reduce the length of the messages or completion" in error_message:
                    status_code = 413
                if "Request contains text fields that are too large." in error_message:
                    status_code = 413
                # openrouter
                if "Please reduce the length of either one, or use the" in error_message:
                    status_code = 413
                # gemini
                if "exceeds the maximum number of tokens allowed" in error_message:
                    status_code = 413
                if ("'reason': 'API_KEY_INVALID'" in error_message 
                    or "API key not valid" in error_message 
                    or "API key expired" in error_message):
                    status_code = 401
                if "User location is not supported for the API use." in error_message:
                    status_code = 403
                if "<center><h1>400 Bad Request</h1></center>" in error_message:
                    status_code = 502
                if "The response was filtered due to the prompt triggering Azure OpenAI's content management policy." in error_message:
                    status_code = 403
                if "<head><title>413 Request Entity Too Large</title></head>" in error_message:
                    status_code = 429

                logger.error(f"Error {status_code} with provider {channel_id} API key: {current_api}: {error_message}")
                if is_debug:
                    import traceback
                    traceback.print_exc()

                if auto_retry and (status_code not in [400, 413] 
                    or urlparse(provider.get('base_url', '')).netloc == 'models.inference.ai.azure.com'):
                    continue
                else:
                    return JSONResponse(
                        status_code=status_code,
                        content={"error": f"Error: Current provider response failed: {error_message}"}
                    )

        current_info = self.request_info_getter()
        current_info["first_response_time"] = -1
        current_info["success"] = False
        current_info["provider"] = None
        return JSONResponse(
            status_code=status_code,
            content={"error": f"All {request_data.model} error: {error_message}"}
        )