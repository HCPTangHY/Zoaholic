"""
方言路由自动注册模块

扫描所有已注册方言的 endpoints，自动创建 FastAPI 路由。
"""

import json
from typing import Any, Dict, TYPE_CHECKING

from fastapi import APIRouter, Request, BackgroundTasks, Depends
from fastapi.responses import JSONResponse

from core.error_response import openai_error_response
from .registry import get_dialect, list_dialects, EndpointDefinition

if TYPE_CHECKING:
    from starlette.responses import Response

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
    from core.auth import security, _extract_token
    from fastapi.security import HTTPAuthorizationCredentials

    async def verify(
        request: Request,
        credentials: HTTPAuthorizationCredentials = Depends(security),
    ) -> int:
        app = request.app
        api_list = app.state.api_list
        
        dialect = get_dialect(dialect_id)
        token = None
        
        # 优先使用方言自定义的 token 提取器
        if dialect and dialect.extract_token:
            token = await dialect.extract_token(request)
        
        # 否则使用默认提取器
        if not token:
            token = await _extract_token(request, credentials)
        
        if not token:
            from fastapi import HTTPException
            raise HTTPException(status_code=403, detail="Invalid or missing API Key")
        
        try:
            api_index = api_list.index(token)
        except ValueError:
            from fastapi import HTTPException
            raise HTTPException(status_code=403, detail="Invalid or missing API Key")
        
        # 更新 request_info 中的 API key 信息，确保统计记录正确的 key
        try:
            from core.middleware import request_info
            from utils import safe_get
            info = request_info.get()
            if info:
                info["api_key"] = token
                config = app.state.config
                info["api_key_name"] = safe_get(config, "api_keys", api_index, "name", default=None)
                info["api_key_group"] = safe_get(config, "api_keys", api_index, "group", default=None)
        except Exception:
            pass

        return api_index

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
            native_body: Dict[str, Any] = await request.json()
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
        )

        if resp.headers.get("x-zoaholic-passthrough") or resp.status_code != 200:
            return resp

        if resp.media_type == "text/event-stream" and hasattr(resp, "body_iterator"):
            current_info = getattr(resp, "current_info", {}) or {}
            app = getattr(resp, "app", None)
            debug = getattr(resp, "debug", False)

            async def convert_stream():
                async for chunk in resp.body_iterator:
                    chunk_text = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
                    converted = await dialect.render_stream(chunk_text) if dialect.render_stream else chunk_text
                    if converted:
                        yield converted

            return LoggingStreamingResponse(convert_stream(), media_type="text/event-stream",
                                            current_info=current_info, app=app, debug=debug,
                                            dialect_id=dialect_id)

        body_bytes = await _read_response_bytes(resp)
        body_text = body_bytes.decode("utf-8") if body_bytes else "{}"
        
        try:
            canonical_json = json.loads(body_text)
        except Exception:
            canonical_json = {}

        current_info = getattr(resp, "current_info", {}) or {}
        
        converted_json = await dialect.render_response(canonical_json, canonical_request.model) if dialect.render_response else canonical_json

        async def converted_iter():
            yield json.dumps(converted_json, ensure_ascii=False)

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
