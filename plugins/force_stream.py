"""
强制流式请求插件

将非流式请求自动转为流式请求发送到上游，然后将流式响应拼接成非流式响应返回给客户端。

工作原理：
1. 请求拦截器检测非流式请求，在 request_info 中设置标记
2. 包装 fetch_response 函数，当检测到标记时：
   - 内部将 payload 的 stream 设为 True
   - 调用 fetch_response_stream 获取流式响应
   - 收集所有 SSE 事件，解析并拼接成完整的非流式响应
   - 以标准 OpenAI Chat Completions 非流式格式返回
3. 对客户端完全透明：Content-Type 为 application/json，响应格式与正常非流式一致

使用场景：
- 某些上游 API 的非流式接口不稳定或超时，但流式接口正常
- 希望统一走流式通道以利用流式相关插件的能力（如 claude_thinking）
- 上游仅支持流式响应

使用方式：
在渠道配置的 enabled_plugins 中添加：
- "force_stream" - 启用强制流式

配置示例：
```yaml
providers:
  - provider: my-provider
    base_url: https://api.example.com/v1
    api: YOUR_API_KEY
    model:
      - my-model
    preferences:
      enabled_plugins:
        - "force_stream"
```

限制：
- 仅对非透传(non-passthrough)路径生效，透传请求不受影响
- 仅在 request.stream 为 False/None 时激活
- 会增加非流式请求的延迟（需等待完整流式响应后才能返回）
"""

import json
import random
import string
import asyncio
from typing import Any, Dict, List, Optional, Tuple

from core.log_config import logger
from core.middleware import request_info
from core.plugins import (
    register_request_interceptor,
    unregister_request_interceptor,
    is_plugin_enabled,
)


# 插件元信息
PLUGIN_INFO = {
    "name": "force_stream",
    "version": "1.0.0",
    "description": "强制流式请求插件 - 将非流式请求转为流式并拼接返回",
    "author": "Zoaholic",
    "dependencies": [],
    "metadata": {
        "category": "interceptors",
        "tags": ["stream", "force", "non-stream"],
    },
}

# 声明提供的扩展
EXTENSIONS = [
    "interceptors:force_stream_request",
]

# 保存原始 fetch_response 引用
_original_fetch_response = None


# ==================== 核心：流式响应拼接 ====================

