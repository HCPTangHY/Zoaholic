"""上游过载错误客户端可重试改写插件（overload_client_retry）

定位：
- 作为"响应拦截器插件"运行，同时覆盖三条路径：
  1. 转换路径流内错误 dict（渠道适配器 yield 的 {"error":..., "status_code":...}）
  2. 透传路径原始文本（SSE data: 行 / 非 SSE JSON）
  3. 非流式 check_response 错误 dict
- 仅当某个渠道在 provider.preferences.enabled_plugins 显式启用本插件时生效。

背景：
- OpenAI / Codex 上游高峰期返回 "Selected model is at capacity" /
  "Our servers are currently overloaded"（HTTP 400 或 SSE error/response.failed 事件）。
- Codex 等客户端把这类错误视为不可重试的终态错误，直接中断任务，需要人工发"继续"。
- 同样的错误若呈现为通用 upstream failed 形态，客户端会触发自带的自动重试
  （Codex 最多 5 次），重试经网关重新路由，有机会落到可用 key/渠道。
- 参考：linux.do/t/topic/2122755 社区在 sub2api 上的验证结论。

处理规则：
- 仅匹配过载特征（at capacity / currently overloaded / server_is_overloaded /
  slow_down / servers are overloaded），其他错误原样放行，不影响排障。
- dict 错误：message 改写为可重试文案；status_code 仅当处于不可重试档
  （400/401/403/413）时改写为 502，使 handler 的服务端重试判断同时生效；
  429/5xx 保持原值（本就可重试）。
- SSE/JSON 文本：仅改写 error.message / response.error.message 字段内容，
  事件类型、结构不动。

插件参数（可选）：
- enabled_plugins 写 "overload_client_retry:<自定义文案>" 自定义改写消息，
  默认 "upstream failed. Please retry."
"""

from __future__ import annotations

import json
from typing import Any

from core.log_config import logger
from core.plugins import (
    get_current_plugin_options,
    register_response_interceptor,
    unregister_response_interceptor,
)

PLUGIN_INFO = {
    "name": "overload_client_retry",
    "version": "1.0.0",
    "description": "上游过载错误客户端可重试改写 - 把 at capacity / overloaded 类错误改写为通用 upstream failed，触发客户端自动重试，避免 Codex 任务中断",
    "author": "Zoaholic Team",
    "dependencies": [],
    "metadata": {
        "category": "interceptors",
        "tags": ["overload", "retry", "codex", "openai", "compat"],
        "params_hint": "可选：自定义改写文案（默认 'upstream failed. Please retry.'），如 'overload_client_retry:upstream failed'。",
        "provider_config": {
            "key": "overload_client_retry",
            "type": "string",
            "title": "过载错误改写为可重试",
            "description": "把 at capacity / servers overloaded 类上游错误改写为通用 upstream failed 文案，让 Codex 等客户端触发自动重试，避免任务中断。",
            "example": "",
        },
    },
}

EXTENSIONS = [
    "interceptors:overload_client_retry_response",
]

DEFAULT_MESSAGE = "upstream failed. Please retry."

# 过载特征（小写匹配）
_OVERLOAD_SIGNATURES = (
    "at capacity",
    "currently overloaded",
    "servers are overloaded",
    "server_is_overloaded",
    "slow_down",
)

# 已属可重试档的状态码保持不动
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _is_overload_text(text: str) -> bool:
    lowered = (text or "").lower()
    return any(sig in lowered for sig in _OVERLOAD_SIGNATURES)


def _rewrite_error_dict(chunk: dict, message: str):
    """改写转换路径/非流式的结构化错误 chunk。

    error/details 改写为通用文案；status_code 仅在不可重试档时映射为 502，
    使 handler 的服务端重试判断同时生效（非流式路径）。
    """
    probe = json.dumps(chunk, ensure_ascii=False, default=str)
    if not _is_overload_text(probe):
        return None
    rewritten = dict(chunk)
    rewritten["error"] = message
    rewritten["details"] = message
    status = rewritten.get("status_code")
    if isinstance(status, int) and status not in _RETRYABLE_STATUS:
        rewritten["status_code"] = 502
    return rewritten


