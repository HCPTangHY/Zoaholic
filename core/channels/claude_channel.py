"""
Claude/Anthropic 渠道适配器

负责处理 Anthropic Claude API 的请求构建和响应流解析
"""

import json
import copy
import asyncio
from datetime import datetime

from ..utils import (
    safe_get,
    get_model_dict,
    get_base64_image,
    get_tools_mode,
    generate_sse_response,
    generate_no_stream_response,
    end_of_line,
)
from ..response import check_response


# ============================================================
# Claude 格式化函数
# ============================================================

def format_text_message(text: str) -> dict:
    """格式化文本消息为 Claude 格式"""
    return {"type": "text", "text": text}


async def format_image_message(image_url: str) -> dict:
    """格式化图片消息为 Claude 格式"""
    base64_image, image_type = await get_base64_image(image_url)
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": image_type,
            "data": base64_image.split(",")[1],
        }
    }


async def gpt2claude_tools_json(json_dict):
    """将 GPT 格式的工具定义转换为 Claude 格式"""
    json_dict = copy.deepcopy(json_dict)

    # 处理 $ref 引用
    def resolve_refs(obj, defs):
        if isinstance(obj, dict):
            # 如果有 $ref 引用，替换为实际定义
            if "$ref" in obj and obj["$ref"].startswith("#/$defs/"):
                ref_name = obj["$ref"].split("/")[-1]
                if ref_name in defs:
                    # 完全替换为引用的对象
                    ref_obj = copy.deepcopy(defs[ref_name])
                    # 保留原始对象中的其他属性
                    for k, v in obj.items():
                        if k != "$ref":
                            ref_obj[k] = v
                    return ref_obj

            # 递归处理所有属性
            for key, value in list(obj.items()):
                obj[key] = resolve_refs(value, defs)

        elif isinstance(obj, list):
            # 递归处理列表中的每个元素
            for i, item in enumerate(obj):
                obj[i] = resolve_refs(item, defs)

        return obj

    # 提取 $defs 定义
    defs = {}
    if "parameters" in json_dict and "defs" in json_dict["parameters"]:
        defs = json_dict["parameters"]["defs"]
        # 从参数中删除 $defs，因为 Claude 不需要它
        del json_dict["parameters"]["defs"]

    # 解析所有引用
    json_dict = resolve_refs(json_dict, defs)

    # 继续原有的键名转换逻辑
    keys_to_change = {
        "parameters": "input_schema",
    }
    for old_key, new_key in keys_to_change.items():
        if old_key in json_dict:
            if new_key:
                if json_dict[old_key] is None:
                    json_dict[old_key] = {
                        "type": "object",
                        "properties": {}
                    }
                json_dict[new_key] = json_dict.pop(old_key)
            else:
                json_dict.pop(old_key)
    return json_dict