async def _assemble_stream_to_response(client, url, headers, payload, engine, model, timeout):
    """
    将流式响应拼接成完整的非流式响应。

    内部调用 fetch_response_stream 获取 SSE 流，解析每个事件，
    累积 content/reasoning_content/tool_calls/usage 等字段，
    最终组装成标准 OpenAI Chat Completions 非流式格式的 dict 并 yield。
    """
    from core.response import fetch_response_stream

    # 获取启用的插件列表（用于响应拦截器）
    try:
        current_info = request_info.get()
        enabled_plugins = current_info.get("_force_stream_enabled_plugins")
    except Exception:
        enabled_plugins = None

    # 在 payload 副本中强制开启 stream
    payload = dict(payload)
    payload["stream"] = True

    # 累积状态
    msg_id = ""
    msg_model = model
    created = 0
    role = "assistant"
    content = ""
    reasoning_content = ""
    tool_calls_accum: Dict[int, dict] = {}  # index → {id, type, function: {name, arguments}}
    finish_reason = None
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0

    try:
        async for chunk in fetch_response_stream(
            client, url, headers, payload, engine, model, timeout,
            enabled_plugins=enabled_plugins,
        ):
            # 错误响应直接透传
            if isinstance(chunk, dict) and "error" in chunk:
                yield chunk
                return

            if not isinstance(chunk, str):
                continue

            # 解析 SSE 事件
            for line in chunk.split("\n"):
                line = line.strip()
                if not line or line.startswith(":"):
                    continue

                # 提取 data: 后的 JSON
                if line.startswith("data: "):
                    data_str = line[6:].strip()
                elif line.startswith("data:"):
                    data_str = line[5:].strip()
                else:
                    continue

                if data_str == "[DONE]":
                    continue

                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                # 提取元信息
                if data.get("id") and not msg_id:
                    msg_id = data["id"]
                if data.get("model"):
                    msg_model = data["model"]
                if data.get("created"):
                    created = data["created"]

                # 解析 choices
                choices = data.get("choices", [])
                if choices:
                    choice = choices[0]
                    delta = choice.get("delta", {})

                    if delta.get("role"):
                        role = delta["role"]

                    # 文本内容
                    if "content" in delta and isinstance(delta["content"], str):
                        content += delta["content"]

                    # 推理内容
                    if "reasoning_content" in delta and isinstance(delta["reasoning_content"], str):
                        reasoning_content += delta["reasoning_content"]

                    # 工具调用（增量累积）
                    if "tool_calls" in delta:
                        for tc in delta["tool_calls"]:
                            idx = tc.get("index", 0)
                            if idx not in tool_calls_accum:
                                tool_calls_accum[idx] = {
                                    "id": tc.get("id", ""),
                                    "type": tc.get("type", "function"),
                                    "function": {
                                        "name": tc.get("function", {}).get("name", ""),
                                        "arguments": "",
                                    },
                                }
                            else:
                                if tc.get("id"):
                                    tool_calls_accum[idx]["id"] = tc["id"]
                                if tc.get("type"):
                                    tool_calls_accum[idx]["type"] = tc["type"]

                            fn = tc.get("function", {})
                            if fn.get("name"):
                                tool_calls_accum[idx]["function"]["name"] = fn["name"]
                            if "arguments" in fn:
                                tool_calls_accum[idx]["function"]["arguments"] += fn["arguments"]

                    # 结束原因
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]

                # 解析 usage
                usage = data.get("usage")
                if usage and isinstance(usage, dict):
                    pt = usage.get("prompt_tokens")
                    ct = usage.get("completion_tokens")
                    tt = usage.get("total_tokens")
                    if pt and pt > 0:
                        prompt_tokens = pt
                    if ct and ct > 0:
                        completion_tokens = ct
                    if tt and tt > 0:
                        total_tokens = tt

    except Exception as e:
        logger.error(f"[force_stream] Error collecting stream: {e}")
        yield {
            "error": f"Force stream collection error: {e}",
            "status_code": 502,
            "details": str(e),
        }
        return

    # 组装 message
    message: Dict[str, Any] = {"role": role, "refusal": None}

    if tool_calls_accum:
        message["content"] = content if content else None
        message["tool_calls"] = [
            tool_calls_accum[i] for i in sorted(tool_calls_accum.keys())
        ]
    else:
        message["content"] = content

    if reasoning_content:
        message["reasoning_content"] = reasoning_content

    # 补全 total_tokens
    if not total_tokens and (prompt_tokens or completion_tokens):
        total_tokens = prompt_tokens + completion_tokens

    # 生成 msg_id（如果流中没有提供）
    if not msg_id:
        random_str = "".join(random.choices(string.ascii_letters + string.digits, k=29))
        msg_id = f"chatcmpl-{random_str}"

    # 组装完整的非流式响应
    assembled = {
        "id": msg_id,
        "object": "chat.completion",
        "created": created,
        "model": msg_model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "logprobs": None,
                "finish_reason": finish_reason or "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
        "system_fingerprint": None,
    }

    logger.info(
        f"[force_stream] Assembled response: model={msg_model}, "
        f"content_len={len(content)}, reasoning_len={len(reasoning_content)}, "
        f"tool_calls={len(tool_calls_accum)}, "
        f"tokens={prompt_tokens}+{completion_tokens}={total_tokens}"
    )

    yield assembled


# ==================== 包装 fetch_response ====================

async def _wrapped_fetch_response(client, url, headers, payload, engine, model, timeout=200, enabled_plugins=None):
    """
    包装后的 fetch_response。

    当检测到 request_info 中的 _force_stream_enabled 标记时，
    内部走流式通道并拼接成非流式响应。
    否则走原始的非流式逻辑。
    """
    global _original_fetch_response

    # 检查是否需要强制流式
    force_stream = False
    try:
        current_info = request_info.get()
        force_stream = current_info.get("_force_stream_enabled", False)
    except Exception:
        pass

    if force_stream:
        logger.debug(f"[force_stream] Intercepting non-stream fetch for model={model}")
        async for chunk in _assemble_stream_to_response(
            client, url, headers, payload, engine, model, timeout
        ):
            yield chunk
        return

    # 原始行为
    async for chunk in _original_fetch_response(
        client, url, headers, payload, engine, model, timeout,
        enabled_plugins=enabled_plugins,
    ):
        yield chunk


