"""上游错误信息屏蔽插件（error_mask）

功能：
- 启用此插件的渠道，所有上游返回的错误信息会被替换为统一的安全提示。
- 保留原始状态码，仅屏蔽 message 内容。
- 通过响应拦截器生效：屏蔽底层 check_response 返回的 error dict 和透传模式的原始文本
- 支持自定义提示语：通过框架 ContextVar 读取渠道配置的 error_mask:xxx 参数

配置位置：
- provider.preferences.enabled_plugins 中添加 "error_mask" 或 "error_mask:自定义提示语"

示例：
  enabled_plugins:
    - error_mask                           # 使用默认提示语
    - error_mask:Service temporarily unavailable  # 自定义提示语
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple, List

from core.log_config import logger
from core.plugins import (
    register_response_interceptor,
    unregister_response_interceptor,
    get_current_plugin_options,
)

PLUGIN_INFO = {
    "name": "error_mask",
    "version": "1.1.0",
    "description": "屏蔽上游错误详情，替换为统一的安全提示语，防止内部信息泄露",
    "author": "Zoaholic Team",
    "dependencies": [],
    "metadata": {
        "category": "interceptors",
        "tags": ["error", "mask", "security"],
        "params_hint": "可通过 error_mask:自定义提示语 设置替换文本，默认为 'The upstream service returned an error. Please try again later.'",
        "params_schema": [
            {
                "key": "message",
                "label": "替换提示语",
                "type": "text",
                "placeholder": "留空使用默认提示",
                "default": "",
            },
        ],
    },
}

EXTENSIONS = [
    "interceptors:error_mask_response",
]

# 默认替换文本
DEFAULT_MESSAGE = "The upstream service returned an error. Please try again later."


def _mask_error_dict(chunk: dict, mask_msg: str) -> dict:
    """处理 check_response 返回的 error dict 格式。

    格式: {"error": "...", "status_code": 4xx/5xx, "details": ...}
    只替换 details 和 error 中的具体信息，保留 status_code。
    """
    masked = dict(chunk)
    masked["error"] = mask_msg
    masked["details"] = mask_msg
    return masked


def _mask_json_string(text: str, mask_msg: str) -> str:
    """尝试将文本解析为 JSON，如果包含 error 字段则替换所有敏感内容。

    处理两种常见格式：
    1. OpenAI 格式: {"error": {"message": "...", "type": "...", ...}}
    2. Gemini 格式: {"error": {"code": 400, "message": "...", "status": "...", "details": [...]}}
    """
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return text

    if not isinstance(data, dict):
        # Gemini 有时返回数组格式 [{"error": {...}}]
        if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
            inner = data[0]
            if "error" in inner:
                masked_inner = json.loads(_mask_json_string(json.dumps(inner, ensure_ascii=False), mask_msg))
                return json.dumps([masked_inner], ensure_ascii=False)
        return text

    error_obj = data.get("error")
    if error_obj is None:
        return text

    if isinstance(error_obj, dict):
        if "message" in error_obj:
            error_obj["message"] = mask_msg
        # 清理 Gemini 的 details 数组（可能包含 fieldViolations 等敏感信息）
        if "details" in error_obj:
            del error_obj["details"]
        # 清理 Gemini 的 status 字段（如 INVALID_ARGUMENT）
        if "status" in error_obj:
            error_obj["status"] = "error"
        data["error"] = error_obj
    elif isinstance(error_obj, str):
        data["error"] = {"message": mask_msg, "type": "upstream_error"}

    return json.dumps(data, ensure_ascii=False)


async def error_mask_response_interceptor(
    response_chunk: Any,
    engine: str,
    model: str,
    is_stream: bool,
) -> Any:
    """响应拦截器：屏蔽上游错误信息。

    拦截器拿到的数据类型取决于路径：
    - dict: check_response 返回的错误（含 "error" 键）
    - str: 透传模式的原始文本（可能是 JSON，可能是 SSE）
    - 其他: 结构化响应数据，不处理
    """
    # 读取自定义提示语（通过框架 ContextVar 获取当前渠道的插件参数）
    custom_options = get_current_plugin_options("error_mask")
    # custom_options 是 error_mask: 后面的内容，如果有的话
    mask_msg = custom_options.strip('"').strip("'") if custom_options else DEFAULT_MESSAGE

    # dict 类型：底层 check_response 返回的错误
    if isinstance(response_chunk, dict) and "error" in response_chunk:
        logger.debug(f"[error_mask] Masking error dict for model={model}")
        return _mask_error_dict(response_chunk, mask_msg)

    # str 类型：透传模式的原始文本
    if isinstance(response_chunk, str):
        # 快速判断：如果不包含 "error" 关键字，大概率不是错误响应，直接放行
        if '"error"' not in response_chunk:
            return response_chunk

        # SSE 格式可能包含多行 data:，逐行检查
        if response_chunk.startswith("data: "):
            lines = response_chunk.split("\n")
            masked_lines = []
            changed = False
            for line in lines:
                if line.startswith("data: ") and '"error"' in line:
                    json_part = line[6:]  # 去掉 "data: " 前缀
                    masked_json = _mask_json_string(json_part, mask_msg)
                    if masked_json != json_part:
                        masked_lines.append("data: " + masked_json)
                        changed = True
                    else:
                        masked_lines.append(line)
                else:
                    masked_lines.append(line)
            if changed:
                logger.debug(f"[error_mask] Masking SSE error for model={model}")
                return "\n".join(masked_lines)
            return response_chunk

        # 纯 JSON 文本
        masked = _mask_json_string(response_chunk, mask_msg)
        if masked != response_chunk:
            logger.debug(f"[error_mask] Masking JSON error for model={model}")
        return masked

    # 其他类型（结构化响应对象等），不处理
    return response_chunk


def setup(manager):
    logger.info(f"[{PLUGIN_INFO['name']}] Initializing...")

    register_response_interceptor(
        interceptor_id="error_mask_response",
        callback=error_mask_response_interceptor,
        priority=10,  # 高优先级，尽早拦截错误
        plugin_name=PLUGIN_INFO["name"],
        metadata={"description": "屏蔽上游错误详情，替换为安全提示语"},
    )

    logger.info(f"[{PLUGIN_INFO['name']}] Response interceptor registered")


def teardown(manager):
    logger.info(f"[{PLUGIN_INFO['name']}] Cleaning up...")
    unregister_response_interceptor("error_mask_response")
    logger.info(f"[{PLUGIN_INFO['name']}] Cleaned up")
