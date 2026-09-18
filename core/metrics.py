"""
运行时指标采集模块。

提供以下维度的实时指标，供 /healthz、/readyz 及外部监控脚本使用：

1. 活跃请求追踪（active requests）
2. 连接池快照（httpx AsyncClient pool stats）
3. 最后成功 / 失败响应时间
4. 进程内存占用（RSS / VMS）
5. 出站 HTTP 摘要统计

所有操作都是 O(1) 或 O(pool_count)，不阻塞事件循环。
"""

from __future__ import annotations

import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Dict, Optional

# ==================== 活跃请求追踪 ====================

_active_lock = RLock()
_active_requests: int = 0
_total_requests: int = 0
_total_success: int = 0
_total_errors: int = 0
_last_request_at: Optional[float] = None      # monotonic
_last_success_at: Optional[float] = None       # monotonic
_last_error_at: Optional[float] = None         # monotonic
_last_request_wall: Optional[datetime] = None  # UTC wall clock
_last_success_wall: Optional[datetime] = None
_last_error_wall: Optional[datetime] = None

# 按模型维度的活跃请求数
_active_by_model: Dict[str, int] = defaultdict(int)


def on_request_start(model: Optional[str] = None) -> None:
    """请求开始时调用。"""
    global _active_requests, _total_requests, _last_request_at, _last_request_wall
    with _active_lock:
        _active_requests += 1
        _total_requests += 1
        _last_request_at = time.monotonic()
        _last_request_wall = datetime.now(timezone.utc)
        if model:
            _active_by_model[model] += 1


def on_request_model(model: Optional[str]) -> None:
    """Attach the parsed model to an already-counted request."""
    if model:
        with _active_lock:
            _active_by_model[model] += 1


def on_request_end(model: Optional[str] = None, success: bool = True) -> None:
    """请求结束时调用。"""
    global _active_requests, _total_success, _total_errors
    global _last_success_at, _last_error_at, _last_success_wall, _last_error_wall
    with _active_lock:
        _active_requests = max(0, _active_requests - 1)
        if model and _active_by_model.get(model, 0) > 0:
            _active_by_model[model] -= 1
            if _active_by_model[model] == 0:
                del _active_by_model[model]
        now = time.monotonic()
        now_wall = datetime.now(timezone.utc)
        if success:
            _total_success += 1
            _last_success_at = now
            _last_success_wall = now_wall
        else:
            _total_errors += 1
            _last_error_at = now
            _last_error_wall = now_wall


def get_request_metrics() -> Dict[str, Any]:
    """返回活跃请求指标快照。"""
    with _active_lock:
        now = time.monotonic()
        result: Dict[str, Any] = {
            "active_requests": _active_requests,
            "total_requests": _total_requests,
            "total_success": _total_success,
            "total_errors": _total_errors,
            "active_by_model": dict(_active_by_model) if _active_by_model else {},
        }

        # 距离上次请求 / 成功 / 失败的秒数
        if _last_request_at is not None:
            result["seconds_since_last_request"] = round(now - _last_request_at, 1)
            result["last_request_at"] = _last_request_wall.isoformat() if _last_request_wall else None
        else:
            result["seconds_since_last_request"] = None
            result["last_request_at"] = None

        if _last_success_at is not None:
            result["seconds_since_last_success"] = round(now - _last_success_at, 1)
            result["last_success_at"] = _last_success_wall.isoformat() if _last_success_wall else None
        else:
            result["seconds_since_last_success"] = None
            result["last_success_at"] = None

        if _last_error_at is not None:
            result["seconds_since_last_error"] = round(now - _last_error_at, 1)
            result["last_error_at"] = _last_error_wall.isoformat() if _last_error_wall else None
        else:
            result["seconds_since_last_error"] = None
            result["last_error_at"] = None

    return result


# ==================== 连接池快照 ====================

def get_pool_metrics(client_manager) -> Dict[str, Any]:
    """
    从 ClientManager 提取连接池统计。

    返回每个 host+proxy 维度的连接数，以及汇总。
    """
    if client_manager is None:
        return {"available": False}

    clients: Dict[str, Any] = getattr(client_manager, "clients", {})
    pool_size = getattr(client_manager, "pool_size", 0)
    max_keepalive = getattr(client_manager, "max_keepalive_connections", 0)

    pools: list[Dict[str, Any]] = []
    total_active = 0
    total_idle = 0

    for key, client in clients.items():
        pool_info = _extract_pool_info(client)
        pool_info["key"] = key
        pools.append(pool_info)
        total_active += pool_info.get("active_connections", 0)
        total_idle += pool_info.get("idle_connections", 0)

    return {
        "available": True,
        "configured_pool_size": pool_size,
        "configured_max_keepalive": max_keepalive,
        "client_count": len(clients),
        "total_active_connections": total_active,
        "total_idle_connections": total_idle,
        "total_connections": total_active + total_idle,
        "pools": pools,
    }


