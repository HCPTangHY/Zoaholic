"""完整请求/响应保存插件（save_full_payload）

功能：
在不影响系统默认截断逻辑和安全性的前提下，将每个请求的完整、未截断的
请求体 (Request Payload) 和响应体 (Response Body) 记录到本地文件中。
并自动将流式响应拼接完整，提取AI说的话。
"""

import os
import json
import time
import uuid
from typing import Any, Dict, Optional, Tuple
from core.plugins import register_request_interceptor, register_response_interceptor
from core.plugins import unregister_request_interceptor, unregister_response_interceptor
from core.middleware import request_info
from core.log_config import logger

PLUGIN_INFO = {
    "name": "save_full_payload",
    "version": "1.1.0",
    "description": "将完整的请求和响应保存到本地文件中，并自动拼接流式响应",
    "author": "Zoaholic User",
    "dependencies": [],
    "metadata": {
        "category": "debug",
        "tags": ["log", "debug", "payload"],
        "params_hint": "启用后自动生效，日志保存在 data/full_payloads/ 目录下",
    },
}

EXTENSIONS = [
    "interceptors:save_full_request",
    "interceptors:save_full_response",
]

LOG_DIR = os.path.join(os.getcwd(), "data", "full_payloads")
os.makedirs(LOG_DIR, exist_ok=True)


def _get_filename(req_id: str) -> str:
    return os.path.join(LOG_DIR, f"req_{req_id}.log")


def _extract_content(chunk_str: str) -> str:
    """尝试从流式 SSE 中提取 AI 说的纯文本内容"""
    content = ""
    for line in chunk_str.split("\n"):
        line = line.strip()
        if line.startswith("data: ") and line != "data: [DONE]":
            try:
                data = json.loads(line[6:])
                if "choices" in data and len(data["choices"]) > 0:
                    delta = data["choices"][0].get("delta", {})
                    if "content" in delta:
                        content += delta["content"]
                elif "type" in data and data["type"] == "content_block_delta":
                    delta = data.get("delta", {})
                    if delta.get("type") == "text_delta":
                        content += delta.get("text", "")
            except Exception:
                pass
    return content


async def save_full_request_interceptor(
    request: Any,
    engine: str,
    provider: Dict[str, Any],
    api_key: Optional[str],
    url: str,
    headers: Dict[str, Any],
    payload: Dict[str, Any],
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    
    current_info = request_info.get()
    req_id = current_info.get("req_id") if current_info and "req_id" in current_info else str(uuid.uuid4())[:8]
    
    if current_info:
        current_info["save_payload_req_id"] = req_id
        current_info["save_payload_extracted_text"] = ""

    # 使用更美观的纯文本格式替代旧的单行 JSON，增强可读性
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    log_content = f"========== REQUEST ({timestamp}) ==========\n"
    log_content += f"Provider: {provider.get('provider')}\n"
    log_content += f"Engine: {engine}\n"
    log_content += f"URL: {url}\n\n"
    log_content += json.dumps(payload, ensure_ascii=False, indent=2) + "\n\n"
    log_content += f"========== RESPONSE ==========\n"

    try:
        # 请求时用 "w" 模式覆盖/创建文件
        with open(_get_filename(req_id), "w", encoding="utf-8") as f:
            f.write(log_content)
    except Exception as e:
        logger.error(f"[save_full_payload] 写入请求失败: {e}")
    
    return url, headers, payload


async def save_full_response_interceptor(
    response_chunk: Any,
    engine: str,
    model: str,
    is_stream: bool,
):
    current_info = request_info.get()
    req_id = current_info.get("save_payload_req_id") if current_info else str(uuid.uuid4())[:8]

    chunk_str = response_chunk
    if isinstance(response_chunk, bytes):
        chunk_str = response_chunk.decode("utf-8", errors="replace")
    elif not isinstance(response_chunk, str):
        chunk_str = json.dumps(response_chunk, ensure_ascii=False)

    # 提取流式纯文本
    if is_stream and current_info is not None:
        text_piece = _extract_content(chunk_str)
        if text_piece:
            current_info["save_payload_extracted_text"] += text_piece
    
    # 直接原样追加内容，不再包裹{"type":"RESPONSE_CHUNK"}外壳
    try:
        with open(_get_filename(req_id), "a", encoding="utf-8") as f:
            if not is_stream and isinstance(response_chunk, (dict, list)):
                 f.write(json.dumps(response_chunk, ensure_ascii=False, indent=2) + "\n")
            else:
                 f.write(chunk_str)
    except Exception:
        pass

    # 检测是否结束，并在末尾打印“提取出的纯文字”
    is_end = False
    if is_stream:
        if "data: [DONE]" in chunk_str or "message_stop" in chunk_str:
            is_end = True
    else:
        is_end = True
        # 非流式直接解析出最终 content
        if current_info is not None:
            try:
                if isinstance(response_chunk, dict) and "choices" in response_chunk:
                    msg = response_chunk["choices"][0].get("message", {})
                    if "content" in msg:
                        current_info["save_payload_extracted_text"] = msg["content"]
                elif isinstance(chunk_str, str):
                    data = json.loads(chunk_str)
                    if "choices" in data:
                         current_info["save_payload_extracted_text"] = data["choices"][0]["message"].get("content", "")
            except Exception:
                pass

    if is_end and current_info and current_info.get("save_payload_extracted_text"):
        try:
            with open(_get_filename(req_id), "a", encoding="utf-8") as f:
                f.write("\n\n========== EXTRACTED TEXT (AI 回复的纯文本) ==========\n")
                f.write(current_info["save_payload_extracted_text"] + "\n")
        except Exception:
            pass

    return response_chunk


def setup(manager):
    logger.info(f"[{PLUGIN_INFO['name']}] 正在初始化... 日志目录: {LOG_DIR}")
    register_request_interceptor(
        interceptor_id="save_full_request",
        callback=save_full_request_interceptor,
        priority=99,
        plugin_name=PLUGIN_INFO["name"],
    )
    register_response_interceptor(
        interceptor_id="save_full_response",
        callback=save_full_response_interceptor,
        priority=99,
        plugin_name=PLUGIN_INFO["name"],
    )


def teardown(manager):
    unregister_request_interceptor("save_full_request")
    unregister_response_interceptor("save_full_response")
