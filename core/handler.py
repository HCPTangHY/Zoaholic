"""
请求处理模块

包含 process_request 函数和 ModelRequestHandler 类，
负责向 provider 发送请求、处理响应、错误重试等逻辑。
"""

import json
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from time import time
from urllib.parse import urlparse
from typing import Dict, Union, Optional, Any, Callable, List, TYPE_CHECKING

import httpx
from fastapi import HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from starlette.responses import Response

from core.log_config import logger
from core.streaming import LoggingStreamingResponse
from core.request import get_payload
from core.response import fetch_response, fetch_response_stream, check_response
from core.stats import update_stats
from core.models import (
    RequestModel,
    ImageGenerationRequest,
    AudioTranscriptionRequest,
    ModerationRequest,
    EmbeddingRequest,
)
from core.utils import get_engine, provider_api_circular_list, truncate_for_logging
from core.routing import get_right_order_providers
from core.error_response import openai_error_response
from utils import safe_get, error_handling_wrapper

if TYPE_CHECKING:
    from fastapi import FastAPI

# 默认超时时间（10分钟，支持长时间 reasoning 请求）
DEFAULT_TIMEOUT = 600

# 调试模式标志
is_debug = False


def set_debug_mode(debug: bool):
    """设置调试模式"""
    global is_debug
    is_debug = debug