# ==================== 请求拦截器 ====================

async def _force_stream_cleanup_interceptor(
    request: Any,
    engine: str,
    provider: Dict[str, Any],
    api_key: Optional[str],
    url: str,
    headers: Dict[str, Any],
    payload: Dict[str, Any],
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """
    清理拦截器（plugin_name=None，对所有 provider 运行）。

    在每次请求/重试时无条件清除 force_stream 标记。
    随后，主拦截器（仅对启用了插件的 provider 运行）会重新设置标记。
    这确保了重试到未启用插件的 provider 时标记不会残留。
    """
    try:
        current_info = request_info.get()
        current_info.pop("_force_stream_enabled", None)
        current_info.pop("_force_stream_enabled_plugins", None)
    except Exception:
        pass
    return url, headers, payload


async def force_stream_request_interceptor(
    request: Any,
    engine: str,
    provider: Dict[str, Any],
    api_key: Optional[str],
    url: str,
    headers: Dict[str, Any],
    payload: Dict[str, Any],
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """
    主请求拦截器（plugin_name=PLUGIN_INFO["name"]，仅对启用了插件的 provider 运行）。

    标记非流式请求，使其在 fetch_response 层走强制流式逻辑。
    不修改 payload 或 request.stream。
    """
    # 仅对非流式请求生效
    if getattr(request, "stream", None):
        return url, headers, payload

    try:
        current_info = request_info.get()

        # 设置标记
        current_info["_force_stream_enabled"] = True
        enabled_plugins = provider.get("preferences", {}).get("enabled_plugins")
        current_info["_force_stream_enabled_plugins"] = enabled_plugins

        logger.info(
            f"[force_stream] Flagged non-stream request: "
            f"engine={engine}, model={payload.get('model', 'unknown')}"
        )
    except Exception as e:
        logger.error(f"[force_stream] Failed to flag request: {e}")

    return url, headers, payload


# ==================== 插件生命周期 ====================

def setup(manager):
    """插件初始化"""
    global _original_fetch_response

    logger.info(f"[{PLUGIN_INFO['name']}] 正在初始化...")

    # 注册请求拦截器
    # 使用 plugin_name 以便前端能识别并列出此插件
    register_request_interceptor(
        interceptor_id="force_stream_request",
        callback=force_stream_request_interceptor,
        priority=10,  # 高优先级，尽早标记
        plugin_name=PLUGIN_INFO["name"],
        metadata={"description": "强制流式请求标记"},
    )

    # 注册一个无 plugin_name 的清理拦截器，对所有 provider 运行
    # 确保重试到未启用插件的 provider 时能正确清除标记
    register_request_interceptor(
        interceptor_id="force_stream_cleanup",
        callback=_force_stream_cleanup_interceptor,
        priority=9,  # 比主拦截器更高优先级（更早执行），先清理再由主拦截器设置
        plugin_name=None,
        metadata={"description": "强制流式标记清理（重试安全）"},
    )

    # 包装 fetch_response
    import core.response
    import core.handler

    _original_fetch_response = core.response.fetch_response

    core.response.fetch_response = _wrapped_fetch_response
    core.handler.fetch_response = _wrapped_fetch_response

    logger.info(f"[{PLUGIN_INFO['name']}] 已初始化，fetch_response 已包装")


def teardown(manager):
    """插件清理"""
    global _original_fetch_response

    logger.info(f"[{PLUGIN_INFO['name']}] 正在清理...")

    # 注销拦截器
    unregister_request_interceptor("force_stream_cleanup")
    unregister_request_interceptor("force_stream_request")

    # 恢复原始 fetch_response
    if _original_fetch_response:
        import core.response
        import core.handler

        core.response.fetch_response = _original_fetch_response
        core.handler.fetch_response = _original_fetch_response
        _original_fetch_response = None

    logger.info(f"[{PLUGIN_INFO['name']}] 已清理完成")


def unload():
    """插件卸载回调"""
    logger.debug(f"[{PLUGIN_INFO['name']}] 模块即将卸载")
