"""
AWS Bedrock 渠道适配器

负责处理 AWS Bedrock API 的请求构建和响应流解析
"""

import re
import json
import hmac
import base64
import hashlib
import asyncio
import datetime
from datetime import timezone
from datetime import datetime as dt

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
from ..json_utils import json_loads, json_dumps_text
from ..response_context import mark_adapter_metrics_managed, mark_content_start, merge_usage
from ..stream_utils import aiter_decoded_lines
from ..usage import extract_cache_usage
from ..file_utils import extract_base64_data
from .claude_channel import gpt2claude_tools_json


# ============================================================
# AWS Bedrock (Claude) 格式化函数
# ============================================================

def format_text_message(text: str) -> dict:
    """格式化文本消息为 AWS Bedrock Claude 格式"""
    return {"type": "text", "text": text}


async def format_image_message(image_url: str) -> dict:
    """格式化图片消息为 AWS Bedrock Claude 格式"""
    base64_image, image_type = await get_base64_image(image_url)
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": image_type,
            "data": extract_base64_data(base64_image),
        }
    }


def sign(key, msg):
    return hmac.new(key, msg.encode('utf-8'), hashlib.sha256).digest()


def get_signature_key(key, date_stamp, region_name, service_name):
    k_date = sign(('AWS4' + key).encode('utf-8'), date_stamp)
    k_region = sign(k_date, region_name)
    k_service = sign(k_region, service_name)
    k_signing = sign(k_service, 'aws4_request')
    return k_signing


def get_signature(request_body, model_id, aws_access_key, aws_secret_key, aws_region, host, content_type, accept_header):
    import urllib.parse
    request_body = json.dumps(request_body)
    SERVICE = "bedrock"
    canonical_querystring = ''
    method = 'POST'
    raw_path = f'/model/{model_id}/invoke-with-response-stream'
    canonical_uri = urllib.parse.quote(raw_path, safe='/-_.~')
    # Create a date for headers and the credential string
    t = datetime.datetime.now(timezone.utc)
    amz_date = t.strftime('%Y%m%dT%H%M%SZ')
    date_stamp = t.strftime('%Y%m%d') # Date YYYYMMDD

    # --- Task 1: Create a Canonical Request ---
    payload_hash = hashlib.sha256(request_body.encode('utf-8')).hexdigest()

    canonical_headers = f'accept:{accept_header}\n' \
                        f'content-type:{content_type}\n' \
                        f'host:{host}\n' \
                        f'x-amz-bedrock-accept:{accept_header}\n' \
                        f'x-amz-content-sha256:{payload_hash}\n' \
                        f'x-amz-date:{amz_date}\n'
    # 注意：头名称需要按字母顺序排序

    signed_headers = 'accept;content-type;host;x-amz-bedrock-accept;x-amz-content-sha256;x-amz-date' # 按字母顺序排序

    canonical_request = f'{method}\n' \
                        f'{canonical_uri}\n' \
                        f'{canonical_querystring}\n' \
                        f'{canonical_headers}\n' \
                        f'{signed_headers}\n' \
                        f'{payload_hash}'

    # --- Task 2: Create the String to Sign ---
    algorithm = 'AWS4-HMAC-SHA256'
    credential_scope = f'{date_stamp}/{aws_region}/{SERVICE}/aws4_request'
    string_to_sign = f'{algorithm}\n' \
                    f'{amz_date}\n' \
                    f'{credential_scope}\n' \
                    f'{hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()}'

    # --- Task 3: Calculate the Signature ---
    signing_key = get_signature_key(aws_secret_key, date_stamp, aws_region, SERVICE)
    signature = hmac.new(signing_key, string_to_sign.encode('utf-8'), hashlib.sha256).hexdigest()

    # --- Task 4: Add Signing Information to the Request ---
    authorization_header = f'{algorithm} Credential={aws_access_key}/{credential_scope}, SignedHeaders={signed_headers}, Signature={signature}'
    return amz_date, payload_hash, authorization_header


