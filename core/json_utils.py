"""
JSON 辅助模块。

目标：
- 优先使用更快的 JSON 实现。
- 在不可用时回退到标准库 json。
- 统一提供文本和字节两种序列化接口。
"""

from __future__ import annotations

import json
from typing import Any

try:
    import orjson  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    orjson = None


ORJSON_AVAILABLE = orjson is not None


def json_loads(data: Any) -> Any:
    """反序列化 JSON。

    支持 str / bytes / bytearray / memoryview。
    """
    if isinstance(data, memoryview):
        data = data.tobytes()

    if ORJSON_AVAILABLE:
        return orjson.loads(data)

    if isinstance(data, (bytes, bytearray)):
        data = data.decode("utf-8")
    return json.loads(data)


def json_dumps_text(data: Any, *, ensure_ascii: bool = False, sort_keys: bool = False) -> str:
    """序列化为 UTF-8 文本。"""
    if ORJSON_AVAILABLE and not ensure_ascii:
        option = 0
        if sort_keys:
            option |= orjson.OPT_SORT_KEYS
        return orjson.dumps(data, option=option).decode("utf-8")

    return json.dumps(data, ensure_ascii=ensure_ascii, sort_keys=sort_keys)


def json_dumps_bytes(data: Any, *, ensure_ascii: bool = False, sort_keys: bool = False) -> bytes:
    """序列化为 UTF-8 字节串。"""
    if ORJSON_AVAILABLE and not ensure_ascii:
        option = 0
        if sort_keys:
            option |= orjson.OPT_SORT_KEYS
        return orjson.dumps(data, option=option)

    return json.dumps(data, ensure_ascii=ensure_ascii, sort_keys=sort_keys).encode("utf-8")
