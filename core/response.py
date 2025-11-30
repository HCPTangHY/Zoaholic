"""
响应处理模块

负责处理 API 响应的流式和非流式数据
所有流式响应渠道通过 channels 模块的注册中心获取适配器
"""

import json
import asyncio
from datetime import datetime

from .log_config import logger
from .utils import safe_get, generate_sse_response, generate_no_stream_response, end_of_line


async def check_response(response, error_log):
    """
    检查 HTTP 响应状态码，如果不是 2xx 则返回错误信息
    
    Args:
        response: httpx 响应对象
        error_log: 错误日志前缀
        
    Returns:
        dict 或 None: 如果有错误返回错误字典，否则返回 None
    """
    if response and not (200 <= response.status_code < 300):
        error_message = await response.aread()
        error_str = error_message.decode('utf-8', errors='replace')
        try:
            error_json = await asyncio.to_thread(json.loads, error_str)
        except json.JSONDecodeError:
            error_json = error_str
        return {"error": f"{error_log} HTTP Error", "status_code": response.status_code, "details": error_json}
    return None


async def fetch_response(client, url, headers, payload, engine, model, timeout=200):
    """
    处理非流式 API 响应
    
    Args:
        client: httpx 异步客户端
        url: 请求 URL
        headers: 请求头
        payload: 请求体
        engine: 引擎类型
        model: 模型名称
        timeout: 超时时间
        
    Yields:
        响应数据
    """
    response = None
    if payload.get("file"):
        file = payload.pop("file")
        response = await client.post(url, headers=headers, data=payload, files={"file": file}, timeout=timeout)
    else:
        json_payload = await asyncio.to_thread(json.dumps, payload)
        response = await client.post(url, headers=headers, content=json_payload, timeout=timeout)
    error_message = await check_response(response, "fetch_response")
    if error_message:
        yield error_message
        return

    if engine == "tts":
        yield response.read()

    elif engine == "gemini" or engine == "vertex-gemini" or engine == "aws":
        response_bytes = await response.aread()
        response_json = await asyncio.to_thread(json.loads, response_bytes)

        if isinstance(response_json, str):
            import ast
            parsed_data = ast.literal_eval(str(response_json))
        elif isinstance(response_json, list):
            parsed_data = response_json
        elif isinstance(response_json, dict):
            parsed_data = [response_json]
        else:
            logger.error(f"error fetch_response: Unknown response_json type: {type(response_json)}")
            parsed_data = response_json

        content = ""
        reasoning_content = ""
        image_base64 = ""
        parts_list = safe_get(parsed_data, 0, "candidates", 0, "content", "parts", default=[])
        for item in parts_list:
            chunk = safe_get(item, "text")
            b64_json = safe_get(item, "inlineData", "data", default="")
            if b64_json:
                image_base64 = b64_json
            is_think = safe_get(item, "thought", default=False)
            if chunk:
                if is_think:
                    reasoning_content += chunk
                else:
                    content += chunk

        usage_metadata = safe_get(parsed_data, -1, "usageMetadata")
        prompt_tokens = safe_get(usage_metadata, "promptTokenCount", default=0)
        candidates_tokens = safe_get(usage_metadata, "candidatesTokenCount", default=0)
        total_tokens = safe_get(usage_metadata, "totalTokenCount", default=0)

        role = safe_get(parsed_data, -1, "candidates", 0, "content", "role")
        if role == "model":
            role = "assistant"
        else:
            logger.error(f"Unknown role: {role}, parsed_data: {parsed_data}")
            role = "assistant"

        has_think = safe_get(parsed_data, 0, "candidates", 0, "content", "parts", 0, "thought", default=False)
        if has_think:
            function_message_parts_index = -1
        else:
            function_message_parts_index = 0
        function_call_name = safe_get(parsed_data, -1, "candidates", 0, "content", "parts", function_message_parts_index, "functionCall", "name", default=None)
        function_call_content = safe_get(parsed_data, -1, "candidates", 0, "content", "parts", function_message_parts_index, "functionCall", "args", default=None)

        timestamp = int(datetime.timestamp(datetime.now()))
        yield await generate_no_stream_response(timestamp, model, content=content, tools_id=None, function_call_name=function_call_name, function_call_content=function_call_content, role=role, total_tokens=total_tokens, prompt_tokens=prompt_tokens, completion_tokens=candidates_tokens, reasoning_content=reasoning_content, image_base64=image_base64)

    elif engine == "claude" or engine == "vertex-claude":
        response_bytes = await response.aread()
        response_json = await asyncio.to_thread(json.loads, response_bytes)

        content = safe_get(response_json, "content", 0, "text")

        prompt_tokens = safe_get(response_json, "usage", "input_tokens")
        output_tokens = safe_get(response_json, "usage", "output_tokens")
        total_tokens = prompt_tokens + output_tokens

        role = safe_get(response_json, "role")

        function_call_name = safe_get(response_json, "content", 1, "name", default=None)
        function_call_content = safe_get(response_json, "content", 1, "input", default=None)
        tools_id = safe_get(response_json, "content", 1, "id", default=None)

        timestamp = int(datetime.timestamp(datetime.now()))
        yield await generate_no_stream_response(timestamp, model, content=content, tools_id=tools_id, function_call_name=function_call_name, function_call_content=function_call_content, role=role, total_tokens=total_tokens, prompt_tokens=prompt_tokens, completion_tokens=output_tokens)

    elif engine == "azure":
        response_bytes = await response.aread()
        response_json = await asyncio.to_thread(json.loads, response_bytes)
        # 删除 content_filter_results
        if "choices" in response_json:
            for choice in response_json["choices"]:
                if "content_filter_results" in choice:
                    del choice["content_filter_results"]

        # 删除 prompt_filter_results
        if "prompt_filter_results" in response_json:
            del response_json["prompt_filter_results"]

        yield response_json

    elif "dashscope.aliyuncs.com" in url and "multimodal-generation" in url:
        response_bytes = await response.aread()
        response_json = await asyncio.to_thread(json.loads, response_bytes)
        content = safe_get(response_json, "output", "choices", 0, "message", "content", 0, default=None)
        yield content

    elif "embedContent" in url:
        response_bytes = await response.aread()
        response_json = await asyncio.to_thread(json.loads, response_bytes)
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
        response_bytes = await response.aread()
        response_json = await asyncio.to_thread(json.loads, response_bytes)
        yield response_json


async def fetch_response_stream(client, url, headers, payload, engine, model, timeout=200):
    """
    通过渠道注册中心获取流式响应适配器并处理响应流
    
    Args:
        client: httpx 异步客户端
        url: 请求 URL
        headers: 请求头
        payload: 请求体
        engine: 引擎类型
        model: 模型名称
        timeout: 超时时间
        
    Yields:
        SSE 格式的响应数据
    """
    from .channels import get_channel
    
    channel = get_channel(engine)
    if channel and channel.stream_adapter:
        async for chunk in channel.stream_adapter(client, url, headers, payload, model, timeout):
            yield chunk
        return
    
    raise ValueError(f"Unknown engine: {engine}")
