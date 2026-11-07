"""
方言路由自动注册模块

扫描所有已注册方言的 endpoints，自动创建 FastAPI 路由。
"""

import json
from typing import Any, Dict, TYPE_CHECKING

from core.json_utils import json_loads, json_dumps_text
from core.stream_utils import OwnedAsyncIterator, close_async_iterator
from fastapi import APIRouter, Request, BackgroundTasks, Depends
from fastapi.responses import JSONResponse

from core.error_response import openai_error_response
from .registry import get_dialect, list_dialects, EndpointDefinition

if TYPE_CHECKING:
    from starlette.responses import Response


def _flatten_stream_content(sse_chunk: str) -> str:
    """将 SSE chunk 中结构化的 delta.content list 拍扁为 markdown 字符串。

    作为方言基类的默认行为：不支持结构化图片的方言（OAI/Claude/Responses）
    在 render_stream 之前自动调用。
    """
    if not isinstance(sse_chunk, str) or not sse_chunk.startswith("data: "):
        return sse_chunk

    data_str = sse_chunk[6:].strip()
    if data_str == "[DONE]":
        return sse_chunk

    try:
        chunk = json_loads(data_str)
    except Exception:
        return sse_chunk

    choices = chunk.get("choices") or []
    modified = False
    for choice in choices:
        delta = choice.get("delta")
        if not delta:
            continue
        content = delta.get("content")
        if isinstance(content, list):
            # 拍扁结构化 content items 为 markdown string
            parts = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                item_type = item.get("type", "")
                if item_type == "text":
                    text = item.get("text", "")
                    if text:
                        parts.append(text)
                elif item_type == "image_url":
                    image_url = item.get("image_url")
                    url = ""
                    if isinstance(image_url, dict):
                        url = image_url.get("url", "")
                    elif isinstance(image_url, str):
                        url = image_url
                    if url:
                        parts.append(f"![image]({url})")
            delta["content"] = "\n\n".join(parts) if parts else ""
            modified = True

    if not modified:
        return sse_chunk

    return f"data: {json_dumps_text(chunk, ensure_ascii=False)}\n\n"


# 全局方言路由器
dialect_router = APIRouter()


async def _read_response_bytes(resp: "Response") -> bytes:
    """从响应中读取全部字节"""
    if hasattr(resp, "body_iterator") and resp.body_iterator is not None:
        chunks = []
        async for chunk in resp.body_iterator:
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            chunks.append(chunk)
        return b"".join(chunks)
    return getattr(resp, "body", None) or b""


def _create_dialect_verify_api_key(dialect_id: str):
    """为方言创建 API key 验证依赖"""
    from core.auth import _extract_token, authenticate_api_key_token, security
    from core.ip_blacklist import (
        get_client_ip_from_request_info,
        is_global_ip_blocked,
        raise_ip_blocked,
    )
    from fastapi.security import HTTPAuthorizationCredentials

    async def verify(
        request: Request,
        credentials: HTTPAuthorizationCredentials = Depends(security),
    ) -> int:
        app = request.app
        client_ip = get_client_ip_from_request_info()
        if is_global_ip_blocked(app, client_ip):
            raise_ip_blocked()

        dialect = get_dialect(dialect_id)
        token: str | None = None

        # 优先使用方言自定义的 token 提取器
        if dialect and dialect.extract_token:
            token = await dialect.extract_token(request)

        # 否则使用默认提取器
        if not token:
            token = await _extract_token(request, credentials)

        return authenticate_api_key_token(app, token, client_ip, request=request)

    return verify


