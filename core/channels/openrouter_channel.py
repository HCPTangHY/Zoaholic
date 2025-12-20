"""
OpenRouter 渠道适配器

负责处理 OpenRouter API 的请求构建和响应流解析
"""

import json
import asyncio
from datetime import datetime

from ..utils import (
    get_model_dict,
    get_base64_image,
    generate_sse_response,
    end_of_line,
    safe_get,
)
from ..response import check_response


# ============================================================
# OpenRouter 格式化函数
# ============================================================

def format_text_message(text: str) -> dict:
    """格式化文本消息为 OpenRouter 格式"""
    return {"type": "text", "text": text}


async def format_image_message(image_url: str) -> dict:
    """格式化图片消息为 OpenRouter 格式"""
    base64_image, _ = await get_base64_image(image_url)
    return {
        "type": "image_url",
        "image_url": {
            "url": base64_image,
        }
    }


async def get_openrouter_payload(request, engine, provider, api_key=None):
    """构建 OpenRouter API 的请求 payload"""
    model_dict = get_model_dict(provider)
    original_model = model_dict[request.model]
    
    headers = {
        'Content-Type': 'application/json',
    }
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'
    
    url = provider.get("base_url", "https://openrouter.ai/api/v1").rstrip('/') + "/chat/completions"
    
    messages = []
    for msg in request.messages:
        if isinstance(msg.content, list):
            content = []
            for item in msg.content:
                if item.type == "text":
                    text_message = format_text_message(item.text)
                    content.append(text_message)
                elif item.type == "image_url" and provider.get("image", True):
                    image_message = await format_image_message(item.image_url.url)
                    content.append(image_message)
            messages.append({"role": msg.role, "content": content})
        else:
            messages.append({"role": msg.role, "content": msg.content})
    
    payload = {
        "model": original_model,
        "messages": messages,
        "stream": request.stream,
    }
    
    miss_fields = [
        'model',
        'messages',
    ]
    
    for field, value in request.model_dump(exclude_unset=True).items():
        if field not in miss_fields and value is not None:
            payload[field] = value
    
    # OpenRouter 特定参数
    if safe_get(provider, "preferences", "transforms"):
        payload["transforms"] = safe_get(provider, "preferences", "transforms")
    
    if safe_get(provider, "preferences", "route"):
        payload["route"] = safe_get(provider, "preferences", "route")
    
    return url, headers, payload


async def fetch_openrouter_response(client, url, headers, payload, model, timeout):
    """处理 OpenRouter 非流式响应"""
    json_payload = await asyncio.to_thread(json.dumps, payload)
    response = await client.post(url, headers=headers, content=json_payload, timeout=timeout)
    
    error_message = await check_response(response, "fetch_openrouter_response")
    if error_message:
        yield error_message
        return

    response_bytes = await response.aread()
    response_json = await asyncio.to_thread(json.loads, response_bytes)
    yield response_json


async def fetch_openrouter_response_stream(client, url, headers, payload, model, timeout):
    """处理 OpenRouter 流式响应"""
    from ..log_config import logger
    
    timestamp = int(datetime.timestamp(datetime.now()))
    json_payload = await asyncio.to_thread(json.dumps, payload)
    
    async with client.stream('POST', url, headers=headers, content=json_payload, timeout=timeout) as response:
        error_message = await check_response(response, "fetch_openrouter_response_stream")
        if error_message:
            yield error_message
            return
        
        buffer = ""
        async for chunk in response.aiter_text():
            buffer += chunk
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                
                if not line:
                    continue
                
                if line == "data: [DONE]":
                    break
                
                if line.startswith("data: "):
                    try:
                        json_data = json.loads(line[6:])
                        choices = json_data.get("choices", [])
                        if choices:
                            delta = choices[0].get("delta", {})
                            content = delta.get("content", "")
                            
                            if content:
                                sse_string = await generate_sse_response(timestamp, model, content=content)
                                yield sse_string
                            
                            # 处理 function call
                            tool_calls = delta.get("tool_calls")
                            if tool_calls:
                                tool_call = tool_calls[0]
                                function = tool_call.get("function", {})
                                if tool_call.get("id"):
                                    sse_string = await generate_sse_response(
                                        timestamp, model, content=None,
                                        tools_id=tool_call["id"],
                                        function_call_name=function.get("name")
                                    )
                                    yield sse_string
                                if function.get("arguments"):
                                    sse_string = await generate_sse_response(
                                        timestamp, model, content=None,
                                        tools_id=tool_call.get("id"),
                                        function_call_content=function["arguments"]
                                    )
                                    yield sse_string
                            
                            # 检查是否结束
                            finish_reason = choices[0].get("finish_reason")
                            if finish_reason:
                                usage = json_data.get("usage", {})
                                sse_string = await generate_sse_response(
                                    timestamp, model, None, None, None, None, None,
                                    usage.get("total_tokens", 0),
                                    usage.get("prompt_tokens", 0),
                                    usage.get("completion_tokens", 0)
                                )
                                yield sse_string
                    except json.JSONDecodeError:
                        logger.error(f"无法解析JSON: {line}")
    
    yield "data: [DONE]" + end_of_line


async def fetch_openrouter_models(client, provider):
    """获取 OpenRouter 可用模型列表"""
    base_url = provider.get("base_url", "https://openrouter.ai/api/v1").rstrip('/')
    api_key = provider.get("api_key", "")
    
    headers = {
        'Content-Type': 'application/json',
    }
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'
    
    try:
        response = await client.get(f"{base_url}/models", headers=headers)
        if response.status_code == 200:
            data = response.json()
            models = data.get("data", [])
            return [model.get("id", "") for model in models if model.get("id")]
    except Exception:
        pass
    return []


def register():
    """注册 OpenRouter 渠道到注册中心"""
    from .registry import register_channel
    
    register_channel(
        id="openrouter",
        type_name="openrouter",
        default_base_url="https://openrouter.ai/api/v1",
        auth_header="Authorization: Bearer {api_key}",
        description="OpenRouter (Multi-provider gateway)",
        request_adapter=get_openrouter_payload,
        response_adapter=fetch_openrouter_response,
        stream_adapter=fetch_openrouter_response_stream,
        models_adapter=fetch_openrouter_models,
    )