async def get_aws_payload(request, engine, provider, api_key=None):
    """构建 AWS Bedrock API 的请求 payload"""
    CONTENT_TYPE = "application/json"
    model_dict = get_model_dict(provider)
    original_model = model_dict[request.model]
    base_url = provider.get('base_url')
    is_fixed_url = base_url.endswith('#')
    if is_fixed_url:
        url = base_url[:-1].rstrip('/')
        # 固定 URL 模式：从实际 URL 解析 host/region，用于可能的签名
        from urllib.parse import urlparse as _urlparse
        _parsed = _urlparse(url)
        HOST = _parsed.netloc
        # 尝试从 host 提取 region，如 bedrock-runtime.us-east-1.amazonaws.com
        _parts = HOST.split('.')
        AWS_REGION = _parts[1] if len(_parts) > 2 else provider.get('aws_region', 'us-east-1')
    else:
        AWS_REGION = base_url.split('.')[1]
        HOST = f"bedrock-runtime.{AWS_REGION}.amazonaws.com"
        url = f"{base_url}/model/{original_model}/invoke-with-response-stream"

    messages = []
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
                    "input": json_loads(tool_call.function.arguments),
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

    max_tokens = 4096

    payload = {
        "messages": messages,
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
    }

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
        'stream',
    ]

    for field, value in request.model_dump(exclude_unset=True).items():
        if field not in miss_fields and value is not None:
            payload[field] = value

    tools_mode = get_tools_mode(provider)
    if request.tools and tools_mode != "none":
        tools = []
        for tool in request.tools:
            json_tool = await gpt2claude_tools_json(tool.dict()["function"])
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

    headers = {}
    if provider.get("aws_access_key") and provider.get("aws_secret_key"):
        ACCEPT_HEADER = "application/vnd.amazon.bedrock.payload+json"
        amz_date, payload_hash, authorization_header = await asyncio.to_thread(
            get_signature, payload, original_model, provider.get("aws_access_key"), provider.get("aws_secret_key"), AWS_REGION, HOST, CONTENT_TYPE, ACCEPT_HEADER
        )
        headers = {
            'Accept': ACCEPT_HEADER,
            'Content-Type': CONTENT_TYPE,
            'X-Amz-Date': amz_date,
            'X-Amz-Bedrock-Accept': ACCEPT_HEADER,
            'X-Amz-Content-Sha256': payload_hash,
            'Authorization': authorization_header,
        }

    return url, headers, payload


async def fetch_aws_response(client, url, headers, payload, model, timeout):
    """处理 AWS Bedrock 非流式响应"""
    # 切换到非流式端点（AWS Bedrock 需要不同的签名）
    url = url.replace("invoke-with-response-stream", "invoke")
    
    timestamp = int(dt.timestamp(dt.now()))
    json_payload = await asyncio.to_thread(json_dumps_text, payload)
    
    # AWS Bedrock 非流式签名需要重新生成（此处简化，实际可能需要更完整的实现）
    # 但根据 core/response.py 之前的硬编码，它似乎是复用 Gemini/Vertex 的解析逻辑？
    # 实际上 AWS Bedrock 非流式返回的是一个包含 bytes 的 JSON。
    
    response = await client.post(url, headers=headers, content=json_payload, timeout=timeout)
    error_message = await check_response(response, "fetch_aws_response")
    if error_message:
        yield error_message
        return

    response_bytes = await response.aread()
    response_json = await asyncio.to_thread(json_loads, response_bytes)
    mark_adapter_metrics_managed()
    
    # 解析 AWS Bedrock Claude 格式；Claude 的缓存字段需要计入统一 prompt_tokens 口径。
    content = safe_get(response_json, "content", 0, "text", default="")
    usage = safe_get(response_json, "usage", default={}) or {}
    cache_usage = extract_cache_usage(usage)
    prompt_tokens = (
        (usage.get("input_tokens") or 0)
        + cache_usage["cached_tokens"]
        + cache_usage["cache_creation_tokens"]
    )
    output_tokens = usage.get("output_tokens", 0)
    merge_usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=output_tokens,
        total_tokens=prompt_tokens + output_tokens,
        **cache_usage,
    )
    if content:
        mark_content_start()
    
    yield await generate_no_stream_response(
        timestamp, model, content=content, role="assistant",
        total_tokens=prompt_tokens + output_tokens,
        prompt_tokens=prompt_tokens, completion_tokens=output_tokens,
        # Bedrock 非流式返回 Claude usage 时，需要把缓存字段继续传给下游输出函数。
        cached_tokens=cache_usage["cached_tokens"],
        cache_creation_tokens=cache_usage["cache_creation_tokens"],
        return_dict=True
    )


