"""
核心中间件模块

提供：
- request_info 上下文变量
- StatsMiddleware：纯 ASGI 中间件，统一统计、鉴权、道德审查和流式包装
"""

import os
import json
import uuid
import asyncio
import contextvars
from time import time
from typing import Optional, Callable, Any

from pydantic import ValidationError
from fastapi import Request, BackgroundTasks
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.responses import StreamingResponse as FastAPIStreamingResponse
from starlette.responses import StreamingResponse as StarletteStreamingResponse
from starlette.types import ASGIApp, Receive, Send, Scope, Message

from core.log_config import logger
from core.models import ModerationRequest, UnifiedRequest
from core.streaming import LoggingStreamingResponse
from core.stats import update_stats
from utils import safe_get
from db import DISABLE_DATABASE


# 请求级统计信息上下文
request_info = contextvars.ContextVar("request_info", default={})


def get_api_key_from_headers(headers: list) -> Optional[str]:
    """
    从 ASGI headers 中提取 API Key：
    - 优先使用 x-api-key
    - 其次解析 Authorization: Bearer <token>
    """
    token = None
    headers_dict = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in headers}
    
    if headers_dict.get("x-api-key"):
        token = headers_dict.get("x-api-key")
    elif headers_dict.get("authorization"):
        api_split_list = headers_dict.get("authorization").split(" ")
        if len(api_split_list) > 1:
            token = api_split_list[1]
    return token


async def get_api_key(request: Request) -> Optional[str]:
    """
    从请求头中提取 API Key：
    - 优先使用 x-api-key
    - 其次解析 Authorization: Bearer <token>
    """
    token = None
    if request.headers.get("x-api-key"):
        token = request.headers.get("x-api-key")
    elif request.headers.get("Authorization"):
        api_split_list = request.headers.get("Authorization").split(" ")
        if len(api_split_list) > 1:
            token = api_split_list[1]
    return token


def should_attempt_unified_request(body: dict) -> bool:
    """
    仅当 body 含统一请求特征字段时才尝试解析 UnifiedRequest。
    避免对渠道管理等非统一 JSON 路由误触发解析。
    """
    if not isinstance(body, dict):
        return False
    keys = set(body.keys())
    # 统一请求典型字段：聊天(messages)、图像(prompt)、审核/embedding/tts 输入(input)、音频(file: JSON 不会出现，但保留以防客户端 JSON 化)
    if any(k in keys for k in ("messages", "prompt", "input", "file")):
        return True
    return False


