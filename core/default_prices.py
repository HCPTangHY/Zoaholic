"""
外部模型价格库 — 从 llm-prices.com 获取默认价格

启动时拉取一次，后台每 24 小时刷新。
作为 get_current_model_prices 的最低优先级 fallback。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger("zoaholic.default_prices")

# 价格缓存
_price_cache: dict[str, dict] = {}  # model_id → {input, output, input_cached, vendor, name}
_cache_updated_at: float = 0
_CACHE_TTL = 86400  # 24 小时
_PRICE_URL = "https://www.llm-prices.com/current-v1.json"
_fetching_lock = asyncio.Lock()


def _normalize_model_id(model_id: str) -> str:
    """标准化模型 ID：小写，去除常见前缀"""
    s = model_id.lower().strip()
    # 去除 provider 前缀（openai/xxx, anthropic/xxx 等）
    if "/" in s:
        s = s.split("/", 1)[-1]
    return s


def _build_index(prices_list: list) -> dict:
    """构建价格索引，支持多种匹配方式"""
    index = {}
    for item in prices_list:
        raw_id = item.get("id", "")
        if not raw_id:
            continue
        normalized = _normalize_model_id(raw_id)
        index[normalized] = item
        # 也存原始 ID
        if raw_id.lower() != normalized:
            index[raw_id.lower()] = item
    return index


async def fetch_prices(force: bool = False) -> bool:
    """从远程拉取价格数据，返回是否成功"""
    global _price_cache, _cache_updated_at

    if not force and _price_cache and (time.time() - _cache_updated_at < _CACHE_TTL):
        return True

    async with _fetching_lock:
        # double check
        if not force and _price_cache and (time.time() - _cache_updated_at < _CACHE_TTL):
            return True

        try:
            import httpx
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(_PRICE_URL)
                if resp.status_code != 200:
                    logger.warning(f"[default_prices] HTTP {resp.status_code} from {_PRICE_URL}")
                    return False
                data = resp.json()
                prices_list = data.get("prices", [])
                if not prices_list:
                    logger.warning("[default_prices] Empty prices list")
                    return False

                _price_cache = _build_index(prices_list)
                _cache_updated_at = time.time()
                updated_at = data.get("updated_at", "unknown")
                logger.info(f"[default_prices] Loaded {len(prices_list)} models (data: {updated_at})")
                return True
        except Exception as e:
            logger.warning(f"[default_prices] Failed to fetch: {e}")
            return False


def lookup_price(model_name: str) -> Optional[tuple[float, float]]:
    """
    查找模型价格。
    
    匹配策略（按优先级）：
    1. 精确匹配（标准化后）
    2. 前缀匹配（取最长匹配）
    3. 去掉日期后缀重试（如 gpt-4o-2024-08-06 → gpt-4o）
    
    Returns:
        (prompt_price, completion_price) 单位 $/M tokens，或 None
    """
    if not _price_cache:
        return None

    normalized = _normalize_model_id(model_name)

    # 1. 精确匹配
    item = _price_cache.get(normalized)
    if item:
        return item.get("input", 0) or 0, item.get("output", 0) or 0

    # 2. 前缀匹配（取最长的 key）
    candidates = [(k, v) for k, v in _price_cache.items() if normalized.startswith(k)]
    if candidates:
        candidates.sort(key=lambda x: len(x[0]), reverse=True)
        item = candidates[0][1]
        return item.get("input", 0) or 0, item.get("output", 0) or 0

    # 3. 去掉日期后缀（-2024-08-06, -20240806, -0125 等）
    import re
    stripped = re.sub(r'-\d{4}[-]?\d{2}[-]?\d{2}$', '', normalized)
    if stripped != normalized:
        item = _price_cache.get(stripped)
        if item:
            return item.get("input", 0) or 0, item.get("output", 0) or 0
    # 也试短日期后缀 (-0125)
    stripped2 = re.sub(r'-\d{4}$', '', normalized)
    if stripped2 != normalized and stripped2 != stripped:
        item = _price_cache.get(stripped2)
        if item:
            return item.get("input", 0) or 0, item.get("output", 0) or 0

    return None


# 后台定时刷新已移至 main.py 统一 daily_maintenance 循环


def get_cache_info() -> dict:
    """返回缓存状态信息"""
    return {
        "cached_models": len(_price_cache),
        "updated_at": _cache_updated_at,
        "ttl": _CACHE_TTL,
        "age_seconds": time.time() - _cache_updated_at if _cache_updated_at else None,
    }