async def fetch_aws_response_stream(client, url, headers, payload, model, timeout):
    """处理 AWS Bedrock 流式响应"""
    from ..log_config import logger
    
    timestamp = int(dt.timestamp(dt.now()))
    json_payload = await asyncio.to_thread(json_dumps_text, payload)
    async with client.stream('POST', url, headers=headers, content=json_payload, timeout=timeout) as response:
        error_message = await check_response(response, "fetch_aws_response_stream")
        if error_message:
            yield error_message
            return

        mark_adapter_metrics_managed()
        # Bedrock 流式响应可能先返回 Claude usage，再在 invocationMetrics 中返回总量；缓存字段需要跨事件暂存。
        input_tokens = 0
        output_tokens = 0
        cached_tokens = 0
        cache_creation_tokens = 0
        async for line in aiter_decoded_lines(response.aiter_bytes(), delimiter=b"\r"):
                if not line or \
                line.strip() == "" or \
                line.strip().startswith(':content-type') or \
                line.strip().startswith(':event-type'):
                    continue

                json_match = re.search(r'event{.*?}', line)
                if not json_match:
                    continue
                try:
                    chunk_data = json_loads(json_match.group(0).lstrip('event'))
                except json.JSONDecodeError:
                    logger.error(f"DEBUG json.JSONDecodeError: {json_match.group(0).lstrip('event')!r}")
                    continue

                if "bytes" in chunk_data:
                    decoded_bytes = base64.b64decode(chunk_data["bytes"])
                    payload_chunk = json_loads(decoded_bytes)

                    claude_usage = safe_get(payload_chunk, "message", "usage", default={}) or safe_get(payload_chunk, "usage", default={}) or {}
                    if claude_usage:
                        # Claude 流式 usage 中的 input_tokens 不含缓存部分，这里先还原为统一 prompt_tokens。
                        _cache_usage = extract_cache_usage(claude_usage)
                        cached_tokens = _cache_usage["cached_tokens"] or cached_tokens
                        cache_creation_tokens = _cache_usage["cache_creation_tokens"] or cache_creation_tokens
                        if claude_usage.get("input_tokens") is not None:
                            input_tokens = (
                                (claude_usage.get("input_tokens") or 0)
                                + cached_tokens
                                + cache_creation_tokens
                            )
                        if claude_usage.get("output_tokens"):
                            output_tokens = claude_usage.get("output_tokens", 0)

                    text = safe_get(payload_chunk, "delta", "text", default="")
                    if text:
                        mark_content_start()
                        sse_string = await generate_sse_response(timestamp, model, text, None, None)
                        yield sse_string

                    usage = safe_get(payload_chunk, "amazon-bedrock-invocationMetrics", default="")
                    if usage:
                        raw_input_tokens = usage.get("inputTokenCount", 0)
                        output_tokens = usage.get("outputTokenCount", output_tokens)
                        input_tokens = input_tokens or raw_input_tokens
                        total_tokens = input_tokens + output_tokens
                        merge_usage(
                            prompt_tokens=input_tokens,
                            completion_tokens=output_tokens,
                            total_tokens=total_tokens,
                            cached_tokens=cached_tokens,
                            cache_creation_tokens=cache_creation_tokens,
                        )
                        sse_string = await generate_sse_response(
                            timestamp, model, None, None, None, None, None,
                            total_tokens, input_tokens, output_tokens,
                            # Bedrock invocationMetrics 触发最终 usage chunk，需要带上先前 Claude usage 中的缓存字段。
                            cached_tokens=cached_tokens,
                            cache_creation_tokens=cache_creation_tokens,
                        )
                        yield sse_string

    yield "data: [DONE]" + end_of_line


def register():
    """注册 AWS 渠道到注册中心"""
    from .registry import register_channel
    
    register_channel(
        id="aws",
        type_name="aws-bedrock",
        default_base_url="https://bedrock-runtime.us-east-1.amazonaws.com",
        auth_header="AWS Signature V4",
        description="AWS Bedrock (Claude, Llama, etc.)",
        request_adapter=get_aws_payload,
        response_adapter=fetch_aws_response,
        stream_adapter=fetch_aws_response_stream,
    )