def _extract_pool_info(client) -> Dict[str, Any]:
    """
    从 httpx.AsyncClient 内部 transport 提取连接池信息。

    httpx 内部使用 httpcore.AsyncConnectionPool，它有：
    - _connections: list of connections
    - 每个 connection 有 .is_idle / .is_available / .is_closed 等属性
    """
    info: Dict[str, Any] = {
        "active_connections": 0,
        "idle_connections": 0,
        "closed_connections": 0,
    }

    # Explicit proxies and environment proxies live in mounts, not necessarily _transport.
    transports = [getattr(client, "_transport", None)]
    transports.extend((getattr(client, "_mounts", {}) or {}).values())
    seen = set()
    for transport in transports:
        if transport is None:
            continue
        pool = getattr(transport, "_pool", transport)
        if id(pool) in seen:
            continue
        seen.add(id(pool))
        for conn in list(getattr(pool, "_connections", []) or []):
            try:
                closed = getattr(conn, "is_closed", False)
                idle = getattr(conn, "is_idle", False)
                if closed() if callable(closed) else closed:
                    info["closed_connections"] += 1
                elif idle() if callable(idle) else idle:
                    info["idle_connections"] += 1
                else:
                    info["active_connections"] += 1
            except Exception:
                info["active_connections"] += 1

    return info


# ==================== 进程内存 ====================

def get_memory_metrics() -> Dict[str, Any]:
    """返回当前进程的内存使用情况。"""
    try:
        # /proc/self/status 在 Linux 上总是可用的，不需要 psutil
        with open("/proc/self/status", "r") as f:
            status = f.read()

        metrics: Dict[str, Any] = {}
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                metrics["rss_kb"] = int(line.split()[1])
                metrics["rss_mb"] = round(metrics["rss_kb"] / 1024, 1)
            elif line.startswith("VmSize:"):
                metrics["vms_kb"] = int(line.split()[1])
                metrics["vms_mb"] = round(metrics["vms_kb"] / 1024, 1)
            elif line.startswith("VmPeak:"):
                metrics["peak_kb"] = int(line.split()[1])
                metrics["peak_mb"] = round(metrics["peak_kb"] / 1024, 1)
            elif line.startswith("Threads:"):
                metrics["threads"] = int(line.split()[1])
            elif line.startswith("FDSize:"):
                metrics["fd_slots"] = int(line.split()[1])

        # 实际打开的 fd 数量
        try:
            metrics["open_fds"] = len(os.listdir("/proc/self/fd"))
        except Exception:
            pass

        return metrics

    except Exception:
        # 非 Linux 或读取失败
        try:
            import resource
            usage = resource.getrusage(resource.RUSAGE_SELF)
            return {
                "rss_kb": usage.ru_maxrss,
                "rss_mb": round(usage.ru_maxrss / 1024, 1),
            }
        except Exception:
            return {"available": False}


# ==================== 出站 HTTP 摘要 ====================

def get_outbound_summary() -> Dict[str, Any]:
    """从 core.http 的环形缓冲区提取摘要统计。"""
    try:
        from core.http import get_outbound_log_entries

        data = get_outbound_log_entries(limit=500)
        items = data.get("items", [])
        if not items:
            return {
                "total_logged": 0,
                "buffer_size": data.get("buffer_size", 0),
            }

        error_count = sum(1 for e in items if e.get("error"))
        status_counts: Dict[str, int] = defaultdict(int)
        host_counts: Dict[str, int] = defaultdict(int)
        total_elapsed = 0
        elapsed_count = 0

        for entry in items:
            sc = entry.get("status_code")
            if sc is not None:
                bucket = f"{sc // 100}xx"
                status_counts[bucket] += 1

            host = entry.get("host", "unknown")
            host_counts[host] += 1

            elapsed = entry.get("elapsed_ms")
            if elapsed is not None:
                total_elapsed += elapsed
                elapsed_count += 1

        return {
            "total_logged": len(items),
            "buffer_size": data.get("buffer_size", 0),
            "max_id": data.get("max_id", 0),
            "transport_errors": error_count,
            "status_distribution": dict(status_counts),
            "avg_elapsed_ms": round(total_elapsed / elapsed_count) if elapsed_count else None,
            "top_hosts": dict(sorted(host_counts.items(), key=lambda x: -x[1])[:10]),
        }
    except Exception:
        return {"available": False}


# ==================== 聚合快照 ====================

def get_full_metrics_snapshot(app_state=None) -> Dict[str, Any]:
    """
    一次性返回所有运行时指标，供 /healthz 使用。
    """
    client_manager = getattr(app_state, "client_manager", None) if app_state else None

    return {
        "requests": get_request_metrics(),
        "connections": get_pool_metrics(client_manager),
        "memory": get_memory_metrics(),
        "outbound": get_outbound_summary(),
    }