async def get_claude_payload(request, engine, provider, api_key=None):
    """构建 Claude API 的请求 payload"""
    model_dict = get_model_dict(provider)
    original_model = model_dict[request.model]

    if "claude-3-7-sonnet" in original_model:
        anthropic_beta = "output-128k-2025-02-19"
    elif "claude-3-5-sonnet" in original_model:
        anthropic_beta = "max-tokens-3-5-sonnet-2024-07-15"
    else:
        anthropic_beta = "tools-2024-05-16"

    headers = {
        "content-type": "application/json",
        "x-api-key": f"{api_key}",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": anthropic_beta,
    }
    url = provider['base_url']

    messages = []
    system_prompt = None
    tool_id = None
    for msg in request.messages:
        tool_call_id = None
        tool_calls = None
        if isinstance(msg.content, list):
            content = []
            for item in msg.content:
                if item.type == "text":
                    text_message = format_text_message(item.text)
                    content.append(text_message)
                elif item.type == "image_url" and provider.get("image", True):
                    image_message = await format_image_message(item.image_url.url)
                    content.append(image_message)
        else:
            content = msg.content
            tool_calls = msg.tool_calls
            tool_id = tool_calls[0].id if tool_calls else None or tool_id
            tool_call_id = msg.tool_call_id

        if tool_calls:
            tools_mode = get_tools_mode(provider)
            tool_calls_list = []
            # 根据 tools_mode 决定处理多少个工具调用
            calls_to_process = tool_calls if tools_mode == "parallel" else tool_calls[:1]
            for tool_call in calls_to_process:
                tool_calls_list.append({
                    "type": "tool_use",
                    "id": tool_call.id,
                    "name": tool_call.function.name,
                    "input": json.loads(tool_call.function.arguments),
                })
            messages.append({"role": msg.role, "content": tool_calls_list})
        elif tool_call_id:
            messages.append({"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": content
            }]})
        elif msg.role == "function":
            messages.append({"role": "assistant", "content": [{
                "type": "tool_use",
                "id": "toolu_017r5miPMV6PGSNKmhvHPic4",
                "name": msg.name,
                "input": {"prompt": "..."}
            }]})
            messages.append({"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": "toolu_017r5miPMV6PGSNKmhvHPic4",
                "content": msg.content
            }]})
        elif msg.role != "system":
            messages.append({"role": msg.role, "content": content})
        elif msg.role == "system":
            system_prompt = content

    conversation_len = len(messages) - 1
    message_index = 0
    while message_index < conversation_len:
        if messages[message_index]["role"] == messages[message_index + 1]["role"]:
            if messages[message_index].get("content"):
                if isinstance(messages[message_index]["content"], list):
                    messages[message_index]["content"].extend(messages[message_index + 1]["content"])
                elif isinstance(messages[message_index]["content"], str) and isinstance(messages[message_index + 1]["content"], list):
                    content_list = [{"type": "text", "text": messages[message_index]["content"]}]
                    content_list.extend(messages[message_index + 1]["content"])
                    messages[message_index]["content"] = content_list
                else:
                    messages[message_index]["content"] += messages[message_index + 1]["content"]
            messages.pop(message_index + 1)
            conversation_len = conversation_len - 1
        else:
            message_index = message_index + 1

    if "claude-3-7-sonnet" in original_model:
        max_tokens = 128000
    elif "claude-3-5-sonnet" in original_model:
        max_tokens = 8192
    elif "claude-sonnet-4" in original_model:
        max_tokens = 64000
    elif "claude-opus-4" in original_model:
        max_tokens = 32000
    else:
        max_tokens = 4096

    payload = {
        "model": original_model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if system_prompt:
        payload["system"] = system_prompt

    if request.max_tokens:
        payload["max_tokens"] = int(request.max_tokens)

    miss_fields = [
        'model',
       'messages',
        'presence_penalty',
        'frequency_penalty',
        'n',
        'user',
        'include_usage',
        'stream_options',
    ]

    for field, value in request.model_dump(exclude_unset=True).items():
        if field not in miss_fields and value is not None:
            payload[field] = value

    tools_mode = get_tools_mode(provider)
    if request.tools and tools_mode != "none":
        tools = payload.get("tools", [])  # 保留已有的工具（如插件添加的）
        for tool in request.tools:
            # 检查是否已经是 Claude 服务器端工具格式（如 web_search_20250305）
            if hasattr(tool, 'dict'):
                tool_dict = tool.dict()
            else:
                tool_dict = tool if isinstance(tool, dict) else {}

            # 服务器端工具的 type 包含日期后缀，直接保留
            tool_type = tool_dict.get("type", "")
            if tool_type and ("_20" in tool_type or tool_type.startswith("web_search") or tool_type.startswith("code_execution") or tool_type.startswith("computer_") or tool_type.startswith("text_editor")):
                # 服务器端工具，直接使用
                tools.append(tool_dict)
            elif "function" in tool_dict:
                # 客户端函数工具，需要转换格式
                json_tool = await gpt2claude_tools_json(tool_dict["function"])
                tools.append(json_tool)
            else:
                # 其他格式，尝试转换
                json_tool = await gpt2claude_tools_json(tool_dict)
                tools.append(json_tool)
        payload["tools"] = tools
        if "tool_choice" in payload:
            if isinstance(payload["tool_choice"], dict):
                if payload["tool_choice"]["type"] == "function":
                    payload["tool_choice"] = {
                        "type": "tool",
                        "name": payload["tool_choice"]["function"]["name"]
                    }
            if isinstance(payload["tool_choice"], str):
                if payload["tool_choice"] == "auto":
                    payload["tool_choice"] = {
                        "type": "auto"
                    }
                if payload["tool_choice"] == "none":
                    payload["tool_choice"] = {
                        "type": "any"
                    }

    if tools_mode == "none":
        payload.pop("tools", None)
        payload.pop("tool_choice", None)

    if "think" in request.model.lower():
        payload["thinking"] = {
            "budget_tokens": 4096,
            "type": "enabled"
        }
        payload["temperature"] = 1
        payload.pop("top_p", None)
        payload.pop("top_k", None)
        if request.model.split("-")[-1].isdigit():
            think_tokens = int(request.model.split("-")[-1])
            if think_tokens < max_tokens:
                payload["thinking"] = {
                    "budget_tokens": think_tokens,
                    "type": "enabled"
                }

    if request.thinking:
        payload["thinking"] = {
            "budget_tokens": request.thinking.budget_tokens,
            "type": request.thinking.type
        }
        payload["temperature"] = 1
        payload.pop("top_p", None)
        payload.pop("top_k", None)

    if safe_get(provider, "preferences", "post_body_parameter_overrides", default=None):
        for key, value in safe_get(provider, "preferences", "post_body_parameter_overrides", default={}).items():
            if key == request.model:
                for k, v in value.items():
                    payload[k] = v
            elif all(_model not in request.model.lower() for _model in model_dict.keys()) and "-" not in key and " " not in key:
                payload[key] = value

    return url, headers, payload


async def fetch_claude_response(client, url, headers, payload, model, timeout):
    """处理 Claude 非流式响应"""
    timestamp = int(datetime.timestamp(datetime.now()))
    json_payload = await asyncio.to_thread(json.dumps, payload)
    response = await client.post(url, headers=headers, content=json_payload, timeout=timeout)

    error_message = await check_response(response, "fetch_claude_response")
    if error_message:
        yield error_message
        return

    response_bytes = await response.aread()
    response_json = await asyncio.to_thread(json.loads, response_bytes)

    # 遍历 content 数组，提取文本内容和客户端工具调用
    # 跳过服务器端工具（server_tool_use, web_search_tool_result）
    content_list = response_json.get("content", [])
    text_parts = []
    function_call_name = None
    function_call_content = None
    tools_id = None

    thinking_parts = []

    for item in content_list:
        item_type = item.get("type", "")

        # 服务器端工具 - 跳过（Claude 已自动处理）
        if item_type in ("server_tool_use", "web_search_tool_result"):
            continue

        # thinking 内容
        if item_type == "thinking":
            thinking_parts.append(item.get("thinking", ""))

        # 文本内容
        if item_type == "text":
            text_parts.append(item.get("text", ""))

        # 客户端工具调用
        if item_type == "tool_use":
            function_call_name = item.get("name")
            function_call_content = item.get("input")
            tools_id = item.get("id")

    content = "".join(text_parts) if text_parts else None
    reasoning_content = "".join(thinking_parts) if thinking_parts else None

    prompt_tokens = safe_get(response_json, "usage", "input_tokens")
    output_tokens = safe_get(response_json, "usage", "output_tokens")
    total_tokens = (prompt_tokens or 0) + (output_tokens or 0)

    role = safe_get(response_json, "role")

    yield await generate_no_stream_response(
        timestamp, model, content=content, tools_id=tools_id,
        function_call_name=function_call_name, function_call_content=function_call_content,
        role=role, total_tokens=total_tokens, prompt_tokens=prompt_tokens,
        completion_tokens=output_tokens, reasoning_content=reasoning_content
    )


async def fetch_claude_response_stream(client, url, headers, payload, model, timeout):
    """处理 Claude 流式响应"""
    timestamp = int(datetime.timestamp(datetime.now()))
    json_payload = await asyncio.to_thread(json.dumps, payload)
    async with client.stream('POST', url, headers=headers, content=json_payload, timeout=timeout) as response:
        error_message = await check_response(response, "fetch_claude_response_stream")
        if error_message:
            yield error_message
            return
        buffer = ""
        input_tokens = 0
        # 跟踪当前 content_block 类型，用于区分服务器端工具和客户端工具
        current_block_type = None
        current_block_id = None
        async for chunk in response.aiter_text():
            buffer += chunk
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)

                if line.startswith("data:") and (line := line.lstrip("data: ")):
                    resp: dict = await asyncio.to_thread(json.loads, line)

                    input_tokens = input_tokens or safe_get(resp, "message", "usage", "input_tokens", default=0)
                    output_tokens = safe_get(resp, "usage", "output_tokens", default=0)
                    if output_tokens:
                        total_tokens = input_tokens + output_tokens
                        sse_string = await generate_sse_response(timestamp, model, None, None, None, None, None, total_tokens, input_tokens, output_tokens)
                        yield sse_string
                        break

                    # 处理 content_block_start 事件，记录当前块类型
                    event_type = resp.get("type", "")
                    if event_type == "content_block_start":
                        content_block = resp.get("content_block", {})
                        current_block_type = content_block.get("type", "")
                        current_block_id = content_block.get("id", "")

                        # 服务器端工具（server_tool_use）- 不转换为 tool_calls
                        # 让 Claude 自动执行，等待结果
                        if current_block_type == "server_tool_use":
                            # 可选：输出搜索中的提示
                            # sse_string = await generate_sse_response(timestamp, model, content=f"[正在搜索: {content_block.get('name', '')}]\n")
                            # yield sse_string
                            continue

                        # 客户端工具（tool_use）- 转换为 tool_calls
                        if current_block_type == "tool_use":
                            function_call_name = content_block.get("name", "")
                            tools_id = content_block.get("id", "")
                            if tools_id and function_call_name:
                                sse_string = await generate_sse_response(timestamp, model, None, tools_id, function_call_name, None)
                                yield sse_string
                            continue

                    # 处理 content_block_delta 事件
                    if event_type == "content_block_delta":
                        delta = resp.get("delta", {})
                        delta_type = delta.get("type", "")

                        # 服务器端工具的 input_json_delta - 跳过（内部处理）
                        if current_block_type == "server_tool_use" and delta_type == "input_json_delta":
                            continue

                        # 客户端工具的 input_json_delta - 输出参数
                        if current_block_type == "tool_use" and delta_type == "input_json_delta":
                            partial_json = delta.get("partial_json", "")
                            if partial_json:
                                sse_string = await generate_sse_response(timestamp, model, None, None, None, partial_json)
                                yield sse_string
                            continue

                    # 处理 web_search_tool_result - 服务器端搜索结果（跳过，Claude 会自动使用）
                    if event_type == "content_block_start":
                        content_block = resp.get("content_block", {})
                        if content_block.get("type") == "web_search_tool_result":
                            current_block_type = "web_search_tool_result"
                            continue

                    if current_block_type == "web_search_tool_result":
                        continue

                    # 正常文本输出
                    text = safe_get(resp, "delta", "text", default="")
                    if text:
                        sse_string = await generate_sse_response(timestamp, model, text)
                        yield sse_string
                        continue

                    # 兼容旧逻辑：直接从 content_block 获取工具信息
                    function_call_name = safe_get(resp, "content_block", "name", default=None)
                    tools_id = safe_get(resp, "content_block", "id", default=None)
                    block_type = safe_get(resp, "content_block", "type", default="")
                    # 只处理客户端工具（tool_use），跳过服务器端工具（server_tool_use）
                    if tools_id and function_call_name and block_type == "tool_use":
                        sse_string = await generate_sse_response(timestamp, model, None, tools_id, function_call_name, None)
                        yield sse_string

                    # thinking 内容
                    thinking_content = safe_get(resp, "delta", "thinking", default="")
                    if thinking_content:
                        sse_string = await generate_sse_response(timestamp, model, reasoning_content=thinking_content)
                        yield sse_string

                    # 客户端工具参数（兼容旧逻辑）
                    function_call_content = safe_get(resp, "delta", "partial_json", default="")
                    if function_call_content and current_block_type != "server_tool_use":
                        sse_string = await generate_sse_response(timestamp, model, None, None, None, function_call_content)
                        yield sse_string

    yield "data: [DONE]" + end_of_line


def register():
    """注册 Claude 渠道到注册中心"""
    from .registry import register_channel
    
    register_channel(
        id="claude",
        type_name="anthropic",
        default_base_url="https://api.anthropic.com/v1/messages",
        auth_header="x-api-key: {api_key}",
        description="Anthropic Claude API",
        request_adapter=get_claude_payload,
        response_adapter=fetch_claude_response,
        stream_adapter=fetch_claude_response_stream,
        models_adapter=None,
    )
