"""
Gemini 渠道适配器

负责处理 Google Gemini API 的请求构建和响应流解析
"""

import re
import json
import copy
import asyncio
from datetime import datetime

from ..models import Message
from ..utils import (
    safe_get,
    get_model_dict,
    get_base64_image,
    generate_sse_response,
    generate_no_stream_response,
    end_of_line,
    upload_image_to_0x0st,
)
from ..response import check_response
from urllib.parse import urlparse


# ============================================================
# Gemini 格式化函数
# ============================================================

def format_text_message(text: str) -> dict:
    """格式化文本消息为 Gemini 格式"""
    return {"text": text}


async def format_image_message(image_url: str) -> dict:
    """格式化图片消息为 Gemini 格式"""
    base64_image, image_type = await get_base64_image(image_url)
    return {
        "inlineData": {
            "mimeType": image_type,
            "data": base64_image.split(",")[1],
        }
    }

gemini_max_token_65k_models = ["gemini-2.5-pro", "gemini-2.0-pro", "gemini-2.0-flash-thinking", "gemini-2.5-flash"]


async def get_gemini_payload(request, engine, provider, api_key=None):
    """构建 Gemini API 的请求 payload"""
    headers = {
        'Content-Type': 'application/json'
    }
    
    # 使用 x-goog-api-key 头部认证，避免 URL 参数中的特殊字符问题
    if api_key:
        headers['x-goog-api-key'] = api_key

    # 获取映射后的实际模型ID
    model_dict = get_model_dict(provider)
    original_model = model_dict[request.model]

    if request.stream:
        gemini_stream = "streamGenerateContent"
        # 流式请求需要 alt=sse 参数才能返回 SSE 格式
        sse_param = "?alt=sse"
    else:
        gemini_stream = "generateContent"
        sse_param = ""
    url = provider['base_url']
    parsed_url = urlparse(url)
    if "/v1beta" in parsed_url.path:
        api_version = "v1beta"
    else:
        api_version = "v1"

    # 不再在 URL 中放置 key，改用请求头认证
    url = f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path.split('/models')[0].rstrip('/')}/models/{original_model}:{gemini_stream}{sse_param}"

    messages = []
    systemInstruction = None
    system_prompt = ""
    function_arguments = None

    try:
        request_messages = [Message(role="user", content=request.prompt)]
    except Exception:
        request_messages = copy.deepcopy(request.messages)
    for msg in request_messages:
        if msg.role == "assistant":
            msg.role = "model"
        
        parts = []
        # 提取该消息可能携带的签名
        msg_signature = getattr(msg, "thoughtSignature", None)

        # 1. 处理思维链
        reasoning = getattr(msg, "reasoning_content", None)
        if reasoning:
            parts.append({"thought": True, "text": reasoning})

        # 2. 处理内容 (文本/图片)
        if isinstance(msg.content, list):
            for item in msg.content:
                if item.type == "text":
                    parts.append(format_text_message(item.text))
                elif item.type == "image_url" and provider.get("image", True):
                    parts.append(await format_image_message(item.image_url.url))
        elif msg.content:
            parts.append({"text": msg.content})

        # 3. 处理工具调用 (Model 角色下)
        if msg.role == "model" and msg.tool_calls:
            for i, tc in enumerate(msg.tool_calls):
                # 转换 arguments
                try:
                    args = json.loads(tc.function.arguments) if isinstance(tc.function.arguments, str) else tc.function.arguments
                except:
                    args = {}
                
                part = {
                    "functionCall": {
                        "name": tc.function.name,
                        "args": args
                    }
                }
                # 签名逻辑：第一个 FC 必须携带签名
                sig = (getattr(tc, "extra_content", {}) or {}).get("google", {}).get("thoughtSignature")
                if not sig and i == 0:
                    sig = msg_signature
                
                if sig:
                    part["thoughtSignature"] = sig
                    msg_signature = None # 已消耗
                
                parts.append(part)

        # 4. 如果没有工具调用但有签名，附在最后一个文本块
        if msg_signature and parts:
            # 找到最后一个非 thought 的文本块或 FC 块
            parts[-1]["thoughtSignature"] = msg_signature

        # 5. 处理函数响应 (Tool 角色下)
        if msg.role == "tool":
            messages.append({
                "role": "function",
                "parts": [{
                    "functionResponse": {
                        "name": msg.name or msg.tool_call_id,
                        "response": {"result": msg.content}
                    }
                }]
            })
        elif msg.role != "system" and parts:
            messages.append({"role": msg.role, "parts": parts})
        elif msg.role == "system":
            # 系统提示词处理逻辑保持不变
            sys_text = "".join([p.get("text", "") for p in parts if "text" in p])
            sys_text = re.sub(r"_+", "_", sys_text)
            system_prompt = system_prompt + "\n\n" + sys_text
    if system_prompt.strip():
        systemInstruction = {"parts": [{"text": system_prompt}]}

    if any(off_model in original_model for off_model in gemini_max_token_65k_models) or original_model.endswith("-image-generation"):
        safety_settings = "OFF"
    else:
        safety_settings = "BLOCK_NONE"

    payload = {
        "contents": messages or [{"role": "user", "parts": [{"text": "No messages"}]}],
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
        if api_version == "v1beta":
            payload["systemInstruction"] = systemInstruction
        if api_version == "v1":
            first_message = safe_get(payload, "contents", 0, "parts", 0, "text", default=None)
            system_instruction = safe_get(systemInstruction, "parts", 0, "text", default=None)
            if first_message and system_instruction:
                payload["contents"][0]["parts"][0]["text"] = system_instruction + "\n" + first_message

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
        'response_format',
        'stream_options',
        'prompt',
        'size',
        'max_completion_tokens',  # 将在下面转换为 maxOutputTokens
        'extra_body',  # OpenAI 扩展字段，在下面单独处理转换
        'thinking',  # OpenAI/Claude 思考配置
        'chat_template_kwargs',  # OpenAI 特有字段
        'min_p',  # OpenAI 特有字段
        'reasoning_effort',
    ]
    generation_config = {}

    def process_tool_parameters(data):
        if isinstance(data, dict):
            # 1. 移除 Gemini 不支持的字段
            unsupported_fields = [
                "additionalProperties",
                "exclusiveMinimum",
                "exclusiveMaximum",
                "minLength",
                "maxLength",
                "pattern",
                "$schema",
                "dependencies",
                "dependentRequired",
                "dependentSchemas",
                "unevaluatedItems",
                "unevaluatedProperties",
            ]
            for field in unsupported_fields:
                data.pop(field, None)

            # 2. 核心修复：确保 required 中的属性在 properties 中确实存在
            properties = data.get("properties")
            required = data.get("required")
            
            if isinstance(required, list):
                if isinstance(properties, dict):
                    # 只保留在 properties 中存在的必填项
                    data["required"] = [field for field in required if field in properties]
                    if not data["required"]:
                        data.pop("required")
                else:
                    # 如果没有 properties，则不能有 required
                    data.pop("required", None)

            # 3. 将 'default' 值移入 'description' (Gemini 部分模型对 default 支持不佳)
            if "default" in data:
                default_value = data.pop("default")
                description = data.get("description", "")
                data["description"] = f"{description}\nDefault: {default_value}"

            # 4. 递归处理嵌套的 properties
            if isinstance(properties, dict):
                for val in properties.values():
                    process_tool_parameters(val)
            
            # 处理 items (针对 array 类型)
            items = data.get("items")
            if isinstance(items, dict):
                process_tool_parameters(items)

    for field, value in request.model_dump(exclude_unset=True).items():
        if field not in miss_fields and value is not None:
            if field == "tools" and ("gemini-2.0-flash-thinking" in original_model or "gemini-2.5-flash-image" in original_model or "gemini-3-pro-image" in original_model):
                continue
            if field == "tools":
                # 处理每个工具的 function 定义
                processed_tools = []
                for tool in value:
                    function_def = tool["function"]
                    if "parameters" in function_def:
                        process_tool_parameters(function_def["parameters"])

                    if function_def["name"] not in ["googleSearch", "google_search"]:
                        processed_tools.append({"function": function_def})

                if processed_tools:
                    tool_config = {"function_calling_config": {"mode": "AUTO"}}
                    
                    # 处理 tool_choice (OpenAI 风格 -> Gemini 风格)
                    tc = request.tool_choice
                    if tc:
                        if tc == "required":
                            tool_config["function_calling_config"]["mode"] = "ANY"
                        elif tc == "none":
                            tool_config["function_calling_config"]["mode"] = "NONE"
                        elif isinstance(tc, dict) and tc.get("type") == "function":
                            fn_name = tc.get("function", {}).get("name")
                            if fn_name:
                                tool_config["function_calling_config"]["mode"] = "ANY"
                                tool_config["function_calling_config"]["allowed_function_names"] = [fn_name]
                        elif hasattr(tc, "type") and tc.type == "function" and tc.function:
                            fn_name = tc.function.name
                            tool_config["function_calling_config"]["mode"] = "ANY"
                            tool_config["function_calling_config"]["allowed_function_names"] = [fn_name]

                    payload.update({
                        "tools": [{
                            "function_declarations": [tool["function"] for tool in processed_tools]
                        }],
                        "tool_config": tool_config
                    })
            elif field == "temperature":
                if "gemini-2.5-flash-image" in original_model:
                    value = 1
                if "gemini-3-pro-image" in original_model:
                    value = 1
                generation_config["temperature"] = value
            elif field == "max_tokens" or field == "max_completion_tokens":
                if value > 65536:
                    value = 65536
                generation_config["maxOutputTokens"] = value
            elif field == "top_p":
                generation_config["topP"] = value
            else:
                payload[field] = value

    payload["generationConfig"] = generation_config
    if "maxOutputTokens" not in generation_config:
        if any(pro_model in original_model for pro_model in gemini_max_token_65k_models):
            payload["generationConfig"]["maxOutputTokens"] = 65536
        else:
            payload["generationConfig"]["maxOutputTokens"] = 8192

        if ("-image" in original_model):
            payload["generationConfig"]["responseModalities"] = [
                "Text",
                "Image",
            ]

    # 处理 OpenAI extra_body.google 配置，转换 snake_case 到 camelCase 后合并到 generationConfig
    request_data = request.model_dump(exclude_unset=True)
    extra_body = request_data.get('extra_body')
    
    if isinstance(extra_body, dict):
        google_config = extra_body.get('google', {})
        if isinstance(google_config, dict) and google_config:
            def _snake_to_camel(s: str) -> str:
                """将 snake_case 转换为 camelCase，但保留已经是 camelCase 的键"""
                if any(c.isupper() for c in s) and '_' not in s:
                    return s
                parts = s.split('_')
                return parts[0] + ''.join(word.capitalize() for word in parts[1:])
            
            def _convert_keys(obj):
                """递归转换字典所有键从 snake_case 到 camelCase"""
                if isinstance(obj, dict):
                    return {_snake_to_camel(k): _convert_keys(v) for k, v in obj.items()}
                elif isinstance(obj, list):
                    return [_convert_keys(item) for item in obj]
                else:
                    return obj
            
            def _deep_merge(target, source):
                """深度合并两个字典"""
                for key, value in source.items():
                    if key in target and isinstance(target[key], dict) and isinstance(value, dict):
                        _deep_merge(target[key], value)
                    else:
                        target[key] = value
            
            converted_config = _convert_keys(google_config)
            # 合并到 generationConfig 中（extra_body.google.thinking_config -> generationConfig.thinkingConfig）
            _deep_merge(payload["generationConfig"], converted_config)

    if "gemini-2.5" in original_model and "gemini-2.5-flash-image" not in original_model:
        # 从请求模型名中检测思考预算设置
        m = re.match(r".*-think-(-?\d+)", request.model)
        if m:
            try:
                val = int(m.group(1))
                budget = None
                # gemini-2.5-pro: [128, 32768]
                if "gemini-2.5-pro" in original_model:
                    if val < 128:
                        budget = 128
                    elif val > 32768:
                        budget = 32768
                    else: # 128 <= val <= 32768
                        budget = val

                # gemini-2.5-flash-lite: [0] or [512, 24576]
                elif "gemini-2.5-flash-lite" in original_model:
                    if val > 0 and val < 512:
                        budget = 512
                    elif val > 24576:
                        budget = 24576
                    else: # Includes 0 and valid range, and clamps invalid negatives
                        budget = val if val >= 0 else 0

                # gemini-2.5-flash (and other gemini-2.5 models as a fallback): [0, 24576]
                else:
                    if val > 24576:
                        budget = 24576
                    else: # Includes 0 and valid range, and clamps invalid negatives
                        budget = val if val >= 0 else 0

                payload["generationConfig"]["thinkingConfig"] = {
                    "includeThoughts": True if budget else False,
                    "thinkingBudget": budget
                }
            except ValueError:
                # 如果转换为整数失败，忽略思考预算设置
                pass
        else:
            payload["generationConfig"]["thinkingConfig"] = {
                "includeThoughts": True,
            }

    return url, headers, payload