class StatsMiddleware:
    """
    纯 ASGI 中间件：统一统计 / 鉴权 / 道德审查 / 流式包装。

    行为：
    - 仅对 /v1 开头的请求生效，其它路径直接放行
    - 从 Header 中读取 API Key，没有则 403
    - 根据配置和付费状态决定是否 429（余额不足）
    - 解析请求体，构造 UnifiedRequest，用于：
        - 记录 model
        - 进行 per-api-key 的限流（user_api_keys_rate_limit）
        - 提取需要审查的文本，调用 /v1/moderations
    - 对流式响应包装为 LoggingStreamingResponse 以记录 usage

    使用纯 ASGI 实现，通过缓存 body 并重放给下游，不会"吃掉"请求体。
    """

    def __init__(self, app: ASGIApp, debug: Optional[bool] = None):
        self.app = app
        if debug is None:
            self.debug = bool(os.getenv("DEBUG", False))
        else:
            self.debug = debug

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 获取路径
        path = scope.get("path", "")
        method = scope.get("method", "GET")

        # OPTIONS 直接放行
        if method == "OPTIONS":
            await self.app(scope, receive, send)
            return

        # 非 /v1 路径不做统计和鉴权
        if not path.startswith("/v1"):
            await self.app(scope, receive, send)
            return

        # 获取 app 实例
        app = scope.get("app")
        if not app:
            await self.app(scope, receive, send)
            return

        start_time = time()
        headers = scope.get("headers", [])

        # 读取 token
        token = get_api_key_from_headers(headers)
        if not token:
            response = JSONResponse(
                status_code=403, content={"error": "Invalid or missing API Key"}
            )
            await response(scope, receive, send)
            return

        enable_moderation = False
        config = app.state.config

        try:
            api_list = app.state.api_list
            api_index = api_list.index(token)
        except ValueError:
            api_index = None

        if api_index is not None:
            enable_moderation = safe_get(
                config,
                "api_keys",
                api_index,
                "preferences",
                "ENABLE_MODERATION",
                default=False,
            )
            if not DISABLE_DATABASE:
                check_api_key = safe_get(config, "api_keys", api_index, "api")
                # 余额检查
                if (
                    safe_get(
                        app.state.paid_api_keys_states,
                        check_api_key,
                        "enabled",
                        default=None,
                    )
                    is False
                    and not path.startswith("/v1/token_usage")
                ):
                    response = JSONResponse(
                        status_code=429,
                        content={
                            "error": "Balance is insufficient, please check your account."
                        },
                    )
                    await response(scope, receive, send)
                    return
        else:
            response = JSONResponse(
                status_code=403, content={"error": "Invalid or missing API Key"}
            )
            await response(scope, receive, send)
            return

        # 获取 client IP
        client = scope.get("client")
        client_ip = client[0] if client else "unknown"

        # 初始化 request_info
        request_id = str(uuid.uuid4())
        request_info_data = {
            "request_id": request_id,
            "start_time": start_time,
            "endpoint": f"{method} {path}",
            "client_ip": client_ip,
            "process_time": 0,
            "first_response_time": -1,
            "provider": None,
            "model": None,
            "success": False,
            "api_key": token,
            "is_flagged": False,
            "text": None,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

        current_request_info = request_info.set(request_info_data)
        current_info = request_info.get()

        # 读取请求体（仅 POST + JSON）
        body_bytes = b""
        parsed_body = None
        headers_dict = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in headers}
        content_type = headers_dict.get("content-type", "")

        if method == "POST" and "application/json" in content_type:
            # 收集所有 body chunks
            body_chunks = []
            while True:
                message = await receive()
                body_chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    break
            body_bytes = b"".join(body_chunks)

            if body_bytes:
                try:
                    parsed_body = json.loads(body_bytes)
                except json.JSONDecodeError:
                    parsed_body = None

        # 创建新的 receive 函数，重放已读取的 body
        body_sent = False
        async def receive_wrapper() -> Message:
            nonlocal body_sent
            if method == "POST" and "application/json" in content_type:
                if not body_sent:
                    body_sent = True
                    return {"type": "http.request", "body": body_bytes, "more_body": False}
                return {"type": "http.request", "body": b"", "more_body": False}
            else:
                return await receive()

        try:
            # 如果能解析为 UnifiedRequest，则执行模型记录/限流/审查
            if parsed_body and should_attempt_unified_request(parsed_body):
                try:
                    request_model = await asyncio.to_thread(UnifiedRequest.model_validate, parsed_body)
                    request_model = request_model.data
                    if self.debug:
                        logger.info(
                            "request_model: %s",
                            json.dumps(request_model.model_dump(exclude_unset=True), indent=2, ensure_ascii=False),
                        )
                    model = request_model.model
                    current_info["model"] = model

                    final_api_key = app.state.api_list[api_index]
                    try:
                        await app.state.user_api_keys_rate_limit[final_api_key].next(model)
                    except Exception:
                        response = JSONResponse(status_code=429, content={"error": "Too many requests"})
                        await response(scope, receive_wrapper, send)
                        return

                    moderated_content = None
                    if request_model.request_type == "chat":
                        moderated_content = request_model.get_last_text_message()
                    elif request_model.request_type == "image":
                        moderated_content = request_model.prompt
                    elif request_model.request_type == "tts":
                        moderated_content = request_model.input
                    elif request_model.request_type == "moderation":
                        pass
                    elif request_model.request_type == "embedding":
                        if isinstance(request_model.input, list) and len(request_model.input) > 0 and isinstance(request_model.input[0], str):
                            moderated_content = "\n".join(request_model.input)
                        else:
                            moderated_content = request_model.input
                    else:
                        logger.error("Unknown request type: %s", request_model.request_type)

                    if enable_moderation and moderated_content:
                        background_tasks_for_moderation = BackgroundTasks()
                        moderation_response = await self._moderate_content(moderated_content, api_index, background_tasks_for_moderation, app)
                        is_flagged = moderation_response.get("results", [{}])[0].get("flagged", False)

                        if is_flagged:
                            logger.error("Content did not pass the moral check: %s", moderated_content)
                            process_time = time() - start_time
                            current_info["process_time"] = process_time
                            current_info["is_flagged"] = is_flagged
                            current_info["text"] = moderated_content
                            await update_stats(current_info, app=app)
                            response = JSONResponse(
                                status_code=400,
                                content={"error": "Content did not pass the moral check, please modify and try again."},
                            )
                            await response(scope, receive_wrapper, send)
                            return
                except ValidationError as e:
                    # 不在中间件返回 422，避免对非统一请求路由造成影响
                    logger.error(
                        "Invalid request body for UnifiedRequest: %s, errors: %s",
                        json.dumps(parsed_body, indent=2, ensure_ascii=False),
                        e.errors(),
                    )

            # 包装 send 以捕获流式响应
            response_started = False
            response_status = 200
            response_headers = []

            async def send_wrapper(message: Message) -> None:
                nonlocal response_started, response_status, response_headers
                if message["type"] == "http.response.start":
                    response_started = True
                    response_status = message.get("status", 200)
                    response_headers = message.get("headers", [])
                await send(message)

            # 调用下游应用
            await self.app(scope, receive_wrapper, send_wrapper)

        except ValidationError as e:
            logger.error(
                "Invalid request body: %s, errors: %s",
                json.dumps(parsed_body, indent=2, ensure_ascii=False) if parsed_body else "None",
                e.errors(),
            )
            content = await asyncio.to_thread(
                jsonable_encoder, {"detail": e.errors()}
            )
            response = JSONResponse(status_code=422, content=content)
            await response(scope, receive_wrapper, send)
        except Exception as e:
            if self.debug:
                import traceback
                traceback.print_exc()
            logger.error("Error processing request: %s", str(e))
            response = JSONResponse(
                status_code=500,
                content={"error": f"Internal server error: {str(e)}"},
            )
            await response(scope, receive_wrapper, send)
        finally:
            request_info.reset(current_request_info)

    async def _moderate_content(
        self, content: str, api_index: int, background_tasks: BackgroundTasks, app: Any
    ):
        """
        调用 /v1/moderations 路由进行道德审查。
        通过直接调用路由函数重用其逻辑。
        """
        from routes.moderations import moderations  # 延迟导入避免循环

        moderation_request = ModerationRequest(input=content)
        response = await moderations(moderation_request, background_tasks, api_index)

        # 读取流式响应的内容
        moderation_result = b""
        async for chunk in response.body_iterator:
            if isinstance(chunk, str):
                moderation_result += chunk.encode("utf-8")
            else:
                moderation_result += chunk

        # 解码并解析 JSON
        moderation_data = json.loads(moderation_result.decode("utf-8"))
        return moderation_data