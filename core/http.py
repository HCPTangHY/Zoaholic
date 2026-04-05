"""
统一出站 HTTP 请求拦截与记录。

通过 monkey-patch httpx.AsyncClient.__init__，在所有出站请求上
自动注入 event_hooks，记录 URL、状态码、耗时等信息到内存环形缓冲区。

本模块由 core/log_config.py 末尾触发安装，确保在任何 httpx.AsyncClient
实例创建之前完成 patch。
"""

import httpx
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from threading import RLock
from time import monotonic
from typing import Any, Callable, Dict, List, Optional

# ==================== 配置 ====================

DEFAULT_OUTBOUND_LOG_BUFFER_SIZE = 500
MAX_OUTBOUND_LOG_BUFFER_SIZE = 10000

# ==================== 缓冲区 ====================

_outbound_log_buffer_size = DEFAULT_OUTBOUND_LOG_BUFFER_SIZE
_outbound_log_buffer: deque = deque(maxlen=_outbound_log_buffer_size)
_outbound_log_lock = RLock()
_outbound_log_next_id = 1

# 上下文代理：在此 ContextVar 有值时，裸创建的 httpx.AsyncClient() 自动注入代理
_current_proxy: ContextVar[Optional[str]] = ContextVar("_current_proxy", default=None)

# ==================== event_hooks ====================


async def _on_request(request: httpx.Request):
    """请求发出前：记录开始时间和基本信息（用于异常路径回填）"""
    request.extensions["_trace_start"] = monotonic()


async def _on_response(response: httpx.Response):
    """收到响应后：记录到缓冲区"""
    global _outbound_log_next_id

    start = response.request.extensions.get("_trace_start")
    elapsed_ms = int((monotonic() - start) * 1000) if start else None

    url_str = str(response.request.url)
    if len(url_str) > 500:
        url_str = url_str[:500] + "..."

    entry: Dict[str, Any] = {
        "id": _outbound_log_next_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "method": response.request.method,
        "url": url_str,
        "host": response.request.url.host,
        "status_code": response.status_code,
        "elapsed_ms": elapsed_ms,
    }

    with _outbound_log_lock:
        _outbound_log_buffer.append(entry)
        _outbound_log_next_id += 1


# ==================== monkey-patch ====================

_original_async_init = httpx.AsyncClient.__init__
_original_async_send = httpx.AsyncClient.send
_installed = False


async def _patched_async_send(self, request: httpx.Request, **kwargs):
    """包装 send 方法，在传输异常时也写入失败记录。"""
    try:
        return await _original_async_send(self, request, **kwargs)
    except Exception as exc:
        _record_transport_error(request, exc)
        raise


def _record_transport_error(request: httpx.Request, exc: Exception):
    """将传输层异常（ConnectError / ReadTimeout / TLS 等）写入缓冲区。"""
    global _outbound_log_next_id

    start = request.extensions.get("_trace_start")
    elapsed_ms = int((monotonic() - start) * 1000) if start else None

    url_str = str(request.url)
    if len(url_str) > 500:
        url_str = url_str[:500] + "..."

    error_type = type(exc).__name__
    error_msg = str(exc)[:200] if str(exc) else error_type

    entry: Dict[str, Any] = {
        "id": _outbound_log_next_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "method": request.method,
        "url": url_str,
        "host": request.url.host,
        "status_code": None,
        "elapsed_ms": elapsed_ms,
        "error": True,
        "error_type": error_type,
        "error_message": error_msg,
    }

    with _outbound_log_lock:
        _outbound_log_buffer.append(entry)
        _outbound_log_next_id += 1


def _patched_async_client_init(self, *args, **kwargs):
    """在 httpx.AsyncClient 创建时自动注入 tracing event_hooks 和上下文代理"""
    existing_hooks = kwargs.get("event_hooks") or {}

    merged_request = [_on_request] + list(existing_hooks.get("request") or [])
    merged_response = [_on_response] + list(existing_hooks.get("response") or [])

    kwargs["event_hooks"] = {
        "request": merged_request,
        "response": merged_response,
    }

    # 自动注入上下文代理：当调用方未显式指定代理参数时，从 ContextVar 继承
    _proxy_url = _current_proxy.get(None)
    if _proxy_url and "proxy" not in kwargs and "proxies" not in kwargs and "transport" not in kwargs:
        from core.utils import get_proxy
        kwargs.update(get_proxy(_proxy_url, {}))

    _original_async_init(self, *args, **kwargs)


def install():
    """安装 monkey-patch。幂等，多次调用安全。"""
    global _installed
    if _installed:
        return
    httpx.AsyncClient.__init__ = _patched_async_client_init
    httpx.AsyncClient.send = _patched_async_send  # type: ignore[assignment]
    _installed = True


@contextmanager
def proxy_context(proxy_url: Optional[str] = None):
    """设置当前上下文的代理 URL。

    在此上下文中裸创建的 httpx.AsyncClient() 会自动注入该代理，
    无需每个调用点手动传递。已显式指定 proxy/proxies/transport 的不受影响。
    """
    if proxy_url:
        token = _current_proxy.set(proxy_url)
        try:
            yield
        finally:
            _current_proxy.reset(token)
    else:
        yield


# ==================== 查询接口 ====================


def get_outbound_log_entries(
    *,
    since_id: Optional[int] = None,
    limit: int = 200,
    host: Optional[str] = None,
    method: Optional[str] = None,
    status_min: Optional[int] = None,
    status_max: Optional[int] = None,
    search: Optional[str] = None,
) -> Dict[str, Any]:
    """返回最近的出站请求记录。"""

    normalized_host = (host or "").strip().lower() or None
    normalized_method = (method or "").strip().upper() or None
    normalized_search = (search or "").strip().lower() or None

    with _outbound_log_lock:
        snapshot: List[Dict[str, Any]] = list(_outbound_log_buffer)
        max_id = _outbound_log_next_id - 1

    filtered: List[Dict[str, Any]] = []
    for entry in snapshot:
        if since_id is not None and entry["id"] <= since_id:
            continue
        if normalized_host and normalized_host not in (entry.get("host") or "").lower():
            continue
        if normalized_method and entry.get("method") != normalized_method:
            continue
        if status_min is not None and (entry.get("status_code") or 0) < status_min:
            continue
        if status_max is not None and (entry.get("status_code") or 0) > status_max:
            continue
        if normalized_search and normalized_search not in (entry.get("url") or "").lower():
            continue
        filtered.append(entry)

    total = len(filtered)
    if limit > 0:
        if since_id is not None:
            filtered = filtered[:limit]
        else:
            filtered = filtered[-limit:]

    return {
        "items": filtered,
        "total": total,
        "max_id": max_id,
        "buffer_size": _outbound_log_buffer_size,
    }


def set_outbound_log_buffer_size(size: int) -> int:
    """调整出站日志缓冲区大小"""
    global _outbound_log_buffer_size, _outbound_log_buffer

    normalized = max(50, min(MAX_OUTBOUND_LOG_BUFFER_SIZE, int(size)))

    with _outbound_log_lock:
        snapshot = list(_outbound_log_buffer)[-normalized:]
        _outbound_log_buffer_size = normalized
        _outbound_log_buffer = deque(snapshot, maxlen=normalized)

    return normalized
