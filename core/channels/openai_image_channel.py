"""
OpenAI Image Generation 渠道适配器

负责将 Chat Completions 格式的请求转换为 OpenAI Image API 调用，
并将生图结果转换回 Chat Completions 格式的响应。

主要功能：
- 从 messages 中提取 prompt（最后一条 user 消息）
- 构建 /v1/images/generations 请求 payload
- 将 b64_json 响应转换为 inline markdown base64 图片
- 支持 gpt-image-2 的新字段：quality, output_format, background 等
"""

import random
import string
import asyncio
from datetime import datetime

from ..utils import (
    get_model_dict,
    generate_sse_response,
    generate_chunked_image_md,
    end_of_line,
    resolve_base_url,
)
from ..response import check_response
from ..json_utils import json_loads, json_dumps_text
from ..response_context import mark_adapter_metrics_managed, mark_content_start, merge_usage


# ============================================================
# 工具函数
# ============================================================


def _extract_prompt_and_images(request) -> tuple:
    """
    从 RequestModel 的 messages 中提取 prompt 文本和图片数据。

    策略：取最后一条 user 消息的文本内容作为 prompt，
    图片附件提取为 base64 数据（用于图片编辑场景）。

    Returns:
        (prompt, images): prompt 是字符串，images 是 base64 字符串列表
    """
    prompt = ""
    images = []

    # 倒序找最后一条 user 消息
    for msg in reversed(request.messages):
        if msg.role != "user":
            continue

        if isinstance(msg.content, str):
            prompt = msg.content
        elif isinstance(msg.content, list):
            text_parts = []
            for item in msg.content:
                item_type = getattr(item, "type", None)
                if item_type == "text" and getattr(item, "text", None):
                    text_parts.append(item.text)
                elif item_type == "image_url" and getattr(item, "image_url", None):
                    url = item.image_url.url if hasattr(item.image_url, "url") else str(item.image_url)
                    images.append(url)
            prompt = "\n".join(text_parts)
        break

    return prompt.strip(), images


def _extract_image_params(request, provider) -> dict:
    """
    从 request 对象中提取 Image API 专有参数。

    三层来源（优先级从低到高）：
    1. 渠道默认值（此函数硬编码）
    2. Provider overrides（由 request.py 的 _deep_merge 统一处理，不在此函数内）
    3. 用户 per-request 传参（从 request 对象的额外字段中提取）

    Returns:
        包含 Image API 参数的字典
    """
    params = {}

    # 尝试从 request 对象中提取 Image API 字段
    # RequestModel 有 model_dump(exclude_unset=True)，额外字段会保留
    try:
        request_dict = request.model_dump(exclude_unset=True)
    except Exception:
        request_dict = {}

    # Image API 支持的参数列表
    image_fields = [
        "quality", "size", "output_format", "output_compression",
        "background", "moderation", "n", "style",
    ]

    for field in image_fields:
        val = request_dict.get(field)
        if val is not None:
            params[field] = val

    return params


# ============================================================
# 请求构建
# ============================================================


async def get_image_payload(request, engine, provider, api_key=None):
    """构建 OpenAI Image API 的请求 payload"""
    headers = {
        'Content-Type': 'application/json',
    }
    if api_key:
        headers['Authorization'] = f"Bearer {api_key}"

    model_dict = get_model_dict(provider)
    original_model = model_dict[request.model]

    base_url = provider.get('base_url', 'https://api.openai.com/v1')
    # 归一化：去除可能的 /chat/completions 等后缀，只保留到 /v1
    for suffix in ('/chat/completions', '/images/generations', '/images/edits', '/responses'):
        if base_url.rstrip('/').endswith(suffix.rstrip('/')):
            base_url = base_url.rstrip('/')[:-len(suffix.rstrip('/'))]
            break

    # 从 messages 提取 prompt
    prompt, images = _extract_prompt_and_images(request)
    if not prompt:
        prompt = "Generate an image"  # fallback

    # 有图片附件 → 编辑模式（/images/edits），否则 → 生成模式（/images/generations）
    is_edit = bool(images)
    if is_edit:
        url = resolve_base_url(base_url, '/images/edits')
    else:
        url = resolve_base_url(base_url, '/images/generations')

    # 构建 payload
    # gpt-image-2 默认且只返回 b64_json，不认 response_format 参数
    payload = {
        "model": original_model,
        "prompt": prompt,
    }

    # 合并用户传的 Image API 参数
    image_params = _extract_image_params(request, provider)
    payload.update(image_params)

    # 编辑模式：将图片转为 edits API 的 images 数组格式
    # GPT Image 模型支持 JSON body，images 是 [{image_url: "data:..."}, ...] 数组
    if is_edit:
        payload["images"] = [{"image_url": img_url} for img_url in images]

    # 清理 Chat Completions 的字段，Image API 不认这些
    for k in (
        "messages", "stream", "tools", "tool_choice",
        "temperature", "top_p", "max_tokens", "max_completion_tokens",
        "presence_penalty", "frequency_penalty",
        "stream_options", "logprobs", "top_logprobs",
    ):
        payload.pop(k, None)

    return url, headers, payload


