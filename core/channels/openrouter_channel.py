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
from ..json_utils import json_loads, json_dumps_text
from ..response_context import mark_adapter_metrics_managed, mark_content_start, merge_usage
from ..stream_utils import aiter_decoded_lines
from ..usage import extract_cache_usage


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
    
    from ..utils import resolve_base_url
    url = resolve_base_url(provider.get("base_url", "https://openrouter.ai/api/v1"), "/chat/completions")
    
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


async def get_openrouter_passthrough_meta(request, engine, provider, api_key=None):
    """透传用：仅构建 url/headers，payload 由入口原生请求提供"""
    headers = {
        'Content-Type': 'application/json',
    }
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'

    from ..utils import resolve_base_url
    url = resolve_base_url(provider.get("base_url", "https://openrouter.ai/api/v1"), "/chat/completions")

    # OpenRouter 特定头
    headers['HTTP-Referer'] = "https://github.com/HCPTangHY/Zoaholic"
    headers['X-Title'] = "Zoaholic"

    return url, headers, {}


async def patch_passthrough_openrouter_payload(
    payload: dict,
    modifications: dict,
    request,
    engine: str,
    provider: dict,
    api_key=None,
) -> dict:
    """透传模式下对 OpenRouter payload 做渠道级修饰（system_prompt 注入）。"""
    system_prompt = modifications.get("system_prompt")
    system_prompt_text = str(system_prompt).strip() if system_prompt is not None else ""
    if not system_prompt_text:
        return payload

    # OpenRouter 使用 OAI 兼容格式，system_prompt 注入到 messages 数组
    messages = payload.get("messages")
    if isinstance(messages, list):
        for msg in messages:
            if isinstance(msg, dict) and msg.get("role") == "system":
                content = msg.get("content")
                if isinstance(content, str):
                    msg["content"] = f"{system_prompt_text}\n\n{content}" if content else system_prompt_text
                elif isinstance(content, list):
                    if content and isinstance(content[0], dict) and "text" in content[0]:
                        old = content[0].get("text") or ""
                        content[0]["text"] = f"{system_prompt_text}\n\n{old}" if old else system_prompt_text
                    else:
                        content.insert(0, {"type": "text", "text": system_prompt_text})
                else:
                    msg["content"] = system_prompt_text
                return payload
        messages.insert(0, {"role": "system", "content": system_prompt_text})
        return payload

    return payload


async def fetch_openrouter_response(client, url, headers, payload, model, timeout):
    """处理 OpenRouter 非流式响应"""
    json_payload = await asyncio.to_thread(json_dumps_text, payload)
    response = await client.post(url, headers=headers, content=json_payload, timeout=timeout)
    
    error_message = await check_response(response, "fetch_openrouter_response")
    if error_message:
        yield error_message
        return

    response_bytes = await response.aread()
    response_json = await asyncio.to_thread(json_loads, response_bytes)
    mark_adapter_metrics_managed()
    usage = response_json.get("usage", {}) if isinstance(response_json, dict) else {}
    # OpenRouter 透出 OpenAI 兼容 usage 时也可能包含缓存命中字段，统一写入日志统计。
    merge_usage(
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        total_tokens=usage.get("total_tokens", 0),
        **extract_cache_usage(usage),
    )
    if safe_get(response_json, "choices", 0, "message", "content", default=None):
        mark_content_start()
    yield response_json


async def fetch_openrouter_response_stream(client, url, headers, payload, model, timeout):
    """处理 OpenRouter 流式响应"""
    from ..log_config import logger
    
    timestamp = int(datetime.timestamp(datetime.now()))
    json_payload = await asyncio.to_thread(json_dumps_text, payload)
    
    async with client.stream('POST', url, headers=headers, content=json_payload, timeout=timeout) as response:
        error_message = await check_response(response, "fetch_openrouter_response_stream")
        if error_message:
            yield error_message
            return
        mark_adapter_metrics_managed()
        
        async for line in aiter_decoded_lines(response.aiter_bytes()):
                line = line.strip()
                
                if not line:
                    continue
                
                if line == "data: [DONE]":
                    break
                
                if line.startswith("data: "):
                    try:
                        json_data = json_loads(line[6:])
                        choices = json_data.get("choices", [])
                        if choices:
                            delta = choices[0].get("delta", {})
                            
                            # 处理 reasoning 思维链（OpenRouter 新格式）
                            reasoning_text = delta.get("reasoning", "")
                            if not reasoning_text:
                                reasoning_details = delta.get("reasoning_details")
                                if reasoning_details and isinstance(reasoning_details, list):
                                    parts = []
                                    for item in reasoning_details:
                                        if isinstance(item, dict) and item.get("text"):
                                            parts.append(item["text"])
                                    reasoning_text = "".join(parts)
                            
                            if reasoning_text:
                                mark_content_start()
                                sse_string = await generate_sse_response(timestamp, model, reasoning_content=reasoning_text)
                                yield sse_string

                            content = delta.get("content", "")
                            
                            if content:
                                mark_content_start()
                                sse_string = await generate_sse_response(timestamp, model, content=content)
                                yield sse_string
                            
                            # 处理 function call
                            tool_calls = delta.get("tool_calls")
                            if tool_calls:
                                tool_call = tool_calls[0]
                                function = tool_call.get("function", {})
                                if tool_call.get("id"):
                                    mark_content_start()
                                    sse_string = await generate_sse_response(
                                        timestamp, model, content=None,
                                        tools_id=tool_call["id"],
                                        function_call_name=function.get("name")
                                    )
                                    yield sse_string
                                if function.get("arguments"):
                                    mark_content_start()
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
                                cache_usage = extract_cache_usage(usage)
                                # 流式结束 chunk 中的缓存字段需要在生成 usage SSE 前写入 current_info，并继续返回给下游。
                                merge_usage(
                                    prompt_tokens=usage.get("prompt_tokens", 0),
                                    completion_tokens=usage.get("completion_tokens", 0),
                                    total_tokens=usage.get("total_tokens", 0),
                                    **cache_usage,
                                )
                                sse_string = await generate_sse_response(
                                    timestamp, model, None, None, None, None, None,
                                    usage.get("total_tokens", 0),
                                    usage.get("prompt_tokens", 0),
                                    usage.get("completion_tokens", 0),
                                    cached_tokens=cache_usage["cached_tokens"],
                                    cache_creation_tokens=cache_usage["cache_creation_tokens"],
                                )
                                yield sse_string
                    except json.JSONDecodeError:
                        logger.error(f"无法解析JSON: {line}")
    
    yield "data: [DONE]" + end_of_line


async def fetch_openrouter_models(client, provider):
    """获取 OpenRouter 可用模型列表"""
    raw_base_url = provider.get("base_url", "https://openrouter.ai/api/v1")
    api_key = provider.get("api_key", "")
    
    headers = {
        'Content-Type': 'application/json',
    }
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'
    
    from ..utils import resolve_base_url
    try:
        response = await client.get(resolve_base_url(raw_base_url, "/models"), headers=headers)
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
        passthrough_adapter=get_openrouter_passthrough_meta,
        passthrough_payload_adapter=patch_passthrough_openrouter_payload,
        response_adapter=fetch_openrouter_response,
        stream_adapter=fetch_openrouter_response_stream,
        models_adapter=fetch_openrouter_models,
    )
