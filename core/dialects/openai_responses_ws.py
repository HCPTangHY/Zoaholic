"""
Responses API WebSocket 透传端点

客户端 --WS--> Zoaholic --(WS/HTTP)--> 上游

协议与 OpenAI 官方 WebSocket mode 一致：
- 客户端发送 {"type": "response.create", ...Responses payload}
- 服务端每个上游事件回一个 WS 文本帧（response.created / output_text.delta / completed ...）
- 错误帧：{"type": "error", "status": int, "error": {...}}
- 单连接串行处理（与官方一致：单连接单在途）

内部路径：response.create 帧 → parse_responses_request → model_handler.request_model
→ 返回的 Response 以 ASGI 方式执行 → SSE body 行 → WS 帧。

这样鉴权 / 限速 / 统计 / 渠道调度 / 自动重试全部复用现有 HTTP 路径；
上游是否走 WS 由渠道的 preferences.websocket 开关独立决定，两端解耦。
"""

import uuid
from time import time
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, WebSocket, WebSocketDisconnect

from ..json_utils import json_loads, json_dumps_text
from ..log_config import logger

router = APIRouter()


# ==================== 帧工具 ====================

async def _send_error_frame(websocket: WebSocket, status: int, message: str,
                            error_type: str = "invalid_request_error", code: Optional[str] = None) -> None:
    error_obj: dict[str, Any] = {"type": error_type, "message": message}
    if code:
        error_obj["code"] = code
    try:
        await websocket.send_text(json_dumps_text({
            "type": "error",
            "status": status,
            "error": error_obj,
        }, ensure_ascii=False))
    except Exception:
        pass


def _get_client_ip(websocket: WebSocket) -> str:
    """与 StatsMiddleware 相同的客户端 IP 解析优先级。"""
    forwarded_for = websocket.headers.get("x-forwarded-for")
    if forwarded_for:
        real_ip = forwarded_for.split(",")[0].strip()
        if real_ip:
            return real_ip
    real_ip_header = websocket.headers.get("x-real-ip")
    if real_ip_header:
        return real_ip_header.strip()
    client = websocket.client
    return client.host if client else "unknown"


def _authenticate(websocket: WebSocket) -> tuple[Optional[int], Optional[str], Optional[str]]:
    """WS 握手鉴权，复用 HTTP 路径同款逻辑。

    返回 (api_index, token, error_msg)。error_msg 非空表示鉴权失败。
    """
    app = websocket.app
    from core.middleware import get_api_key_from_headers
    from core.auth import is_api_key_disabled
    from core.ip_blacklist import is_global_ip_blocked, is_key_ip_blocked

    headers = [(k.lower().encode("latin-1"), v.encode("latin-1"))
               for k, v in websocket.headers.items()]
    token = get_api_key_from_headers(headers)
    if not token:
        return None, None, "Invalid or missing API Key"

    api_list = getattr(app.state, "api_list", [])
    try:
        api_index = api_list.index(token)
    except ValueError:
        return None, None, "Invalid or missing API Key"

    if is_api_key_disabled(app, api_index):
        return None, None, "API Key has been disabled"

    client_ip = _get_client_ip(websocket)
    if is_global_ip_blocked(app, client_ip):
        return None, None, "IP is blocked"
    if is_key_ip_blocked(app, api_index, client_ip):
        return None, None, "IP is blocked"

    return api_index, token, None


# ==================== Chat Completions → Responses API 转换 ====================


def _chat_chunk_to_responses_events(chunk: dict) -> list[dict]:
    """把 Chat Completions SSE chunk 转换为 Responses API 事件列表。

    handler 返回的 Response body 是 Chat Completions SSE（方言渲染层在 HTTP 路由层，
    WS 端点不经过）。需要在此转换为客户端期望的 Responses API 事件格式。

    已经是 Responses 格式（有 type 字段）的 chunk 原样返回。
    """
    # 已经是 Responses API 格式
    if chunk.get("type"):
        return [chunk]

    # 不是 Chat Completions chunk
    if chunk.get("object") != "chat.completion.chunk":
        return [chunk]

    events: list[dict] = []
    choices = chunk.get("choices") or []
    usage = chunk.get("usage")

    for choice in choices:
        delta = choice.get("delta") or {}
        finish_reason = choice.get("finish_reason")

        # reasoning content
        reasoning = delta.get("reasoning_content")
        if reasoning:
            events.append({
                "type": "response.reasoning_summary_text.delta",
                "delta": reasoning,
            })

        # text content
        content = delta.get("content")
        if content:
            events.append({
                "type": "response.output_text.delta",
                "delta": content,
            })

        # tool calls
        for tc in (delta.get("tool_calls") or []):
            tc_func = tc.get("function") or {}
            if tc_func.get("name"):
                events.append({
                    "type": "response.output_item.added",
                    "item": {
                        "type": "function_call",
                        "call_id": tc.get("id", ""),
                        "name": tc_func["name"],
                    },
                    "output_index": tc.get("index", 0),
                })
            if tc_func.get("arguments"):
                events.append({
                    "type": "response.function_call_arguments.delta",
                    "delta": tc_func["arguments"],
                    "call_id": tc.get("id", ""),
                    "output_index": tc.get("index", 0),
                })

        # finish → response.completed（附带 usage）
        if finish_reason:
            resp_usage = {}
            if isinstance(usage, dict):
                resp_usage = {
                    "input_tokens": usage.get("prompt_tokens", 0),
                    "output_tokens": usage.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                }
                cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
                if cached:
                    resp_usage["input_tokens_details"] = {"cached_tokens": cached}
            events.append({
                "type": "response.completed",
                "response": {
                    "id": chunk.get("id", ""),
                    "object": "response",
                    "status": "completed",
                    "model": chunk.get("model", ""),
                    "usage": resp_usage,
                },
            })

    # usage-only chunk（无 choices 或 choices 为空）→ response.completed
    if not choices and isinstance(usage, dict):
        resp_usage = {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        }
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        if cached:
            resp_usage["input_tokens_details"] = {"cached_tokens": cached}
        events.append({
            "type": "response.completed",
            "response": {
                "id": chunk.get("id", ""),
                "object": "response",
                "status": "completed",
                "model": chunk.get("model", ""),
                "usage": resp_usage,
            },
        })

    return events


