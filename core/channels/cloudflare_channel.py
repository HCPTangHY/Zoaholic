"""
Cloudflare Workers AI 渠道适配器

负责处理 Cloudflare Workers AI API 的请求构建和响应流解析
"""

import json
import asyncio
from datetime import datetime

from ..utils import (
    get_model_dict,
    get_text_message,
    get_image_message,
    generate_sse_response,
    end_of_line,
)
from ..response import check_response


async def get_cloudflare_payload(request, engine, provider, api_key=None):
    """构建 Cloudflare Workers AI API 的请求 payload"""
    model_dict = get_model_dict(provider)
    original_model = model_dict[request.model]
    
    headers = {
        'Content-Type': 'application/json',
    }
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'
    
    # Cloudflare Workers AI URL 格式
    account_id = provider.get("account_id", "")
    base_url = provider.get("base_url", f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai")
    url = base_url.rstrip('/') + f"/run/{original_model}"
    
    messages = []
    for msg in request.messages:
        if isinstance(msg.content, list):
            content = []
            for item in msg.content:
                if item.type == "text":
                    text_message = await get_text_message(item.text, engine)
                    content.append(text_message)
                elif item.type == "image_url" and provider.get("image", True):
                    image_message = await get_image_message(item.image_url.url, engine)
                    content.append(image_message)
            # Cloudflare 使用简单的 content 字符串
            text_content = " ".join([c.get("text", "") for c in content if c.get("type") == "text"])
            messages.append({"role": msg.role, "content": text_content})
        else:
            messages.append({"role": msg.role, "content": msg.content})
    
    payload = {
        "messages": messages,
        "stream": request.stream,
    }
    
    # 可选参数
    if request.max_tokens:
        payload["max_tokens"] = request.max_tokens
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if request.top_p is not None:
        payload["top_p"] = request.top_p
    
    return url, headers, payload


async def fetch_cloudflare_response_stream(client, url, headers, payload, model, timeout):
    """处理 Cloudflare Workers AI 流式响应"""
    from ..log_config import logger
    
    timestamp = int(datetime.timestamp(datetime.now()))
    json_payload = await asyncio.to_thread(json.dumps, payload)
    
    async with client.stream('POST', url, headers=headers, content=json_payload, timeout=timeout) as response:
        error_message = await check_response(response, "fetch_cloudflare_response_stream")
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
                        response_text = json_data.get("response", "")
                        
                        if response_text:
                            sse_string = await generate_sse_response(timestamp, model, content=response_text)
                            yield sse_string
                    except json.JSONDecodeError:
                        logger.error(f"无法解析JSON: {line}")
    
    yield "data: [DONE]" + end_of_line


def register():
    """注册 Cloudflare Workers AI 渠道到注册中心"""
    from .registry import register_channel
    
    register_channel(
        id="cloudflare",
        type_name="cloudflare",
        default_base_url="https://api.cloudflare.com/client/v4/accounts/{account_id}/ai",
        auth_header="Authorization: Bearer {api_key}",
        description="Cloudflare Workers AI",
        request_adapter=get_cloudflare_payload,
        stream_adapter=fetch_cloudflare_response_stream,
    )