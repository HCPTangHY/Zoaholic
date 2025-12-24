"""
GPT/OpenAI 渠道适配器

负责处理 OpenAI 兼容 API 的请求构建和响应流解析
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
    end_of_line,
    upload_image_to_0x0st,
)
from ..response import check_response


# ============================================================
# OpenAI 格式化函数
# ============================================================

def format_text_message(text: str) -> dict:
    """格式化文本消息为 OpenAI 格式"""
    return {"type": "text", "text": text}


async def format_image_message(image_url: str) -> dict:
    """格式化图片消息为 OpenAI 格式"""
    base64_image, _ = await get_base64_image(image_url)
    return {
        "type": "image_url",
        "image_url": {
            "url": base64_image,
        }
    }


async def get_openai_passthrough_meta(request, engine, provider, api_key=None):
    """透传用：仅构建 url/headers，payload 由入口原生请求提供"""
    headers = {
        'Content-Type': 'application/json',
    }
    if api_key:
        headers['Authorization'] = f"Bearer {api_key}"

    base_api = BaseAPI(provider.get('base_url'))
    url = base_api.chat_url
    if "openrouter.ai" in url:
        headers['HTTP-Referer'] = "https://github.com/HCPTangHY/Zoaholic"
        headers['X-Title'] = "Zoaholic"

    return url, headers, {}


async def get_gpt_payload(request, engine, provider, api_key=None):
    """构建 OpenAI 兼容 API 的请求 payload"""
    headers = {
        'Content-Type': 'application/json',
    }
    model_dict = get_model_dict(provider)
    original_model = model_dict[request.model]
    if api_key:
        headers['Authorization'] = f"Bearer {api_key}"
 
    # 这里统一根据 base_url 拼出真正的聊天端点：
    # - 如果传入的是 https://api.openai.com/v1 → 自动补 /chat/completions
    # - 如果传入的是 .../v1/chat/completions 或 .../v1/responses → 原样使用
    base_api = BaseAPI(provider['base_url'])
    url = base_api.chat_url
    if "openrouter.ai" in url:
        headers['HTTP-Referer'] = "https://github.com/HCPTangHY/Zoaholic"
        headers['X-Title'] = "Zoaholic"

    messages = []
    for msg in request.messages:
        tool_calls = None
        tool_call_id = None
        if isinstance(msg.content, list):
            content = []
            for item in msg.content:
                if item.type == "text":
                    text_message = format_text_message(item.text)
                    if "v1/responses" in url:
                        text_message["type"] = "input_text"
                    content.append(text_message)
                elif item.type == "image_url" and provider.get("image", True) and "o1-mini" not in original_model:
                    image_message = await format_image_message(item.image_url.url)
                    if "v1/responses" in url:
                        image_message = {
                            "type": "input_image",
                            "image_url": image_message["image_url"]["url"]
                        }
                    content.append(image_message)
        else:
            content = msg.content
            if msg.role == "system" and "o3-mini" in original_model and not content.startswith("Formatting re-enabled"):
                content = "Formatting re-enabled. " + content
            tool_calls = msg.tool_calls
            tool_call_id = msg.tool_call_id

        if tool_calls:
            tool_calls_list = []
            for tool_call in tool_calls:
                tool_calls_list.append({
                    "id": tool_call.id,
                    "type": tool_call.type,
                    "function": {
                        "name": tool_call.function.name,
                        "arguments": tool_call.function.arguments
                    }
                })
            if provider.get("tools") is not False:
                messages.append({"role": msg.role, "tool_calls": tool_calls_list})
        elif tool_call_id:
            if provider.get("tools"):
                messages.append({"role": msg.role, "tool_call_id": tool_call_id, "content": content})
        else:
            messages.append({"role": msg.role, "content": content})

    if ("o1-mini" in original_model or "o1-preview" in original_model) and len(messages) > 1 and messages[0]["role"] == "system":
        system_msg = messages.pop(0)
        messages[0]["content"] = system_msg["content"] + messages[0]["content"]

    if "v1/responses" in url:
        payload = {
            "model": original_model,
            "input": messages,
        }
    else:
        payload = {
            "model": original_model,
            "messages": messages,
        }

    miss_fields = [
        'model',
        'messages',
    ]

    for field, value in request.model_dump(exclude_unset=True).items():
        if field not in miss_fields and value is not None:
            if field == "max_tokens" and ("o1" in original_model or "o3" in original_model or "o4" in original_model or "gpt-5" in original_model):
                payload["max_completion_tokens"] = value
            else:
                payload[field] = value

    if provider.get("tools") is False or "o1-mini" in original_model or "chatgpt-4o-latest" in original_model or "grok" in original_model:
        payload.pop("tools", None)
        payload.pop("tool_choice", None)

    if "api.x.ai" in url:
        payload.pop("stream_options", None)
        payload.pop("presence_penalty", None)
        payload.pop("frequency_penalty", None)

    if "grok-3-mini" in original_model:
        if request.model.endswith("high"):
            payload["reasoning_effort"] = "high"
        elif request.model.endswith("low"):
            payload["reasoning_effort"] = "low"

    if "o1" in original_model or \
    "o3" in original_model or "o4" in original_model or \
    "gpt-oss" in original_model or "gpt-5" in original_model:
        if request.model.endswith("high"):
            if "v1/responses" in url:
                payload["reasoning"] = {"effort": "high"}
            else:
                payload["reasoning_effort"] = "high"
        elif request.model.endswith("low"):
            if "v1/responses" in url:
                payload["reasoning"] = {"effort": "low"}
            else:
                payload["reasoning_effort"] = "low"

        if "temperature" in payload:
            payload.pop("temperature")

        if "v1/responses" in url:
            payload.pop("stream_options", None)

    # 代码生成/数学解题  0.0
    # 数据抽取/分析	     1.0
    # 通用对话          1.3
    # 翻译	           1.3
    # 创意类写作/诗歌创作 1.5
    if "deepseek-r" in original_model.lower():
        if "temperature" not in payload:
            payload["temperature"] = 0.6

    if request.model.endswith("-search") and "gemini" in original_model:
        if "tools" not in payload:
            payload["tools"] = [{
                "type": "function",
                "function": {
                    "name": "googleSearch",
                    "description": "googleSearch"
                }
            }]
        else:
            if not any(tool["function"]["name"] == "googleSearch" for tool in payload["tools"]):
                payload["tools"].append({
                    "type": "function",
                    "function": {
      "name": "googleSearch",
                        "description": "googleSearch"
                    }
                })

    if safe_get(provider, "preferences", "post_body_parameter_overrides", default=None):
        for key, value in safe_get(provider, "preferences", "post_body_parameter_overrides", default={}).items():
            if key == request.model:
                for k, v in value.items():
                    payload[k] = v
            elif all(_model not in request.model.lower() for _model in model_dict.keys()) and "-" not in key and " " not in key:
                payload[key] = value

    return url, headers, payload


async def fetch_openai_response(client, url, headers, payload, model, timeout):
    """处理 OpenAI 兼容 API 的非流式响应"""
    json_payload = await asyncio.to_thread(json.dumps, payload)
    response = await client.post(url, headers=headers, content=json_payload, timeout=timeout)
    
    error_message = await check_response(response, "fetch_openai_response")
    if error_message:
        yield error_message
        return

    response_bytes = await response.aread()
    response_json = await asyncio.to_thread(json.loads, response_bytes)

    # 兼容原 core/response.py 中的特殊逻辑
    if "dashscope.aliyuncs.com" in url and "multimodal-generation" in url:
        content = safe_get(response_json, "output", "choices", 0, "message", "content", 0, default=None)
        yield content
    elif "embedContent" in url:
        content = safe_get(response_json, "embedding", "values", default=[])
        response_embedContent = {
            "object": "list",
            "data": [
                {
                    "object": "embedding",
                    "embedding": content,
                    "index": 0
                }
            ],
            "model": model,
            "usage": {
                "prompt_tokens": 0,
                "total_tokens": 0
            }
        }
        yield response_embedContent
    else:
        yield response_json


async def fetch_gpt_response_stream(client, url, headers, payload, model, timeout):
    """处理 GPT/OpenAI 流式响应"""
    from ..log_config import logger
    
    timestamp = int(datetime.timestamp(datetime.now()))
    random.seed(timestamp)
    random_str = ''.join(random.choices(string.ascii_letters + string.digits, k=29))
    is_thinking = False
    has_send_thinking = False
    ark_tag = False
    json_payload = await asyncio.to_thread(json.dumps, payload)
    
    async with client.stream('POST', url, headers=headers, content=json_payload, timeout=timeout) as response:
        error_message = await check_response(response, "fetch_gpt_response_stream")
        if error_message:
            yield error_message
            return

        buffer = ""
        enter_buffer = ""

        input_tokens = 0
        output_tokens = 0
        done_received = False

        async for chunk in response.aiter_text():
            buffer += chunk
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                # logger.info("line: %s", repr(line))
                if line.startswith(": keepalive"):
                    yield line + end_of_line
                    continue
                if line and not line.startswith(":") and (result:=line.lstrip("data: ").strip()) and not line.startswith("event: "):
                    if result.strip() == "[DONE]":
                        done_received = True
                        break
                    line = await asyncio.to_thread(json.loads, result)
                    
                    # 检查返回的 JSON 是否包含错误信息
                    if 'error' in line:
                        yield {"error": "OpenAI Stream Error", "status_code": 400, "details": line}
                        return
                        
                    line['id'] = f"chatcmpl-{random_str}"

                    # v1/responses
                    if line.get("type") == "response.reasoning_summary_text.delta" and line.get("delta"):
                        sse_string = await generate_sse_response(timestamp, payload["model"], reasoning_content=line.get("delta"))
                        yield sse_string
                        continue
                    elif line.get("type") == "response.output_text.delta" and line.get("delta"):
                        sse_string = await generate_sse_response(timestamp, payload["model"], content=line.get("delta"))
                        yield sse_string
                        continue
                    elif line.get("type") == "response.output_text.done":
                        sse_string = await generate_sse_response(timestamp, payload["model"], stop="stop")
                        yield sse_string
                        continue
                    elif line.get("type") == "response.completed":
                        input_tokens = safe_get(line, "response", "usage", "input_tokens", default=0)
                        output_tokens = safe_get(line, "response", "usage", "output_tokens", default=0)
                        continue
                    elif line.get("type", "").startswith("response."):
                        continue

                    # 处理 <think> 标签
                    content = safe_get(line, "choices", 0, "delta", "content", default="")
                    if "<think>" in content:
                        is_thinking = True
                        ark_tag = True
                        content = content.replace("<think>", "")
                    if "</think>" in content:
                        end_think_reasoning_content = ""
                        end_think_content = ""
                        is_thinking = False

                        if content.rstrip('\n').endswith("</think>"):
                            end_think_reasoning_content = content.replace("</think>", "").rstrip('\n')
                        elif content.lstrip('\n').startswith("</think>"):
                            end_think_content = content.replace("</think>", "").lstrip('\n')
                        else:
                            end_think_reasoning_content = content.split("</think>")[0]
                            end_think_content = content.split("</think>")[1]

                        if end_think_reasoning_content:
                            sse_string = await generate_sse_response(timestamp, payload["model"], reasoning_content=end_think_reasoning_content)
                            yield sse_string
                        if end_think_content:
                            sse_string = await generate_sse_response(timestamp, payload["model"], content=end_think_content)
                            yield sse_string
                        continue
                    if is_thinking and ark_tag:
                        if not has_send_thinking:
                            content = content.replace("\n\n", "")
                        if content:
                            sse_string = await generate_sse_response(timestamp, payload["model"], reasoning_content=content)
                            yield sse_string
                            has_send_thinking = True
                        continue

                    # 处理 poe thinking 标签
                    if "Thinking..." in content and "\n> " in content:
                        is_thinking = True
                        content = content.replace("Thinking...", "").replace("\n> ", "")
                    if is_thinking and "\n\n" in content and not ark_tag:
                        is_thinking = False
                    if is_thinking and not ark_tag:
                        content = content.replace("\n> ", "")
                        if not has_send_thinking:
                            content = content.replace("\n", "")
                        if content:
                            sse_string = await generate_sse_response(timestamp, payload["model"], reasoning_content=content)
                            yield sse_string
                            has_send_thinking = True
                        continue

                    no_stream_content = safe_get(line, "choices", 0, "message", "content", default=None)
                    openrouter_reasoning = safe_get(line, "choices", 0, "delta", "reasoning", default="")
                    openrouter_base64_image = safe_get(line, "choices", 0, "delta", "images", 0, "image_url", "url", default="")
                    if openrouter_base64_image:
                        image_url = await upload_image_to_0x0st(openrouter_base64_image)
                        sse_string = await generate_sse_response(timestamp, payload["model"], content=f"\n\n![image]({image_url})")
                        yield sse_string
                        continue
                    azure_databricks_claude_summary_content = safe_get(line, "choices", 0, "delta", "content", 0, "summary", 0, "text", default="")
                    azure_databricks_claude_signature_content = safe_get(line, "choices", 0, "delta", "content", 0, "summary", 0, "signature", default="")
                    if azure_databricks_claude_signature_content:
                        pass
                    elif azure_databricks_claude_summary_content:
                        sse_string = await generate_sse_response(timestamp, payload["model"], reasoning_content=azure_databricks_claude_summary_content)
                        yield sse_string
                    elif openrouter_reasoning:
                        if openrouter_reasoning.endswith("\\"):
                            enter_buffer += openrouter_reasoning
                            continue
                        elif enter_buffer.endswith("\\") and openrouter_reasoning == 'n':
                            enter_buffer += "n"
                            continue
                        elif enter_buffer.endswith("\\n") and openrouter_reasoning == '\\n':
                            enter_buffer += "\\n"
                            continue
                        elif enter_buffer.endswith("\\n\\n"):
                            openrouter_reasoning = '\n\n' + openrouter_reasoning
                            enter_buffer = ""
                        elif enter_buffer:
                            openrouter_reasoning = enter_buffer + openrouter_reasoning
                            enter_buffer = ''
                        openrouter_reasoning = openrouter_reasoning.replace("\\n", "\n")

                        sse_string = await generate_sse_response(timestamp, payload["model"], reasoning_content=openrouter_reasoning)
                        yield sse_string
                    elif no_stream_content and has_send_thinking == False:
                        sse_string = await generate_sse_response(safe_get(line, "created", default=None), safe_get(line, "model", default=None), content=no_stream_content)
                        yield sse_string
                    else:
                        if no_stream_content:
                            del line["choices"][0]["message"]
                        json_line = await asyncio.to_thread(json.dumps, line)
                        yield "data: " + json_line.strip() + end_of_line
            
            if done_received:
                break

    if input_tokens and output_tokens:
        sse_string = await generate_sse_response(timestamp, payload["model"], None, None, None, None, None, total_tokens=input_tokens + output_tokens, prompt_tokens=input_tokens, completion_tokens=output_tokens)
        yield sse_string

    yield "data: [DONE]" + end_of_line


async def fetch_openai_models(client, provider):
    """获取 OpenAI 兼容 API 的模型列表"""
    base_url = provider.get('base_url', 'https://api.openai.com/v1').rstrip('/')
    api_key = provider.get('api')
    if isinstance(api_key, list):
        api_key = api_key[0] if api_key else None
    
    headers = {'Content-Type': 'application/json'}
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'
    
    url = f"{base_url}/models"
    response = await client.get(url, headers=headers)
    response.raise_for_status()
    
    data = response.json()
    models = []
    if isinstance(data, dict) and 'data' in data:
        models = [m.get('id') for m in data['data'] if m.get('id')]
    elif isinstance(data, list):
        models = [m.get('id') if isinstance(m, dict) else m for m in data]
    
    return models


def register():
    """注册 GPT 渠道到注册中心"""
    from .registry import register_channel
    
    register_channel(
        id="openai",
        type_name="openai",
        default_base_url="https://api.openai.com/v1",
        auth_header="Authorization: Bearer {api_key}",
        description="OpenAI 兼容 API",
        request_adapter=get_gpt_payload,
        passthrough_adapter=get_openai_passthrough_meta,
        response_adapter=fetch_openai_response,
        stream_adapter=fetch_gpt_response_stream,
        models_adapter=fetch_openai_models,
    )
