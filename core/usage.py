"""Usage extraction helpers.

集中处理各上游缓存 usage 字段，原因是同一请求可能走方言解析、通道适配器
或透传流式解析。把字段映射放在这里，可以避免多个路径遗漏 Prompt Caching 统计。
"""

from __future__ import annotations

from typing import Any, Mapping


def to_non_negative_int(value: Any) -> int:
    """将上游 usage 值规范为非负整数，目的在于容忍字符串数字和缺失字段。"""
    if value is None:
        return 0
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return 0
    return normalized if normalized > 0 else 0


def _nested(mapping: Mapping[str, Any], *keys: str) -> Any:
    """按路径读取嵌套字段，避免在各通道里重复写防御性 isinstance 判断。"""
    current: Any = mapping
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def extract_cache_usage(usage: Any) -> dict[str, int]:
    """从不同上游的 usage 对象中提取统一缓存 token 字段。

    目的：把 OpenAI、Responses API、Claude、Gemini 和 DeepSeek 的缓存字段统一为
    cached_tokens 与 cache_creation_tokens，供 request_stats 直接存储。
    """
    if not isinstance(usage, Mapping):
        return {"cached_tokens": 0, "cache_creation_tokens": 0}

    # 缓存命中字段名称因供应商不同而不同；按明确字段路径依次读取，取第一个非零值。
    cached_candidates = (
        _nested(usage, "prompt_tokens_details", "cached_tokens"),
        _nested(usage, "input_tokens_details", "cached_tokens"),
        usage.get("cached_tokens"),
        usage.get("prompt_cache_hit_tokens"),
        usage.get("cachedContentTokenCount"),
        usage.get("cache_read_input_tokens"),
    )
    cached_tokens = 0
    for candidate in cached_candidates:
        cached_tokens = to_non_negative_int(candidate)
        if cached_tokens > 0:
            break

    # 缓存创建 token 在 Claude 原生字段和内核标准字段中都可能出现；同时读取二者，避免出口转换丢失该信息。
    cache_creation_tokens = 0
    for candidate in (usage.get("cache_creation_tokens"), usage.get("cache_creation_input_tokens")):
        cache_creation_tokens = to_non_negative_int(candidate)
        if cache_creation_tokens > 0:
            break

    return {
        "cached_tokens": cached_tokens,
        "cache_creation_tokens": cache_creation_tokens,
    }


def build_openai_usage(
    prompt_tokens: Any,
    completion_tokens: Any,
    total_tokens: Any,
    cached_tokens: Any = 0,
    cache_creation_tokens: Any = 0,
) -> dict[str, Any]:
    """构造内核统一使用的 OpenAI usage 对象。

    这样做的原因是流式和非流式出口都需要输出 prompt_tokens_details.cached_tokens，
    同时保留 cache_creation_tokens 这个内核补充字段，供 Claude 方言还原为
    cache_creation_input_tokens。
    """
    usage = {
        "prompt_tokens": to_non_negative_int(prompt_tokens),
        "completion_tokens": to_non_negative_int(completion_tokens),
        "total_tokens": to_non_negative_int(total_tokens),
    }

    cached_val = to_non_negative_int(cached_tokens)
    if cached_val > 0:
        # OpenAI 标准缓存命中字段位于 prompt_tokens_details.cached_tokens，下游 OAI 客户端依赖该结构。
        usage["prompt_tokens_details"] = {"cached_tokens": cached_val}

    creation_val = to_non_negative_int(cache_creation_tokens)
    if creation_val > 0:
        # OpenAI 没有标准缓存创建字段；这里保留内核字段，目的在于让 Claude 出口能无损还原原生 usage。
        usage["cache_creation_tokens"] = creation_val

    return usage
