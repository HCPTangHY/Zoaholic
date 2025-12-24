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
from datetime import datetime, timedelta, timezone
from typing import Optional, Any

from pydantic import ValidationError
from fastapi import Request, BackgroundTasks
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Send, Scope, Message

from core.log_config import logger
from core.models import ModerationRequest, UnifiedRequest
from core.stats import update_stats
from core.utils import truncate_for_logging
from core.error_response import openai_error_response
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
        
        # 缓存方言端点前缀列表
        self._dialect_prefixes = self._get_dialect_prefixes()
    
    def _get_dialect_prefixes(self) -> list:
        """获取所有方言端点前缀"""
        prefixes = set()
        try:
            from core.dialects import list_dialects
            for dialect in list_dialects():
                for endpoint in dialect.endpoints:
                    # 提取端点前缀（如 /v1beta）
                    prefix = endpoint.prefix or ""
                    if prefix:
                        prefixes.add(prefix)
        except Exception:
            pass
        return list(prefixes)
    
    def _is_dialect_endpoint(self, path: str) -> bool:
        """检查路径是否是方言端点"""
        for prefix in self._dialect_prefixes:
            if path.startswith(prefix):
                return True
        return False

    def _normalize_endpoint(self, method: str, path: str) -> str:
        """归一化端点路径，将带模型名的路径转换为模板格式"""
        # 处理 Gemini 风格路径: /v1beta/models/{model}:generateContent
        if "/models/" in path and ":" in path:
            # 提取前缀和动作
            # /v1beta/models/gemini-pro:generateContent -> /v1beta/models/{model}:generateContent
            parts = path.split("/models/", 1)
            prefix = parts[0]
            suffix = parts[1]
            if ":" in suffix:
                action = suffix.split(":", 1)[1]
                return f"{method} {prefix}/models/{{model}}:{action}"
        
        return f"{method} {path}"

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

        # 方言端点使用自己的认证逻辑，跳过中间件认证但仍初始化 request_info
        # 方言路由处理器会使用 DialectDefinition.extract_token 进行认证
        is_dialect = self._is_dialect_endpoint(path)

        # 非 /v1 路径且非方言端点不做统计和鉴权
        if not path.startswith("/v1") and not is_dialect:
            await self.app(scope, receive, send)
            return

        # 获取 app 实例
        app = scope.get("app")
        if not app:
            await self.app(scope, receive, send)
            return

        start_time = time()
        headers = scope.get("headers", [])

        # 方言端点跳过中间件认证，但仍需初始化基础上下文
        token = None
        api_index = None
        enable_moderation = False
        config = app.state.config

        if is_dialect:
            # 方言端点：token/api_index 将由路由处理器填充
            token = "dialect-pending"
            api_index = None
        else:
            # 标准端点：执行完整认证
            token = get_api_key_from_headers(headers)
            if not token:
                response = openai_error_response("Invalid or missing API Key", 403)
                await response(scope, receive, send)
                return

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
                        response = openai_error_response("Balance is insufficient, please check your account.", 429)
                        await response(scope, receive, send)
                        return
            else:
                response = openai_error_response("Invalid or missing API Key", 403)
                await response(scope, receive, send)
                return

        # 获取 client IP
        client = scope.get("client")
        client_ip = client[0] if client else "unknown"

        # 获取用户key相关信息
        api_key_name = safe_get(config, "api_keys", api_index, "name", default=None)
        api_key_group = safe_get(config, "api_keys", api_index, "group", default=None)

        # 初始化 request_info
        request_id = str(uuid.uuid4())
        request_info_data = {
            "request_id": request_id,
            "start_time": start_time,
            "endpoint": self._normalize_endpoint(method, path),
            "client_ip": client_ip,
            "process_time": 0,
            "first_response_time": -1,
            "provider": None,
            "model": None,
            "success": False,
            "api_key": token,
            "api_key_name": api_key_name,
            "api_key_group": api_key_group,
            "is_flagged": False,
            "text": None,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            # 扩展日志字段
            "provider_id": None,
            "provider_key_index": None,
            "retry_count": 0,
            "retry_path": None,
            "request_headers": None,
            "request_body": None,
            "response_body": None,
            "raw_data_expires_at": None,
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

        # 获取原始数据保留时间配置（小时），默认为0表示不保存
        raw_data_retention_hours = safe_get(
            config, "preferences", "log_raw_data_retention_hours", default=0
        )
        
        # 如果配置了保留时间，保存请求头和请求体
        if raw_data_retention_hours > 0:
            # 过滤敏感头信息
            safe_headers = {k: v for k, v in headers_dict.items()
                          if k not in ("authorization", "x-api-key")}
            current_info["request_headers"] = json.dumps(safe_headers, ensure_ascii=False)
            
            # 保存请求体（使用深度截断，保留结构同时限制大小）
            if body_bytes:
                current_info["request_body"] = truncate_for_logging(body_bytes)
            
            # 设置过期时间
            current_info["raw_data_expires_at"] = datetime.now(timezone.utc) + timedelta(hours=raw_data_retention_hours)

        # 创建新的 receive 函数，重放已读取的 body
        body_sent = False
        async def receive_wrapper() -> Message:
            nonlocal body_sent
            if method == "POST" and "application/json" in content_type:
                if not body_sent:
                    body_sent = True
                    return {"type": "http.request", "body": body_bytes, "more_body": False}
                # 等待真正的 disconnect 消息，而不是返回空 body
                # 这对于 StreamingResponse 的正确迭代很重要
                return await receive()
            else:
                return await receive()

        try:
            # 如果能解析为 UnifiedRequest，则执行模型记录/限流/审查
            if parsed_body and should_attempt_unified_request(parsed_body):
                try:
                    request_model = await asyncio.to_thread(UnifiedRequest.model_validate, parsed_body)
                    request_model = request_model.data
                    if self.debug:
                        pass
                    model = request_model.model
                    current_info["model"] = model

                    final_api_key = app.state.api_list[api_index]
                    try:
                        await app.state.user_api_keys_rate_limit[final_api_key].next(model)
                    except Exception:
                        response = openai_error_response("Too many requests", 429)
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
                            response = openai_error_response("Content did not pass the moral check, please modify and try again.", 400)
                            await response(scope, receive_wrapper, send)
                            return
                except ValidationError as e:
                    # 不在中间件返回 422，避免对非统一请求路由造成影响
                    # 也不打印庞大的 Payload，防止日志刷屏
                    pass

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
            # 将 validation 错误信息格式化为可读字符串
            error_details = "; ".join([f"{err['loc'][-1]}: {err['msg']}" for err in e.errors()[:3]])
            if len(e.errors()) > 3:
                error_details += f" (and {len(e.errors()) - 3} more errors)"
            response = openai_error_response(f"Invalid request body: {error_details}", 422)
            await response(scope, receive_wrapper, send)
        except Exception as e:
            if self.debug:
                import traceback
                traceback.print_exc()
            logger.error("Error processing request: %s", str(e))
            response = openai_error_response(f"Internal server error: {str(e)}", 500)
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