def _rewrite_error_object(error_obj: dict, message: str) -> bool:
    """改写 JSON 对象内的 error dict（OpenAI/Gemini 通用形态），返回是否命中。"""
    probe = json.dumps(error_obj, ensure_ascii=False, default=str)
    if not _is_overload_text(probe):
        return False
    if "message" in error_obj:
        error_obj["message"] = message
    else:
        error_obj["message"] = message
    return True


def _rewrite_json_payload(data: Any, message: str):
    """在已解析的 JSON 结构中定位并改写过载错误，返回改写后的对象或 None。

    覆盖三种位置：
    - 顶层 {"error": {"message": ...}}（chat completions / Gemini）
    - {"type":"error","error":{...}}（Responses SSE error 事件）
    - {"type":"response.failed","response":{"error":{...}}}（Responses failed 事件）
    """
    changed = False
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            changed |= _rewrite_error_object(err, message)
        elif isinstance(err, str) and _is_overload_text(err):
            data["error"] = {"message": message, "type": "upstream_error"}
            changed = True
        resp = data.get("response")
        if isinstance(resp, dict) and isinstance(resp.get("error"), dict):
            changed |= _rewrite_error_object(resp["error"], message)
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                changed |= (_rewrite_json_payload(item, message) is not None)
    return data if changed else None


def _rewrite_text_chunk(text: str, message: str):
    """改写透传文本 chunk：SSE data: 行逐行处理，纯 JSON 整体处理。"""
    if '"error"' not in text and "response.failed" not in text:
        return None

    if "data: " in text:
        # 透传 chunk 可能以 event: 行开头，逐行扫描 data: 行处理
        lines = text.split("\n")
        out_lines = []
        changed = False
        for line in lines:
            if line.startswith("data: ") and ('"error"' in line or "response.failed" in line):
                json_part = line[6:]
                try:
                    data = json.loads(json_part)
                except (json.JSONDecodeError, ValueError):
                    out_lines.append(line)
                    continue
                rewritten = _rewrite_json_payload(data, message)
                if rewritten is not None:
                    out_lines.append("data: " + json.dumps(rewritten, ensure_ascii=False))
                    changed = True
                else:
                    out_lines.append(line)
            else:
                out_lines.append(line)
        return "\n".join(out_lines) if changed else None

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    rewritten = _rewrite_json_payload(data, message)
    if rewritten is None:
        return None
    return json.dumps(rewritten, ensure_ascii=False)


async def overload_client_retry_response_interceptor(
    response_chunk: Any,
    engine: str,
    model: str,
    is_stream: bool,
) -> Any:
    """响应拦截器入口：把过载类错误改写为客户端可重试形态。"""
    try:
        options = get_current_plugin_options("overload_client_retry")
        message = options.strip('"').strip("'") if options else DEFAULT_MESSAGE

        if isinstance(response_chunk, dict) and "error" in response_chunk:
            rewritten = _rewrite_error_dict(response_chunk, message)
            if rewritten is not None:
                logger.info(
                    "[overload_client_retry] engine=%s model=%s stream=%s 过载错误已改写为可重试形态",
                    engine, model, is_stream,
                )
                return rewritten
            return response_chunk

        if isinstance(response_chunk, str):
            rewritten = _rewrite_text_chunk(response_chunk, message)
            if rewritten is not None:
                logger.info(
                    "[overload_client_retry] engine=%s model=%s stream=%s 透传过载错误已改写",
                    engine, model, is_stream,
                )
                return rewritten
            return response_chunk

        return response_chunk
    except Exception as exc:  # 防御：插件异常不阻断响应
        logger.error(f"[overload_client_retry] 处理失败，原样放行: {exc}")
        return response_chunk


def setup(manager):
    register_response_interceptor(
        interceptor_id="overload_client_retry_response",
        callback=overload_client_retry_response_interceptor,
        priority=5,  # 早于 error_mask(10)，优先命中过载特征
        plugin_name=PLUGIN_INFO["name"],
        metadata={"description": "过载错误改写为客户端可重试形态"},
    )


def teardown(manager):
    unregister_response_interceptor("overload_client_retry_response")
