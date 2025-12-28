"""
Vertex AI 渠道适配器

负责处理 Google Vertex AI 的请求构建和响应流解析
支持 Vertex Gemini 和 Vertex Claude
"""

import re
import json
import copy
import time
import base64
import asyncio
import httpx
from datetime import datetime

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from ..utils import (
    safe_get,
    get_model_dict,
    get_base64_image,
    get_tools_mode,
    generate_sse_response,
    end_of_line,
    parse_json_safely,
    ThreadSafeCircularList,
)
from ..response import check_response
from .claude_channel import gpt2claude_tools_json


# ============================================================
# Vertex Gemini 格式化函数
# ============================================================

def format_gemini_text_message(text: str) -> dict:
    """格式化文本消息为 Vertex Gemini 格式"""
    return {"text": text}


async def format_gemini_image_message(image_url: str) -> dict:
    """格式化图片消息为 Vertex Gemini 格式"""
    base64_image, image_type = await get_base64_image(image_url)
    return {
        "inlineData": {
            "mimeType": image_type,
            "data": base64_image.split(",")[1],
        }
    }


# ============================================================
# Vertex Claude 格式化函数
# ============================================================

def format_claude_text_message(text: str) -> dict:
    """格式化文本消息为 Vertex Claude 格式"""
    return {"type": "text", "text": text}


async def format_claude_image_message(image_url: str) -> dict:
    """格式化图片消息为 Vertex Claude 格式"""
    base64_image, image_type = await get_base64_image(image_url)
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": image_type,
            "data": base64_image.split(",")[1],
        }
    }

# ============================================================
# Vertex AI 区域配置
# 参考文档:
# - Claude: https://cloud.google.com/vertex-ai/generative-ai/docs/partner-models/use-claude?hl=zh_cn
# - Gemini: https://cloud.google.com/vertex-ai/generative-ai/docs/learn/locations?hl=zh-cn#available-regions
# ============================================================

# Claude 3.5 Sonnet / Claude 3.7 Sonnet / Claude 4.5
c35s = ThreadSafeCircularList(["us-east5", "europe-west1"])

# Claude 3 Sonnet
c3s = ThreadSafeCircularList(["us-east5", "us-central1", "asia-southeast1"])

# Claude 3 Opus
c3o = ThreadSafeCircularList(["us-east5"])

# Claude 4 (Sonnet/Opus)
c4 = ThreadSafeCircularList(["us-east5", "europe-west1", "asia-east1"])

# Claude 3 Haiku
c3h = ThreadSafeCircularList(["us-east5", "us-central1", "europe-west1", "europe-west4"])

# Gemini 1.x 系列
gemini1 = ThreadSafeCircularList(["us-central1", "us-east4", "us-west1", "us-west4", "europe-west1", "europe-west2"])

# Gemini Preview 模型 (global)
gemini_preview = ThreadSafeCircularList(["global"])

# Gemini 2.5 Pro 系列
gemini2_5_pro_exp = ThreadSafeCircularList([
    "us-central1",
    "us-east1",
    "us-east4",
    "us-east5",
    "us-south1",
    "us-west1",
    "us-west4",
    "europe-central2",
    "europe-north1",
    "europe-southwest1",
    "europe-west1",
    "europe-west4",
    "europe-west8",
    "europe-west9"
])

# ============================================================

gemini_max_token_65k_models = ["gemini-2.5-pro", "gemini-2.0-pro", "gemini-2.0-flash-thinking", "gemini-2.5-flash"]