async def gemini_json_process(response_json):
    """处理 Gemini JSON 响应
    
    遍历所有 parts 收集：
    - thought=True 的部分作为 reasoning_content
    - 普通文本作为 content
    - inlineData 作为图片
    - functionCall 作为函数调用
    """
    from ..log_config import logger
    
    promptTokenCount = 0
    candidatesTokenCount = 0
    totalTokenCount = 0
    image_base64 = None
    thought_signature = None
    
    # 收集所有内容
    reasoning_parts = []
    content_parts = []
    function_call_name = None
    function_full_response = None

    json_data = safe_get(response_json, "candidates", 0, "content", default=None)
    finishReason = safe_get(response_json, "candidates", 0, "finishReason", default=None)
    
    parts_data = safe_get(json_data, "parts", default=[])
    
    # 遍历所有 parts
    for part in parts_data:
        if not isinstance(part, dict):
            continue
            
        # 提取签名 (可能在任何 part 中)
        sig = part.get("thoughtSignature")
        if sig:
            thought_signature = sig
        
        # 处理思考内容 (thought=True)
        if part.get("thought") is True:
            text = part.get("text", "")
            if text:
                reasoning_parts.append(text)
            continue
        
        # 处理普通文本
        if "text" in part and not part.get("thought"):
            text = part.get("text", "")
            if text:
                content_parts.append(text)
        
        # 处理图片
        if "inlineData" in part:
            b64_json = safe_get(part, "inlineData", "data", default="")
            if b64_json:
                image_base64 = b64_json
        
        # 处理函数调用 (只取第一个)
        if "functionCall" in part and function_call_name is None:
            function_call_name = safe_get(part, "functionCall", "name", default=None)
            function_full_response = safe_get(part, "functionCall", "args", default=None)

    if finishReason:
        promptTokenCount = safe_get(response_json, "usageMetadata", "promptTokenCount", default=0)
        candidatesTokenCount = safe_get(response_json, "usageMetadata", "candidatesTokenCount", default=0)
        totalTokenCount = safe_get(response_json, "usageMetadata", "totalTokenCount", default=0)
        if finishReason != "STOP":
            logger.error(f"finishReason: {finishReason}")

    # 合并收集到的内容
    reasoning_content = "".join(reasoning_parts)
    content = "".join(content_parts)
    
    # 判断是否有思考内容
    is_thinking = bool(reasoning_parts)

    # 提取 blockReason
    blockReason = safe_get(response_json, "promptFeedback", "blockReason", default=None)
    if not blockReason:
        blockReason = safe_get(response_json, "candidates", 0, "blockReason", default=None)

    return is_thinking, reasoning_content, content, image_base64, function_call_name, function_full_response, finishReason, blockReason, promptTokenCount, candidatesTokenCount, totalTokenCount, thought_signature