def _fire_and_forget_channel_stats(update_channel_stats_func: Callable, *args, **kwargs) -> None:
    """异步写入 ChannelStat，不依赖 FastAPI BackgroundTasks。

    背景：
    - BackgroundTasks 会在响应生命周期结束后执行。
    - 对于流式接口/客户端提前断开等场景，BackgroundTasks 有可能不被执行，
      导致 channel_stats 缺失，从而 /v1/stats 的成功率永远是 0 或空。

    这里用 create_task 让统计写入尽量独立于请求/响应生命周期。
    """

    async def _run():
        try:
            await update_channel_stats_func(*args, **kwargs)
        except Exception as e:
            # 避免 "Task exception was never retrieved"
            logger.error(f"Error updating channel stats: {str(e)}")

    try:
        asyncio.create_task(_run())
    except RuntimeError:
        # event loop 未就绪（极少数启动/关闭阶段），忽略即可
        pass


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
    

    current_info = request_info_getter()
    
    # 记录发送到上游的请求头和请求体（如果配置了保留时间）
    if current_info.get("raw_data_expires_at"):
        try:
            # 记录上游请求头（过滤敏感头信息）
            safe_upstream_headers = {k: v for k, v in headers.items()
                                    if k.lower() not in ("authorization", "x-api-key", "api-key")}
            current_info["upstream_request_headers"] = json.dumps(safe_upstream_headers, ensure_ascii=False)
            
            # 使用深度截断，保留结构同时限制大小
            # 使用 asyncio.to_thread 避免大请求体阻塞事件循环
            upstream_payload = {k: v for k, v in payload.items() if k != 'file'}
            current_info["upstream_request_body"] = await asyncio.to_thread(truncate_for_logging, upstream_payload)
        except Exception as e:
            logger.error(f"Error saving upstream request data: {str(e)}")
    # 确保日志中一定记录模型名（使用当前请求对象上的 model）
    if hasattr(request, "model") and getattr(request, "model", None):
        current_info["model"] = request.model
    
    # 记录渠道ID和上游key索引
    current_info["provider_id"] = channel_id
    if api_key:
        try:
            # 从 provider_api_circular_list 中获取所有 keys
            circular_list = provider_api_circular_list.get(provider['provider'])
            if circular_list and hasattr(circular_list, 'items'):
                api_keys_list = circular_list.items
                if api_key in api_keys_list:
                    current_info["provider_key_index"] = api_keys_list.index(api_key)
        except (ValueError, TypeError, AttributeError):
            pass

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
                    last_message_role=last_message_role,
                    request_url=url,
                    app=app,
                )
                response = LoggingStreamingResponse(
                    wrapped_generator,
                    media_type="text/event-stream",
                    current_info=current_info,
                    app=app,
                    debug=is_debug
                )
            else:
                generator = fetch_response(client, url, headers, payload, engine, original_model, timeout_value)
                wrapped_generator, first_response_time = await error_handling_wrapper(
                    generator, channel_id, engine, request.stream,
                    app.state.error_triggers, keepalive_interval=keepalive_interval,
                    last_message_role=last_message_role,
                    request_url=url,
                    app=app,
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
                    
                    # 非流式响应也需要记录统计
                    async def non_stream_iter():
                        yield encoded_element
                    
                    response = LoggingStreamingResponse(
                        non_stream_iter(),
                        media_type="application/json",
                        current_info=current_info,
                        app=app,
                        debug=is_debug
                    )

            # 更新成功计数和首次响应时间
            _fire_and_forget_channel_stats(
                update_channel_stats_func,
                current_info["request_id"],
                channel_id,
                request.model,
                current_info["api_key"],
                success=True,
                provider_api_key=api_key,
            )
            current_info["first_response_time"] = first_response_time
            current_info["success"] = True
            current_info["status_code"] = 200
            current_info["provider"] = channel_id
            return response

    except (Exception, HTTPException, asyncio.CancelledError, httpx.ReadError,
            httpx.RemoteProtocolError, httpx.LocalProtocolError, httpx.ReadTimeout,
            httpx.ConnectError) as e:
        _fire_and_forget_channel_stats(
            update_channel_stats_func,
            current_info["request_id"],
            channel_id,
            request.model,
            current_info["api_key"],
            success=False,
            provider_api_key=api_key,
        )
        raise e


def _filter_passthrough_headers(original_headers: Optional[Dict[str, str]]) -> Dict[str, Any]:
    """过滤入口请求头中的认证字段和需要移除的头，避免透传错误信息到上游"""
    drop_names = {
        "authorization", "x-api-key", "api-key", "x-goog-api-key",  # 认证相关
        "host",  # 必须移除，否则上游服务（如 Deno Deploy）会路由错误
        "content-length",  # 由 httpx 自动计算
        "accept-encoding",  # 移除压缩请求，避免返回 gzip 压缩的响应导致乱码
    }
    return {
        k: v
        for k, v in (original_headers or {}).items()
        if k.lower() not in drop_names
    }


async def _fetch_passthrough_stream(client, url, headers, payload, timeout):
    """
    透传模式的流式响应处理
    
    直接转发上游 SSE 流，不做任何格式转换
    
    注意：使用特殊的超时配置，read timeout 设置为 None 以支持
    Google Search grounding 等需要长时间处理的操作。
    """
    # 为流式请求创建特殊的超时配置
    # read timeout 设置为 None，因为：
    # 1. Gemini 使用 Google Search 时，搜索可能需要较长时间
    # 2. 思考模式下，模型思考时可能有较长的静默期
    # 3. 我们依赖 connect/write timeout 来处理真正的网络问题
    stream_timeout = httpx.Timeout(
        connect=15.0,
        read=None,  # 无限等待读取，支持 Google Search 等长时间操作
        write=300.0,  # 写入超时300秒，支持大型请求体（多图片/PDF）
        pool=10.0,
    )
    
    json_payload = await asyncio.to_thread(json.dumps, payload)
    async with client.stream('POST', url, headers=headers, content=json_payload, timeout=stream_timeout) as response:
        error_message = await check_response(response, "passthrough_stream")
        if error_message:
            yield error_message
            return
        
        # 使用 aiter_bytes 替代 aiter_text，然后手动解码
        # 这样可以更好地处理边界情况和避免编码问题导致流中断
        buffer = b""
        async for raw_chunk in response.aiter_bytes():
            # 合并缓冲区和新数据
            buffer += raw_chunk
            
            # 尝试解码为文本
            try:
                # 使用 errors="replace" 避免编码错误导致流终止
                text = buffer.decode("utf-8", errors="replace")
                buffer = b""  # 成功解码后清空缓冲区
                if text:
                    yield text
            except UnicodeDecodeError:
                # 如果解码失败（可能是不完整的 UTF-8 序列），保留缓冲区等待更多数据
                # 但如果缓冲区太大，强制输出避免内存问题
                if len(buffer) > 10 * 1024:  # 10KB
                    text = buffer.decode("utf-8", errors="replace")
                    buffer = b""
                    if text:
                        yield text
        
        # 处理剩余的缓冲区数据
        if buffer:
            text = buffer.decode("utf-8", errors="replace")
            if text:
                yield text


async def _fetch_passthrough_response(client, url, headers, payload, timeout):
    """
    透传模式的非流式响应处理
    
    直接转发上游 JSON 响应，不做任何格式转换
    """
    import time as _time
    t0 = _time.time()
    
    json_payload = await asyncio.to_thread(json.dumps, payload)
    t1 = _time.time()
    logger.debug(f"[passthrough] json.dumps took {t1-t0:.3f}s")
    
    # 使用与流式请求相同的超时配置
    # 避免整数超时覆盖客户端的精细超时设置
    request_timeout = httpx.Timeout(
        connect=15.0,
        read=timeout,  # 使用传入的超时作为读取超时
        write=300.0,  # 写入超时300秒，支持大型请求体（多图片/PDF）
        pool=10.0,
    )
    
    response = await client.post(url, headers=headers, content=json_payload, timeout=request_timeout)
    t2 = _time.time()
    logger.debug(f"[passthrough] POST request took {t2-t1:.3f}s, status={response.status_code}")
    
    error_message = await check_response(response, "passthrough_non_stream")
    if error_message:
        yield error_message
        return
    
    response_bytes = await response.aread()
    t3 = _time.time()
    logger.debug(f"[passthrough] aread() took {t3-t2:.3f}s, size={len(response_bytes)} bytes")
    
    yield response_bytes.decode("utf-8")


async def _passthrough_error_wrapper(generator, channel_id):
    """
    透传模式的简单错误包装器
    
    只检测 HTTP 错误（由 check_response 完成），不做 JSON 解析
    直接透传所有内容
    """
    from time import time as time_now
    start_time = time_now()
    first_response_time = None
    
    async def wrapped():
        nonlocal first_response_time
        first_chunk = True
        async for chunk in generator:
            if first_chunk:
                first_response_time = time_now() - start_time
                first_chunk = False
                
                # 检查是否是错误响应（只检查 dict 类型的错误）
                if isinstance(chunk, dict) and 'error' in chunk:
                    status_code = chunk.get('status_code', 500)
                    detail = chunk.get('details')
                    error_obj = chunk.get('error')
                    
                    if isinstance(detail, dict) and 'error' in detail:
                        inner = detail.get('error')
                        if isinstance(inner, dict):
                            detail = inner.get('message') or detail
                        elif isinstance(inner, str):
                            detail = inner
                    
                    if not detail and isinstance(error_obj, dict):
                        detail = error_obj.get('message')
                        if not status_code or status_code == 500:
                            status_code = error_obj.get('code') or status_code
                    
                    if not detail:
                        detail = str(chunk)
                        
                    try:
                        status_code = int(status_code)
                        if status_code < 100 or status_code > 599:
                            status_code = 500
                    except (TypeError, ValueError):
                        status_code = 500
                        
                    raise HTTPException(
                        status_code=status_code,
                        detail=str(detail)
                    )
            
            yield chunk
    
    # 透传模式：直接获取第一个 chunk，不做额外过滤
    # SSE 流的内容（如 event:, data:）都是有效内容，不应该被跳过
    gen = wrapped()
    try:
        first = await gen.__anext__()
    except StopAsyncIteration:
        raise HTTPException(status_code=502, detail="Upstream server returned an empty response.")
    
    async def final_gen():
        yield first
        async for chunk in gen:
            yield chunk
    
    return final_gen(), first_response_time or (time_now() - start_time)


async def process_request_passthrough(
    request: RequestModel,
    provider: Dict[str, Any],
    background_tasks: BackgroundTasks,
    app: "FastAPI",
    request_info_getter: Callable[[], Dict[str, Any]],
    update_channel_stats_func: Callable,
    passthrough_ctx: Any,
    endpoint: Optional[str] = None,
    role: Optional[str] = None,
    timeout_value: int = DEFAULT_TIMEOUT,
    keepalive_interval: Optional[int] = None,
) -> Response:
    """
    透传模式请求处理：
    - 复用 channel.request_adapter 生成 url/headers
    - payload 取入口原生请求 + 轻量修改
    - 不跑上游响应的 Canonical 转换
    """
    from core.dialects.passthrough import apply_passthrough_modifications
    from core.plugins.interceptors import apply_request_interceptors
    from core.channels import get_channel

    timeout_value = int(timeout_value)
    model_dict = provider["_model_dict_cache"]
    original_model = model_dict[request.model]

    if provider["provider"].startswith("sk-"):
        api_key = provider["provider"]
    elif provider.get("api"):
        api_key = await provider_api_circular_list[provider["provider"]].next(original_model)
    else:
        api_key = None

    engine, stream_mode = get_engine(provider, endpoint, original_model)
    if stream_mode is not None:
        request.stream = stream_mode

    channel = get_channel(engine)
    adapter = (channel.passthrough_adapter if channel else None) or (channel.request_adapter if channel else None)
    if not adapter:
        raise ValueError(f"Unknown engine: {engine}")

    url, adapter_headers, _ = await adapter(request, engine, provider, api_key)

    headers: Dict[str, Any] = dict(adapter_headers or {})
    headers.update(_filter_passthrough_headers(passthrough_ctx.original_headers))
    headers.update(safe_get(provider, "preferences", "headers", default={}))
    headers.setdefault("Content-Type", "application/json")

    payload = apply_passthrough_modifications(
        passthrough_ctx.original_payload,
        passthrough_ctx.modifications,
        passthrough_ctx.dialect_id,
        request_model=request.model,
        original_model=original_model,
    )

    # 渠道级透传 payload 修饰（把“渠道特殊逻辑”收敛在各自 channel 文件内）
    if channel and getattr(channel, "passthrough_payload_adapter", None):
        payload = await channel.passthrough_payload_adapter(
            payload,
            passthrough_ctx.modifications,
            request,
            engine,
            provider,
            api_key,
        )

    enabled_plugins = safe_get(provider, "preferences", "enabled_plugins", default=None)
    url, headers, payload = await apply_request_interceptors(
        request, engine, provider, api_key, url, headers, payload, enabled_plugins
    )

    if is_debug:
        pass

    current_info = request_info_getter()
    channel_id = f"{provider['provider']}"
    current_info["dialect_id"] = passthrough_ctx.dialect_id

    if current_info.get("raw_data_expires_at"):
        safe_upstream_headers = {
            k: v for k, v in headers.items()
            if k.lower() not in ("authorization", "x-api-key", "api-key", "x-goog-api-key")
        }
        current_info["upstream_request_headers"] = json.dumps(safe_upstream_headers, ensure_ascii=False)
        upstream_payload = {k: v for k, v in payload.items() if k != "file"}
        # 使用 asyncio.to_thread 避免大请求体阻塞事件循环
        current_info["upstream_request_body"] = await asyncio.to_thread(truncate_for_logging, upstream_payload)

    if getattr(request, "model", None):
        current_info["model"] = request.model

    current_info["provider_id"] = channel_id
    if api_key:
        try:
            # 从 provider_api_circular_list 中获取所有 keys
            circular_list = provider_api_circular_list.get(provider['provider'])
            if circular_list and hasattr(circular_list, 'items'):
                api_keys_list = circular_list.items
                if api_key in api_keys_list:
                    current_info["provider_key_index"] = api_keys_list.index(api_key)
        except (ValueError, TypeError, AttributeError):
            pass

    proxy = safe_get(app.state.config, "preferences", "proxy", default=None)
    proxy = safe_get(provider, "preferences", "proxy", default=proxy)

    try:
        async with app.state.client_manager.get_client(url, proxy) as client:
            last_message_role = safe_get(request, "messages", -1, "role", default=None)

            if request.stream:
                # 透传模式：使用原始流处理，不做格式转换
                generator = _fetch_passthrough_stream(
                    client, url, headers, payload, timeout_value
                )
                # 使用简单的透传错误包装器，不做 JSON 解析
                wrapped_generator, first_response_time = await _passthrough_error_wrapper(
                    generator, channel_id
                )
                response = LoggingStreamingResponse(
                    wrapped_generator,
                    media_type="text/event-stream",
                    current_info=current_info,
                    app=app,
                    debug=is_debug,
                )
            else:
                # 透传模式：使用原始响应处理，不做格式转换
                generator = _fetch_passthrough_response(
                    client, url, headers, payload, timeout_value
                )
                # 使用简单的透传错误包装器，不做 JSON 解析
                wrapped_generator, first_response_time = await _passthrough_error_wrapper(
                    generator, channel_id
                )

                async def passthrough_iter():
                    async for chunk in wrapped_generator:
                        yield chunk

                response = LoggingStreamingResponse(
                    passthrough_iter(),
                    media_type="application/json",
                    current_info=current_info,
                    app=app,
                    debug=is_debug,
                )

            current_info["first_response_time"] = first_response_time
    except (Exception, HTTPException, asyncio.CancelledError, httpx.ReadError,
            httpx.RemoteProtocolError, httpx.LocalProtocolError, httpx.ReadTimeout,
            httpx.ConnectError) as e:
        _fire_and_forget_channel_stats(
            update_channel_stats_func,
            current_info["request_id"],
            channel_id,
            request.model,
            current_info["api_key"],
            success=False,
            provider_api_key=api_key,
        )
        raise e

    response.headers["x-zoaholic-passthrough"] = "request"

    _fire_and_forget_channel_stats(
        update_channel_stats_func,
        current_info["request_id"],
        channel_id,
        request.model,
        current_info["api_key"],
        success=True,
        provider_api_key=api_key,
    )
    current_info["success"] = True
    current_info["status_code"] = 200
    current_info["provider"] = channel_id

    return response


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
        endpoint: Optional[str] = None,
        dialect_id: Optional[str] = None,
        original_payload: Optional[Dict[str, Any]] = None,
   original_headers: Optional[Dict[str, str]] = None,
    ) -> Response:
        """
        处理模型请求
        
        Args:
            request_data: 请求数据
            api_index: API key 索引
            background_tasks: 后台任务
            endpoint: 请求端点
            dialect_id: 入口方言 ID（原生路由传入）
            original_payload: 原始 native 请求体（透传用）
            original_headers: 原始请求头（透传用）
            
        Returns:
            响应对象
        """
        config = self.app.state.config
        request_model_name = request_data.model
        
        if not safe_get(config, 'api_keys', api_index, 'model'):
            raise HTTPException(status_code=404, detail=f"No matching model found: {request_model_name}")

        # 调度算法优先级：API Key preferences > 全局 preferences > 默认值
        scheduling_algorithm = safe_get(
            config, 'api_keys', api_index, "preferences", "SCHEDULING_ALGORITHM",
            default=safe_get(config, "preferences", "SCHEDULING_ALGORITHM", default="fixed_priority")
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
        # 获取配置的最大重试次数上限，默认为 10
        max_retry_limit = safe_get(config, 'preferences', 'max_retry_count', default=10)
        if max_retry_limit < 1:
            max_retry_limit = 1

        # 计算最大尝试次数（包含首轮 + 自动重试）。
        # 修复：
        # - 单 provider 分支此前未受 max_retry_limit 约束，且使用 get_items_count 会把禁用 key 也算进去，
        #   容易在“只有 1 个可用 key，但配置里堆了大量禁用 key”时触发 1000+ 次重试。
        # - 统一按“启用的 key 数量”计算，并在所有分支上应用 max_retry_limit。
        def _provider_key_slots(p: Dict[str, Any]) -> int:
            """返回该 provider 可用于重试的 key 数量（至少为 1）。

            注意：没有配置 api（例如无需 key 的渠道）也按 1 计。
            """
            try:
                enabled = provider_api_circular_list[p["provider"]].get_enabled_items_count()
            except Exception:
                enabled = 0
            try:
                enabled_int = int(enabled)
            except (TypeError, ValueError):
                enabled_int = 0
            return max(1, enabled_int)

        def _calc_retry_count(providers: List[Dict[str, Any]]) -> int:
            """计算“额外重试次数”。

            设计目标：
            - 保持原有语义：总尝试次数 ≈ num_matching_providers + retry_count
            - retry_count 受 max_retry_limit 约束
            - 仅按“启用的 key 数量”估算，避免禁用 key 造成 retry_count 虚高
            """
            n = len(providers)
            if n <= 0:
                return 0

            if n == 1:
                slots = _provider_key_slots(providers[0])
                # 单 provider：至少允许 1 次重试；若有多 key，可覆盖更多 key
                base = slots if slots > 1 else 1
                return min(base, max_retry_limit)

            total_slots = sum(_provider_key_slots(p) for p in providers)
            tmp_retry_count = total_slots * 2
            return min(tmp_retry_count, max_retry_limit)

        retry_count = _calc_retry_count(matching_providers)
        max_attempts = num_matching_providers + retry_count

        # 初始化重试路径记录
        retry_path: List[Dict[str, Any]] = []
        current_retry_count = 0

        while True:
            if index >= max_attempts:
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
                passthrough_ctx = None
                if dialect_id and original_payload is not None and isinstance(request_data, RequestModel):
                    from core.dialects.passthrough import evaluate_passthrough
                    passthrough_ctx = await evaluate_passthrough(
                        dialect_id=dialect_id,
                        original_payload=original_payload,
                        original_headers=original_headers or {},
                        target_provider=provider,
                        request_model=request_model_name,
                    )

                process_fn = process_request_passthrough if (passthrough_ctx and passthrough_ctx.enabled) else process_request
                response = await process_fn(
                    request_data, provider, background_tasks, self.app,
                    self.request_info_getter, self.update_channel_stats_func,
                    passthrough_ctx=passthrough_ctx,
                    endpoint=endpoint,
                    role=role,
                    timeout_value=local_timeout_value,
                    keepalive_interval=keepalive_interval,
                ) if process_fn is process_request_passthrough else await process_request(
                    request_data, provider, background_tasks, self.app,
                    self.request_info_getter, self.update_channel_stats_func,
                    endpoint, role, local_timeout_value, keepalive_interval
                )

                # 成功时记录重试路径和重试次数
                current_info = self.request_info_getter()
                if retry_path:
                    current_info["retry_path"] = json.dumps(retry_path, ensure_ascii=False)
                current_info["retry_count"] = current_retry_count
                return response
            except asyncio.CancelledError:
                # 客户端取消请求，直接向上抛出，不再重试
                logger.info(f"Request cancelled by client for model {request_model_name}")
                raise
            except (Exception, HTTPException, httpx.ReadError,
                    httpx.RemoteProtocolError, httpx.LocalProtocolError, httpx.ReadTimeout,
                    httpx.ConnectError) as e:
                # 记录重试路径
                current_retry_count += 1
                
                # 获取完整的错误详情
                error_details = getattr(e, "detail", None) if isinstance(e, HTTPException) else None
                if isinstance(error_details, (dict, list)):
                    try:
                        full_error = json.dumps(error_details, ensure_ascii=False)
                    except Exception:
                        full_error = str(error_details)
                elif isinstance(e, HTTPException):
                    full_error = str(error_details) if error_details is not None else str(e)
                else:
                    full_error = str(e)

                retry_path.append({
                    "provider": provider_name,
                    "error": full_error[:2000],  # 增加错误信息长度限制到 2000 字符
                    "status_code": None  # 稍后更新
                })

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
                    
                    # 检测 HTTP/2 StreamReset 错误，自动重置连接池
                    error_str = str(e)
                    if "StreamReset" in error_str or "stream_id" in error_str:
                        try:
                            # 从 provider 的 base_url 提取 host 并重置连接
                            base_url = provider.get('base_url', '')
                            if base_url:
                                host = urlparse(base_url).netloc
                                if host and hasattr(self.app.state, 'client_manager'):
                                    await self.app.state.client_manager.reset_client(host)
                                    logger.info(f"Auto-reset HTTP/2 connection for {host} due to StreamReset error")
                        except Exception as reset_err:
                            logger.warning(f"Failed to auto-reset connection: {reset_err}")
                elif isinstance(e, httpx.LocalProtocolError):
                    status_code = 502  # Bad Gateway
                    error_message = "Local protocol error"
                elif isinstance(e, HTTPException):
                    status_code = e.status_code
                    # 错误解析应尽量由各渠道适配器完成，这里只做通用兜底。
                    error_message = str(getattr(e, "detail", None) or str(e))
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
                    # provider 列表发生变化（或重新排序）时，重算最大尝试次数
                    retry_count = _calc_retry_count(matching_providers)
                    max_attempts = num_matching_providers + retry_count
                    if num_matching_providers != last_num_matching_providers:
                        index = 0

                cooling_time = safe_get(provider, "preferences", "api_key_cooldown_period", default=0)
                # 仅统计“启用”的 key 数量，避免禁用 key 造成误判
                try:
                    api_key_count = provider_api_circular_list[channel_id].get_enabled_items_count()
                except Exception:
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

                # 更新重试路径中的状态码
                if retry_path:
                    retry_path[-1]["status_code"] = status_code

                retry_enabled = (
                    auto_retry
                    and (
                        status_code not in [400, 413]
                        or urlparse(provider.get('base_url', '')).netloc == 'models.inference.ai.azure.com'
                    )
                )

                # 若还有剩余尝试次数，则进行自动重试（并对 429/5xx 做简单退避，避免瞬时打爆上游/卡死进程）
                if retry_enabled and index < max_attempts:
                    if status_code in {429, 500, 502, 503, 504}:
                        base_delay = 0.5 if status_code == 429 else 0.2
                        # current_retry_count 从 1 开始；最多指数到 2^5，再封顶 5 秒
                        delay = min(5.0, base_delay * (2 ** min(max(current_retry_count - 1, 0), 5)))
                        await asyncio.sleep(delay)
                    continue

                # retry_enabled 但已无重试额度：跳出循环，走统一的“所有重试失败”出口
                if retry_enabled and index >= max_attempts:
                    break

                # 不重试：直接返回本次错误
                # 失败时也记录重试信息和统计
                current_info = self.request_info_getter()
                if retry_path:
                    current_info["retry_path"] = json.dumps(retry_path, ensure_ascii=False)
                current_info["retry_count"] = current_retry_count
                current_info["success"] = False
                current_info["status_code"] = status_code
                # 记录处理时间
                if "start_time" in current_info:
                    process_time = time() - current_info["start_time"]
                    current_info["process_time"] = process_time
                # 写入失败统计
                background_tasks.add_task(update_stats, current_info, app=self.app)
                return openai_error_response(
                    f"Error: Current provider response failed: {error_message}",
                    status_code,
                )

        # 所有重试都失败
        current_info = self.request_info_getter()
        current_info["first_response_time"] = -1
        current_info["success"] = False
        current_info["status_code"] = status_code
        current_info["provider"] = None
        # 记录最终的重试信息
        if retry_path:
            current_info["retry_path"] = json.dumps(retry_path, ensure_ascii=False)
        current_info["retry_count"] = current_retry_count
        # 记录处理时间
        if "start_time" in current_info:
            process_time = time() - current_info["start_time"]
            current_info["process_time"] = process_time
        # 写入失败统计
        background_tasks.add_task(update_stats, current_info, app=self.app)
        return openai_error_response(
            f"All {request_data.model} error: {error_message}",
            status_code,
        )
