"""
透传请求处理模块。

修改原因：core.handler.py 文件过大，透传请求处理逻辑需要独立维护。
修改方式：将原 handler.py 中的透传 helper 和 process_request_passthrough 原样迁移到本文件，并由 core.handler 重新导出。
目的：保持外部导入路径兼容，同时让普通请求与透传请求的职责边界更清楚。
"""

import asyncio
import json
from typing import Any, Callable, Dict, Optional, TYPE_CHECKING

import httpx
from fastapi import BackgroundTasks, HTTPException
from starlette.responses import Response

from core.json_utils import json_dumps_text
from core.log_config import logger
from core.models import RequestModel
from core.request import get_payload
from core.response import check_response
from core.streaming import LoggingStreamingResponse
from core.utils import get_engine, is_local_api_key, provider_api_circular_list
from utils import apply_custom_headers, has_header_case_insensitive, safe_get, wait_for_timeout

if TYPE_CHECKING:
    from fastapi import FastAPI

# 修改原因：process_request_passthrough 的默认值原来绑定 handler.DEFAULT_TIMEOUT，函数拆出后不能在模块顶层反向导入 handler。
# 修改方式：在本模块保留同值常量，仅用于函数默认参数绑定。
# 目的：保持默认超时时间不变，并避免与 core.handler 的兼容导入形成循环依赖。
DEFAULT_TIMEOUT = 600


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


async def _fetch_passthrough_stream(client, url, headers, payload, timeout, engine=None, model=None, enabled_plugins=None):
    """
    透传模式的流式响应处理
    
    直接转发上游 SSE 流，不做任何格式转换
    
    注意：使用特殊的超时配置，read timeout 设置为 None 以支持
    Google Search grounding 等需要长时间处理的操作。
    """
    from .response import _log_upstream_request
    _log_upstream_request(url, payload)
    
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
    
    json_payload = await asyncio.to_thread(json_dumps_text, payload)
    async with client.stream('POST', url, headers=headers, content=json_payload, timeout=stream_timeout) as response:
        from core.plugins.interceptors import apply_response_interceptors
        error_message = await check_response(response, "passthrough_stream")
        if error_message:
            error_message = await apply_response_interceptors(error_message, engine or "passthrough", model or "", is_stream=True, enabled_plugins=enabled_plugins)
            yield error_message            
            return
        
        # aiter_text 由 httpx 内部处理 UTF-8 解码（含多字节字符边界），
        # SSE 服务端通常在每个事件后 flush，因此每个 chunk 大概率是完整的 SSE 事件。
        async for text in response.aiter_text():
            if text:
                text = await apply_response_interceptors(text, engine or "passthrough", model or "", is_stream=True, enabled_plugins=enabled_plugins)
                yield text


async def _fetch_passthrough_response(client, url, headers, payload, timeout, engine=None, model=None, enabled_plugins=None):
    """
    透传模式的非流式响应处理
    
    直接转发上游 JSON 响应，不做任何格式转换
    """
    from .response import _log_upstream_request
    _log_upstream_request(url, payload)
    
    import time as _time
    t0 = _time.time()
    from core.plugins.interceptors import apply_response_interceptors
    
    json_payload = await asyncio.to_thread(json_dumps_text, payload)
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

    # 快路径：未启用响应插件时，直接按文本流转发。
    # 这样可以避免先 aread() 再 decode() 带来的整包双份内存占用。
    if not enabled_plugins:
        async with client.stream('POST', url, headers=headers, content=json_payload, timeout=request_timeout) as response:
            t2 = _time.time()
            logger.debug(f"[passthrough] POST request took {t2-t1:.3f}s, status={response.status_code}")

            error_message = await check_response(response, "passthrough_non_stream")
            if error_message:
                yield error_message
                return

            async for text_chunk in response.aiter_text():
                if text_chunk:
                    yield text_chunk
        return

    response = await client.post(url, headers=headers, content=json_payload, timeout=request_timeout)
    t2 = _time.time()
    logger.debug(f"[passthrough] POST request took {t2-t1:.3f}s, status={response.status_code}")

    error_message = await check_response(response, "passthrough_non_stream")
    if error_message:
        error_message = await apply_response_interceptors(error_message, engine or "passthrough", model or "", is_stream=False, enabled_plugins=enabled_plugins)
        yield error_message
        return

    response_bytes = await response.aread()
    t3 = _time.time()
    logger.debug(f"[passthrough] aread() took {t3-t2:.3f}s, size={len(response_bytes)} bytes")

    result = response_bytes.decode("utf-8")
    result = await apply_response_interceptors(result, engine or "passthrough", model or "", is_stream=False, enabled_plugins=enabled_plugins)
    yield result