# ==================== SSE → WS 帧转发 ====================

async def _handle_sse_line(websocket: WebSocket, line: bytes) -> None:
    """把一行 SSE 数据转换为 WS 帧。Chat Completions chunk 转 Responses 事件，错误 chunk 映射 error 帧。"""
    line = line.strip()
    if not line or line.startswith(b":") or line.startswith(b"event:"):
        return
    if not line.startswith(b"data:"):
        return
    data = line[5:].strip()
    if data == b"[DONE]":
        return
    try:
        event = json_loads(data.decode("utf-8", errors="replace"))
    except Exception:
        return
    if not isinstance(event, dict):
        return

    # Zoaholic 内部错误 chunk：{"error": ..., "status_code": N, "details": ...}
    # 或 openai_error_response 的 SSE 版：{"error": {"message": ..., "type": ...}}
    # 统一映射为官方 WS 错误帧，避免客户端把错误当成正常事件
    err = event.get("error")
    if err is not None and "type" not in event:
        status = event.get("status_code") or 500
        if isinstance(err, dict):
            await _send_error_frame(
                websocket, status,
                str(err.get("message", "upstream error")),
                str(err.get("type", "api_error")),
                str(err.get("code")) if err.get("code") else None,
            )
        else:
            await _send_error_frame(websocket, status, str(err))
        return

    # Chat Completions chunk → Responses API 事件
    for out_event in _chat_chunk_to_responses_events(event):
        try:
            await websocket.send_text(json_dumps_text(out_event, ensure_ascii=False))
        except Exception:
            raise WebSocketDisconnect()


async def _relay_response_as_ws(response, websocket: WebSocket, app) -> None:
    """以 ASGI 方式执行 Response，body 按 SSE 行拆分并转发为 WS 帧。

    LoggingStreamingResponse 的统计逻辑（usage 解析、enqueue_stats）在其 __call__
    的 finally 中完成，用假 scope/receive 驱动即可完整保留统计链路。

    关键：send 回调中 WS 发送失败时只记录标记、不抛异常。
    原因：客户端收到 response.completed 后立即断连，但上游适配器的 merge_usage()
    在 generator 尾部（yield [DONE] 之前），如果 send 抛异常会导致
    __call__ → aclose() → GeneratorExit，merge_usage 永远不执行，
    completion_tokens 归零，stream_guard 把成功请求标为 502。
    """
    buffer = b""
    status_code = 200
    ws_closed = False  # WS 断连后停止发送但不中断 ASGI 驱动

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        nonlocal buffer, status_code, ws_closed
        msg_type = message.get("type")
        if msg_type == "http.response.start":
            status_code = message.get("status", 200)
            return
        if msg_type != "http.response.body":
            return
        body = message.get("body", b"")
        if not body:
            return
        if status_code >= 400:
            buffer += body
            return
        if ws_closed:
            return  # WS 已断，静默丢弃，让 ASGI 正常耗尽 generator
        buffer += body
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            try:
                await _handle_sse_line(websocket, line)
            except (WebSocketDisconnect, RuntimeError, Exception) as e:
                if "not connected" in str(e).lower() or isinstance(e, WebSocketDisconnect):
                    ws_closed = True
                    return
                raise

    scope = {"type": "http", "app": app, "method": "POST", "path": "/v1/responses", "headers": []}
    await response(scope, receive, send)

    # 收尾
    if status_code >= 400 and not ws_closed:
        raw = buffer.decode("utf-8", errors="replace")
        try:
            err_json = json_loads(raw)
            err = err_json.get("error", {}) if isinstance(err_json, dict) else {}
            if isinstance(err, dict):
                await _send_error_frame(
                    websocket, status_code,
                    str(err.get("message", raw[:500])),
                    str(err.get("type", "api_error")),
                )
            else:
                await _send_error_frame(websocket, status_code, raw[:500])
        except Exception:
            await _send_error_frame(websocket, status_code, raw[:500] or "upstream error")
    elif buffer.strip() and not ws_closed:
        try:
            await _handle_sse_line(websocket, buffer)
        except Exception:
            pass


