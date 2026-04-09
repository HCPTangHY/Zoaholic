from typing import Any


def canonicalize_header_name(name: str) -> str:
    """将请求头名称规范化为常见的 HTTP 头写法。"""
    parts = str(name or "").strip().split("-")
    return "-".join(part[:1].upper() + part[1:].lower() if part else "" for part in parts)



def merge_headers_case_insensitive(*header_sets) -> dict[str, str]:
    """按大小写无关的方式合并多个请求头字典。"""
    merged: dict[str, str] = {}
    normalized_to_actual: dict[str, str] = {}

    for header_set in header_sets:
        if not isinstance(header_set, dict):
            continue

        for raw_key, raw_value in header_set.items():
            if raw_value is None:
                continue

            normalized_key = str(raw_key).strip().lower()
            if not normalized_key:
                continue

            canonical_key = canonicalize_header_name(str(raw_key))
            previous_key = normalized_to_actual.get(normalized_key)
            if previous_key and previous_key in merged and previous_key != canonical_key:
                del merged[previous_key]

            normalized_to_actual[normalized_key] = canonical_key
            merged[canonical_key] = raw_value

    return merged



def set_header_default_case_insensitive(headers: dict[str, str], key: str, value: str) -> dict[str, str]:
    """大小写无关地设置请求头默认值。"""
    normalized_key = str(key or "").strip().lower()
    if not normalized_key:
        return headers

    for existing_key in headers.keys():
        if str(existing_key).strip().lower() == normalized_key:
            return headers

    headers[canonicalize_header_name(key)] = value
    return headers



def build_attempt_provider_list(providers: list[dict[str, Any]], start_index: int = 0) -> list[dict[str, Any]]:
    """基于展开后的 provider 槽位列表，构建单次请求的去重尝试列表。"""
    if not providers:
        return []

    total = len(providers)
    normalized_start = start_index % total
    ordered_slots = providers[normalized_start:] + providers[:normalized_start]
    attempt_providers: list[dict[str, Any]] = []
    seen_provider_names = set()

    for provider in ordered_slots:
        provider_name = str(provider.get("provider", ""))
        if provider_name in seen_provider_names:
            continue
        seen_provider_names.add(provider_name)
        attempt_providers.append(provider)

    return attempt_providers
