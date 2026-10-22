from __future__ import annotations

import gc
import os
import sys
import time
from collections import Counter
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from routes.deps import verify_admin_api_key

# 修改原因：/debug/* 端点会暴露进程内存细节，tracemalloc 开关还构成性能攻击面。
# 修改方式：路由级统一挂 admin 鉴权；注册与否由 ENABLE_DEBUG_ENDPOINTS 控制（见 routes/__init__.py）；
# 昂贵的对象普查/快照操作增加最小调用间隔，防止即使持有凭证也把服务打卡。
# 目的：内存排查工具仅限管理员、默认不开启、开启后也有频率保护。
router = APIRouter(dependencies=[Depends(verify_admin_api_key)])

_baseline: dict[str, int] | None = None

# 昂贵诊断操作的最小调用间隔（秒），可环境变量调整
DEBUG_MIN_INTERVAL = float(os.getenv("DEBUG_MEMORY_MIN_INTERVAL", "2"))
_last_expensive_call: float = 0.0


def _throttle_expensive() -> None:
    """对需要遍历 gc 对象或拍快照的端点限频。"""
    global _last_expensive_call
    now = time.monotonic()
    if now - _last_expensive_call < DEBUG_MIN_INTERVAL:
        raise HTTPException(status_code=429, detail="debug endpoint cooling down, retry later")
    _last_expensive_call = now


def _get_rss_mb() -> float | None:
    try:
        with open("/proc/self/status", "r") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except Exception:
        pass
    return None


def _type_census() -> dict[str, int]:
    counter: Counter[str] = Counter()
    for obj in gc.get_objects():
        counter[type(obj).__name__] += 1
    return dict(counter.most_common(30))


def _coroutine_census() -> dict[str, int]:
    counter: Counter[str] = Counter()
    for obj in gc.get_objects():
        if type(obj).__name__ == 'coroutine':
            code = getattr(obj, 'cr_code', None)
            if code:
                loc = f"{code.co_filename}:{code.co_name}"
            else:
                loc = "<unknown>"
            counter[loc] += 1
    return dict(counter.most_common(20))


@router.get("/debug/memory")
async def debug_memory():
    _throttle_expensive()
    gc_stats = gc.get_stats()
    top_types = _type_census()
    return {
        "rss_mb": _get_rss_mb(),
        "gc_stats": gc_stats,
        "gc_tracked_objects": len(gc.get_objects()),
        "top30_types": top_types,
        "coroutine_details": _coroutine_census(),
    }


@router.get("/debug/memory/diff")
async def debug_memory_diff():
    global _baseline
    _throttle_expensive()
    current = _type_census()
    if _baseline is None:
        _baseline = current
        return {"message": "baseline taken, call again to see diff", "rss_mb": _get_rss_mb(), "baseline_top30": current}

    diff = {}
    all_keys = set(current) | set(_baseline)
    for k in all_keys:
        c = current.get(k, 0)
        b = _baseline.get(k, 0)
        d = c - b
        if d != 0:
            diff[k] = {"current": c, "baseline": b, "diff": d}

    sorted_diff = dict(sorted(diff.items(), key=lambda x: -abs(x[1]["diff"]))[:20])
    _baseline = current
    return {"rss_mb": _get_rss_mb(), "top20_growth": sorted_diff}

import tracemalloc as _tm

# 修改原因：tracemalloc 开/关会改变全局运行时状态（开启后显著拖慢服务），
# 属于有副作用操作，不应使用 GET。
# 修改方式：start/stop 改为 POST；top 保持 GET 但受限频保护。
# 目的：避免预取/扫描类 GET 请求意外触发，也让语义更正确。
@router.post("/debug/memory/tracemalloc/start")
async def tm_start():
    if _tm.is_tracing():
        return {"status": "already tracing"}
    _tm.start(10)
    return {"status": "started", "rss_mb": _get_rss_mb()}

@router.get("/debug/memory/tracemalloc/top")
async def tm_top():
    _throttle_expensive()
    if not _tm.is_tracing():
        return {"error": "not tracing, call /debug/memory/tracemalloc/start first"}
    snapshot = _tm.take_snapshot()
    stats = snapshot.statistics("lineno")
    top = []
    for s in stats[:30]:
        top.append({
            "file": str(s.traceback),
            "size_mb": round(s.size / 1048576, 2),
            "count": s.count,
        })
    current, peak = _tm.get_traced_memory()
    return {
        "rss_mb": _get_rss_mb(),
        "traced_current_mb": round(current / 1048576, 2),
        "traced_peak_mb": round(peak / 1048576, 2),
        "top30": top,
    }

@router.post("/debug/memory/tracemalloc/stop")
async def tm_stop():
    if _tm.is_tracing():
        _tm.stop()
    return {"status": "stopped"}