def create_jwt(client_email, private_key):
    """创建 JWT token 用于 Vertex AI 认证"""
    # JWT Header
    header = json.dumps({
        "alg": "RS256",
        "typ": "JWT"
    }).encode()

    # JWT Payload
    now = int(time.time())
    payload = json.dumps({
        "iss": client_email,
        "scope": "https://www.googleapis.com/auth/cloud-platform",
        "aud": "https://oauth2.googleapis.com/token",
        "exp": now + 3600,
        "iat": now
    }).encode()

    # Encode header and payload
    segments = [
        base64.urlsafe_b64encode(header).rstrip(b'='),
        base64.urlsafe_b64encode(payload).rstrip(b'=')
    ]

    # Create signature
    signing_input = b'.'.join(segments)
    private_key = load_pem_private_key(private_key.encode(), password=None)
    signature = private_key.sign(
        signing_input,
        padding.PKCS1v15(),
        hashes.SHA256()
    )

    segments.append(base64.urlsafe_b64encode(signature).rstrip(b'='))
    return b'.'.join(segments).decode()


async def get_access_token(client_email, private_key):
    """获取 Vertex AI 访问令牌"""
    jwt = await asyncio.to_thread(create_jwt, client_email, private_key)

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": jwt
            },
            headers={'Content-Type': "application/x-www-form-urlencoded"}
        )
        response.raise_for_status()
        return response.json()["access_token"]


