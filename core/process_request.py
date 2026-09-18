"""
普通上游请求处理模块。

修改原因：core.handler.py 文件过大，普通上游请求处理逻辑需要独立维护。
修改方式：将原 handler.py 中的 process_request 函数原样迁移到本文件，并由 core.handler 重新导出。
目的：保持外部导入路径兼容，同时降低 handler.py 的维护成本。
"""

import asyncio
import json
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING, Union

import httpx
from fastapi import BackgroundTasks, HTTPException
from starlette.responses import Response

from core.log_config import logger
from core.models import (
    AudioTranscriptionRequest,
    EmbeddingRequest,
    ImageGenerationRequest,
    ModerationRequest,
    RequestModel,
)
from core.request import get_payload
from core.response import fetch_response, fetch_response_stream
from core.streaming import LoggingStreamingResponse
from core.stream_errors import reset_stream_state, UpstreamStreamError, extract_stream_error
from core.byok import get_byok_real_key, is_byok_provider
from core.utils import get_engine, is_local_api_key, provider_api_circular_list
from utils import apply_custom_headers, error_handling_wrapper, safe_get

if TYPE_CHECKING:
    from fastapi import FastAPI

# 修改原因：process_request 的默认值原来绑定 handler.DEFAULT_TIMEOUT，函数拆出后不能在模块顶层反向导入 handler。
# 修改方式：在本模块保留同值常量，仅用于函数默认参数绑定。
# 目的：保持默认超时时间不变，并避免与 core.handler 的兼容导入形成循环依赖。
DEFAULT_TIMEOUT = 600


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
    keepalive_interval: Optional[int] = None,
    force_api_key: Optional[str] = None,
    api_key_info: Optional[Dict[str, Any]] = None,
    key_enabled_plugins: Optional[List[str]] = None,
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
    # 修改原因：core.handler 顶层需要重新导出 process_request，顶层反向导入 handler 状态会形成循环导入。
    # 修改方式：在请求执行时延迟导入 handler 中保留的 helper 和 debug 标志。
    # 目的：共享 set_debug_mode、OAuth 解析和统计写入逻辑，同时避免模块初始化时互相等待。
    from core.handler import _fire_and_forget_channel_stats, _resolve_oauth_api_key, is_debug

    timeout_value = int(timeout_value)
    model_dict = provider["_model_dict_cache"]
    original_model = model_dict[request.model]
    
    channel_id = f"{provider['provider']}"
    current_info_early = request_info_getter()
    reset_stream_state(current_info_early)
    current_info_early["_provider_cfg"] = provider
    current_info_early["provider_id"] = channel_id
    byok_context_key = (
        current_info_early.get("_byok_real_key")
        or current_info_early.get("byok_real_key")
        or get_byok_real_key()
    )
    # 修改原因：force_api_key 同时被渠道测试和 BYOK 使用，不能仅凭 provider.api 为空判断为 BYOK。
    # 修改方式：只有 force_api_key 与鉴权阶段写入的 BYOK 上下文真实 key 一致时，才按 BYOK 请求处理。
    # 目的：真实 BYOK 请求不进入本地 key 池；普通单渠道测试仍保留 provider_api_key 统计和 key_rules 定位能力。
    byok_provider_request = bool(force_api_key) and force_api_key == byok_context_key and is_byok_provider(provider)
    if byok_provider_request:
        api_key = force_api_key
    elif force_api_key:
        api_key = force_api_key
    elif is_local_api_key(provider['provider']):
        api_key = provider['provider']
    elif provider.get("api"):
        # 修改原因：provider_api_circular_list 已改为普通 dict，读取缺失 provider 不应再创建空 key 池。
        # 修改方式：使用 get 读取现有循环列表，缺失时返回明确的配置错误。
        # 目的：避免请求路径因 provider 名不存在而产生长期驻留的空 ThreadSafeCircularList。
        circular_list = provider_api_circular_list.get(provider['provider'])
        if not circular_list:
            raise HTTPException(status_code=404, detail=f"Provider '{provider['provider']}' API key pool not found")
        api_key = await circular_list.next(original_model)
    else:
        api_key = None

    # 修改原因：BYOK 的渠道统计需要归集到 provider api 的显式占位符，而不能继续写入空值。
    # 修改方式：真实上游请求仍使用 force_api_key 中的用户 key，统计和 _used_api_key 只写入 "*"。
    # 目的：让 channel_stats.provider_api_key 可按 BYOK provider 的 "*" 统一聚合，同时不泄露真实 key。
    original_api_key = "*" if byok_provider_request else api_key

    # 将实际使用的 api_key 提前存入 request_info，供重试循环精确定位出错的 key
    current_info_early["_used_api_key"] = original_api_key
    current_info_early["_is_byok_request"] = byok_provider_request
    # 修改原因：OAuth 凭据现在按 provider name 分层，响应 wrapper 的被动 quota 采集也需要知道当前渠道。
    # 修改方式：在请求早期写入 _oauth_channel_id，并把同一 channel_id 传给 OAuthManager.resolve。
    # 目的：让 access_token 解析和 quota 回写都只作用于当前渠道。
    current_info_early["_oauth_channel_id"] = channel_id
    api_key = await _resolve_oauth_api_key(app, api_key, channel_id=channel_id)

    engine, stream_override, stream_mode = get_engine(provider, endpoint, original_model)

    if stream_override is not None:
        request.stream = stream_override

    # 记录 provider 活跃度（内存级，O(1)）
    try:
        from routes.stats import record_provider_activity
        record_provider_activity(channel_id)
    except Exception:
        pass
    if engine != "moderation":
        logger.info(f"provider: {channel_id[:11]:<11} model: {request.model:<22} engine: {engine[:13]:<13} role: {role}")

    last_message_role = safe_get(request, "messages", -1, "role", default=None)
    
    # 提前计算代理，以便 get_payload 内部创建的裸 httpx.AsyncClient 也能走代理
    proxy = safe_get(app.state.config, "preferences", "proxy", default=None)  # global proxy
    proxy = safe_get(provider, "preferences", "proxy", default=proxy)  # provider proxy

    from core.http import proxy_context
    with proxy_context(proxy):
        url, headers, payload = await get_payload(request, engine, provider, api_key)
    # custom headers 已在 get_payload → apply_request_interceptors 之前统一应用
    

    current_info = request_info_getter()
    
    # 记录发送到上游的请求头和请求体（如果配置了保留时间）
    if current_info.get("raw_data_expires_at"):
        try:
            # 记录上游请求头（过滤敏感头信息）
            # 修改原因：Gemini BYOK 会把真实上游 key 放在 x-goog-api-key，上游请求头日志不能保存该值。
            # 修改方式：在原有认证头过滤基础上补充 x-goog-api-key。
            # 目的：开启原始日志保留时也不会泄露用户提供的真实上游 key。
            safe_upstream_headers = {k: v for k, v in headers.items()
                                    if k.lower() not in ("authorization", "x-api-key", "api-key", "x-goog-api-key")}
            current_info["upstream_request_headers"] = json.dumps(safe_upstream_headers, ensure_ascii=False)
            
            # upstream_request_body 已移到 response.py fetch 层记录（能抓到 force_stream 等插件修改后的真实值）
        except Exception as e:
            logger.error(f"Error saving upstream request data: {str(e)}")
    # 确保日志中一定记录模型名（使用当前请求对象上的 model）
    if hasattr(request, "model") and getattr(request, "model", None):
        current_info["model"] = request.model
    
    # 记录渠道ID和上游key索引
    current_info["provider_id"] = channel_id
    current_info["_provider_cfg"] = provider  # stream_guard key_rules 用
    if byok_provider_request:
        # 修改原因：BYOK 请求没有本地 provider key pool 索引，不能把真实上游 key 映射到 provider_key_index。
        # 修改方式：显式写 None，覆盖测试或特殊入口中缺失初始化的 request_info。
        # 目的：让日志和统计消费者都能明确知道本次没有本地渠道 key 索引。
        current_info["provider_key_index"] = None
    # 修改原因：BYOK 的 original_api_key 是统计占位符 "*"，不能参与本地 key 索引计算。
    # 修改方式：仅非 BYOK 请求进入 provider_api_circular_list 索引匹配。
    # 目的：保持 provider_key_index 为 None，防止 "*" 被当作本地上游 key 处理。
    if original_api_key and not byok_provider_request:
        try:
            # 修改原因：OAuth 解析后 api_key 已是 access_token，不能用于 provider.api 索引匹配。
            # 修改方式：索引匹配始终使用 original_api_key，也就是配置中的 key_id。
            # 目的：避免 token 明文进入统计索引逻辑，并保持自动冷却定位正确。
            circular_list = provider_api_circular_list.get(provider['provider'])
            if circular_list and hasattr(circular_list, 'items'):
                api_keys_list = circular_list.items
                if original_api_key in api_keys_list:
                    current_info["provider_key_index"] = api_keys_list.index(original_api_key)
        except (ValueError, TypeError, AttributeError):
            pass

    proxy = safe_get(app.state.config, "preferences", "proxy", default=None)  # global proxy
    proxy = safe_get(provider, "preferences", "proxy", default=proxy)  # provider proxy
    
    # 获取该渠道启用的插件列表
    enabled_plugins = safe_get(provider, "preferences", "enabled_plugins", default=None)
    # 修改原因：新增 key_outbound 阶段需要 Key 级 enabled_plugins，不能复用渠道级 enabled_plugins。
    # 修改方式：由 handler 传入 api_key_info 和 key_enabled_plugins；缺失时使用空字典和 None 兼容测试路径。
    # 目的：让响应返回路径能够分别执行渠道出站和 Key 出站拦截器。
    api_key_info = api_key_info or {}

    # 判断实际上游流式模式（stream_mode 核心逻辑）
    client_wants_stream = bool(request.stream)
    if stream_mode == "force_stream":
        upstream_stream = True
    elif stream_mode == "force_non_stream":
        upstream_stream = False
    else:  # auto
        upstream_stream = client_wants_stream

    # 强制流式时确保 payload 里也带 stream=True
    if upstream_stream and not client_wants_stream and stream_mode == "force_stream":
        payload = dict(payload)
        payload["stream"] = True
        logger.info(f"[stream_mode] force_stream: client=non-stream, upstream=stream, model={original_model}")
    elif not upstream_stream and client_wants_stream and stream_mode == "force_non_stream":
        payload = dict(payload)
        payload["stream"] = False
        logger.info(f"[stream_mode] force_non_stream: client=stream, upstream=non-stream, model={original_model}")

    # Gemini/Vertex URL 适配：流式和非流式用不同端点
    if upstream_stream and "generateContent" in url and "streamGenerateContent" not in url:
        url = url.replace("generateContent", "streamGenerateContent")
    elif not upstream_stream and "streamGenerateContent" in url:
        url = url.replace("streamGenerateContent", "generateContent")

    try:
        async with app.state.client_manager.get_client(url, proxy) as client:
            if upstream_stream:
                generator = fetch_response_stream(
                    client, url, headers, payload, engine, original_model, timeout_value,
                    enabled_plugins=enabled_plugins,
                    provider=provider,
                    api_key_info=api_key_info,
                    key_enabled_plugins=key_enabled_plugins,
                )
                wrapped_generator, first_response_time = await error_handling_wrapper(
                    generator, channel_id, engine, True,
                    app.state.error_triggers, keepalive_interval=keepalive_interval,
                    last_message_role=last_message_role,
                    request_url=url,
                    app=app,
                    current_info=current_info,
                )

                if client_wants_stream:
                    # 正常流式：直接转发
                    response = LoggingStreamingResponse(
                        wrapped_generator,
                        media_type="text/event-stream",
                        current_info=current_info,
                        app=app,
                        debug=is_debug
                    )
                else:
                    # force_stream：上游流式 → 拼装成非流式 JSON 返回客户端
                    from .stream_convert import assemble_stream_to_json
                    assembled = await assemble_stream_to_json(wrapped_generator)
                    error = current_info.get("_stream_error") or extract_stream_error(assembled)
                    if error:
                        raise UpstreamStreamError(error)

                    async def force_stream_iter():
                        yield json.dumps(assembled, ensure_ascii=False)

                    response = LoggingStreamingResponse(
                        force_stream_iter(),
                        media_type="application/json",
                        current_info=current_info,
                        app=app,
                        debug=is_debug
                    )
            else:
                generator = fetch_response(
                    client, url, headers, payload, engine, original_model, timeout_value,
                    enabled_plugins=enabled_plugins,
                    provider=provider,
                    api_key_info=api_key_info,
                    key_enabled_plugins=key_enabled_plugins,
                )
                wrapped_generator, first_response_time = await error_handling_wrapper(
                    generator, channel_id, engine, False,
                    app.state.error_triggers, keepalive_interval=keepalive_interval,
                    last_message_role=last_message_role,
                    request_url=url,
                    app=app,
                    current_info=current_info,
                )

                if not client_wants_stream:
                    # 正常非流式
                    if endpoint == "/v1/audio/speech":
                        if isinstance(wrapped_generator, bytes):
                            response = Response(content=wrapped_generator, media_type="audio/mpeg")
                    else:
                        response = LoggingStreamingResponse(
                            wrapped_generator,
                            media_type="application/json",
                            current_info=current_info,
                            app=app,
                            debug=is_debug
                        )
                else:
                    # force_non_stream：上游非流式 → 拆成 SSE 返回客户端
                    from .stream_convert import convert_json_to_sse
                    first_element = await anext(wrapped_generator)

                    async def force_non_stream_iter():
                        if isinstance(first_element, dict):
                            async for sse_chunk in convert_json_to_sse(first_element, original_model):
                                yield sse_chunk
                        else:
                            yield first_element

                    response = LoggingStreamingResponse(
                        force_non_stream_iter(),
                        media_type="text/event-stream",
                        current_info=current_info,
                        app=app,
                        debug=is_debug
                    )

            # 流式渠道结果等待发送结束；非流式内部消费者不经过 ASGI，保持原有统计入口。
            stats_args = (current_info["request_id"], channel_id, request.model, current_info["api_key"])
            if isinstance(response, LoggingStreamingResponse) and response.media_type == "text/event-stream":
                current_info["_channel_stats_call"] = (
                    update_channel_stats_func, stats_args, {"provider_api_key": original_api_key},
                )
            else:
                _fire_and_forget_channel_stats(
                    update_channel_stats_func, *stats_args, success=True,
                    provider_api_key=original_api_key,
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
            provider_api_key=original_api_key,
        )
        raise e
