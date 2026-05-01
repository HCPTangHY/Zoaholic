"""
流式/非流式转换工具

核心功能：
- force_stream: 客户端请求非流 → 内部走流式打上游 → 拼装成非流式 JSON 返回
- force_non_stream: 客户端请求流式 → 内部走非流打上游 → 拆成 SSE 事件流返回
"""

import json
import random
import string
import time

from .log_config import logger
from .utils import generate_sse_response, end_of_line


async def assemble_stream_to_json(stream_generator):
    """
    收集流式 SSE 响应，拼装成标准 OAI 非流式 JSON 响应。
    
    Args:
        stream_generator: 产出 SSE 字符串或 error dict 的异步生成器
    
    Returns:
        dict: 标准 OpenAI chat.completion 格式的响应
    """
    msg_id = ""
    msg_model = ""
    created = 0
    role = "assistant"
    content = ""
    reasoning_content = ""
    tool_calls_accum = {}  # index → {id, type, function: {name, arguments}}
    finish_reason = None
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0

    try:
        async for chunk in stream_generator:
            # 错误响应直接返回
            if isinstance(chunk, dict) and "error" in chunk:
                return chunk

            if isinstance(chunk, bytes):
                chunk = chunk.decode("utf-8", errors="replace")
            elif not isinstance(chunk, str):
                continue

            for line in chunk.split("\n"):
                line = line.strip()
                if not line or line.startswith(":"):
                    continue

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

                if data.get("id") and not msg_id:
                    msg_id = data["id"]
                if data.get("model"):
                    msg_model = data["model"]
                if data.get("created"):
                    created = data["created"]

                choices = data.get("choices", [])
                if choices:
                    choice = choices[0]
                    delta = choice.get("delta", {})

                    if delta.get("role"):
                        role = delta["role"]
                    if "content" in delta and isinstance(delta["content"], str):
                        content += delta["content"]
                    if "reasoning_content" in delta and isinstance(delta["reasoning_content"], str):
                        reasoning_content += delta["reasoning_content"]

                    if delta.get("tool_calls"):
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

                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]

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
        logger.error(f"[stream_convert] Error assembling stream: {e}")
        return {
            "error": f"Stream assembly error: {e}",
            "status_code": 502,
            "details": str(e),
        }

    # 组装 message
    message = {"role": role, "refusal": None}
    if tool_calls_accum:
        message["content"] = content if content else None
        message["tool_calls"] = [tool_calls_accum[i] for i in sorted(tool_calls_accum.keys())]
    else:
        message["content"] = content
    if reasoning_content:
        message["reasoning_content"] = reasoning_content

    if not total_tokens and (prompt_tokens or completion_tokens):
        total_tokens = prompt_tokens + completion_tokens
    if not msg_id:
        random_str = "".join(random.choices(string.ascii_letters + string.digits, k=29))
        msg_id = f"chatcmpl-{random_str}"

    assembled = {
        "id": msg_id,
        "object": "chat.completion",
        "created": created or int(time.time()),
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
        f"[stream_convert] Assembled: model={msg_model}, "
        f"content_len={len(content)}, reasoning_len={len(reasoning_content)}, "
        f"tool_calls={len(tool_calls_accum)}, "
        f"tokens={prompt_tokens}+{completion_tokens}={total_tokens}"
    )

    return assembled


async def convert_json_to_sse(json_response, model=""):
    """
    将非流式 JSON 响应拆成 SSE 事件流。
    
    Args:
        json_response: dict, 标准 OAI chat.completion 格式
        model: 模型名
    
    Yields:
        str: SSE 格式的字符串
    """
    if isinstance(json_response, dict) and "error" in json_response:
        yield json_response
        return

    msg_id = json_response.get("id", "")
    msg_model = json_response.get("model", model)
    created = json_response.get("created", int(time.time()))

    choices = json_response.get("choices", [])
    if not choices:
        yield json_response
        return

    choice = choices[0]
    message = choice.get("message", {})
    role = message.get("role", "assistant")
    content = message.get("content", "")
    reasoning_content = message.get("reasoning_content", "")
    tool_calls = message.get("tool_calls", [])
    finish_reason = choice.get("finish_reason", "stop")
    usage = json_response.get("usage")

    # 发送 role delta
    yield generate_sse_response(msg_id, msg_model, content=None, role=role, created=created)
    yield end_of_line

    # 发送 reasoning_content（如果有）
    if reasoning_content:
        # 分块发送避免单条 SSE 过大
        chunk_size = 200
        for i in range(0, len(reasoning_content), chunk_size):
            chunk = reasoning_content[i:i + chunk_size]
            delta = {"reasoning_content": chunk}
            sse_data = {
                "id": msg_id, "object": "chat.completion.chunk",
                "created": created, "model": msg_model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            }
            yield f"data: {json.dumps(sse_data, ensure_ascii=False)}\n\n"

    # 发送 content
    if content:
        chunk_size = 200
        for i in range(0, len(content), chunk_size):
            chunk = content[i:i + chunk_size]
            yield generate_sse_response(msg_id, msg_model, content=chunk, created=created)
            yield end_of_line

    # 发送 tool_calls（如果有）
    if tool_calls:
        for idx, tc in enumerate(tool_calls):
            # 第一个 chunk: id + type + function.name
            delta = {
                "tool_calls": [{
                    "index": idx,
                    "id": tc.get("id", ""),
                    "type": tc.get("type", "function"),
                    "function": {"name": tc.get("function", {}).get("name", ""), "arguments": ""},
                }]
            }
            sse_data = {
                "id": msg_id, "object": "chat.completion.chunk",
                "created": created, "model": msg_model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            }
            yield f"data: {json.dumps(sse_data, ensure_ascii=False)}\n\n"

            # arguments 分块
            args = tc.get("function", {}).get("arguments", "")
            if args:
                for i in range(0, len(args), 200):
                    delta = {"tool_calls": [{"index": idx, "function": {"arguments": args[i:i + 200]}}]}
                    sse_data = {
                        "id": msg_id, "object": "chat.completion.chunk",
                        "created": created, "model": msg_model,
                        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(sse_data, ensure_ascii=False)}\n\n"

    # 发送 finish + usage
    finish_data = {
        "id": msg_id, "object": "chat.completion.chunk",
        "created": created, "model": msg_model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
    }
    if usage:
        finish_data["usage"] = usage
    yield f"data: {json.dumps(finish_data, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"
