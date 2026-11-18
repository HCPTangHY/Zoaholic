"""
OpenAI Responses API 方言

实现 OpenAI Responses API 格式的输入输出转换：
- input (Responses API) -> messages (Chat Completions)
- 支持 /v1/responses 端点
- 支持流式和非流式响应
"""

from typing import Any, Dict, List, Optional, Union, TYPE_CHECKING

from core.json_utils import json_loads, json_dumps_text
from core.models import RequestModel, Message, ContentItem
from core.usage import extract_cache_usage

from .registry import DialectDefinition, EndpointDefinition, register_dialect

if TYPE_CHECKING:
    from fastapi import Request, BackgroundTasks


# ============================================================
# Responses API -> Chat Completions 转换
# ============================================================


def _normalize_tool_arguments(raw_args: Any) -> str:
    """将工具调用参数归一化为字符串。"""
    if raw_args is None:
        return "{}"
    if isinstance(raw_args, str):
        return raw_args
    return json_dumps_text(raw_args, ensure_ascii=False)


def _normalize_tool_output(output_val: Any) -> str:
    """将工具调用输出归一化为字符串（兼容空字符串、None、对象、数组和长文本）。"""
    if output_val is None:
        return ""
    if isinstance(output_val, str):
        return output_val
    return json_dumps_text(output_val, ensure_ascii=False)


def _normalize_tool_call_entry(raw_tc: Dict[str, Any], fallback_id: str) -> Dict[str, Any]:
    """将 Responses / Chat 风格的工具调用项归一化为标准 OpenAI tool_call 结构。"""
    fn_obj = raw_tc.get("function")
    if not isinstance(fn_obj, dict):
        fn_obj = {}
    call_id = raw_tc.get("call_id") or raw_tc.get("id") or fallback_id
    raw_name = raw_tc.get("name") or fn_obj.get("name")
    fn_name = str(raw_name) if raw_name is not None else ""
    if raw_tc.get("arguments") is not None:
        raw_args = raw_tc["arguments"]
    elif fn_obj.get("arguments") is not None:
        raw_args = fn_obj["arguments"]
    else:
        raw_args = raw_tc.get("input")
    tc_dict: Dict[str, Any] = {
        "id": str(call_id),
        "type": "function",
        "function": {
            "name": fn_name,
            "arguments": _normalize_tool_arguments(raw_args),
        },
    }
    if raw_tc.get("extra_content"):
        tc_dict["extra_content"] = raw_tc["extra_content"]
    return tc_dict


def _append_or_merge_assistant_tool_call(
    messages: List[Dict[str, Any]],
    tc_dict: Dict[str, Any],
    pending_reasoning: str = "",
) -> None:
    """将工具调用合并到上一条 assistant 消息，若不存在则新建一条 assistant 消息。"""
    if (
        messages
        and messages[-1].get("role") == "assistant"
        and "tool_call_id" not in messages[-1]
    ):
        prev = messages[-1]
        if prev.get("content") is None:
            prev["content"] = ""
        prev.setdefault("tool_calls", []).append(tc_dict)
        if pending_reasoning and not prev.get("reasoning_content"):
            prev["reasoning_content"] = pending_reasoning
    else:
        msg: Dict[str, Any] = {
            "role": "assistant",
            "content": "",
            "tool_calls": [tc_dict],
        }
        if pending_reasoning:
            msg["reasoning_content"] = pending_reasoning
        messages.append(msg)