async def _passthrough_error_wrapper(generator, channel_id, keepalive_interval: Optional[int] = None):
    """
    透传模式的简单错误包装器。

    - 只检测 HTTP 错误（由 check_response 完成），不做 JSON 解析。
    - 对 SSE 透传流注入注释帧 keepalive，保持与普通流式路径一致的空闲保活语义。
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
    
    # 透传模式：直接获取第一个 chunk，不做额外过滤。
    # SSE 流的内容（如 event:, data:）都是有效内容，不应该被跳过。
    gen = wrapped()

    async def final_gen(first=None, wait_task=None, emit_initial_keepalive: bool = False):
        if first is not None:
            yield first

        if keepalive_interval:
            # 修改原因：透传流同样可能在首包前或两次事件之间长时间静默。
            # 修改方式：复用 wait_for_timeout 的单飞 __anext__ 任务，超时只发 SSE 注释帧，不并发拉取上游。
            # 目的：不解析/改写透传协议内容的前提下，为下游连接提供稳定空闲字节。
            if emit_initial_keepalive:
                yield ": keepalive\n\n"
            while True:
                try:
                    item, status = await wait_for_timeout(gen, timeout=keepalive_interval, wait_task=wait_task)
                    if status == "timeout":
                        wait_task = item
                        yield ": keepalive\n\n"
                        continue
                    if status == "reentrant":
                        wait_task = None
                        await asyncio.sleep(keepalive_interval)
                        yield ": keepalive\n\n"
                        continue
                    wait_task = None
                    yield item
                except asyncio.CancelledError:
                    if wait_task is not None and not wait_task.done():
                        wait_task.cancel()
                    logger.debug(f"provider: {channel_id:<11} passthrough stream cancelled by client")
                    return
                except StopAsyncIteration:
                    if wait_task is not None and not wait_task.done():
                        wait_task.cancel()
                    return
        else:
            async for chunk in gen:
                yield chunk

    try:
        if keepalive_interval:
            first, status = await wait_for_timeout(gen, timeout=keepalive_interval)
            if status == "timeout":
                return final_gen(wait_task=first, emit_initial_keepalive=True), 3.1415
            if status == "reentrant":
                return final_gen(emit_initial_keepalive=True), 3.1415
        else:
            first = await gen.__anext__()
    except StopAsyncIteration:
        raise HTTPException(status_code=502, detail="Upstream server returned an empty response.")
    
    return final_gen(first=first), first_response_time or (time_now() - start_time)


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
    # 修改原因：core.handler 顶层需要重新导出透传函数，顶层反向导入 handler 状态会形成循环导入。
    # 修改方式：在透传请求执行时延迟导入 handler 中保留的 helper 和 debug 标志。
    # 目的：共享 set_debug_mode、OAuth 解析和统计写入逻辑，同时避免模块初始化时互相等待。
    from core.handler import _fire_and_forget_channel_stats, _resolve_oauth_api_key, is_debug

    from core.dialects.passthrough import apply_passthrough_modifications
    from core.plugins.interceptors import apply_request_interceptors
    from core.channels import get_channel

    timeout_value = int(timeout_value)
    model_dict = provider["_model_dict_cache"]
    original_model = model_dict[request.model]

    channel_id = f"{provider['provider']}"
    if is_local_api_key(provider["provider"]):
        api_key = provider["provider"]
    elif provider.get("api"):
        # 修改原因：provider_api_circular_list 已改为普通 dict，读取缺失 provider 不应再创建空 key 池。
        # 修改方式：使用 get 读取现有循环列表，缺失时返回明确的配置错误。
        # 目的：避免透传请求路径因 provider 名不存在而产生长期驻留的空 ThreadSafeCircularList。
        circular_list = provider_api_circular_list.get(provider["provider"])
        if not circular_list:
            raise HTTPException(status_code=404, detail=f"Provider '{provider['provider']}' API key pool not found")
        api_key = await circular_list.next(original_model)
    else:
        api_key = None

    original_api_key = api_key

    # 将实际使用的 api_key 提前存入 request_info，供重试循环精确定位出错的 key
    current_info_early = request_info_getter()
    current_info_early["_used_api_key"] = original_api_key
    # 修改原因：透传路径同样可能命中 OAuth 渠道，且 Codex 被动 quota 采集发生在响应读取阶段。
    # 修改方式：在透传请求早期保存 _oauth_channel_id，并按当前 provider name 解析 OAuth key_id。
    # 目的：避免透传请求从其他渠道读取同名账号凭据。
    current_info_early["_oauth_channel_id"] = channel_id
    api_key = await _resolve_oauth_api_key(app, api_key, channel_id=channel_id)

    engine, stream_override, stream_mode = get_engine(provider, endpoint, original_model)
    if stream_override is not None:
        request.stream = stream_override

    channel = get_channel(engine)
    adapter = (channel.passthrough_adapter if channel else None) or (channel.request_adapter if channel else None)
    if not adapter:
        raise ValueError(f"Unknown engine: {engine}")

    # 提前计算代理，以便 adapter 内部创建的裸 httpx.AsyncClient 也能走代理
    proxy = safe_get(app.state.config, "preferences", "proxy", default=None)
    proxy = safe_get(provider, "preferences", "proxy", default=proxy)

    from core.http import proxy_context
    with proxy_context(proxy):
        url, adapter_headers, _ = await adapter(request, engine, provider, api_key)

    # ── 透传 URL 路径修正 ──
    # passthrough_adapter 返回的 URL 对应方言的"主端点"（如 Claude 的 /messages）。
    # 当入口请求是子路径（如 /v1/messages/count_tokens）时，需要追加路径后缀。
    #
    # 后缀从端点的 passthrough_root 显式配置计算，不依赖 adapter URL 的路径结构，
    # 因此无论 base_url 配成什么样（如 https://proxy.com/anthropic/v1）都能正确工作。
    if endpoint and passthrough_ctx.dialect_id:
        from core.dialects.registry import get_dialect as _get_dialect
        _dialect = _get_dialect(passthrough_ctx.dialect_id)
        if _dialect:
            # 查找匹配当前 endpoint 的透传根路径（显式配置，不依赖路由模板字符串）
            _root = None
            for _ep in _dialect.endpoints:
                if _ep.passthrough_root and endpoint.startswith(_ep.passthrough_root):
                    if _root is None or len(_ep.passthrough_root) > len(_root):
                        _root = _ep.passthrough_root
            # 用 passthrough_root 计算后缀：
            # 例如 root="/v1/messages", endpoint="/v1/messages/count_tokens" → suffix="/count_tokens"
            if _root and len(endpoint) > len(_root):
                _suffix = endpoint[len(_root):]  # 如 "/count_tokens"
                url = url.rstrip("/") + _suffix

    headers: Dict[str, Any] = dict(adapter_headers or {})
    apply_custom_headers(headers, _filter_passthrough_headers(passthrough_ctx.original_headers))
    apply_custom_headers(headers, safe_get(provider, "preferences", "headers", default={}))
    if not has_header_case_insensitive(headers, "Content-Type"):
        headers["Content-Type"] = "application/json"

    payload = apply_passthrough_modifications(
        passthrough_ctx.original_payload,
        passthrough_ctx.modifications,
        passthrough_ctx.dialect_id,
        request_model=request.model,
        original_model=original_model,
    )

    # 渠道级透传 payload 修饰（把"渠道特殊逻辑"收敛在各自 channel 文件内）
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
    current_info["dialect_id"] = passthrough_ctx.dialect_id

    if current_info.get("raw_data_expires_at"):
        safe_upstream_headers = {
            k: v for k, v in headers.items()
            if k.lower() not in ("authorization", "x-api-key", "api-key", "x-goog-api-key")
        }
        current_info["upstream_request_headers"] = json.dumps(safe_upstream_headers, ensure_ascii=False)
        # upstream_request_body 已移到 response.py fetch 层记录

    if getattr(request, "model", None):
        current_info["model"] = request.model

    current_info["provider_id"] = channel_id
    current_info["_provider_cfg"] = provider  # stream_guard key_rules 用
    if original_api_key:
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

    proxy = safe_get(app.state.config, "preferences", "proxy", default=None)
    proxy = safe_get(provider, "preferences", "proxy", default=proxy)

    # 透传路径的 stream_mode 处理（与非透传路径对齐）
    client_wants_stream = bool(request.stream)
    if stream_mode == "force_stream":
        upstream_stream = True
    elif stream_mode == "force_non_stream":
        upstream_stream = False
    else:
        upstream_stream = client_wants_stream

    if upstream_stream and not client_wants_stream and stream_mode == "force_stream":
        payload = dict(payload) if not isinstance(payload, dict) else {**payload}
        payload["stream"] = True
        logger.info(f"[stream_mode/passthrough] force_stream: client=non-stream, upstream=stream, model={original_model}")
    elif not upstream_stream and client_wants_stream and stream_mode == "force_non_stream":
        payload = dict(payload) if not isinstance(payload, dict) else {**payload}
        payload["stream"] = False
        logger.info(f"[stream_mode/passthrough] force_non_stream: client=stream, upstream=non-stream, model={original_model}")

    # Gemini/Vertex URL 适配
    if upstream_stream and "generateContent" in url and "streamGenerateContent" not in url:
        url = url.replace("generateContent", "streamGenerateContent")
    elif not upstream_stream and "streamGenerateContent" in url:
        url = url.replace("streamGenerateContent", "generateContent")

    try:
        async with app.state.client_manager.get_client(url, proxy) as client:
            last_message_role = safe_get(request, "messages", -1, "role", default=None)

            if upstream_stream:
                # 修改原因：AWS Bedrock 透传流式响应不是普通 SSE，默认处理器无法解析二进制事件流。
                # 修改方式：若渠道注册了 passthrough_stream_adapter，则优先使用渠道专用处理器。
                # 目的：只让需要特殊解码的渠道接管透传响应读取，其他渠道继续走通用原样转发。
                if channel and getattr(channel, "passthrough_stream_adapter", None):
                    generator = channel.passthrough_stream_adapter(
                        client, url, headers, payload, original_model, timeout_value
                    )
                else:
                    # 透传模式：使用原始流处理，不做格式转换
                    generator = _fetch_passthrough_stream(
                        client, url, headers, payload, timeout_value,
                        engine=engine, model=request.model,
                        enabled_plugins=enabled_plugins,
                    )
                # 使用简单的透传错误包装器，不做 JSON 解析
                wrapped_generator, first_response_time = await _passthrough_error_wrapper(
                    generator, channel_id, keepalive_interval=keepalive_interval
                )

                if client_wants_stream:
                    response = LoggingStreamingResponse(
                        wrapped_generator,
                        media_type="text/event-stream",
                        current_info=current_info,
                        app=app,
                        debug=is_debug,
                    )
                else:
                    # force_stream 透传：上游流式 → 拼装成非流式 JSON
                    from .stream_convert import assemble_stream_to_json
                    assembled = await assemble_stream_to_json(wrapped_generator)

                    async def force_stream_passthrough_iter():
                        yield json.dumps(assembled, ensure_ascii=False)

                    response = LoggingStreamingResponse(
                        force_stream_passthrough_iter(),
                        media_type="application/json",
                        current_info=current_info,
                        app=app,
                        debug=is_debug,
                    )
            else:
                # 修改原因：少数渠道需要在透传非流式路径中复用自己的上游响应读取逻辑。
                # 修改方式：若渠道注册了 passthrough_response_adapter，则优先调用该处理器。
                # 目的：让 AWS Bedrock 的 invoke 响应可以与流式透传一样收敛在 AWS channel 内。
                if channel and getattr(channel, "passthrough_response_adapter", None):
                    generator = channel.passthrough_response_adapter(
                        client, url, headers, payload, original_model, timeout_value
                    )
                else:
                    # 透传模式：使用原始响应处理，不做格式转换
                    generator = _fetch_passthrough_response(
                        client, url, headers, payload, timeout_value,
                        engine=engine, model=request.model,
                        enabled_plugins=enabled_plugins,
                    )
                # 使用简单的透传错误包装器，不做 JSON 解析
                wrapped_generator, first_response_time = await _passthrough_error_wrapper(
                    generator, channel_id
                )

                if client_wants_stream:
                    # force_non_stream 透传：上游非流式 → 拆成 SSE
                    from .stream_convert import convert_json_to_sse

                    async def force_non_stream_passthrough_iter():
                        raw = b""
                        async for chunk in wrapped_generator:
                            raw += chunk if isinstance(chunk, bytes) else chunk.encode()
                        async for sse_line in convert_json_to_sse(raw):
                            yield sse_line

                    response = LoggingStreamingResponse(
                        force_non_stream_passthrough_iter(),
                        media_type="text/event-stream",
                        current_info=current_info,
                        app=app,
                        debug=is_debug,
                    )
                else:
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
            provider_api_key=original_api_key,
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
        provider_api_key=original_api_key,
    )
    current_info["success"] = True
    current_info["status_code"] = 200
    current_info["provider"] = channel_id

    return response