async def fetch_gemini_response(client, url, headers, payload, model, timeout):
    """处理 Gemini 非流式响应"""
    timestamp = int(datetime.timestamp(datetime.now()))
    json_payload = await asyncio.to_thread(json.dumps, payload)
    response = await client.post(url, headers=headers, content=json_payload, timeout=timeout)
    
    error_message = await check_response(response, "fetch_gemini_response")
    if error_message:
        yield error_message
        return

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
        parsed_data = response_json

    # 检查 blockReason
    if isinstance(parsed_data, list) and len(parsed_data) > 0:
        first_resp = parsed_data[0]
        is_thinking, reasoning_content, content, image_base64, function_call_name, function_full_response, finishReason, blockReason, promptTokenCount, candidatesTokenCount, totalTokenCount, thought_signature = await gemini_json_process(first_resp)
        
        if blockReason and blockReason != "STOP":
            yield {"error": f"Gemini Blocked: {blockReason}", "status_code": 400, "details": first_resp}
            return
        
        if not safe_get(first_resp, "candidates") and blockReason:
            yield {"error": f"Gemini Blocked: {blockReason}", "status_code": 400, "details": first_resp}
            return

        # 获取 usage (可能在最后一个响应对象中)
        last_resp = parsed_data[-1]
        usage_metadata = safe_get(last_resp, "usageMetadata")
        prompt_tokens = safe_get(usage_metadata, "promptTokenCount", default=promptTokenCount)
        candidates_tokens = safe_get(usage_metadata, "candidatesTokenCount", default=candidatesTokenCount)
        total_tokens = safe_get(usage_metadata, "totalTokenCount", default=totalTokenCount)

        role = safe_get(first_resp, "candidates", 0, "content", "role")
        if role == "model":
            role = "assistant"
        elif not role:
            role = "assistant"

        yield await generate_no_stream_response(
            timestamp, model, content=content, tools_id=None, 
            function_call_name=function_call_name, function_call_content=function_full_response, 
            role=role, total_tokens=total_tokens, prompt_tokens=prompt_tokens, 
            completion_tokens=candidates_tokens, reasoning_content=reasoning_content, 
            image_base64=image_base64, thought_signature=thought_signature
        )
        return


