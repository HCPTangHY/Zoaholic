"""
Responses API WebSocket 传输层

协议依据：
- OpenAI 官方文档 WebSocket Mode（developers.openai.com/api/docs/guides/websocket-mode）
- openai/codex 仓库 codex-rs/codex-api/src/endpoint/responses_websocket.rs

要点：
- 端点与 HTTP SSE 同路径，协议头 http(s) → ws(s)
- 客户端发送 {"type": "response.create", ...payload}（去掉 stream/background/stream_options）
- 服务端事件模型与 SSE 完全一致，解析复用 _responses_events_to_sse
- 错误以 {"type": "error", "status": int, "error": {...}, "headers": {...}} 帧下发
- 单连接最长 60 分钟，code=websocket_connection_limit_reached 时需重建连接
- 单连接单在途（无多路复用），连接忙时新请求回退 HTTP

连接管理：
- 按 (url, Authorization, proxy) 复用连接，55 分钟到期强制重建
- websockets 12 不原生支持 HTTP 代理，CONNECT 隧道自行实现（约 40 行）
- 已知风险：chatgpt.com 的 WS 端点存在策略性秒断（issue #13041），
  任何内容产出前的失败都会以 WsUnavailable 抛出，由渠道入口回退 HTTP SSE
"""

import asyncio
import base64
import json
import socket
import time
from typing import Any, AsyncIterator, Optional
from urllib.parse import urlparse

from ..log_config import logger
from ..json_utils import json_loads, json_dumps_text

MAX_CONN_AGE = 55 * 60           # 服务端限制 60 分钟，提前 5 分钟换
WS_MAX_SIZE = 64 * 1024 * 1024  # gpt-image base64 单帧可能超过默认 1MB
CONNECT_RESPONSE_LIMIT = 65536


# 渠道注册时声明的 WebSocket 传输开关元数据（preference_toggles）
WS_PREFERENCE_TOGGLE = {
    "key": "websocket",
    "label": "WebSocket 传输（实验）",
    "tip": "使用 WebSocket 连接上游 Responses 端点（连接复用、更低延迟）。连接失败时自动回退 HTTP，仅影响流式请求",
}


class WsUnavailable(Exception):
    """WS 传输不可用且尚未产出任何内容，调用方应回退 HTTP。"""


class _WsServerError(Exception):
    """服务端 error 帧，携带 status/error/headers 供上层映射为错误 chunk。"""

    def __init__(self, status: int, error: dict, headers: dict):
        super().__init__(error.get("message") or f"websocket error {status}")
        self.status = status
        self.error = error
        self.headers = headers


# ==================== 开关与配置 ====================

def websocket_enabled(provider: Optional[dict]) -> bool:
    """provider.preferences.websocket 为真时启用 WS 传输。"""
    try:
        return bool((provider or {}).get("preferences", {}).get("websocket"))
    except Exception:
        return False


def resolve_transport_proxy(provider: Optional[dict]) -> Optional[str]:
    """与 process_request 相同的代理优先级：provider.preferences.proxy → 全局 preferences.proxy。"""
    try:
        proxy = (provider or {}).get("preferences", {}).get("proxy")
        if proxy:
            return proxy
    except Exception:
        pass
    try:
        from main import app
        return (app.state.config.get("preferences") or {}).get("proxy")
    except Exception:
        return None


def http_url_to_ws(url: str) -> str:
    if url.startswith("https://"):
        return "wss://" + url[len("https://"):]
    if url.startswith("http://"):
        return "ws://" + url[len("http://"):]
    return url


def build_ws_request_frame(payload: dict) -> str:
    """构造 response.create 帧：去掉传输层字段，附带 type。"""
    body = {k: v for k, v in (payload or {}).items() if k not in ("stream", "stream_options", "background")}
    body["type"] = "response.create"
    return json_dumps_text(body)


def error_frame_to_chunk(source: str, status: int, error: dict) -> dict:
    """映射为 check_response 的错误 chunk 格式，保证响应拦截器行为一致。"""
    return {"error": f"{source} HTTP Error", "status_code": status, "details": {"error": error}}


# ==================== CONNECT 隧道 ====================