async def get_vertex_gemini_payload(request, engine, provider, api_key=None):
    """构建 Vertex Gemini API 的请求 payload"""
    headers = {
        'Content-Type': 'application/json'
    }
    if provider.get("client_email") and provider.get("private_key"):
        access_token = await get_access_token(provider['client_email'], provider['private_key'])
        headers['Authorization'] = f"Bearer {access_token}"
    if provider.get("project_id"):
        project_id = provider.get("project_id")

    if request.stream:
        gemini_stream = "streamGenerateContent"
    else:
        gemini_stream = "generateContent"
    model_dict = get_model_dict(provider)
    original_model = model_dict[request.model]

    # https://cloud.google.com/vertex-ai/generative-ai/docs/models/gemini/2-0-flash?hl=zh-cn
    pro_models = ["gemini-2.5"]
    global_models = ["gemini-2.5-flash-image-preview", "gemini-3-pro"]
    if any(global_model in original_model for global_model in global_models):
        location = gemini_preview
    elif any(pro_model in original_model for pro_model in pro_models):
        location = gemini2_5_pro_exp
    else:
        location = gemini1

    if "google-vertex-ai" in provider.get("base_url", "") or any(global_model in original_model for global_model in global_models):
        url = provider.get("base_url").rstrip('/') + "/v1/projects/{PROJECT_ID}/locations/{LOCATION}/publishers/google/models/{MODEL_ID}:{stream}".format(
            LOCATION=await location.next(),
            PROJECT_ID=project_id,
            MODEL_ID=original_model,
            stream=gemini_stream
        )
    elif api_key is not None and api_key[2] == ".":
        url = f"https://aiplatform.googleapis.com/v1/publishers/google/models/{original_model}:{gemini_stream}?key={api_key}"
        headers.pop("Authorization", None)
    else:
        url = "https://{LOCATION}-aiplatform.googleapis.com/v1/projects/{PROJECT_ID}/locations/{LOCATION}/publishers/google/models/{MODEL_ID}:{stream}".format(
            LOCATION=await location.next(),
            PROJECT_ID=project_id,
            MODEL_ID=original_model,
            stream=gemini_stream
        )

    messages = []
    systemInstruction = None
    system_prompt = ""
    function_arguments = None
    request_messages = copy.deepcopy(request.messages)
    for msg in request_messages:
        if msg.role == "assistant":
            msg.role = "model"
        tool_calls = None
        if isinstance(msg.content, list):
            content = []
            for item in msg.content:
                if item.type == "text":
                    text_message = format_gemini_text_message(item.text)
                    content.append(text_message)
                elif item.type == "image_url" and provider.get("image", True):
                    image_message = await format_gemini_image_message(item.image_url.url)
                    content.append(image_message)
        elif msg.content:
            content = [{"text": msg.content}]
        elif msg.content is None:
            tool_calls = msg.tool_calls

        if tool_calls:
            tools_mode = get_tools_mode(provider)
            # 根据 tools_mode 决定处理多少个工具调用
            calls_to_process = tool_calls if tools_mode == "parallel" else tool_calls[:1]
            parts = []
            for tool_call in calls_to_process:
                function_arguments = {
                    "functionCall": {
                        "name": tool_call.function.name,
                        "args": json.loads(tool_call.function.arguments)
                    }
                }
                parts.append(function_arguments)
            messages.append(
                {
                    "role": "model",
                    "parts": parts
                }
            )
        elif msg.role == "tool":
            function_call_name = function_arguments["functionCall"]["name"]
            messages.append(
                {
                    "role": "function",
                    "parts": [{
                    "functionResponse": {
                        "name": function_call_name,
                        "response": {
                            "name": function_call_name,
                            "content": {
                                "result": msg.content,
                            }
                        }
                    }
                    }]
                }
            )
        elif msg.role != "system" and content:
            messages.append({"role": msg.role, "parts": content})
        elif msg.role == "system":
            system_prompt = system_prompt + "\n\n" + content[0]["text"]
    if system_prompt.strip():
        systemInstruction = {"parts": [{"text": system_prompt}]}

    if any(off_model in original_model for off_model in gemini_max_token_65k_models):
        safety_settings = "OFF"
    else:
        safety_settings = "BLOCK_NONE"

    payload = {
        "contents": messages,
        "safetySettings": [
            {
                "category": "HARM_CATEGORY_HARASSMENT",
                "threshold": safety_settings
            },
            {
                "category": "HARM_CATEGORY_HATE_SPEECH",
                "threshold": safety_settings
            },
            {
                "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                "threshold": safety_settings
            },
            {
                "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
                "threshold": safety_settings
            },
            {
                "category": "HARM_CATEGORY_CIVIC_INTEGRITY",
                "threshold": "BLOCK_NONE"
            },
        ]
    }
    if systemInstruction:
        payload["system_instruction"] = systemInstruction

    miss_fields = [
        'model',
        'messages',
        'stream',
        'tool_choice',
        'presence_penalty',
        'frequency_penalty',
        'n',
        'user',
        'include_usage',
        'logprobs',
        'top_logprobs',
        'stream_options',
        'prompt',
        'size',
        'max_tokens',  # will use max_output_tokens
    ]
    generation_config = {}

    for field, value in request.model_dump(exclude_unset=True).items():
        if field not in miss_fields and value is not None:
            if field == "tools":
                payload.update({
                    "tools": [{
                        "function_declarations": [tool["function"] for tool in value]
                    }],
                    "tool_config": {
                        "function_calling_config": {
                            "mode": "AUTO"
                        }
                    }
                })
            elif field == "temperature":
                generation_config["temperature"] = value
            elif field == "max_tokens":
                if value > 65535:
                    value = 65535
                generation_config["max_output_tokens"] = value
            elif field == "top_p":
                generation_config["top_p"] = value
            else:
                payload[field] = value

    payload["generationConfig"] = generation_config
    if "max_output_tokens" not in generation_config:
        if any(pro_model in original_model for pro_model in gemini_max_token_65k_models):
            payload["generationConfig"]["max_output_tokens"] = 65535
        else:
            payload["generationConfig"]["max_output_tokens"] = 8192

    if "gemini-2.5" in original_model:
        # 从请求模型名中检测思考预算设置
        m = re.match(r".*-think-(-?\d+)", request.model)
        if m:
            try:
                val = int(m.group(1))
                budget = None
                if "gemini-2.5-pro" in original_model:
                    if val < 128:
                        budget = 128
                    elif val > 32768:
                        budget = 32768
                    else:
                        budget = val
                elif "gemini-2.5-flash-lite" in original_model:
                    if val > 0 and val < 512:
                        budget = 512
                    elif val > 24576:
                        budget = 24576
                    else:
                        budget = val if val >= 0 else 0
                else:
                    if val > 24576:
                        budget = 24576
                    else:
                        budget = val if val >= 0 else 0

                payload["generationConfig"]["thinkingConfig"] = {
                    "includeThoughts": True if budget else False,
                    "thinkingBudget": budget
                }
            except ValueError:
                pass
        else:
            payload["generationConfig"]["thinkingConfig"] = {
                "includeThoughts": True,
            }

    if safe_get(provider, "preferences", "post_body_parameter_overrides", default=None):
        for key, value in safe_get(provider, "preferences", "post_body_parameter_overrides", default={}).items():
            if key == request.model:
                for k, v in value.items():
                    payload[k] = v
            elif all(_model not in request.model.lower() for _model in model_dict.keys()) and "-" not in key and " " not in key:
                payload[key] = value

    return url, headers, payload


