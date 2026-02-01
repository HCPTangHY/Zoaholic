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
from urllib.parse import urlparse, urlunparse

from ..utils import (
    BaseAPI,
    safe_get,
    get_model_dict,
    get_base64_image,
    generate_sse_response,
    end_of_line,
)
from ..response import check_response


# ============================================================
# 请求构建
# ============================================================


def format_input_text(text: str) -> dict:
    """格式化文本为 Responses API input_text 格式"""
    return {"type": "input_text", "text": text}


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
        headers['Authorization'] = f"Bearer {api_key}"

    base_url = provider.get('base_url', 'https://api.openai.com/v1/responses')

    # 确保 URL 以 /responses 结尾
    parsed = urlparse(base_url)
    if not parsed.path.endswith('/responses'):
        if parsed.path.endswith('/v1'):
            url = base_url.rstrip('/') + '/responses'
        elif '/v1/' in parsed.path:
            url = base_url.split('/v1/')[0] + '/v1/responses'
        else:
            url = base_url.rstrip('/') + '/v1/responses'
    else:
        url = base_url

    return url, headers, {}


async def get_responses_payload(request, engine, provider, api_key=None):
    """构建 OpenAI Responses API 的请求 payload"""
    headers = {
        'Content-Type': 'application/json',
    }
    model_dict = get_model_dict(provider)
    original_model = model_dict[request.model]

    if api_key:
        headers['Authorization'] = f"Bearer {api_key}"

    # 构建 URL
    base_url = provider.get('base_url', 'https://api.openai.com/v1/responses')
    parsed = urlparse(base_url)

    # 确保 URL 以 /responses 结尾
    if not parsed.path.endswith('/responses'):
        if parsed.path.endswith('/v1'):
            url = base_url.rstrip('/') + '/responses'
        elif '/v1/' in parsed.path:
            url = base_url.split('/v1/')[0] + '/v1/responses'
        else:
            url = base_url.rstrip('/') + '/v1/responses'
    else:
        url = base_url

    # 构建 input（将 messages 转换为 Responses API input 格式）
    input_items = []
    for msg in request.messages:
        role = msg.role
        tool_calls = msg.tool_calls
        tool_call_id = msg.tool_call_id

        if isinstance(msg.content, list):
            content = []
            for item in msg.content:
                if item.type == "text":
                    content.append(format_input_text(item.text))
                elif item.type == "image_url" and provider.get("image", True):
                    image_item = await format_input_image(item.image_url.url)
                    content.append(image_item)
            if content:
                input_items.append({"role": role, "content": content})
        else:
            content = msg.content
            if tool_calls:
                # 处理 tool_calls
                tool_calls_list = []
                for tool_call in tool_calls:
                    tool_calls_list.append({
                        "type": "function_call",
                        "id": tool_call.id,
                        "name": tool_call.function.name,
                        "arguments": tool_call.function.arguments
                    })
                input_items.append({"role": role, "content": tool_calls_list})
            elif tool_call_id:
                # 处理 tool 结果
                input_items.append({
                    "type": "function_call_output",
                    "call_id": tool_call_id,
                    "output": content
                })
            else:
                input_items.append({"role": role, "content": content})

    # 构建 payload
    payload = {
        "model": original_model,
        "input": input_items,
    }

    # 添加 stream 参数
    if request.stream:
        payload["stream"] = True

    # 处理 reasoning effort（从模型后缀提取）
    if request.model.endswith("-high"):
        payload["reasoning"] = {"effort": "high"}
    elif request.model.endswith("-low"):
        payload["reasoning"] = {"effort": "low"}

    # 可选参数
    miss_fields = ['model', 'messages', 'stream']

    for field, value in request.model_dump(exclude_unset=True).items():
        if field not in miss_fields and value is not None:
            if field == "max_tokens":
                payload["max_output_tokens"] = value
            elif field == "max_completion_tokens":
                payload["max_output_tokens"] = value
            elif field == "tools":
                # 转换 tools 格式
                converted_tools = []
                for tool in value:
                    if isinstance(tool, dict):
                        tool_type = tool.get("type", "function")
                        if tool_type == "function" and "function" in tool:
                            # 将 Chat Completions 格式转为 Responses API 格式
                            func = tool["function"]
                            converted_tools.append({
                                "type": "function",
                                "name": func.get("name", ""),
                                "description": func.get("description", ""),
                                "parameters": func.get("parameters", {})
                            })
                        else:
                            converted_tools.append(tool)
                    else:
                        converted_tools.append(tool)
                if converted_tools:
                    payload["tools"] = converted_tools
            elif field == "response_format":
                # 转换 response_format 为 text.format
                if isinstance(value, dict):
                    format_type = value.get("type")
                    if format_type == "json_object":
                        payload["text"] = {"format": {"type": "json_object"}}
                    elif format_type == "json_schema":
                        payload["text"] = {"format": value}
            elif field not in ["temperature", "stream_options"]:
                # Responses API 不支持 temperature（reasoning 模型）
                # stream_options 也需要移除
                payload[field] = value

    # 覆盖配置
    if safe_get(provider, "preferences", "post_body_parameter_overrides", default=None):
        for key, value in safe_get(provider, "preferences", "post_body_parameter_overrides", default={}).items():
            if key == request.model:
                for k, v in value.items():
                    payload[k] = v
            elif all(_model not in request.model.lower() for _model in model_dict.keys()) and "-" not in key and " " not in key:
                payload[key] = value

    return url, headers, payload


# ============================================================
# 响应处理
# ============================================================


