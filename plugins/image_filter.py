"""图片过滤插件（image_filter）

功能：
- 过滤请求消息中的图片内容（image_url / base64 图片 / 图片类型的 file）
- 适用于不支持多模态输入的纯文本模型
- 可选保留占位文本提示用户图片已被过滤

配置位置：
- provider.preferences.enabled_plugins 中添加 "image_filter" 或 "image_filter:quiet"

参数：
- 无参数或 "image_filter" — 过滤图片并插入 [图片已过滤] 占位提示
- "image_filter:quiet" — 静默过滤，不插入任何占位提示
- "image_filter:自定义提示语" — 过滤图片并插入自定义占位文本

处理逻辑：
1. 遍历所有消息的 content
2. 如果 content 是 list（多模态格式），移除 type 为 image_url 的项
3. 移除 type 为 file 且 mime_type 以 image/ 开头的项
4. 移除 type 为 file 且 url/data 包含 data:image/ 前缀的项
5. 如果整个 content list 被清空，替换为占位文本
6. 如果 content 是纯字符串，不做处理（纯文本没有图片）
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from core.log_config import logger
from core.plugins import (
    register_request_interceptor,
    unregister_request_interceptor,
    get_plugin_options,
)


PLUGIN_INFO = {
    "name": "image_filter",
    "version": "1.0.0",
    "description": "图片过滤插件 — 过滤请求中的图片内容，适用于不支持多模态输入的纯文本模型。支持 image_url、base64 图片和 file 类型图片的过滤。",
    "author": "Zoaholic Team",
    "dependencies": [],
    "metadata": {
        "category": "interceptors",
        "tags": ["filter", "image", "multimodal", "text-only"],
        "params_hint": "留空 = 过滤并插入 [图片已过滤] 提示 | quiet = 静默过滤 | 其他文本 = 自定义占位提示",
        "params_schema": [
            {
                "key": "mode",
                "label": "过滤模式",
                "type": "select",
                "options": [
                    {"value": "", "label": "插入 [图片已过滤] 提示（默认）"},
                    {"value": "quiet", "label": "静默过滤（不插入提示）"},
                ],
                "default": "",
            },
            {
                "key": "placeholder",
                "label": "自定义占位文本",
                "type": "text",
                "placeholder": "留空使用默认提示",
                "default": "",
                "visible_when": {"mode": ""},
            },
        ],
    },
}

EXTENSIONS = [
    "interceptors:image_filter_request",
]

DEFAULT_PLACEHOLDER = "[图片已过滤]"


def _is_image_item(item: Dict[str, Any]) -> bool:
    """判断一个 content item 是否为图片类型"""
    item_type = item.get("type", "")

    # 标准 image_url 类型
    if item_type == "image_url":
        return True

    # input_image 类型（Responses API 格式）
    if item_type == "input_image":
        return True

    # file 类型 — 检查是否为图片文件
    if item_type == "file":
        file_info = item.get("file", {})
        if isinstance(file_info, dict):
            # 通过 mime_type 判断
            mime = file_info.get("mime_type", "")
            if mime and mime.startswith("image/"):
                return True
            # 通过 url 的 data URI 前缀判断
            url = file_info.get("url", "")
            if url and url.startswith("data:image/"):
                return True
            # 通过 data 字段 + mime 判断
            data = file_info.get("data", "")
            if data and mime and mime.startswith("image/"):
                return True

    # input_file 类型 — 检查是否为图片
    if item_type == "input_file":
        # input_file 可能有 image_url 字段
        if item.get("image_url"):
            return True

    return False


def _filter_messages(payload: Dict[str, Any], placeholder: Optional[str]) -> int:
    """
    过滤 payload 中消息的图片内容。
    返回被过滤的图片数量。
    """
    messages = payload.get("messages") or payload.get("input") or []
    filtered_count = 0

    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue

        # 分离图片和非图片内容
        kept = []
        removed = 0
        for item in content:
            if isinstance(item, dict) and _is_image_item(item):
                removed += 1
            else:
                kept.append(item)

        if removed == 0:
            continue

        filtered_count += removed

        if kept:
            # 还有非图片内容，保留
            if placeholder:
                kept.append({"type": "text", "text": placeholder})
            msg["content"] = kept
        else:
            # 所有内容都是图片，用占位文本替换
            if placeholder:
                msg["content"] = placeholder
            else:
                msg["content"] = ""

    return filtered_count


async def _image_filter_interceptor(
    request: Any,
    engine: str,
    provider: Dict[str, Any],
    api_key: Optional[str],
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
    """请求拦截器：过滤消息中的图片内容"""
    # 获取插件参数
    options = get_plugin_options(provider, "image_filter") or ""

    if options.lower() == "quiet":
        placeholder = None
    elif options:
        placeholder = options
    else:
        placeholder = DEFAULT_PLACEHOLDER

    count = _filter_messages(payload, placeholder)
    if count > 0:
        logger.info(f"[image_filter] 已过滤 {count} 个图片内容 (provider={provider.get('provider', '?')})")

    return url, headers, payload


def setup(manager):
    """注册拦截器"""
    register_request_interceptor(
        "image_filter_request",
        _image_filter_interceptor,
        priority=10,  # 较早执行，在其他插件处理之前过滤
        plugin_name="image_filter",
    )
    logger.info("[image_filter] 插件已加载")


def teardown(manager):
    """卸载拦截器"""
    unregister_request_interceptor("image_filter_request")
    logger.info("[image_filter] 插件已卸载")