async def get_vertex_claude_payload(request, engine, provider, api_key=None):
    """构建 Vertex Claude API 的请求 payload"""
    headers = {
        'Content-Type': 'application/json',
    }
    if provider.get("client_email") and provider.get("private_key"):
        access_token = await get_access_token(provider['client_email'], provider['private_key'])
        headers['Authorization'] = f"Bearer {access_token}"
    if provider.get("project_id"):
        project_id = provider.get("project_id")

    model_dict = get_model_dict(provider)
    original_model = model_dict[request.model]
    if "claude-3-5-sonnet" in original_model or "claude-3-7-sonnet" in original_model or "4-5@" in original_model:
        location = c35s
    elif "claude-3-opus" in original_model:
        location = c3o
    elif "claude-sonnet-4" in original_model or "claude-opus-4" in original_model:
        location = c4
    elif "claude-3-sonnet" in original_model:
        location = c3s
    elif "claude-3-haiku" in original_model:
        location = c3h

    claude_stream = "streamRawPredict"
    url = "https://{LOCATION}-aiplatform.googleapis.com/v1/projects/{PROJECT_ID}/locations/{LOCATION}/publishers/anthropic/models/{MODEL}:{stream}".format(
        LOCATION=await location.next(),
        PROJECT_ID=project_id,
        MODEL=original_model,
        stream=claude_stream
    )

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
                    text_message = format_claude_text_message(item.text)
                    content.append(text_message)
                elif item.type == "image_url" and provider.get("image", True):
                    image_message = await format_claude_image_message(item.image_url.url)
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
        max_tokens = 20000
    elif "claude-3-5-sonnet" in original_model:
        max_tokens = 8192
    else:
        max_tokens = 4096

    payload = {
        "anthropic_version": "vertex-2023-10-16",
        "messages": messages,
        "system": system_prompt or "You are Claude, a large language model trained by Anthropic.",
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

    return url, headers, payload


async def fetch_vertex_gemini_response(client, url, headers, payload, model, timeout):
    """处理 Vertex Gemini 非流式响应"""
    # Vertex Gemini 非流式与标准 Gemini 类似
    from .gemini_channel import fetch_gemini_response
    async for chunk in fetch_gemini_response(client, url, headers, payload, model, timeout):
        yield chunk


async def fetch_vertex_claude_response(client, url, headers, payload, model, timeout):
    """处理 Vertex Claude 非流式响应"""
    # 切换到非流式端点
    url = url.replace("streamRawPredict", "rawPredict")
    
    timestamp = int(datetime.timestamp(datetime.now()))
    json_payload = await asyncio.to_thread(json.dumps, payload)
    response = await client.post(url, headers=headers, content=json_payload, timeout=timeout)
    
    error_message = await check_response(response, "fetch_vertex_claude_response")
    if error_message:
        yield error_message
        return

    response_bytes = await response.aread()
    response_json = await asyncio.to_thread(json.loads, response_bytes)
    
    # Vertex Claude 格式解析与标准 Claude 类似
    content = safe_get(response_json, "content", 0, "text")
    prompt_tokens = safe_get(response_json, "usage", "input_tokens")
    output_tokens = safe_get(response_json, "usage", "output_tokens")
    total_tokens = (prompt_tokens or 0) + (output_tokens or 0)
    role = safe_get(response_json, "role")

    from ..utils import generate_no_stream_response
    yield await generate_no_stream_response(
        timestamp, model, content=content, role=role,
        total_tokens=total_tokens, prompt_tokens=prompt_tokens, completion_tokens=output_tokens
    )


async def fetch_vertex_claude_response_stream(client, url, headers, payload, model, timeout):
    """处理 Vertex Claude 流式响应"""
    from ..log_config import logger
    
    timestamp = int(datetime.timestamp(datetime.now()))
    json_payload = await asyncio.to_thread(json.dumps, payload)
    async with client.stream('POST', url, headers=headers, content=json_payload, timeout=timeout) as response:
        error_message = await check_response(response, "fetch_vertex_claude_response_stream")
        if error_message:
            yield error_message
            return

        buffer = ""
        revicing_function_call = False
        function_full_response = "{"
        need_function_call = False
        is_finish = False
        promptTokenCount = 0
        candidatesTokenCount = 0
        totalTokenCount = 0

        async for chunk in response.aiter_text():
            buffer += chunk
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)

                if line and '\"finishReason\": \"' in line:
                    is_finish = True
                if is_finish and '\"promptTokenCount\": ' in line:
                    json_data = parse_json_safely( "{" + line + "}")
                    promptTokenCount = json_data.get('promptTokenCount', 0)
                if is_finish and '\"candidatesTokenCount\": ' in line:
                    json_data = parse_json_safely( "{" + line + "}")
                    candidatesTokenCount = json_data.get('candidatesTokenCount', 0)
                if is_finish and '\"totalTokenCount\": ' in line:
                    json_data = parse_json_safely( "{" + line + "}")
                    totalTokenCount = json_data.get('totalTokenCount', 0)

                if line and '\"text\": \"' in line and is_finish == False:
                    try:
                        json_data = await asyncio.to_thread(json.loads, "{" + line.strip().rstrip(",") + "}")
                        content = json_data.get('text', '')
                        sse_string = await generate_sse_response(timestamp, model, content=content)
                        yield sse_string
                    except json.JSONDecodeError:
                        logger.error(f"无法解析JSON: {line}")

                if line and ('\"type\": \"tool_use\"' in line or revicing_function_call):
                    revicing_function_call = True
                    need_function_call = True
                    if ']' in line:
                        revicing_function_call = False
                        continue

                    function_full_response += line

        if need_function_call:
            function_call = await asyncio.to_thread(json.loads, function_full_response)
            function_call_name = function_call["name"]
            function_call_id = function_call["id"]
            sse_string = await generate_sse_response(timestamp, model, content=None, tools_id=function_call_id, function_call_name=function_call_name)
            yield sse_string
            function_full_response = await asyncio.to_thread(json.dumps, function_call["input"])
            sse_string = await generate_sse_response(timestamp, model, content=None, tools_id=function_call_id, function_call_name=None, function_call_content=function_full_response)
            yield sse_string

        sse_string = await generate_sse_response(timestamp, model, None, None, None, None, None, totalTokenCount, promptTokenCount, candidatesTokenCount)
        yield sse_string

    yield "data: [DONE]" + end_of_line


def register():
    """注册 Vertex AI 渠道到注册中心"""
    from .registry import register_channel
    from .gemini_channel import fetch_gemini_response_stream
    
    # 注册 Vertex Gemini
    register_channel(
        id="vertex-gemini",
        type_name="vertex-gemini",
        default_base_url="https://aiplatform.googleapis.com",
        auth_header="Authorization: Bearer {access_token}",
        description="Google Vertex AI (Gemini)",
        request_adapter=get_vertex_gemini_payload,
        response_adapter=fetch_vertex_gemini_response,
        stream_adapter=fetch_gemini_response_stream,
        models_adapter=None,
    )
    
    # 注册 Vertex Claude
    register_channel(
        id="vertex-claude",
        type_name="vertex-claude",
        default_base_url="https://aiplatform.googleapis.com",
        auth_header="Authorization: Bearer {access_token}",
        description="Google Vertex AI (Claude)",
        request_adapter=get_vertex_claude_payload,
        response_adapter=fetch_vertex_claude_response,
        stream_adapter=fetch_vertex_claude_response_stream,
        models_adapter=None,
    )