async def fetch_responses_response(client, url, headers, payload, model, timeout):
    """处理 Responses API 的非流式响应"""
    json_payload = await asyncio.to_thread(json.dumps, payload)
    response = await client.post(url, headers=headers, content=json_payload, timeout=timeout)

    error_message = await check_response(response, "fetch_responses_response")
    if error_message:
        yield error_message
        return

    response_bytes = await response.aread()
    response_json = await asyncio.to_thread(json.loads, response_bytes)

    # 将 Responses API 响应转换为 Chat Completions 格式
    converted = await convert_responses_to_chat_completions(response_json, model)
    yield converted


async def convert_responses_to_chat_completions(response: dict, model: str) -> dict:
    """将 Responses API 非流式响应转换为 Chat Completions 格式"""
    timestamp = int(datetime.timestamp(datetime.now()))
    random.seed(timestamp)
    random_str = ''.join(random.choices(string.ascii_letters + string.digits, k=29))

    content = ""
    reasoning_content = ""
    tool_calls = []

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
                    content += c.get("text", "")
                elif c_type == "tool_use":
                    tool_calls.append({
                        "id": c.get("id", f"call_{random_str[:24]}"),
                        "type": "function",
                        "function": {
                            "name": c.get("name", ""),
                            "arguments": c.get("arguments", "{}")
                        }
                    })

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
        result["usage"] = {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0)
        }

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

    json_payload = await asyncio.to_thread(json.dumps, payload)

    async with client.stream('POST', url, headers=headers, content=json_payload, timeout=timeout) as response:
        error_message = await check_response(response, "fetch_responses_stream")
        if error_message:
            yield error_message
            return

        buffer = ""
        input_tokens = 0
        output_tokens = 0
        has_sent_role = False
        has_sent_content = False  # 追踪是否已发送任何内容

        async for chunk in response.aiter_text():
            buffer += chunk

            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)

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
                        data = await asyncio.to_thread(json.loads, data_str)
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

                    # reasoning delta（新的 reasoning 事件格式）
                    if event_type == "response.reasoning.delta":
                        delta = data.get("delta", "")
                        if delta:
                            sse_string = await generate_sse_response(
                                timestamp, model, reasoning_content=delta
                            )
                            yield sse_string
                            has_sent_content = True

                    # reasoning summary delta -> reasoning_content
                    elif event_type == "response.reasoning_summary_text.delta":
                        delta = data.get("delta", "")
                        if delta:
                            sse_string = await generate_sse_response(
                                timestamp, model, reasoning_content=delta
                            )
                            yield sse_string
                            has_sent_content = True

                    # output text delta -> content
                    elif event_type == "response.output_text.delta":
                        delta = data.get("delta", "")
                        if delta:
                            sse_string = await generate_sse_response(
                                timestamp, model, content=delta
                            )
                            yield sse_string
                            has_sent_content = True

                    # output text done -> finish_reason
                    # 只有当已发送内容时才发送 stop，避免空响应
                    elif event_type == "response.output_text.done":
                        if has_sent_content:
                            sse_string = await generate_sse_response(
                                timestamp, model, stop="stop"
                            )
                            yield sse_string

                    # function call arguments delta
                    elif event_type == "response.function_call_arguments.delta":
                        delta = data.get("delta", "")
                        if delta:
                            sse_string = await generate_sse_response(
                                timestamp, model, function_call_content=delta
                            )
                            yield sse_string
                            has_sent_content = True

                    # function call done
                    elif event_type == "response.function_call_arguments.done":
                        call_id = data.get("call_id", f"call_{random_str[:24]}")
                        name = data.get("name", "")
                        sse_string = await generate_sse_response(
                            timestamp, model, tools_id=call_id, function_call_name=name
                        )
                        yield sse_string

                    # response completed -> 提取 usage，同时确保发送 stop
                    elif event_type == "response.completed":
                        response_data = data.get("response", {})
                        usage = response_data.get("usage", {})
                        input_tokens = usage.get("input_tokens", 0)
                        output_tokens = usage.get("output_tokens", 0)
                        
                        # 如果还没发送 stop，在这里发送
                        if has_sent_content:
                            sse_string = await generate_sse_response(
                                timestamp, model, stop="stop"
                            )
                            yield sse_string

        # 发送 usage 信息
        if input_tokens or output_tokens:
            sse_string = await generate_sse_response(
                timestamp, model,
                total_tokens=input_tokens + output_tokens,
                prompt_tokens=input_tokens,
                completion_tokens=output_tokens
            )
            yield sse_string

        yield "data: [DONE]" + end_of_line


async def fetch_responses_models(client, provider):
    """获取 Responses API 支持的模型列表"""
    base_url = provider.get('base_url', 'https://api.openai.com/v1').rstrip('/')
    api_key = provider.get('api')
    if isinstance(api_key, list):
        api_key = api_key[0] if api_key else None

    headers = {'Content-Type': 'application/json'}
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'

    # 获取模型列表
    if '/v1/responses' in base_url:
        models_url = base_url.replace('/v1/responses', '/v1/models')
    else:
        models_url = f"{base_url}/models"

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
        default_base_url="https://api.openai.com/v1/responses",
        auth_header="Authorization: Bearer {api_key}",
        description="OpenAI Responses API（GPT-5/o1/o3/o4 等新模型专用）",
        request_adapter=get_responses_payload,
        passthrough_adapter=get_responses_passthrough_meta,
        response_adapter=fetch_responses_response,
        stream_adapter=fetch_responses_stream,
        models_adapter=fetch_responses_models,
    )
