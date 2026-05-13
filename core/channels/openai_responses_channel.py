"""
OpenAI Responses API 渠道适配器

负责处理 OpenAI Responses API 的请求构建和响应流解析
专用于 GPT-5、o1、o3、o4 等新模型

主要功能：
- 构建 Responses API 格式的请求 payload
- 解析 Responses API 的流式事件并转换为 Chat Completions 格式
- 支持 reasoning 输出
"""

import json
import random
import string
import asyncio
from datetime import datetime

from ..utils import (
    BaseAPI,
    safe_get,
    get_model_dict,
    get_base64_image,
    generate_sse_response,
    generate_chunked_image_md,
    end_of_line,
)
from ..response import check_response
from ..json_utils import json_loads, json_dumps_text
from ..response_context import mark_adapter_metrics_managed, mark_content_start, merge_usage
from ..stream_utils import aiter_decoded_lines
from ..usage import extract_cache_usage


# ============================================================
# 工具函数
# ============================================================

def _normalize_responses_base_url(base_url: str) -> str:
    """归一化 base_url，去除末尾的 /responses 端点路径，确保只保留到 /v1 层级。
    兼容旧配置 .../v1/responses 和新配置 .../v1。"""
    if base_url.endswith('#'):
        return base_url  # 保留 '#'，由 resolve_base_url 处理
    url = base_url.rstrip('/')
    if url.endswith('/v1/responses'):
        url = url[:-len('/responses')]
    return url

# ============================================================
# 请求构建
# ============================================================


def format_text_item(text: str, role: str) -> dict:
    """格式化文本为 Responses API 格式，assistant 用 output_text，其他用 input_text"""
    item_type = "output_text" if role == "assistant" else "input_text"
    return {"type": item_type, "text": text}


async def format_input_image(image_url: str) -> dict:
    """格式化图片为 Responses API input_image 格式"""
    base64_image, _ = await get_base64_image(image_url)
    return {
        "type": "input_image",
        "image_url": base64_image,
    }


async def get_responses_passthrough_meta(request, engine, provider, api_key=None):
    """透传用：仅构建 url/headers，payload 由入口原生请求提供"""
    headers = {
        'Content-Type': 'application/json',
    }
    if api_key:
        # 支持 org-id:sk-key 格式 — 拆出组织ID注入 OpenAI-Organization 头
        if ':' in str(api_key) and str(api_key).startswith('org-'):
            org_id, actual_key = str(api_key).split(':', 1)
            headers['Authorization'] = f"Bearer {actual_key}"
            headers['OpenAI-Organization'] = org_id
        else:
            headers['Authorization'] = f"Bearer {api_key}"

    from ..utils import resolve_base_url
    url = resolve_base_url(_normalize_responses_base_url(
        provider.get('base_url', 'https://api.openai.com/v1')
    ), '/responses')

    return url, headers, {}