async def fetch_gemini_response_stream(client, url, headers, payload, model, timeout):
    """处理 Gemini 流式响应"""
    timestamp = int(datetime.timestamp(datetime.now()))
    json_payload = await asyncio.to_thread(json.dumps, payload)
    async with client.stream('POST', url, headers=headers, content=json_payload, timeout=timeout) as response:
        error_message = await check_response(response, "fetch_gemini_response_stream")
        if error_message:
            yield error_message
            return
        buffer = ""
        promptTokenCount = 0
        candidatesTokenCount = 0
        totalTokenCount = 0
        parts_json = ""
        async for chunk in response.aiter_text():
            buffer += chunk
            if buffer and "\n" not in buffer:
                buffer += "\n"

            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                if line.startswith("data: "):
                    parts_json = line.lstrip("data: ").strip()
                    try:
                        response_json = await asyncio.to_thread(json.loads, parts_json)
                    except json.JSONDecodeError:
                        continue
                else:
                    parts_json += line
                    parts_json = parts_json.lstrip("[,")
                    try:
                        response_json = await asyncio.to_thread(json.loads, parts_json)
                    except json.JSONDecodeError:
                        continue

                # https://ai.google.dev/api/generate-content?hl=zh-cn#FinishReason
                is_thinking, reasoning_content, content, image_base64, function_call_name, function_full_response, finishReason, blockReason, promptTokenCount, candidatesTokenCount, totalTokenCount, thought_signature = await gemini_json_process(response_json)

                if is_thinking:
                    sse_string = await generate_sse_response(timestamp, model, reasoning_content=reasoning_content, thought_signature=thought_signature)
                    yield sse_string
                if not image_base64 and content:
                    sse_string = await generate_sse_response(timestamp, model, content=content, thought_signature=thought_signature)
                    yield sse_string

                if image_base64:
                    if "gemini-2.5-flash-image" not in model and "gemini-3-pro-image" not in model:
                        yield await generate_no_stream_response(timestamp, model, content=content, tools_id=None, function_call_name=None, function_call_content=None, role=None, total_tokens=totalTokenCount, prompt_tokens=promptTokenCount, completion_tokens=candidatesTokenCount, image_base64=image_base64, thought_signature=thought_signature)
                    else:
                        image_url = await upload_image_to_0x0st("data:image/png;base64," + image_base64)
                        sse_string = await generate_sse_response(timestamp, model, content=f"\n\n![image]({image_url})", thought_signature=thought_signature)
                        yield sse_string

                if function_call_name:
                    sse_string = await generate_sse_response(timestamp, model, content=None, tools_id="chatcmpl-9inWv0yEtgn873CxMBzHeCeiHctTV", function_call_name=function_call_name, thought_signature=thought_signature)
                    yield sse_string
                if function_full_response:
                    sse_string = await generate_sse_response(timestamp, model, content=None, tools_id="chatcmpl-9inWv0yEtgn873CxMBzHeCeiHctTV", function_call_name=None, function_call_content=function_full_response, thought_signature=thought_signature)
                    yield sse_string

                if parts_json == "[]" or (blockReason and blockReason != "STOP"):
                    yield {"error": f"Gemini Blocked: {blockReason or 'Empty Response'}", "status_code": 400, "details": response_json}
                    return
                elif finishReason and finishReason != "STOP":
                    yield {"error": f"Gemini Finish Reason: {finishReason}", "status_code": 400, "details": response_json}
                    return
                elif finishReason:
                    sse_string = await generate_sse_response(timestamp, model, stop="stop")
                    yield sse_string
                    break

                parts_json = ""

        sse_string = await generate_sse_response(timestamp, model, None, None, None, None, None, totalTokenCount, promptTokenCount, candidatesTokenCount)
        yield sse_string

    yield "data: [DONE]" + end_of_line