def convert_responses_input_to_messages(input_data: Union[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """
    将 Responses API 的 input 转换为 Chat Completions 的 messages 格式

    Responses API input 格式：
    - 字符串: 直接作为 user 消息
    - 数组: 消息或事件项列表：
      - 普通消息: 包含 role 和 content（字符串或数组，含 input_text / output_text / summary_text / input_image 等）
      - 顶层工具调用: function_call / custom_tool_call -> assistant.tool_calls
      - 顶层工具输出: function_call_output / custom_tool_call_output -> role="tool"
      - 混排的 Chat 格式消息: assistant(tool_calls) / role="tool"
    """
    messages: List[Dict[str, Any]] = []

    if isinstance(input_data, str):
        return [{"role": "user", "content": input_data}]

    if isinstance(input_data, list):
        call_seq = 0
        pending_reasoning = ""

        for item in input_data:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
                continue

            if not isinstance(item, dict):
                continue

            top_type = item.get("type", "")

            if top_type == "reasoning":
                summary_parts = []
                for key in ("summary", "content"):
                    blocks = item.get(key)
                    if isinstance(blocks, list):
                        for block in blocks:
                            if isinstance(block, dict) and isinstance(block.get("text"), str) and block["text"]:
                                summary_parts.append(block["text"])
                if summary_parts:
                    reasoning_text = "".join(summary_parts)
                    if messages and messages[-1].get("role") == "assistant":
                        prev_rc = messages[-1].get("reasoning_content")
                        messages[-1]["reasoning_content"] = (
                            f"{prev_rc}{reasoning_text}" if isinstance(prev_rc, str) and prev_rc else reasoning_text
                        )
                    else:
                        pending_reasoning = f"{pending_reasoning}{reasoning_text}"
                continue

            if top_type in ("function_call", "custom_tool_call"):
                fallback_id = f"call_{call_seq}"
                call_seq += 1
                tc_dict = _normalize_tool_call_entry(item, fallback_id)
                _append_or_merge_assistant_tool_call(messages, tc_dict, pending_reasoning)
                pending_reasoning = ""
                continue

            role = item.get("role", "user")
            content = item.get("content")

            if (
                top_type in ("function_call_output", "custom_tool_call_output")
                or role == "tool"
                or (item.get("tool_call_id") and top_type != "message")
            ):
                call_id = item.get("call_id") or item.get("tool_call_id") or item.get("id") or f"call_{call_seq}"
                call_seq += 1
                raw_output = item.get("output") if "output" in item else content
                tool_msg: Dict[str, Any] = {
                    "role": "tool",
                    "tool_call_id": str(call_id),
                    "content": _normalize_tool_output(raw_output),
                }
                if item.get("name"):
                    tool_msg["name"] = str(item["name"])
                messages.append(tool_msg)
                continue

            inline_tool_calls: List[Dict[str, Any]] = []
            if isinstance(item.get("tool_calls"), list):
                for raw_tc in item["tool_calls"]:
                    if isinstance(raw_tc, dict):
                        fallback_id = f"call_{call_seq}"
                        call_seq += 1
                        inline_tool_calls.append(_normalize_tool_call_entry(raw_tc, fallback_id))

            converted_msg: Optional[Dict[str, Any]] = None
            if content is None:
                if inline_tool_calls:
                    converted_msg = {
                        "role": role,
                        "content": "",
                        "tool_calls": inline_tool_calls,
                    }
            elif isinstance(content, str):
                converted_msg = {"role": role, "content": content}
                if inline_tool_calls:
                    converted_msg["tool_calls"] = inline_tool_calls
            elif isinstance(content, list):
                converted_content = []
                for content_item in content:
                    if isinstance(content_item, str):
                        converted_content.append({"type": "text", "text": content_item})
                    elif isinstance(content_item, dict):
                        item_type = content_item.get("type", "")

                        if item_type in ("input_text", "output_text", "text", "summary_text"):
                            text_val = content_item.get("text", "")
                            if text_val is not None and text_val != "":
                                converted_content.append({
                                    "type": "text",
                                    "text": text_val
                                })
                        elif item_type in ("tool_use", "function_call", "custom_tool_call"):
                            fallback_id = f"call_{call_seq}"
                            call_seq += 1
                            inline_tool_calls.append(_normalize_tool_call_entry(content_item, fallback_id))
                        elif item_type == "input_image":
                            image_url = content_item.get("image_url") or content_item.get("url")
                            if isinstance(image_url, dict):
                                image_url = image_url.get("url")
                            if image_url:
                                converted_content.append({
                                    "type": "image_url",
                                    "image_url": {"url": image_url}
                                })
                        elif item_type == "input_audio":
                            data = content_item.get("input_audio", {}).get("data", "")
                            if data:
                                converted_content.append({
                                    "type": "file",
                                    "file": {"mime_type": "audio/wav", "data": data}
                                })
                        elif item_type == "input_file":
                            file_obj = {}
                            for field in ["file_id", "filename", "file_url", "file_data"]:
                                val = content_item.get(field)
                                if val is not None:
                                    if field == "file_url" or (field == "file_data" and str(val).startswith("data:")):
                                        file_obj["url"] = val
                                    elif field == "file_data":
                                        file_obj["data"] = val
                                    else:
                                        file_obj[field] = val
                            converted_content.append({
                                "type": "file",
                                "file": file_obj
                            })
                        else:
                            converted_content.append(content_item)

                if converted_content or inline_tool_calls:
                    converted_msg = {
                        "role": role,
                        "content": converted_content if converted_content else "",
                    }
                    if inline_tool_calls:
                        converted_msg["tool_calls"] = inline_tool_calls
            else:
                converted_msg = {"role": role, "content": str(content)}
                if inline_tool_calls:
                    converted_msg["tool_calls"] = inline_tool_calls

            if converted_msg is not None:
                if role == "assistant" and pending_reasoning:
                    converted_msg["reasoning_content"] = pending_reasoning
                    pending_reasoning = ""
                if item.get("reasoning_content") and not converted_msg.get("reasoning_content"):
                    converted_msg["reasoning_content"] = item["reasoning_content"]
                messages.append(converted_msg)

    return messages


def convert_responses_tools(tools: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
    """
    将 Responses API 的 tools 转换为 Chat Completions 格式

    Responses API tools 格式更扁平化：
    - type: "custom" / "function" / "web_search" / "file_search" 等
    - name: 工具名称
    - description: 工具描述

    Chat Completions tools 格式：
    - type: "function"
    - function: { name, description, parameters }
    """
    if not tools:
        return None

    converted_tools = []
    for tool in tools:
        tool_type = tool.get("type", "")

        # 内置工具类型，不需要转换
        if tool_type in ("web_search", "file_search", "code_interpreter", "image_generation"):
            converted_tools.append(tool)
            continue

        # custom 类型转换为 function
        if tool_type == "custom":
            converted_tools.append({
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters", {"type": "object", "properties": {}})
                }
            })
        # function 类型
        elif tool_type == "function":
            # 可能已经是 Chat Completions 格式
            if "function" in tool:
                converted_tools.append(tool)
            else:
                converted_tools.append({
                    "type": "function",
                    "function": {
                        "name": tool.get("name", ""),
                        "description": tool.get("description", ""),
                        "parameters": tool.get("parameters", {"type": "object", "properties": {}})
                    }
                })
        else:
            # 其他类型原样传递
            converted_tools.append(tool)

    return converted_tools if converted_tools else None


async def parse_responses_request(
    native_body: Dict[str, Any],
    path_params: Dict[str, str],
    headers: Dict[str, str],
) -> RequestModel:
    """
    将 Responses API 请求转换为 Canonical (RequestModel) 格式

    主要转换：
    - input -> messages
    - reasoning.effort -> (通过模型后缀处理)
    - tools 格式转换
    """
    # 获取基本字段
    model = native_body.get("model", "")
    input_data = native_body.get("input", [])
    stream = native_body.get("stream", False)

    # 转换 input -> messages
    messages = convert_responses_input_to_messages(input_data)

    # 处理顶层 instructions -> system message（Responses API 公开语义）
    instructions = native_body.get("instructions")
    if instructions and isinstance(instructions, str) and instructions.strip():
        # 插入到 messages 最前面作为 system 消息
        messages.insert(0, {"role": "system", "content": instructions.strip()})

    # 构建基本请求
    request_data = {
        "model": model,
        "messages": messages,
        "stream": stream,
    }

    # 转换 tools
    tools = convert_responses_tools(native_body.get("tools"))
    if tools:
        request_data["tools"] = tools

    # 透传 reasoning 参数（如 {"effort": "high"}），由 channel 层决定如何处理
    reasoning = native_body.get("reasoning")
    if reasoning is not None:
        request_data["reasoning"] = reasoning

    # 可选参数映射
    optional_fields = [
        "temperature", "top_p", "max_tokens", "max_completion_tokens", "max_output_tokens",
        "presence_penalty", "frequency_penalty", "n", "user",
        "tool_choice", "response_format", "stream_options"
    ]

    for field in optional_fields:
        if field in native_body and native_body[field] is not None:
            # max_output_tokens -> max_tokens
            if field == "max_output_tokens":
                request_data["max_tokens"] = native_body[field]
            else:
                request_data[field] = native_body[field]

    # text.format -> response_format
    text_format = native_body.get("text", {}).get("format")
    if text_format:
        format_type = text_format.get("type")
        if format_type == "json_object":
            request_data["response_format"] = {"type": "json_object"}
        elif format_type == "json_schema":
            request_data["response_format"] = text_format

    return RequestModel(**request_data)


def _responses_usage_from_canonical(usage: Any) -> Dict[str, Any]:
    """将内核 OpenAI usage 转为 Responses API usage。

    Responses API 的缓存命中字段位于 input_tokens_details.cached_tokens；集中转换可以同时覆盖
    非流式响应和 usage-only 流式事件。
    """
    usage = usage if isinstance(usage, dict) else {}
    response_usage: Dict[str, Any] = {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
    }
    cache_usage = extract_cache_usage(usage)
    if cache_usage["cached_tokens"] > 0:
        response_usage["input_tokens_details"] = {"cached_tokens": cache_usage["cached_tokens"]}
    return response_usage


async def render_responses_response(
    canonical_response: Dict[str, Any],
    model: str,
) -> Dict[str, Any]:
    """
    将 Canonical (OpenAI Chat Completions) 响应转换为 Responses API 格式

    Chat Completions 响应:
    {
        "id": "chatcmpl-xxx",
        "choices": [{"message": {"role": "assistant", "content": "..."}}],
        "usage": {...}
    }

    Responses API 响应:
    {
        "id": "resp_xxx",
        "object": "response",
        "output": [
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "..."}]}
        ]
    }
    """
    import random
    import string
    from datetime import datetime

    timestamp = int(datetime.timestamp(datetime.now()))
    random.seed(timestamp)

    # 生成响应 ID
    resp_id = "resp_" + ''.join(random.choices(string.ascii_lowercase + string.digits, k=32))

    output = []

    # 检查是否有 reasoning 内容
    choices = canonical_response.get("choices", [])
    if choices:
        message = choices[0].get("message", {})
        content = message.get("content", "")
        reasoning_content = message.get("reasoning_content", "")
        tool_calls = message.get("tool_calls", [])

        # 如果有 reasoning_content，添加 reasoning item
        if reasoning_content:
            reasoning_id = "rs_" + ''.join(random.choices(string.ascii_lowercase + string.digits, k=32))
            output.append({
                "id": reasoning_id,
                "type": "reasoning",
                "content": [],
                "summary": [{"type": "summary_text", "text": reasoning_content}]
            })

        # 添加 message item
        msg_id = "msg_" + ''.join(random.choices(string.ascii_lowercase + string.digits, k=32))
        message_item = {
            "id": msg_id,
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": []
        }

        if content:
            if isinstance(content, list):
                # 结构化 content list → Responses output items
                for ci in content:
                    if not isinstance(ci, dict):
                        continue
                    ci_type = ci.get("type", "")
                    if ci_type == "text":
                        text_val = ci.get("text", "")
                        if text_val:
                            message_item["content"].append({
                                "type": "output_text",
                                "text": text_val,
                                "annotations": []
                            })
                    elif ci_type == "image_url":
                        image_url = ci.get("image_url")
                        url = ""
                        if isinstance(image_url, dict):
                            url = image_url.get("url", "")
                        elif isinstance(image_url, str):
                            url = image_url
                        if url:
                            # Responses API 没有标准的 image output，
                            # 降级为 markdown 文本
                            message_item["content"].append({
                                "type": "output_text",
                                "text": f"![image]({url})",
                                "annotations": []
                            })
            else:
                message_item["content"].append({
                    "type": "output_text",
                    "text": content,
                    "annotations": []
                })

        # 处理 tool_calls
        if tool_calls:
            for tc in tool_calls:
                tc_id = "call_" + ''.join(random.choices(string.ascii_lowercase + string.digits, k=24))
                message_item["content"].append({
                    "type": "tool_use",
                    "id": tc.get("id", tc_id),
                    "name": tc.get("function", {}).get("name", ""),
                    "arguments": tc.get("function", {}).get("arguments", "{}")
                })

        if message_item["content"]:
            output.append(message_item)

    # 构建响应
    response = {
        "id": resp_id,
        "object": "response",
        "created_at": timestamp,
        "model": model,
        "output": output,
        "status": "completed"
    }

    # 添加 usage
    usage = canonical_response.get("usage")
    if usage:
        # Responses 方言出口需要把 OAI 缓存命中字段转为 input_tokens_details.cached_tokens。
        response["usage"] = _responses_usage_from_canonical(usage)

    return response


async def render_responses_stream(canonical_sse_chunk: str) -> str:
    """
    将 Canonical SSE 流转换为 Responses API 流格式

    Chat Completions 流事件:
    data: {"choices": [{"delta": {"content": "..."}}]}

    Responses API 流事件:
    event: response.output_text.delta
    data: {"type": "response.output_text.delta", "delta": "..."}
    """
    if not isinstance(canonical_sse_chunk, str) or not canonical_sse_chunk.startswith("data: "):
        return canonical_sse_chunk

    data_str = canonical_sse_chunk[6:].strip()
    if data_str == "[DONE]":
        return canonical_sse_chunk

    try:
        canonical = json_loads(data_str)
    except Exception:
        return canonical_sse_chunk

    error = canonical.get("error")
    if error:
        if not isinstance(error, dict):
            error = {"message": str(error)}
        event = {"type": "error", "message": error.get("message", "Upstream stream failed"),
                 "code": error.get("code") or error.get("type") or "server_error",
                 "param": error.get("param")}
        return f"event: error\ndata: {json_dumps_text(event, ensure_ascii=False)}\n\n"

    usage = canonical.get("usage")
    if isinstance(usage, dict):
        # 经过内核重组的 usage-only chunk 在 Responses API 中用 response.completed 表达。
        event = {
            "type": "response.completed",
            "response": {
                "id": canonical.get("id", ""),
                "object": "response",
                "status": "completed",
                "model": canonical.get("model", ""),
                "usage": _responses_usage_from_canonical(usage),
            },
        }
        return f"event: response.completed\ndata: {json_dumps_text(event, ensure_ascii=False)}\n\n"

    # 其它流式内容保持原有行为，避免在本次缓存字段修复中改变文本与工具调用事件结构。
    return canonical_sse_chunk


def parse_responses_usage(data: Any) -> Optional[Dict[str, int]]:
    """从 Responses API 格式中提取 usage"""
    if not isinstance(data, dict):
        return None

    # Responses API 流式完成事件
    if data.get("type") == "response.completed":
        usage = data.get("response", {}).get("usage", {})
        if usage:
            prompt = usage.get("input_tokens", 0)
            completion = usage.get("output_tokens", 0)
            total = usage.get("total_tokens", 0) or (prompt + completion)
            # Responses API 的缓存命中字段位于 input_tokens_details，需要和普通 token 同步回传。
            cache_usage = extract_cache_usage(usage)
            return {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": total,
                **cache_usage,
            }

    # 非流式响应
    usage = data.get("usage")
    if usage:
        prompt = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
        completion = usage.get("output_tokens") or usage.get("completion_tokens") or 0
        total = usage.get("total_tokens") or (prompt + completion)
        # 非流式 Responses API 同样携带 input_tokens_details.cached_tokens，这里避免只解析 token 总数。
        cache_usage = extract_cache_usage(usage)
        if prompt or completion or cache_usage["cached_tokens"] or cache_usage["cache_creation_tokens"]:
            return {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": total,
                **cache_usage,
            }

    return None


# ============================================================
# 端点处理函数
# ============================================================


async def responses_handler(
    request: "Request",
    background_tasks: "BackgroundTasks",
    api_index: int,
    **kwargs,
):
    """
    OpenAI Responses API 端点 - POST /v1/responses

    将 Responses API 请求转换为 Chat Completions 格式，调用内部处理器
    """
    from routes.deps import get_model_handler
    from .passthrough import build_lightweight_dialect_request, estimate_native_payload_tokens
    from .router import clear_request_body_cache, consume_request_json

    native_body: Dict[str, Any] = await consume_request_json(request)
    headers = dict(request.headers)

    lightweight_request = build_lightweight_dialect_request("openai-responses", native_body, {})
    if lightweight_request is not None:
        request_model = lightweight_request
        estimated_tokens = estimate_native_payload_tokens(native_body)

        async def _parse_canonical():
            return await parse_responses_request(native_body, {}, headers)

        canonical_factory = _parse_canonical
    else:
        request_model = await parse_responses_request(native_body, {}, headers)
        estimated_tokens = None
        canonical_factory = None

    model_handler = get_model_handler()
    try:
        extra_kwargs: Dict[str, Any] = {}
        if canonical_factory is not None:
            extra_kwargs["canonical_request_factory"] = canonical_factory
            extra_kwargs["estimated_prompt_tokens"] = estimated_tokens
        return await model_handler.request_model(
            request_model,
            api_index,
            background_tasks,
            dialect_id="openai-responses",
            original_payload=native_body,
            original_headers=headers,
            raw_request=request,
            **extra_kwargs,
        )
    finally:
        clear_request_body_cache(request)
        native_body = None
        canonical_factory = None


async def responses_subpath_handler(
    request: "Request",
    background_tasks: "BackgroundTasks",
    api_index: int,
    **kwargs,
):
    """
    OpenAI Responses compact endpoint - POST /v1/responses/compact

    Compact is a native Responses auxiliary endpoint.  Keep the original payload
    for upstream passthrough and only build a minimal RequestModel for auth,
    rate limit, model routing, and provider selection.
    """
    from core.error_response import openai_error_response
    from routes.deps import get_model_handler
    from .router import clear_request_body_cache, consume_request_json

    try:
        native_body: Dict[str, Any] = await consume_request_json(request)
    except Exception:
        native_body = {}

    model = native_body.get("model")
    if not isinstance(model, str) or not model.strip():
        clear_request_body_cache(request)
        return openai_error_response("Missing required parameter: model", 400)

    request_model = RequestModel(
        model=model.strip(),
        messages=[Message(role="user", content="")],
        stream=False,
    )

    model_handler = get_model_handler()
    try:
        return await model_handler.request_model(
            request_model,
            api_index,
            background_tasks,
            endpoint=request.url.path,
            dialect_id="openai-responses",
            original_payload=native_body,
            original_headers=dict(request.headers),
            raw_request=request,
            passthrough_only=True,
        )
    finally:
        clear_request_body_cache(request)
        native_body = None


# ============================================================
# 注册
# ============================================================


def register() -> None:
    """注册 OpenAI Responses API 方言"""
    register_dialect(
        DialectDefinition(
            id="openai-responses",
            name="OpenAI Responses API",
            description="OpenAI Responses API 格式（GPT-5/o1/o3 等新模型专用）",
            parse_request=parse_responses_request,
            render_response=render_responses_response,
            render_stream=render_responses_stream,
            parse_usage=parse_responses_usage,
            target_engine="openai-responses",
            endpoints=[
                EndpointDefinition(
                    path="/v1/responses",
                    methods=["POST"],
                    handler=responses_handler,
                    tags=["Responses"],
                    summary="Create Response",
                    description="创建响应请求，兼容 OpenAI Responses API 格式（GPT-5/o1/o3 等新模型）",
                ),
                # POST /v1/responses/* - Responses 子端点通配透传入口
                EndpointDefinition(
                    path="/v1/responses/{subpath:path}",
                    passthrough_root="/v1/responses",
                    methods=["POST"],
                    handler=responses_subpath_handler,
                    tags=["Responses"],
                    summary="Responses Passthrough Subpaths",
                    description="OpenAI Responses API 子端点透传入口（仅在上游为 openai-responses 时可用）",
                    passthrough_only=True,
                ),
            ],
        )
    )
