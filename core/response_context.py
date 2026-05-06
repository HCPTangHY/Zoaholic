"""
请求响应上下文辅助模块。

用于在通道适配器内部直接写入：
- usage
- content_start_time
- adapter 管理标记
"""

from __future__ import annotations

from time import time
from typing import Optional

from .middleware import request_info


def get_current_request_info() -> Optional[dict]:
    try:
        current = request_info.get()
    except Exception:
        return None
    return current if isinstance(current, dict) else None


def mark_adapter_metrics_managed() -> None:
    current = get_current_request_info()
    if current is not None:
        current["adapter_metrics_managed"] = True


def mark_content_start() -> None:
    current = get_current_request_info()
    if not current:
        return
    if current.get("content_start_time") is not None:
        return
    start_time = current.get("start_time")
    if start_time is None:
        return
    current["content_start_time"] = time() - start_time


def _to_non_negative_int(value) -> Optional[int]:
    """将值转换为非负整数。返回 None 表示无效或为负。0 被视为有效值但不会覆盖已有值。"""
    if value is None:
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    if normalized < 0:
        return None
    return normalized


def merge_usage(
    prompt_tokens: Optional[int] = None,
    completion_tokens: Optional[int] = None,
    total_tokens: Optional[int] = None,
    cached_tokens: Optional[int] = None,
    cache_creation_tokens: Optional[int] = None,
) -> None:
    """合并 usage 到当前请求上下文。

    规则：
    - 仅用正数（> 0）覆盖 prompt/completion，0 不覆盖已有值。
    - total_tokens 若为正数则直接写入。
    - cached/cache_creation 仅用正数覆盖，避免后续空 usage 覆盖已采集的缓存信息。
    - 否则按 prompt+completion 重算。
    """
    current = get_current_request_info()
    if not current:
        return

    prompt_val = _to_non_negative_int(prompt_tokens)
    completion_val = _to_non_negative_int(completion_tokens)
    total_val = _to_non_negative_int(total_tokens)
    cached_val = _to_non_negative_int(cached_tokens)
    cache_creation_val = _to_non_negative_int(cache_creation_tokens)

    if prompt_val is not None and prompt_val > 0:
        current["prompt_tokens"] = prompt_val
    if completion_val is not None and completion_val > 0:
        current["completion_tokens"] = completion_val

    # 缓存字段与普通 token 一起写入 current_info，目的在于让通道适配器路径和方言解析路径统计一致。
    if cached_val is not None and cached_val > 0:
        current["cached_tokens"] = cached_val
    if cache_creation_val is not None and cache_creation_val > 0:
        current["cache_creation_tokens"] = cache_creation_val

    if total_val is not None and total_val > 0:
        current["total_tokens"] = total_val
        return

    current["total_tokens"] = (
        current.get("prompt_tokens", 0)
        + current.get("completion_tokens", 0)
    )