# ==================== 单帧处理 ====================

async def _handle_response_create(websocket: WebSocket, payload: dict, api_index: int, token: str, client_ip: str) -> None:
    """处理一个 response.create 帧：走完整 handler 调度，事件经 WS 回传。"""
    app = websocket.app
    from core.dialects.openai_responses import parse_responses_request
    from core.middleware import request_info
    from routes.deps import get_model_handler
    from utils import safe_get

    # WS 协议本身即流式：强制上游流式，事件逐帧回传
    payload = dict(payload)
    payload["stream"] = True

    config = app.state.config
    request_info_data = {
        "request_id": str(uuid.uuid4()),
        "start_time": time(),
        "endpoint": "WS /v1/responses",
        "client_ip": client_ip,
        "process_time": 0,
        "first_response_time": -1,
        "provider": None,
        "model": payload.get("model"),
        "success": False,
        "api_key": token,
        "api_key_name": safe_get(config, "api_keys", api_index, "name", default=None),
        "api_key_group": safe_get(config, "api_keys", api_index, "group", default=None),
        "is_flagged": False,
        "text": None,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
        "cache_creation_tokens": 0,
        "provider_id": None,
        "provider_key_index": None,
        "retry_count": 0,
        "retry_path": None,
        "request_headers": None,
        "request_body": None,
        "upstream_request_headers": None,
        "upstream_request_body": None,
        "upstream_response_headers": None,
        "upstream_response_body": None,
        "response_body": None,
        "raw_data_expires_at": None,
        # 修改原因：dialect_id 必须与注册的方言 ID 一致，否则 LoggingStreamingResponse 的
        # 修改方式：设为 "openai-responses"（注册 ID），不要自创 ID。
        # 目的：_try_extract_usage 能通过 get_dialect 找到正确的 parse_usage，token 统计正常入库。
        "dialect_id": "openai-responses",
    }
    info_token = request_info.set(request_info_data)
    try:
        request_model = await parse_responses_request(payload, {}, {})
        model_handler = get_model_handler()
        # 修改原因：传 dialect_id + original_payload 会触发 evaluate_passthrough，
        #   当 dialect_id == target_engine（都是 "openai-responses"）时走 passthrough 路径，
        #   passthrough 用 _fetch_passthrough_stream 直接 httpx POST，完全绕过渠道的
        #   stream_adapter（fetch_responses_stream_transport），WS 传输层被跳过。
        # 修改方式：不传 original_payload，阻止 passthrough 触发，走正常 process_request 路径。
        # 目的：正常路径使用 stream_adapter → WS 优先/HTTP 回退 → 连接池复用。
        response = await model_handler.request_model(
            request_model,
            api_index,
            BackgroundTasks(),
            endpoint="/v1/responses",
            dialect_id="openai-responses",
            raw_request=None,
        )
        await _relay_response_as_ws(response, websocket, app)
    except WebSocketDisconnect:
        raise
    except Exception as e:
        status = getattr(e, "status_code", 500)
        detail = getattr(e, "detail", None) or str(e)
        logger.warning(f"[ws-responses] frame handling error: {status} {detail}")
        await _send_error_frame(websocket, status if isinstance(status, int) else 500, str(detail))
    finally:
        request_info.reset(info_token)


# ==================== 端点 ====================

@router.websocket("/v1/responses")
async def responses_websocket(websocket: WebSocket) -> None:
    """Responses API WebSocket mode 端点，协议与 OpenAI 官方一致。"""
    api_index, token, error_msg = _authenticate(websocket)
    if api_index is None:
        # 1008 Policy Violation：与官方 Codex 客户端对策略拒绝的处理一致
        await websocket.close(code=1008, reason=error_msg or "Unauthorized")
        return

    await websocket.accept()
    client_ip = _get_client_ip(websocket)
    logger.info(f"[ws-responses] client connected: ip={client_ip} key_index={api_index}")

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                frame = json_loads(raw)
            except Exception:
                await _send_error_frame(websocket, 400, "invalid JSON frame")
                continue
            if not isinstance(frame, dict):
                await _send_error_frame(websocket, 400, "frame must be a JSON object")
                continue

            frame_type = frame.get("type")
            if frame_type != "response.create":
                await _send_error_frame(
                    websocket, 400,
                    f"unsupported frame type: {frame_type!r}",
                )
                continue

            payload = {k: v for k, v in frame.items() if k != "type"}
            if not payload.get("model"):
                await _send_error_frame(websocket, 400, "missing required field: model")
                continue

            await _handle_response_create(websocket, payload, api_index, token, client_ip)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"[ws-responses] connection error: {type(e).__name__}: {e}")
    finally:
        logger.info(f"[ws-responses] client disconnected: ip={client_ip}")


def register_ws_endpoint(app) -> None:
    """把 WS 端点注册到 FastAPI app。"""
    app.include_router(router)
