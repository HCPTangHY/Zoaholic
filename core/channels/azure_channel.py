"""
Azure OpenAI 渠道适配器

负责处理 Azure OpenAI API 的请求构建和响应流解析
"""

import json
import asyncio
import urllib.parse
from datetime import datetime

from ..utils import (
    safe_get,
    get_model_dict,
    get_base64_image,
    get_tools_mode,
    generate_sse_response,
    end_of_line,
)
from ..response import check_response


# ============================================================
# Azure OpenAI 格式化函数
# ============================================================

def format_text_message(text: str) -> dict:
    """格式化文本消息为 Azure OpenAI 格式"""
    return {"type": "text", "text": text}


async def format_image_message(image_url: str) -> dict:
    """格式化图片消息为 Azure OpenAI 格式"""
    base64_image, _ = await get_base64_image(image_url)
    return {
        "type": "image_url",
        "image_url": {
            "url": base64_image,
        }
    }


def build_azure_endpoint(base_url, deployment_id, api_version="2025-01-01-preview"):
    """构建 Azure OpenAI 端点 URL"""
    # 移除base_url末尾的斜杠(如果有)
    base_url = base_url.rstrip('/')
    final_url = base_url

    if "models/chat/completions" not in final_url:
        # 构建路径
        path = f"/openai/deployments/{deployment_id}/chat/completions"
        # 使用urljoin拼接base_url和path
        final_url = urllib.parse.urljoin(base_url, path)

    if "?api-version=" not in final_url:
        # 添加api-version查询参数
        final_url = f"{final_url}?api-version={api_version}"

    return final_url


async def get_azure_payload(request, engine, provider, api_key=None):
    """构建 Azure OpenAI API 的请求 payload"""
    headers = {
        'Content-Type': 'application/json',
    }
    model_dict = get_model_dict(provider)
    original_model = model_dict[request.model]
    headers['api-key'] = f"{api_key}"

    url = build_azure_endpoint(
        base_url=provider['base_url'],
        deployment_id=original_model,
    )

    messages = []
    for msg in request.messages:
        tool_calls = None
        tool_call_id = None
        if isinstance(msg.content, list):
            content = []
            for item in msg.content:
                if item.type == "text":
                    text_message = format_text_message(item.text)
                    content.append(text_message)
                elif item.type == "image_url" and provider.get("image", True) and "o1-mini" not in original_model:
                    image_message = await format_image_message(item.image_url.url)
                    content.append(image_message)
        else:
            content = msg.content
            tool_calls = msg.tool_calls
            tool_call_id = msg.tool_call_id

        if tool_calls:
            tools_mode = get_tools_mode(provider)
            if tools_mode != "none":
                tool_calls_list = []
                # 根据 tools_mode 决定处理多少个工具调用
                calls_to_process = tool_calls if tools_mode == "parallel" else tool_calls[:1]
                for tool_call in calls_to_process:
                    tool_calls_list.append({
                        "id": tool_call.id,
                        "type": tool_call.type,
                        "function": {
                            "name": tool_call.function.name,
                            "arguments": tool_call.function.arguments
                        }
                    })
                messages.append({"role": msg.role, "tool_calls": tool_calls_list})
        elif tool_call_id:
            tools_mode = get_tools_mode(provider)
            if tools_mode != "none":
                messages.append({"role": msg.role, "tool_call_id": tool_call_id, "content": content})
        else:
            messages.append({"role": msg.role, "content": content})

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
            if field == "max_tokens" and "o1" in original_model:
                payload["max_completion_tokens"] = value
            else:
                payload[field] = value

    tools_mode = get_tools_mode(provider)
    if tools_mode == "none" or "o1" in original_model or "chatgpt-4o-latest" in original_model or "grok" in original_model:
        payload.pop("tools", None)
        payload.pop("tool_choice", None)

    if safe_get(provider, "preferences", "post_body_parameter_overrides", default=None):
        for key, value in safe_get(provider, "preferences", "post_body_parameter_overrides", default={}).items():
            if key == request.model:
                for k, v in value.items():
                    payload[k] = v
            elif all(_model not in request.model.lower() for _model in model_dict.keys()) and "-" not in key and " " not in key:
                payload[key] = value

    return url, headers, payload


async def fetch_azure_response(client, url, headers, payload, model, timeout):
    """处理 Azure OpenAI 非流式响应"""
    json_payload = await asyncio.to_thread(json.dumps, payload)
    response = await client.post(url, headers=headers, content=json_payload, timeout=timeout)
    
    error_message = await check_response(response, "fetch_azure_response")
    if error_message:
        yield error_message
        return

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


async def fetch_azure_response_stream(client, url, headers, payload, model, timeout):
    """处理 Azure OpenAI 流式响应"""
    timestamp = int(datetime.timestamp(datetime.now()))
    is_thinking = False
    has_send_thinking = False
    ark_tag = False
    json_payload = await asyncio.to_thread(json.dumps, payload)
    async with client.stream('POST', url, headers=headers, content=json_payload, timeout=timeout) as response:
        error_message = await check_response(response, "fetch_azure_response_stream")
        if error_message:
            yield error_message
            return

        buffer = ""
        sse_string = ""
        async for chunk in response.aiter_text():
            buffer += chunk
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                if line and not line.startswith(":") and (result:=line.lstrip("data: ").strip()):
                    if result.strip() == "[DONE]":
                        break
                    line = await asyncio.to_thread(json.loads, result)
                    no_stream_content = safe_get(line, "choices", 0, "message", "content", default="")
                    content = safe_get(line, "choices", 0, "delta", "content", default="")

                    # 处理 <think> 标签
                    if "<think>" in content:
                        is_thinking = True
                        ark_tag = True
                        content = content.replace("<think>", "")
                    if "</think>" in content:
                        is_thinking = False
                        content = content.replace("</think>", "")
                        if not content:
                            continue
                    if is_thinking and ark_tag:
                        if not has_send_thinking:
                            content = content.replace("\n\n", "")
                        if content:
                            sse_string = await generate_sse_response(timestamp, payload["model"], reasoning_content=content)
                            yield sse_string
                            has_send_thinking = True
                        continue

                    if no_stream_content or content or sse_string:
                        input_tokens = safe_get(line, "usage", "prompt_tokens", default=0)
                        output_tokens = safe_get(line, "usage", "completion_tokens", default=0)
                        total_tokens = safe_get(line, "usage", "total_tokens", default=0)
                        sse_string = await generate_sse_response(timestamp, safe_get(line, "model", default=None), content=no_stream_content or content, total_tokens=total_tokens, prompt_tokens=input_tokens, completion_tokens=output_tokens)
                        yield sse_string
                    else:
                        if no_stream_content:
                            del line["choices"][0]["message"]
                        json_line = await asyncio.to_thread(json.dumps, line)
                        yield "data: " + json_line.strip() + end_of_line
    yield "data: [DONE]" + end_of_line


def register():
    """注册 Azure 渠道到注册中心"""
    from .registry import register_channel
    
    register_channel(
        id="azure",
        type_name="azure",
        default_base_url="https://{resource}.openai.azure.com",
        auth_header="api-key: {api_key}",
        description="Azure OpenAI Service",
        request_adapter=get_azure_payload,
        response_adapter=fetch_azure_response,
        stream_adapter=fetch_azure_response_stream,
        models_adapter=None,
    )