async def fetch_gemini_models(client, provider):
    """获取 Gemini API 的模型列表"""
    base_url = provider.get('base_url', 'https://generativelanguage.googleapis.com/v1beta').rstrip('/')
    api_key = provider.get('api')
    if isinstance(api_key, list):
        api_key = api_key[0] if api_key else None
    
    # 使用请求头认证，避免 URL 参数中的特殊字符问题
    url = f"{base_url}/models"
    headers = {
        'Content-Type': 'application/json',
        'x-goog-api-key': api_key,
    }
    
    response = await client.get(url, headers=headers)
    response.raise_for_status()
    
    data = response.json()
    models = []
    if isinstance(data, dict) and 'models' in data:
        # Gemini 返回格式: {"models": [{"name": "models/gemini-pro", ...}]}
        for m in data['models']:
            name = m.get('name', '')
            # 移除 "models/" 前缀
            if name.startswith('models/'):
                name = name[7:]
            if name:
                models.append(name)
    
    return models


def register():
    """注册 Gemini 渠道到注册中心"""
    from .registry import register_channel
    
    register_channel(
        id="gemini",
        type_name="gemini",
        default_base_url="https://generativelanguage.googleapis.com/v1beta",
        auth_header="x-goog-api-key: {api_key}",
        description="Google Gemini API",
        request_adapter=get_gemini_payload,
        response_adapter=fetch_gemini_response,
        stream_adapter=fetch_gemini_response_stream,
        models_adapter=fetch_gemini_models,
    )
