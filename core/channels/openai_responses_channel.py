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


async def patch_passthrough_responses_payload(
    payload: dict,
    modifications: dict,
    request,
    engine: str,
    provider: dict,
    api_key=None,
) -> dict:
    """透传模式下对 Responses API payload 做渠道级修饰（尽量对齐 b119589 的稳定行为）。

    目标：
    - 只做轻量修补，但确保不会因为 Responses API 严格校验而炸：
      1) system_prompt 注入
      2) system/developer（开头连续）尽量收敛到 instructions（避免被当作 input 传上游）
      3) 清理常见不支持字段
      4) 强制 store=false

    注意：
    - o1-mini / o1-preview 场景：优先用 developer message 放进 input，避免依赖 instructions
    """

    # 识别上游原始模型（用于兼容 o1-mini / o1-preview）
    try:
        model_dict = get_model_dict(provider)
        original_model = model_dict.get(getattr(request, "model", None), getattr(request, "model", ""))
    except Exception:
        original_model = getattr(request, "model", "") or ""

    is_o1_mini_like = ("o1-mini" in original_model) or ("o1-preview" in original_model)

    # 渠道 system_prompt
    system_prompt = modifications.get("system_prompt")
    system_prompt_text = str(system_prompt).strip() if system_prompt is not None else ""

    input_items = payload.get("input")
    if not isinstance(input_items, list):
        input_items = None

    def _make_dev_message_item(text: str) -> dict:
        # 尽量跟现有 payload 风格一致
        if input_items and isinstance(input_items, list) and input_items and isinstance(input_items[0], dict) and "type" in input_items[0]:
            return {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": text}]}
        return {"role": "developer", "content": text}

    # 1) o1-mini/o1-preview：system_prompt 尽量塞进 input 作为 developer
    if system_prompt_text and is_o1_mini_like and isinstance(input_items, list):
        # 如果第一个就是 developer/system，尝试拼接；否则插入
        if input_items and isinstance(input_items[0], dict) and input_items[0].get("role") in ("developer", "system"):
            first = input_items[0]
            old = _as_text_from_responses_content(first.get("content")).strip()
            merged = f"{system_prompt_text}\n\n{old}" if old else system_prompt_text

            # 回写 content（尽量保持原结构）
            if isinstance(first.get("content"), list):
                # 找第一个 input_text/text
                wrote = False
                for part in first.get("content"):
                    if isinstance(part, dict) and part.get("type") in ("input_text", "text"):
                        part["type"] = "input_text"
                        part["text"] = merged
                        wrote = True
                        break
                if not wrote:
                    first["content"].insert(0, {"type": "input_text", "text": system_prompt_text})
            else:
                first["content"] = merged

            first["role"] = "developer"
        else:
            input_items.insert(0, _make_dev_message_item(system_prompt_text))

        payload["input"] = input_items

    # 2) 非 o1-mini：system/developer（开头连续）抽到 instructions + system_prompt 注入 instructions
    else:
        extracted_parts = []
        if isinstance(input_items, list) and input_items:
            new_input = list(input_items)
            while new_input:
                first = new_input[0]
                if not isinstance(first, dict):
                    break
                role = first.get("role")
                if role not in ("system", "developer"):
                    break
                extracted_parts.append(_as_text_from_responses_content(first.get("content")).strip())
                new_input.pop(0)
            if len(new_input) != len(input_items):
                payload["input"] = new_input

        extracted_text = "\n\n".join([p for p in extracted_parts if p]).strip()

        old_inst = payload.get("instructions")
        old_inst_text = old_inst.strip() if isinstance(old_inst, str) else ""

        inst_parts = []
        if system_prompt_text:
            inst_parts.append(system_prompt_text)
        if extracted_text:
            inst_parts.append(extracted_text)
        if old_inst_text:
            inst_parts.append(old_inst_text)

        final_inst = "\n\n".join([p for p in inst_parts if p]).strip()
        if final_inst:
            payload["instructions"] = final_inst

    # 3) 清理 Responses API 常见不支持字段（透传也做兜底，避免上游严格校验报错）
    for k in (
        "temperature", "top_p",
        "presence_penalty", "frequency_penalty",
        "n", "logprobs", "top_logprobs",
        "stream_options",
    ):
        payload.pop(k, None)

    # 4) 兼容性：部分上游/网关要求 Responses API 显式设置 store=false，否则会报错
    payload["store"] = False

    return payload


async def get_responses_payload(request, engine, provider, api_key=None):
    """构建 OpenAI Responses API 的请求 payload（对齐 b119589 的稳定实现）"""
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
                    content_items.append(format_input_text(item.text))
                elif getattr(item, "type", None) == "image_url" and provider.get("image", True):
                    image_item = await format_input_image(item.image_url.url)
                    content_items.append(image_item)
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
                    "content": [{"type": "input_text", "text": content or ""}],
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

    # 覆盖配置
    if safe_get(provider, "preferences", "post_body_parameter_overrides", default=None):
        for key, value in safe_get(provider, "preferences", "post_body_parameter_overrides", default={}).items():
            if key == request.model:
                for k, v in value.items():
                    payload[k] = v
            elif all(_model not in request.model.lower() for _model in model_dict.keys()) and "-" not in key and " " not in key:
                payload[key] = value

    # 兼容性：部分上游/网关要求 Responses API 显式设置 store=false，否则会报错
    # （例如："Store must be set to false"）
    payload["store"] = False

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
        passthrough_payload_adapter=patch_passthrough_responses_payload,
        response_adapter=fetch_responses_response,
        stream_adapter=fetch_responses_stream,
        models_adapter=fetch_responses_models,
    )