def _create_generic_handler(dialect_id: str, endpoint: EndpointDefinition):
    """为方言端点创建通用处理函数"""
    verify_api_key = _create_dialect_verify_api_key(dialect_id)

    async def handler(
        request: Request,
        background_tasks: BackgroundTasks,
        api_index: int = Depends(verify_api_key),
    ):
        from routes.deps import get_model_handler
        from core.streaming import LoggingStreamingResponse

        dialect = get_dialect(dialect_id)
        if not dialect or not dialect.parse_request:
            return openai_error_response(f"{dialect_id} dialect not registered", 500)

        try:
            scope = getattr(request, "scope", {})
            if "_zoaholic_parsed_json" in scope:
                native_body = scope.pop("_zoaholic_parsed_json")
                # Consume the replayed body once so disconnect handling still reads the socket.
                await request.body()
                request._json = native_body
            else:
                native_body = await request.json()
        except Exception:
            native_body = {}

        headers = dict(request.headers)
        path_params = dict(request.path_params)
        if ":" in request.url.path:
            path_params["action"] = request.url.path.split(":")[-1]

        canonical_request = await dialect.parse_request(native_body, path_params, headers)

        model_handler = get_model_handler()
        resp = await model_handler.request_model(
            request_data=canonical_request,
            api_index=api_index,
            background_tasks=background_tasks,
            endpoint=request.url.path,
            dialect_id=dialect_id,
            original_payload=native_body,
            original_headers=headers,
            raw_request=request,
            passthrough_only=endpoint.passthrough_only,
        )

        # Inbound plugins may inspect the original body while request_model runs.
        # Once routing is complete, streaming doesn't need its byte cache.
        if "_zoaholic_parsed_json" not in scope and hasattr(request, "_body"):
            request._body = b""

        if resp.headers.get("x-zoaholic-passthrough") or resp.status_code != 200:
            return resp

        if resp.media_type == "text/event-stream" and hasattr(resp, "body_iterator"):
            current_info = getattr(resp, "current_info", {}) or {}
            app = getattr(resp, "app", None)
            debug = getattr(resp, "debug", False)

            async def convert_stream():
                # 优先使用有状态的流渲染器工厂（如 Claude 方言），
                # 每次流请求创建独立实例以维护 message_start 等生命周期状态
                stream_renderer = dialect.render_stream_factory() if dialect.render_stream_factory else None
                render_fn = stream_renderer or dialect.render_stream
                # 默认拍扁：方言未声明 structured_stream 时，自动将结构化 content list 拍扁为 markdown string
                should_flatten = not dialect.structured_stream
                source_iterator = resp.body_iterator
                try:
                    async for chunk in source_iterator:
                        chunk_text = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
                        if should_flatten:
                            chunk_text = _flatten_stream_content(chunk_text)
                        converted = await render_fn(chunk_text) if render_fn else chunk_text
                        if converted:
                            yield converted
                finally:
                    # 修改原因：关闭外层方言流不会自动关闭 async for 正在消费的内层生成器。
                    # 修改方式：无论正常结束、客户端断开还是转换异常，都显式关闭原响应迭代器。
                    # 目的：退出 client.stream 上下文并把连接归还 httpx 连接池。
                    await close_async_iterator(source_iterator)

            return LoggingStreamingResponse(OwnedAsyncIterator(convert_stream(), resp.body_iterator), media_type="text/event-stream",
                                            current_info=current_info, app=app, debug=debug,
                                            dialect_id=dialect_id)

        body_bytes = await _read_response_bytes(resp)
        body_text = body_bytes.decode("utf-8") if body_bytes else "{}"
        
        try:
            canonical_json = json_loads(body_text)
        except Exception:
            canonical_json = {}

        current_info = getattr(resp, "current_info", {}) or {}
        
        converted_json = await dialect.render_response(canonical_json, canonical_request.model) if dialect.render_response else canonical_json

        async def converted_iter():
            yield json_dumps_text(converted_json, ensure_ascii=False)

        return LoggingStreamingResponse(converted_iter(), media_type="application/json",
                                        current_info=current_info, app=getattr(resp, "app", None),
                                        debug=getattr(resp, "debug", False),
                                        dialect_id=dialect_id)

    return handler


def _create_custom_handler_wrapper(dialect_id: str, endpoint: EndpointDefinition):
    """为自定义处理函数创建包装器"""
    verify_api_key = _create_dialect_verify_api_key(dialect_id)

    async def wrapper(
        request: Request,
        background_tasks: BackgroundTasks,
        api_index: int = Depends(verify_api_key),
    ):
        dialect = get_dialect(dialect_id)

        return await endpoint.handler(request=request, background_tasks=background_tasks,
                                       api_index=api_index, dialect=dialect)

    return wrapper


def register_dialect_routes() -> None:
    """扫描所有已注册方言，自动注册路由"""
    from routes.deps import rate_limit_dependency

    for dialect in list_dialects():
        for endpoint in dialect.endpoints:
            handler = _create_custom_handler_wrapper(dialect.id, endpoint) if endpoint.handler else _create_generic_handler(dialect.id, endpoint)
            dialect_router.add_api_route(
                endpoint.full_path,
                handler,
                methods=endpoint.methods,
                tags=endpoint.tags or [f"{dialect.name} Dialect"],
                summary=endpoint.summary,
                description=endpoint.description,
                dependencies=[Depends(rate_limit_dependency)],
            )