def _as_text_from_responses_content(content) -> str:
    """把 Responses input 中的 content（str 或 list）尽量抽成纯文本，用于 instructions 合并。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for it in content:
            if isinstance(it, str):
                parts.append(it)
            elif isinstance(it, dict):
                t = it.get("type")
                if t in ("input_text", "text", "output_text"):
                    txt = it.get("text")
                    if txt:
                        parts.append(str(txt))
        return "".join(parts)
    return str(content)


async def get_responses_payload(request, engine, provider, api_key=None):
    """构建 OpenAI Responses API 的请求 payload（对齐 b119589 的稳定实现）"""
    headers = {
        'Content-Type': 'application/json',
    }
    model_dict = get_model_dict(provider)
    original_model = model_dict[request.model]

    if api_key:
        # 支持 org-id:sk-key 格式
        if ':' in str(api_key) and str(api_key).startswith('org-'):
            org_id, actual_key = str(api_key).split(':', 1)
            headers['Authorization'] = f"Bearer {actual_key}"
            headers['OpenAI-Organization'] = org_id
        else:
            headers['Authorization'] = f"Bearer {api_key}"

    from ..utils import resolve_base_url
    url = resolve_base_url(_normalize_responses_base_url(
        provider.get('base_url', 'https://api.openai.com/v1')
    ), '/responses')

    # 构建 input 和 instructions
    input_items = []
    instructions_list = []

    messages = list(request.messages)

    for msg in messages:
        role = msg.role
        tool_calls = msg.tool_calls
        tool_call_id = msg.tool_call_id
        content = msg.content

        # 处理 system/developer：尽量进 instructions（除非目标模型不支持）
        if role in ("system", "developer"):
            # o3-mini 特殊处理：为 system 消息添加前缀以绕过限制
            if role == "system" and "o3-mini" in original_model and isinstance(content, str) and not content.startswith("Formatting re-enabled"):
                content = "Formatting re-enabled. " + content

            # o1-mini/o1-preview 可能不支持 instructions 参数：改用 developer 角色塞进 input
            if "o1-mini" in original_model or "o1-preview" in original_model:
                role = "developer"
            else:
                instructions_list.append(content or "")
                continue

        # content(list) -> message(content=[input_text/input_image...])
        if isinstance(content, list):
            content_items = []
            for item in content:
                if getattr(item, "type", None) == "text":
                    content_items.append(format_text_item(item.text, role))
                elif getattr(item, "type", None) == "image_url" and provider.get("image", True):
                    image_item = await format_input_image(item.image_url.url)
                    content_items.append(image_item)
                elif getattr(item, "type", None) == "file":
                    if getattr(item.file, "url", None) and item.file.url.startswith("data:image/"):
                        image_item = await format_input_image(item.file.url)
                        content_items.append(image_item)
                    elif getattr(item.file, "data", None) and str(item.file.mime_type).startswith("image/"):
                        image_item = await format_input_image(f"data:{item.file.mime_type};base64,{item.file.data}")
                        content_items.append(image_item)
                    else:
                        file_item = {"type": "input_file"}
                        if getattr(item.file, "filename", None):
                            file_item["filename"] = item.file.filename
                        if getattr(item.file, "file_id", None):
                            file_item["file_id"] = item.file.file_id
                        elif getattr(item.file, "url", None):
                            if item.file.url.startswith("http"):
                                file_item["file_url"] = item.file.url
                            else:
                                file_item["file_data"] = item.file.url
                        elif getattr(item.file, "data", None):
                            file_item["file_data"] = f"data:{item.file.mime_type or 'application/octet-stream'};base64,{item.file.data}"
                        content_items.append(file_item)
            if content_items:
                input_items.append({"type": "message", "role": role, "content": content_items})

        # tool result -> function_call_output（顶层 item）
        elif tool_call_id:
            input_items.append({
                "type": "function_call_output",
                "call_id": tool_call_id,
                "output": content or "",
            })

        else:
            # 文本消息
            if content or role == "assistant":
                input_items.append({
                    "type": "message",
                    "role": role,
                    "content": [format_text_item(content or "", role)],
                })

            # tool_calls -> function_call（顶层 item）
            if tool_calls:
                for tool_call in tool_calls:
                    input_items.append({
                        "type": "function_call",
                        "call_id": tool_call.id,
                        "name": tool_call.function.name,
                        "arguments": tool_call.function.arguments,
                    })

    # 构建 payload
    payload = {
        "model": original_model,
        "input": input_items,
    }

    if instructions_list:
        payload["instructions"] = "\n\n".join(instructions_list)

    # 添加 stream 参数
    if request.stream:
        payload["stream"] = True

    # 处理 reasoning effort + summary（对齐 b119589）
    if any(m in original_model for m in ["o1", "o3", "o4", "gpt-5"]):
        reasoning_config = {}

        # o1-preview 和 o1-mini 不支持 reasoning effort
        if "o1-preview" not in original_model and "o1-mini" not in original_model:
            effort = "medium"
            if request.model.endswith("-high"):
                effort = "high"
            elif request.model.endswith("-low"):
                effort = "low"
            reasoning_config["effort"] = effort

        # 启用 reasoning summary 以获取推理过程
        reasoning_config["summary"] = "auto"

        # 允许覆盖，但补齐默认值
        if "reasoning" not in payload:
            payload["reasoning"] = reasoning_config
        elif isinstance(payload.get("reasoning"), dict):
            for k, v in reasoning_config.items():
                if k not in payload["reasoning"]:
                    payload["reasoning"][k] = v

    # 可选参数（严格按 Responses API 支持字段映射，避免上游报 Unsupported parameter）
    miss_fields = ['model', 'messages', 'stream', 'instructions']

    def _convert_tool_choice(tc):
        # Chat Completions 的 tool_choice 可能是："auto"/"none"/"required" 或 {type,function:{name}}
        if tc is None:
            return None
        if isinstance(tc, str):
            return tc
        if isinstance(tc, dict):
            # 兼容 {"type":"function","function":{"name":"xxx"}}
            if tc.get("type") == "function":
                func = tc.get("function") or {}
                name = func.get("name")
                if name:
                    return {"type": "function", "name": name}
            return tc
        # Pydantic ToolChoice
        try:
            tc_dict = tc.model_dump(exclude_unset=True)
            if tc_dict.get("type") == "function":
                func = tc_dict.get("function") or {}
                name = func.get("name")
                if name:
                    return {"type": "function", "name": name}
            return tc_dict
        except Exception:
            return None

    for field, value in request.model_dump(exclude_unset=True).items():
        if field in miss_fields or value is None:
            continue

        # token 限制
        if field in ("max_tokens", "max_completion_tokens"):
            payload["max_output_tokens"] = value
            continue

        # tools
        if field == "tools":
            converted_tools = []
            for tool in value:
                if isinstance(tool, dict):
                    tool_type = tool.get("type", "function")
                    if tool_type == "function" and "function" in tool:
                        func = tool.get("function") or {}
                        converted_tools.append({
                            "type": "function",
                            "name": func.get("name", ""),
                            "description": func.get("description", ""),
                            "parameters": func.get("parameters", {}),
                        })
                    else:
                        converted_tools.append(tool)
                else:
                    # Pydantic Tool
                    try:
                        tool_dict = tool.model_dump(exclude_unset=True)
                    except Exception:
                        tool_dict = None

                    if isinstance(tool_dict, dict):
                        # 兼容 Chat Completions tool
                        if tool_dict.get("type") == "function" and "function" in tool_dict:
                            func = tool_dict.get("function") or {}
                            converted_tools.append({
                                "type": "function",
                                "name": func.get("name", ""),
                                "description": func.get("description", ""),
                                "parameters": func.get("parameters", {}),
                            })
                        else:
                            converted_tools.append(tool_dict)

            if converted_tools:
                payload["tools"] = converted_tools
            continue

        # tool_choice
        if field == "tool_choice":
            converted = _convert_tool_choice(value)
            if converted is not None:
                payload["tool_choice"] = converted
            continue

        # response_format -> text.format
        if field == "response_format":
            try:
                rf = value if isinstance(value, dict) else value.model_dump(exclude_unset=True)
            except Exception:
                rf = None

            if isinstance(rf, dict):
                format_type = rf.get("type")
                if format_type == "json_object":
                    payload["text"] = {"format": {"type": "json_object"}}
                elif format_type == "json_schema":
                    payload["text"] = {"format": rf}
            continue

        # Responses API/推理模型常见不支持
        if field in (
            "temperature", "top_p",
            "presence_penalty", "frequency_penalty",
            "n", "logprobs", "top_logprobs",
            "stream_options",
        ):
            continue

        # 其他字段：只允许少数 Responses API 标准字段透传（保守策略）
        if field in ("parallel_tool_calls", "metadata", "include"):
            payload[field] = value
            continue

        # 其余一律忽略，避免上游严格校验报错
        continue

    # 最终兜底：再次确保不携带常见不支持字段
    payload.pop("top_p", None)
    payload.pop("temperature", None)
    payload.pop("presence_penalty", None)
    payload.pop("frequency_penalty", None)
    payload.pop("n", None)
    payload.pop("logprobs", None)
    payload.pop("top_logprobs", None)
    payload.pop("stream_options", None)
    payload.pop("max_tokens", None)
    payload.pop("max_completion_tokens", None)

    # 覆盖配置
    # 兼容性：部分上游/网关要求 Responses API 显式设置 store=false，否则会报错
    # （例如："Store must be set to false"）
    payload["store"] = False

    return url, headers, payload


# ============================================================
# 响应处理
# ============================================================


async def fetch_responses_response(client, url, headers, payload, model, timeout):
    """处理 Responses API 的非流式响应"""
    json_payload = await asyncio.to_thread(json_dumps_text, payload)
    response = await client.post(url, headers=headers, content=json_payload, timeout=timeout)

    error_message = await check_response(response, "fetch_responses_response")
    if error_message:
        yield error_message
        return

    response_bytes = await response.aread()
    response_json = await asyncio.to_thread(json_loads, response_bytes)

    # 将 Responses API 响应转换为 Chat Completions 格式
    converted = await convert_responses_to_chat_completions(response_json, model)
    mark_adapter_metrics_managed()
    usage = converted.get("usage") or {}
    raw_usage = response_json.get("usage") or {}
    # 转换后的 usage 不保留 input_tokens_details，因此缓存字段要从 Responses API 原始 usage 中提取。
    merge_usage(
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        total_tokens=usage.get("total_tokens", 0),
        **extract_cache_usage(raw_usage),
    )
    if safe_get(converted, "choices", 0, "message", "content", default=None):
        mark_content_start()
    yield converted


async def convert_responses_to_chat_completions(response: dict, model: str) -> dict:
    """将 Responses API 非流式响应转换为 Chat Completions 格式"""
    timestamp = int(datetime.timestamp(datetime.now()))
    random.seed(timestamp)
    random_str = ''.join(random.choices(string.ascii_letters + string.digits, k=29))

    content_text = ""
    content_images = []  # 收集图片 items
    reasoning_content = ""
    tool_calls = []

    def append_tool_call(source: dict):
        # 修改原因：Responses API 非流式可能把 function_call 作为多个顶层 output item 返回，旧转换只看 message.content。
        # 修改方式：统一从顶层 function_call 和兼容旧 tool_use 两种来源收集工具调用，并按收集顺序写入 index。
        # 目的：保证并行工具调用转换成 Chat Completions 后仍能逐项区分。
        arguments = source.get("arguments", "{}")
        if not isinstance(arguments, str):
            arguments = json_dumps_text(arguments, ensure_ascii=False)
        tool_calls.append({
            "index": len(tool_calls),
            "id": source.get("call_id") or source.get("id") or f"call_{random_str[:20]}{len(tool_calls):04d}",
            "type": "function",
            "function": {
                "name": source.get("name", ""),
                "arguments": arguments,
            }
        })

    # 解析 output
    output = response.get("output", [])
    for item in output:
        item_type = item.get("type", "")

        if item_type == "reasoning":
            # 提取 reasoning summary
            summary = item.get("summary", [])
            for s in summary:
                if s.get("type") == "summary_text":
                    reasoning_content += s.get("text", "")

        elif item_type == "message":
            # 提取消息内容
            item_content = item.get("content", [])
            for c in item_content:
                c_type = c.get("type", "")
                if c_type == "output_text":
                    content_text += c.get("text", "")
                elif c_type == "tool_use":
                    append_tool_call(c)

        elif item_type == "function_call":
            append_tool_call(item)

        elif item_type == "image_generation_call":
            # gpt-image-2 等模型的生图结果：结构化 image_url item
            result = item.get("result", "")
            if result and result.strip():
                content_images.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{result}"}
                })

    # 构建 content：有图片时用结构化 list，纯文本时保持 string
    if content_images:
        content_items = []
        if content_text:
            content_items.append({"type": "text", "text": content_text})
        content_items.extend(content_images)
        content = content_items
    else:
        content = content_text

    # 构建 Chat Completions 响应
    message = {
        "role": "assistant",
        "content": content or None,
        "refusal": None
    }

    if reasoning_content:
        message["reasoning_content"] = reasoning_content

    if tool_calls:
        message["tool_calls"] = tool_calls

    result = {
        "id": f"chatcmpl-{random_str}",
        "object": "chat.completion",
        "created": timestamp,
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "logprobs": None,
            "finish_reason": "tool_calls" if tool_calls else "stop"
        }],
        "usage": None,
        "system_fingerprint": "fp_responses_api"
    }

    # 添加 usage
    usage = response.get("usage", {})
    if usage:
        # Responses 非流式转换为 Chat Completions 时保留 input_tokens_details.cached_tokens。
        cache_usage = extract_cache_usage(usage)
        result["usage"] = {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0)
        }
        if cache_usage["cached_tokens"] > 0:
            result["usage"]["prompt_tokens_details"] = {"cached_tokens": cache_usage["cached_tokens"]}
        if cache_usage["cache_creation_tokens"] > 0:
            result["usage"]["cache_creation_tokens"] = cache_usage["cache_creation_tokens"]

    return result


async def fetch_responses_stream(client, url, headers, payload, model, timeout):
    """
    处理 Responses API 的流式响应

    将 Responses API 的流式事件转换为 Chat Completions SSE 格式

    Responses API 事件类型：
    - response.created
    - response.in_progress
    - response.output_item.added
    - response.output_text.delta
    - response.reasoning_summary_text.delta
    - response.output_text.done
    - response.completed
    """
    from ..log_config import logger

    timestamp = int(datetime.timestamp(datetime.now()))
    random.seed(timestamp)
    random_str = ''.join(random.choices(string.ascii_letters + string.digits, k=29))

    json_payload = await asyncio.to_thread(json_dumps_text, payload)

    async with client.stream('POST', url, headers=headers, content=json_payload, timeout=timeout) as response:
        error_message = await check_response(response, "fetch_responses_stream")
        if error_message:
            yield error_message
            return

        mark_adapter_metrics_managed()
        input_tokens = 0
        output_tokens = 0
        # Responses API 的缓存字段只在 completed 事件 usage 中出现，先暂存再写入 current_info。
        cached_tokens = 0
        cache_creation_tokens = 0
        has_sent_role = False
        has_sent_content = False  # 追踪是否已发送任何内容
        # 修改原因：Responses API 的参数 delta 可能在多个工具调用之间并行出现，旧实现没有记录 call_id 到 index 的关系。
        # 修改方式：维护 call_id、item_id、output_index 到 Chat Completions tool_call_index 的映射，并记录哪些 index 已发过工具头。
        # 目的：确保每个 function_call_arguments.delta 都追加到正确的工具调用。
        tc_index = 0
        current_call_id_to_index: dict = {}
        current_item_id_to_index: dict = {}
        current_output_index_to_index: dict = {}
        sent_tool_header_indexes = set()
        seen_argument_indexes = set()
        has_sent_tool_calls = False

        def lookup_tool_call_index(event_data: dict, call_id=None):
            item_id = event_data.get("item_id")
            output_index = event_data.get("output_index")
            if call_id is not None and call_id in current_call_id_to_index:
                return current_call_id_to_index[call_id]
            if item_id is not None and item_id in current_item_id_to_index:
                return current_item_id_to_index[item_id]
            if output_index is not None and output_index in current_output_index_to_index:
                return current_output_index_to_index[output_index]
            return None

        def register_tool_call_index(index: int, event_data: dict, call_id=None):
            item_id = event_data.get("item_id") or safe_get(event_data, "item", "id", default=None)
            output_index = event_data.get("output_index")
            if call_id is not None:
                current_call_id_to_index[call_id] = index
            if item_id is not None:
                current_item_id_to_index[item_id] = index
            if output_index is not None:
                current_output_index_to_index[output_index] = index

        async for line in aiter_decoded_lines(response.aiter_bytes()):

                # 跳过空行和注释
                if not line or line.startswith(":"):
                    continue

                # 跳过 event: 行
                if line.startswith("event:"):
                    continue

                # 处理 data: 行
                if line.startswith("data:"):
                    data_str = line[5:].strip()

                    if data_str == "[DONE]":
                        break

                    try:
                        data = json_loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    event_type = data.get("type", "")

                    # 发送角色信息（仅首次）
                    # 支持更多的内容事件类型
                    if not has_sent_role and event_type in (
                        "response.output_text.delta",
                        "response.reasoning_summary_text.delta",
                        "response.reasoning.delta",
                        "response.content_part.delta",
                    ):
                        sse_string = await generate_sse_response(timestamp, model, role="assistant")
                        yield sse_string
                        has_sent_role = True

                    # function call item added
                    if event_type == "response.output_item.added":
                        item = data.get("item", {}) or {}
                        if item.get("type") == "function_call":
                            call_id = item.get("call_id") or item.get("id") or data.get("call_id") or data.get("item_id") or f"call_{random_str[:20]}{tc_index:04d}"
                            name = item.get("name") or data.get("name", "")
                            tool_index = lookup_tool_call_index(data, call_id)
                            if tool_index is None:
                                tool_index = tc_index
                                tc_index += 1
                            register_tool_call_index(tool_index, data, call_id)
                            if tool_index not in sent_tool_header_indexes:
                                mark_content_start()
                                sse_string = await generate_sse_response(
                                    timestamp, model, tools_id=call_id, function_call_name=name,
                                    tool_call_index=tool_index,
                                )
                                yield sse_string
                                sent_tool_header_indexes.add(tool_index)
                                has_sent_content = True
                                has_sent_tool_calls = True

                    # reasoning delta（新的 reasoning 事件格式）
                    elif event_type == "response.reasoning.delta":
                        delta = data.get("delta", "")
                        if delta:
                            mark_content_start()
                            sse_string = await generate_sse_response(
                                timestamp, model, reasoning_content=delta
                            )
                            yield sse_string
                            has_sent_content = True

                    # reasoning summary delta -> reasoning_content
                    elif event_type == "response.reasoning_summary_text.delta":
                        delta = data.get("delta", "")
                        if delta:
                            mark_content_start()
                            sse_string = await generate_sse_response(
                                timestamp, model, reasoning_content=delta
                            )
                            yield sse_string
                            has_sent_content = True

                    # output text delta -> content
                    elif event_type == "response.output_text.delta":
                        delta = data.get("delta", "")
                        if delta:
                            mark_content_start()
                            sse_string = await generate_sse_response(
                                timestamp, model, content=delta
                            )
                            yield sse_string
                            has_sent_content = True

                    # output text done
                    # 注意：不在此处发送 stop，统一由 response.completed 发送
                    # 避免 text + image 混合响应时提前终止流（image 在 output_item.done 中处理）
                    elif event_type == "response.output_text.done":
                        pass

                    # function call arguments delta
                    elif event_type == "response.function_call_arguments.delta":
                        delta = data.get("delta", "")
                        call_id = data.get("call_id") or data.get("item_id")
                        tool_index = lookup_tool_call_index(data, call_id)
                        if tool_index is None:
                            # 修改原因：部分兼容网关可能不发送 output_item.added，只在参数 delta 中首次暴露工具调用。
                            # 修改方式：首次看到未知 call_id/item_id 时立即补发工具头，并分配新的 index。
                            # 目的：保持“工具头先于参数”的流式顺序，避免客户端拿不到 id/name 容器。
                            call_id = call_id or f"call_{random_str[:20]}{tc_index:04d}"
                            tool_index = tc_index
                            tc_index += 1
                            register_tool_call_index(tool_index, data, call_id)
                        if tool_index not in sent_tool_header_indexes:
                            mark_content_start()
                            sse_string = await generate_sse_response(
                                timestamp, model, tools_id=call_id, function_call_name=data.get("name", ""),
                                tool_call_index=tool_index,
                            )
                            yield sse_string
                            sent_tool_header_indexes.add(tool_index)
                            has_sent_tool_calls = True
                        if delta:
                            mark_content_start()
                            sse_string = await generate_sse_response(
                                timestamp, model, function_call_content=delta,
                                tool_call_index=tool_index,
                            )
                            yield sse_string
                            seen_argument_indexes.add(tool_index)
                            has_sent_content = True
                            has_sent_tool_calls = True

                    # function call done
                    elif event_type == "response.function_call_arguments.done":
                        call_id = data.get("call_id") or data.get("item_id") or f"call_{random_str[:20]}{tc_index:04d}"
                        name = data.get("name", "")
                        tool_index = lookup_tool_call_index(data, call_id)
                        if tool_index is None:
                            tool_index = tc_index
                            tc_index += 1
                        register_tool_call_index(tool_index, data, call_id)
                        if tool_index not in sent_tool_header_indexes:
                            mark_content_start()
                            sse_string = await generate_sse_response(
                                timestamp, model, tools_id=call_id, function_call_name=name,
                                tool_call_index=tool_index,
                            )
                            yield sse_string
                            sent_tool_header_indexes.add(tool_index)
                            has_sent_content = True
                            has_sent_tool_calls = True
                        arguments = data.get("arguments", "")
                        if arguments and tool_index not in seen_argument_indexes:
                            mark_content_start()
                            sse_string = await generate_sse_response(
                                timestamp, model, function_call_content=arguments,
                                tool_call_index=tool_index,
                            )
                            yield sse_string
                            seen_argument_indexes.add(tool_index)
                            has_sent_content = True
                            has_sent_tool_calls = True

                    # image generation call completed -> inline markdown image
                    elif event_type == "response.output_item.done":
                        item = data.get("item", {})
                        if item.get("type") == "image_generation_call":
                            result = item.get("result", "")
                            if result and result.strip():
                                if not has_sent_role:
                                    sse_string = await generate_sse_response(timestamp, model, role="assistant")
                                    yield sse_string
                                    has_sent_role = True

                                mark_content_start()
                                # 发结构化 image content item，方言出口各自转换
                                image_content_item = [{
                                    "type": "image_url",
                                    "image_url": {"url": f"data:image/png;base64,{result}"}
                                }]
                                sse_string = await generate_sse_response(
                                    timestamp, model, content=image_content_item
                                )
                                yield sse_string
                                has_sent_content = True

                    # response completed -> 提取 usage，同时确保发送 stop
                    elif event_type == "response.completed":
                        response_data = data.get("response", {})
                        usage = response_data.get("usage", {})
                        input_tokens = usage.get("input_tokens", 0)
                        output_tokens = usage.get("output_tokens", 0)
                        # completed 事件携带 input_tokens_details.cached_tokens，需要在转换为 Chat SSE 前保存。
                        _cache_usage = extract_cache_usage(usage)
                        cached_tokens = _cache_usage["cached_tokens"] or cached_tokens
                        cache_creation_tokens = _cache_usage["cache_creation_tokens"] or cache_creation_tokens
                        merge_usage(
                            prompt_tokens=input_tokens,
                            completion_tokens=output_tokens,
                            total_tokens=input_tokens + output_tokens,
                            cached_tokens=cached_tokens,
                            cache_creation_tokens=cache_creation_tokens,
                        )
                        
                        # 如果还没发送 stop，在这里发送
                        if has_sent_content:
                            # 修改原因：Responses 流式工具调用完成时应向下游表达 tool_calls 结束，而不是普通 stop。
                            # 修改方式：只要本轮发送过工具调用，就把最终 finish_reason 改为 tool_calls。
                            # 目的：让 OpenAI 兼容客户端按工具调用结束状态继续执行工具。
                            stop_reason = "tool_calls" if has_sent_tool_calls else "stop"
                            sse_string = await generate_sse_response(
                                timestamp, model, stop=stop_reason
                            )
                            yield sse_string

        # 发送 usage 信息
        if input_tokens or output_tokens:
            sse_string = await generate_sse_response(
                timestamp, model,
                total_tokens=input_tokens + output_tokens,
                prompt_tokens=input_tokens,
                completion_tokens=output_tokens,
                # Responses API 的缓存字段在 completed 事件中暂存，最终 Chat SSE usage chunk 需要一并输出。
                cached_tokens=cached_tokens,
                cache_creation_tokens=cache_creation_tokens,
            )
            yield sse_string

        yield "data: [DONE]" + end_of_line


async def fetch_responses_models(client, provider):
    """获取 Responses API 支持的模型列表"""
    from ..utils import resolve_base_url
    raw_base_url = provider.get('base_url', 'https://api.openai.com/v1')
    is_fixed = raw_base_url.endswith('#')
    base_url = resolve_base_url(_normalize_responses_base_url(raw_base_url), '')
    api_key = provider.get('api')
    if isinstance(api_key, list):
        api_key = api_key[0] if api_key else None

    headers = {'Content-Type': 'application/json'}
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'

    models_url = base_url if is_fixed else f"{base_url}/models"

    response = await client.get(models_url, headers=headers)
    response.raise_for_status()

    data = response.json()
    models = []
    if isinstance(data, dict) and 'data' in data:
        models = [m.get('id') for m in data['data'] if m.get('id')]
    elif isinstance(data, list):
        models = [m.get('id') if isinstance(m, dict) else m for m in data]

    return models


# ============================================================
# 注册
# ============================================================


def register():
    """注册 OpenAI Responses API 渠道到注册中心"""
    from .registry import register_channel

    register_channel(
        id="openai-responses",
        type_name="openai-responses",
        default_base_url="https://api.openai.com/v1",
        auth_header="Authorization: Bearer {api_key}",
        description="OpenAI Responses API（GPT-5/o1/o3/o4 等新模型专用）",
        request_adapter=get_responses_payload,
        passthrough_adapter=get_responses_passthrough_meta,
        response_adapter=fetch_responses_response,
        stream_adapter=fetch_responses_stream,
        models_adapter=fetch_responses_models,
        source="builtin",
    )