async def _open_tunnel_socket(proxy_url: str, host: str, port: int) -> socket.socket:
    """通过 HTTP 代理建立 CONNECT 隧道，返回已连接的裸 socket（TLS 由 websockets 完成）。"""
    p = urlparse(proxy_url)
    proxy_host = p.hostname
    proxy_port = p.port or 8080
    if not proxy_host:
        raise WsUnavailable(f"invalid proxy url: {proxy_url!r}")

    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setblocking(False)
    try:
        await asyncio.wait_for(loop.sock_connect(sock, (proxy_host, proxy_port)), timeout=15)

        auth = b""
        if p.username:
            cred = base64.b64encode(f"{p.username}:{p.password or ''}".encode()).decode()
            auth = f"Proxy-Authorization: Basic {cred}\r\n".encode()
        req = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n".encode() + auth + b"\r\n"
        await asyncio.wait_for(loop.sock_sendall(sock, req), timeout=15)

        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = await asyncio.wait_for(loop.sock_recv(sock, 4096), timeout=15)
            if not chunk:
                raise WsUnavailable("proxy closed connection during CONNECT")
            buf += chunk
            if len(buf) > CONNECT_RESPONSE_LIMIT:
                raise WsUnavailable("proxy CONNECT response too large")
        status_line = buf.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
        if " 200" not in status_line:
            raise WsUnavailable(f"proxy CONNECT failed: {status_line}")
        return sock
    except Exception:
        sock.close()
        raise


# ==================== 连接池 ====================

class _PooledConn:
    __slots__ = ("ws", "created_at", "lock", "response_headers")

    def __init__(self, ws, response_headers=None):
        self.ws = ws
        self.created_at = time.monotonic()
        self.lock = asyncio.Lock()
        self.response_headers = response_headers

    def expired(self) -> bool:
        return (time.monotonic() - self.created_at) > MAX_CONN_AGE

    def closed(self) -> bool:
        try:
            from websockets.protocol import State
            return self.ws.state is not State.OPEN
        except Exception:
            return True


class ResponsesWsPool:
    """按 key 复用 WS 连接；连接忙时抛出 WsUnavailable 让调用方回退 HTTP。"""

    def __init__(self):
        self._conns: dict[tuple, _PooledConn] = {}
        self._guard = asyncio.Lock()

    async def _connect(self, url: str, headers: dict, proxy: Optional[str], timeout: float) -> _PooledConn:
        import websockets

        ws_url = http_url_to_ws(url)
        parsed = urlparse(ws_url)
        if not parsed.hostname:
            raise WsUnavailable(f"invalid websocket url: {ws_url!r}")
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)

        sock = None
        if proxy:
            sock = await _open_tunnel_socket(proxy, parsed.hostname, port)

        extra = [
            (k, v) for k, v in (headers or {}).items()
            if k.lower() not in ("host", "content-length", "connection")
        ]
        try:
            ws = await websockets.connect(
                ws_url,
                sock=sock,
                extra_headers=extra or None,
                open_timeout=min(timeout or 200, 30),
                max_size=WS_MAX_SIZE,
            )
        except Exception as e:
            if sock is not None:
                sock.close()
            raise WsUnavailable(f"websocket handshake failed: {e}") from e
        logger.info(f"[responses-ws] connected: {ws_url} (proxy={'yes' if proxy else 'no'})")
        return _PooledConn(ws, getattr(ws, "response_headers", None))

    async def get(self, key: tuple, url: str, headers: dict, proxy: Optional[str], timeout: float) -> _PooledConn:
        async with self._guard:
            conn = self._conns.get(key)
            if conn is not None and (conn.expired() or conn.closed()):
                await self._drop(key, conn)
                conn = None
            if conn is None:
                conn = await self._connect(url, headers, proxy, timeout)
                self._conns[key] = conn
            return conn

    async def _drop(self, key: tuple, conn: _PooledConn) -> None:
        if self._conns.get(key) is conn:
            self._conns.pop(key, None)
        try:
            await conn.ws.close()
        except Exception:
            pass

    async def discard(self, key: tuple, conn: _PooledConn) -> None:
        async with self._guard:
            await self._drop(key, conn)


_pool = ResponsesWsPool()


# ==================== 事件迭代 ====================

async def _ws_event_iter(ws, on_headers=None) -> AsyncIterator[dict]:
    """把 WS 文本帧产出为事件 dict；error 帧抛出 _WsServerError。"""
    async for raw in ws:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        try:
            data = json_loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("type") == "error":
            headers = data.get("headers") or {}
            if on_headers:
                try:
                    on_headers(headers)
                except Exception:
                    pass
            raise _WsServerError(
                int(data.get("status") or 400),
                data.get("error") or {},
                headers,
            )
        yield data