# ============================================================
# 响应处理
# ============================================================


async def fetch_image_response(client, url, headers, payload, model, timeout):
    """处理 Image API 的非流式响应，转换为 Chat Completions 格式"""
    json_payload = await asyncio.to_thread(json_dumps_text, payload)
    response = await client.post(url, headers=headers, content=json_payload, timeout=timeout)

    error_message = await check_response(response, "fetch_image_response")
    if error_message:
        yield error_message
        return

    response_bytes = await response.aread()
    response_json = await asyncio.to_thread(json_loads, response_bytes)

    # 提取 b64_json 图片 → 结构化 content list
    data_list = response_json.get("data", [])
    content_items = []

    for i, item in enumerate(data_list):
        b64 = item.get("b64_json", "")
        if b64:
            # 检测图片格式
            mime = "image/png"  # 默认
            output_format = payload.get("output_format", "png")
            if output_format == "jpeg":
                mime = "image/jpeg"
            elif output_format == "webp":
                mime = "image/webp"

            content_items.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"}
            })

        # 如果有 revised_prompt，也加上
        revised = item.get("revised_prompt")
        if revised:
            content_items.append({
                "type": "text",
                "text": f"*Revised prompt: {revised}*"
            })

    content = content_items if content_items else None

    # 构建 Chat Completions 格式响应
    timestamp = int(datetime.timestamp(datetime.now()))
    random.seed(timestamp)
    random_str = ''.join(random.choices(string.ascii_letters + string.digits, k=29))

    result = {
        "id": f"chatcmpl-{random_str}",
        "object": "chat.completion",
        "created": timestamp,
        "model": model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": content,
                "refusal": None,
            },
            "logprobs": None,
            "finish_reason": "stop",
        }],
        "usage": response_json.get("usage"),
        "system_fingerprint": "fp_image_api",
    }

    mark_adapter_metrics_managed()
    # Image API 的 usage 格式不同，尝试提取
    usage = response_json.get("usage") or {}
    if usage:
        merge_usage(
            prompt_tokens=usage.get("input_tokens", 0),
            completion_tokens=usage.get("output_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
        )
    if content:
        mark_content_start()

    yield result


async def fetch_image_stream(client, url, headers, payload, model, timeout):
    """
    Image API 不支持原生流式，但客户端可能以 stream=true 请求。
    做法：普通 POST 拿到完整结果，再转成 SSE 格式分块返回。
    """
    # Image API 不认 stream 参数，清掉
    payload.pop("stream", None)

    output_format = payload.get("output_format", "png")

    json_payload = await asyncio.to_thread(json_dumps_text, payload)
    response = await client.post(url, headers=headers, content=json_payload, timeout=timeout)

    error_message = await check_response(response, "fetch_image_stream")
    if error_message:
        yield error_message
        return

    response_bytes = await response.aread()
    response_json = await asyncio.to_thread(json_loads, response_bytes)

    mark_adapter_metrics_managed()

    # 提取 usage
    usage = response_json.get("usage") or {}
    if usage:
        merge_usage(
            prompt_tokens=usage.get("input_tokens", 0),
            completion_tokens=usage.get("output_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
        )

    data_list = response_json.get("data", [])
    if not data_list:
        yield "data: [DONE]" + end_of_line
        return

    timestamp = int(datetime.timestamp(datetime.now()))
    random.seed(timestamp)

    # 发送 role
    sse_string = await generate_sse_response(timestamp, model, role="assistant")
    yield sse_string

    mark_content_start()

    for i, item in enumerate(data_list):
        b64 = item.get("b64_json", "")
        if not b64:
            continue

        # 多张图之间加换行
        if i > 0:
            sse_sep = await generate_sse_response(timestamp, model, content="\n\n")
            yield sse_sep

        # revised_prompt 先发
        revised = item.get("revised_prompt")
        if revised:
            sse_rev = await generate_sse_response(timestamp, model, content=f"*Revised prompt: {revised}*\n\n")
            yield sse_rev

        # 发结构化 image content item
        mime = "image/png"
        if output_format == "jpeg":
            mime = "image/jpeg"
        elif output_format == "webp":
            mime = "image/webp"

        image_content_item = [{
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"}
        }]
        sse_string = await generate_sse_response(timestamp, model, content=image_content_item)
        yield sse_string

    # stop
    sse_string = await generate_sse_response(timestamp, model, stop="stop")
    yield sse_string

    yield "data: [DONE]" + end_of_line


# ============================================================
# 注册
# ============================================================


def register():
    """注册 OpenAI Image API 渠道到注册中心"""
    from .registry import register_channel

    register_channel(
        id="openai-image",
        type_name="openai-image",
        default_base_url="https://api.openai.com/v1",
        auth_header="Authorization: Bearer {api_key}",
        description="OpenAI Image Generation API（gpt-image-2 等生图模型专用）",
        request_adapter=get_image_payload,
        response_adapter=fetch_image_response,
        stream_adapter=fetch_image_stream,
    )
