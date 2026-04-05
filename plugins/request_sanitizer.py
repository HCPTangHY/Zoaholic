"""请求参数清洗插件（request_sanitizer）

定位：
- 作为"请求拦截器插件"运行，在请求发送到上游之前修正常见的非法/不兼容参数。
- 仅当渠道在 provider.preferences.enabled_plugins 显式启用本插件时生效。

修正规则：
1. 强制流式：将 stream 设为 True
2. 温度范围：将 temperature 钳制到 [0, 1]
3. 移除 safety_settings：上游不接受该参数（"safety_settings: Extra inputs are not permitted"）
4. 修正空 system 消息：移除内容为空白的 system 消息
   （"system: text content blocks must contain non-whitespace text"）
5. 修正空顶层 system 字段：移除 Claude API 格式中空白的顶层 "system" 字段

配置位置：
- provider.preferences.enabled_plugins 中添加 "request_sanitizer"
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from core.log_config import logger
from core.plugins import register_request_interceptor, unregister_request_interceptor


PLUGIN_INFO = {
    "name": "request_sanitizer",
    "version": "1.0.0",
    "description": "请求参数清洗插件 - 自动修正常见非法参数，避免上游报错",
    "author": "Zoaholic Team",
    "dependencies": [],
    "metadata": {
        "category": "interceptors",
        "tags": ["sanitizer", "compat", "payload"],
        "params_hint": "在 provider.preferences.enabled_plugins 中添加 request_sanitizer 即可启用。",
    },
}

EXTENSIONS = [
    "interceptors:request_sanitizer_request",
]


def _force_stream(payload: Dict[str, Any]) -> bool:
    """强制开启流式，返回是否做了修改"""
    if not payload.get("stream"):
        payload["stream"] = True
        return True
    return False


def _clamp_temperature(payload: Dict[str, Any]) -> bool:
    """将 temperature 钳制到 [0, 1] 范围，返回是否做了修改"""
    temp = payload.get("temperature")
    if temp is None or not isinstance(temp, (int, float)):
        return False

    original = temp
    clamped = max(0.0, min(1.0, float(temp)))
    if clamped != original:
        payload["temperature"] = clamped
        return True
    return False


def _remove_safety_settings(payload: Dict[str, Any]) -> bool:
    """移除 safety_settings 参数，返回是否做了修改"""
    if "safety_settings" in payload:
        del payload["safety_settings"]
        return True
    return False


def _is_blank_text(content: Any) -> bool:
    """判断 content 是否为空白文本"""
    if isinstance(content, str):
        return not content.strip()
    # content 可能是 list 格式 [{"type": "text", "text": "..."}]
    if isinstance(content, list):
        # 如果列表为空，视为空白
        if not content:
            return True
        # 检查所有 text block 是否都为空白
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    text = item.get("text", "")
                    if isinstance(text, str) and text.strip():
                        return False
                else:
                    # 非 text 类型的 block（如 image），不算空白
                    return False
            else:
                return False
        return True
    # None 也视为空白
    if content is None:
        return True
    return False


def _fix_empty_top_level_system(payload: Dict[str, Any]) -> bool:
    """移除空白的顶层 system 字段（Claude API 格式），返回是否做了修改

    Claude API 使用顶层 "system" 字段而非 messages 数组中的 system 消息。
    当该字段为纯空白文本时，Claude 会拒绝请求：
    "system: text content blocks must contain non-whitespace text"
    """
    system = payload.get("system")
    if system is None:
        return False

    # 字符串格式：直接检查是否为空白
    if isinstance(system, str):
        if not system.strip():
            del payload["system"]
            return True
        return False

    # 列表格式 [{"type": "text", "text": "..."}]：复用已有的 _is_blank_text
    if isinstance(system, list) and _is_blank_text(system):
        del payload["system"]
        return True

    return False


def _fix_empty_system_messages(payload: Dict[str, Any]) -> bool:
    """移除内容为空白的 system 消息，返回是否做了修改"""
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return False

    original_len = len(messages)
    cleaned = [
        msg for msg in messages
        if not (
            isinstance(msg, dict)
            and msg.get("role") == "system"
            and _is_blank_text(msg.get("content"))
        )
    ]

    if len(cleaned) < original_len:
        payload["messages"] = cleaned
        return True
    return False


async def request_sanitizer_request_interceptor(
    request: Any,
    engine: str,
    provider: Dict[str, Any],
    api_key: Optional[str],
    url: str,
    headers: Dict[str, Any],
    payload: Dict[str, Any],
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    if not isinstance(payload, dict) or not payload:
        return url, headers, payload

    fixes: List[str] = []

    if _force_stream(payload):
        fixes.append("stream=true")

    if _clamp_temperature(payload):
        fixes.append(f"temperature={payload['temperature']}")

    if _remove_safety_settings(payload):
        fixes.append("removed safety_settings")

    if _fix_empty_system_messages(payload):
        fixes.append("removed empty system messages")

    if _fix_empty_top_level_system(payload):
        fixes.append("removed empty top-level system field")

    if fixes:
        logger.info(
            f"[request_sanitizer] Sanitized payload: {', '.join(fixes)} "
            f"(engine={engine}, model={payload.get('model', '?')})"
        )

    return url, headers, payload


def setup(manager):
    logger.info(f"[{PLUGIN_INFO['name']}] 正在初始化...")

    register_request_interceptor(
        interceptor_id="request_sanitizer_request",
        callback=request_sanitizer_request_interceptor,
        priority=900,
        plugin_name=PLUGIN_INFO["name"],
        metadata={"description": "请求参数清洗（强制流式、温度范围、移除非法字段、修复空系统消息）"},
    )

    logger.info(f"[{PLUGIN_INFO['name']}] 已注册请求拦截器")


def teardown(manager):
    logger.info(f"[{PLUGIN_INFO['name']}] 正在清理...")
    unregister_request_interceptor("request_sanitizer_request")
    logger.info(f"[{PLUGIN_INFO['name']}] 已清理完成")