# ==================== 流式入口 ====================

async def fetch_responses_ws_stream(url, headers, payload, model, timeout, proxy=None, on_headers=None):
    """
    Responses API WebSocket 流式传输。

    产出与 fetch_responses_stream 相同的 Chat Completions SSE 字符串 / 错误 dict。
    任何内容产出前的传输失败抛出 WsUnavailable，由渠道入口回退 HTTP SSE；
    内容产出后的失败映射为错误 chunk，不再回退（避免重复内容）。

    on_headers: 可选回调，接收握手响应头与 error 帧内嵌 headers（codex 额度采集用）。
    """
    # 延迟导入避免循环依赖：openai_responses_channel 的传输入口也 import 本模块
    from .openai_responses_channel import _responses_events_to_sse

    auth_key = ""
    for k, v in (headers or {}).items():
        if k.lower() in ("authorization", "x-api-key"):
            auth_key = v
            break
    key = (url, auth_key, proxy or "")

    conn = await _pool.get(key, url, headers, proxy, timeout or 200)
    if conn.lock.locked():
        # 单连接单在途：并发请求不排队，直接回退 HTTP
        raise WsUnavailable("websocket connection busy with another in-flight response")

    async with conn.lock:
        # 握手响应头只在连接建立时采集一次（codex 额度：x-codex-* / x-models-etag 等）
        if on_headers and conn.response_headers:
            try:
                on_headers(conn.response_headers)
            except Exception:
                pass

        frame = build_ws_request_frame(payload)
        try:
            await conn.ws.send(frame)
        except Exception as e:
            await _pool.discard(key, conn)
            raise WsUnavailable(f"websocket send failed: {e}") from e

        sent_any = False
        # 跟踪 response.completed：服务端正常关闭时 websockets 迭代器静默结束，
        # 必须靠完成标记区分“正常结束”与“秒断/截断”，否则空流不会触发 HTTP 回退。
        stream_state = {"completed": False, "events": 0}

        async def _tracked_events():
            async for event in _ws_event_iter(conn.ws, on_headers):
                if event.get("type") == "response.completed":
                    stream_state["completed"] = True
                stream_state["events"] += 1
                yield event

        # 修改原因：事件处理器在事件耗尽时会无条件产出收尾 [DONE]，而 [DONE] 一旦下发就无法再安全回退 HTTP。
        # 修改方式：滞后一个 chunk 缓冲，异常路径在吐出收尾 chunk 前拦截。
        # 目的：空流（服务端秒断）时一个客户端可见字节都不产出，保证 HTTP 回退绝对安全。
        held = None
        try:
            async with asyncio.timeout(timeout or 200):
                async for sse_string in _responses_events_to_sse(_tracked_events(), model):
                    if held is not None:
                        sent_any = True
                        yield held
                    held = sse_string
            if stream_state["completed"]:
                if held is not None:
                    yield held
            elif stream_state["events"] == 0:
                # 连接在任何事件到达前被服务端关闭（如 1008 策略秒断）→ 回退 HTTP
                raise WsUnavailable("websocket closed before any response event")
            else:
                # 已有事件但未完成：截断，映射为错误 chunk，不回退（避免重复内容）
                yield error_frame_to_chunk(
                    "fetch_responses_ws_stream", 500,
                    {"message": "websocket stream ended before response.completed", "type": "stream_error"},
                )
                return
        except WsUnavailable:
            raise
        except _WsServerError as e:
            if held is not None:
                sent_any = True
                yield held
            # 60 分钟连接上限：废弃连接；未产出内容时可安全回退 HTTP
            if e.error.get("code") == "websocket_connection_limit_reached":
                await _pool.discard(key, conn)
                if not sent_any:
                    raise WsUnavailable("websocket connection limit reached") from e
            yield error_frame_to_chunk("fetch_responses_ws_stream", e.status, e.error)
            return
        except Exception as e:
            if held is not None:
                sent_any = True
                yield held
            await _pool.discard(key, conn)
            if not sent_any:
                raise WsUnavailable(f"websocket stream failed: {e}") from e
            logger.error(f"[responses-ws] stream error after content started: {e}")
            yield error_frame_to_chunk(
                "fetch_responses_ws_stream", 500,
                {"message": f"websocket stream error: {e}", "type": "stream_error"},
            )
            return
