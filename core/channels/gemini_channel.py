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


def normalize_gemini_payload(payload: dict) -> dict:
    """规范化 Gemini 负载，合并驼峰和下划线字段，处理拼写错误"""
    # 1. 定义映射关系 (别名 -> 标准键)
    # Gemini AI Studio 标准使用 camelCase 顶层键
    mapping = {
        "generation_config": "generationConfig",
        "generate_config": "generationConfig",
        "safety_settings": "safetySettings",
        "safety": "safetySettings",
        "safty": "safetySettings",
        "system_instruction": "systemInstruction",
        "tool_config": "toolConfig", # 虽然 REST 有时用下划线，但规范化为 camelCase 后下面会处理
    }

    # 2. 合并字典中的字段
    for alias, canonical in mapping.items():
        if alias in payload:
            value = payload.pop(alias)
            if canonical not in payload:
                payload[canonical] = value
            elif isinstance(value, dict) and isinstance(payload[canonical], dict):
                # 深度合并字典 (如 generationConfig)
                payload[canonical].update(value)
            # 如果是列表等其他类型，以标准键为准，不覆盖

    # 3. 特殊处理 tool_config: Gemini AI Studio 实际倾向于使用 tool_config (snake_case)
    # 保持与原有代码一致性
    if "toolConfig" in payload:
        payload["tool_config"] = payload.pop("toolConfig")

    return payload


async def patch_passthrough_gemini_payload(
    payload: dict,
    modifications: dict,
    request,
    engine: str,
    provider: dict,
    api_key=None,
) -> dict:
    """透传模式下对 Gemini native payload 做渠道级修饰（system_prompt 注入）。"""
    system_prompt = modifications.get("system_prompt")
    system_prompt_text = str(system_prompt).strip() if system_prompt is not None else ""
    if not system_prompt_text:
        return payload

    # 兼容 systemInstruction / system_instruction
    key = "systemInstruction" if "systemInstruction" in payload else ("system_instruction" if "system_instruction" in payload else "systemInstruction")
    sys_inst = payload.get(key)

    if isinstance(sys_inst, dict):
        parts = sys_inst.get("parts")
        if isinstance(parts, list) and parts and isinstance(parts[0], dict):
            old = parts[0].get("text") or ""
            parts[0]["text"] = f"{system_prompt_text}\n\n{old}" if old else system_prompt_text
        else:
            sys_inst["parts"] = [{"text": system_prompt_text}]
        payload[key] = sys_inst
    else:
        payload[key] = {"parts": [{"text": system_prompt_text}]}

    return payload


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
            # Google AI Studio API 要求函数响应的角色为 "user"
            # 它将函数执行结果视为由用户/环境提供的上下文
            messages.append({
                "role": "user",
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
        'parallel_tool_calls',
        'logit_bias',
        'service_tier',
        # OpenAI stop sequences (Gemini 原生接口不接受 stop 顶层字段)
        'stop',
    ]
    generation_config = {}

    def process_tool_parameters(data):
        if isinstance(data, dict):
            # 0. 处理逻辑组合符 (OpenAI anyOf/oneOf/allOf [..., null] -> Gemini nullable: True)
            for key in ["anyOf", "oneOf", "allOf"]:
                if key in data:
                    logic_list = data.pop(key)
                    if isinstance(logic_list, list) and logic_list:
                        # 寻找第一个带 type 的项作为主要定义
                        main_item = next((item for item in logic_list if isinstance(item, dict) and item.get("type") and item.get("type") != "null"), logic_list[0])
                        if isinstance(main_item, dict):
                            # 将主要项的属性合并回当前层，但保留当前层已有的 description 等
                            for k, v in main_item.items():
                                if k not in data:
                                    data[k] = v
                        # 如果列表中包含 type: null，设置 nullable
                        if any(isinstance(item, dict) and item.get("type") == "null" for item in logic_list):
                            data["nullable"] = True

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
                "not",
                "minItems",
                "maxItems",
                "uniqueItems",
                "minimum",
                "maximum",
                "multipleOf",
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
                    # 深度克隆以避免修改原始请求对象
                    function_def = copy.deepcopy(tool["function"])
                    # 移除 OpenAI 特有的 strict 字段
                    function_def.pop("strict", None)
                    
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

    # OpenAI stop -> Gemini generationConfig.stopSequences
    # 说明：部分第三方客户端（如 SillyTavern）会携带 stop 字段；
    # Gemini 的 REST 接口不接受顶层 stop，但支持 generationConfig.stopSequences。
    stop_value = getattr(request, "stop", None)
    if stop_value:
        if isinstance(stop_value, str):
            payload["generationConfig"]["stopSequences"] = [stop_value]
        elif isinstance(stop_value, list):
            payload["generationConfig"]["stopSequences"] = [str(x) for x in stop_value if x is not None]

    if "maxOutputTokens" not in generation_config:
        payload["generationConfig"]["maxOutputTokens"] = 32768

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

    return url, headers, normalize_gemini_payload(payload)


def _extract_gemini_block_message(response_json: dict) -> str | None:
    if not isinstance(response_json, dict):
        return None
    return (
        safe_get(response_json, "promptFeedback", "blockReasonMessage", default=None)
        or safe_get(response_json, "promptFeedback", "blockReason", default=None)
        or safe_get(response_json, "candidates", 0, "blockReason", default=None)
    )


def _normalize_gemini_http_error(error_message: dict) -> dict:
    """把 Gemini/FirebaseVertex 的 HTTP 错误归一化成可直接返回给用户的 message。"""
    if not isinstance(error_message, dict):
        return {"error": "Gemini HTTP Error", "status_code": 400, "details": str(error_message)}

    status_code = error_message.get("status_code") or 400
    details = error_message.get("details")

    message = None
    if isinstance(details, dict):
        message = (
            _extract_gemini_block_message(details)
            or safe_get(details, "error", "message", default=None)
            or safe_get(details, "message", default=None)
        )
    elif isinstance(details, str):
        message = details

    if not message:
        message = str(details) if details is not None else str(error_message)

    return {
        "error": error_message.get("error", "Gemini HTTP Error"),
        "status_code": int(status_code) if str(status_code).isdigit() else 400,
        "details": message,
    }


def gemini_json_process(response_json):
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
    
    # 清理掉 image_base64 如果没有内容，避免流式无内容触发图像处理
    if image_base64 and not image_base64.strip():
        image_base64 = None
    
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
        yield _normalize_gemini_http_error(error_message)
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
        is_thinking, reasoning_content, content, image_base64, function_call_name, function_full_response, finishReason, blockReason, promptTokenCount, candidatesTokenCount, totalTokenCount, thought_signature = gemini_json_process(first_resp)
        
        if blockReason and blockReason != "STOP":
            msg = _extract_gemini_block_message(first_resp) or blockReason
            yield {"error": f"Gemini Blocked: {blockReason}", "status_code": 400, "details": msg}
            return
        
        if not safe_get(first_resp, "candidates") and blockReason:
            msg = _extract_gemini_block_message(first_resp) or blockReason
            yield {"error": f"Gemini Blocked: {blockReason}", "status_code": 400, "details": msg}
            return

        # 获取 usage (可能在最后一个响应对象中)
        last_resp = parsed_data[-1]
        usage_metadata = safe_get(last_resp, "usageMetadata")
        prompt_tokens = safe_get(usage_metadata, "promptTokenCount", default=promptTokenCount)
        candidates_tokens = safe_get(usage_metadata, "candidatesTokenCount", default=candidatesTokenCount)
        total_tokens = safe_get(usage_metadata, "totalTokenCount", default=totalTokenCount)

        # 检查是否返回了有效内容
        has_content = content and content.strip()
        has_reasoning = reasoning_content and reasoning_content.strip()
        has_function_call = function_call_name is not None
        has_image = image_base64 is not None
        
        is_image_model = "-image" in model.lower() or "image-generation" in model.lower()
        
        # 图像模型必须有图片
        if is_image_model and not has_image:
            yield {
                "error": "Gemini image generation failed: no image was generated",
                "status_code": 502,
                "details": "Gemini image generation failed: no image was generated",
            }
            return
        
        # 普通模型必须有内容
        if not is_image_model and not has_content and not has_reasoning and not has_function_call:
            yield {
                "error": "Gemini returned empty response",
                "status_code": 502,
                "details": "Gemini returned empty response",
            }
            return

        role = safe_get(first_resp, "candidates", 0, "content", "role")
        if role == "model":
            role = "assistant"
        elif not role:
            role = "assistant"

        # 检查是否需要处理图像
        # 无论是否是专门的绘图模型，只要 Gemini 返回了图片（比如普通大模型使用 Python 代码执行工具画了图表），
        # 我们都统一将其转换为 Markdown 图片追加到文本内容中，确保在标准聊天客户端中可见，
        # 并防止被 utils.py 错误地转换为只包含 b64_json 的图片生成 API 格式。
        if image_base64:
            try:
                from ..log_config import logger
                logger.info(f"[Gemini] Processing image for non-stream response, model={model}")
                
                # 发送 SSE 注释作为 keepalive 并不适用这里，因为是非流式
                # 上传到图床
                image_url = await upload_image_to_0x0st("data:image/png;base64," + image_base64, max_size_mb=50.0)
                
                if image_url and image_url.startswith("http"):
                    content = (content or "") + f"\n\n![image]({image_url})"
                    image_base64 = None # 清除，防止 generate_no_stream_response 返回图像 API 格式
                else:
                    # 上传失败，返回 data URI 格式的 base64
                    content = (content or "") + f"\n\n![image](data:image/png;base64,{image_base64})"
                    image_base64 = None
            except Exception as e:
                from ..log_config import logger
                logger.error(f"[Gemini] Error processing image in non-stream: {e}")
                # 出错时保持原样，由 generate_no_stream_response 处理

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
            yield _normalize_gemini_http_error(error_message)
            return
        buffer = ""
        promptTokenCount = 0
        candidatesTokenCount = 0
        totalTokenCount = 0
        parts_json = ""
        
        # 用于追踪整个流中是否有有效内容
        has_content = False  # 是否有文本内容
        has_image = False    # 是否有图片
        has_function_call = False  # 是否有函数调用
        has_reasoning = False  # 是否有思维链
        stream_finished_normally = False  # 是否正常结束
        
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
                is_thinking, reasoning_content, content, image_base64, function_call_name, function_full_response, finishReason, blockReason, promptTokenCount, candidatesTokenCount, totalTokenCount, thought_signature = gemini_json_process(response_json)
                
                # 调试日志：记录每个 chunk 的关键信息
                from ..log_config import logger
                if image_base64:
                    logger.info(f"[Gemini] image_base64 received, length={len(image_base64)}, finishReason={finishReason}")
                if finishReason:
                    logger.info(f"[Gemini] finishReason={finishReason}, has_image={bool(image_base64)}, content_len={len(content) if content else 0}")

                # 追踪有效内容
                if is_thinking and reasoning_content:
                    has_reasoning = True
                if content and content.strip():
                    has_content = True
                if image_base64:
                    has_image = True
                if function_call_name:
                    has_function_call = True

                if is_thinking:
                    sse_string = await generate_sse_response(timestamp, model, reasoning_content=reasoning_content, thought_signature=thought_signature)
                    yield sse_string
                if not image_base64 and content:
                    sse_string = await generate_sse_response(timestamp, model, content=content, thought_signature=thought_signature)
                    yield sse_string

                if image_base64:
                    if "-image" not in model.lower() and "image-generation" not in model.lower():
                        pass # Ignored base64 from non-image model in streaming mode, usually duplicate or not supported by standard SSE
                    else:
                        image_size_mb = len(image_base64) * 3 / 4 / (1024 * 1024)
                        logger.info(f"[Gemini] Processing image, size={image_size_mb:.2f} MB")
                        
                        # 发送 SSE 注释作为 keepalive，防止客户端超时断开
                        # SSE 规范：以冒号开头的行是注释，客户端会忽略但能保持连接
                        yield ": uploading image\n\n"
                            
                        # 上传到图床（不压缩，保持原图质量）
                        image_url = await upload_image_to_0x0st("data:image/png;base64," + image_base64, max_size_mb=50.0)
                        
                        # 检查上传是否成功
                        if image_url and image_url.startswith("http"):
                            logger.info(f"[Gemini] Image uploaded successfully: {image_url}")
                            sse_string = await generate_sse_response(timestamp, model, content=f"\n\n![image]({image_url})", thought_signature=thought_signature)
                            yield sse_string
                        else:
                            # 上传失败，返回 data URI 格式的 base64（直接嵌入）
                            logger.warning(f"[Gemini] Image upload failed, returning inline base64 data URI")
                            sse_string = await generate_sse_response(
                                timestamp, model, 
                                content=f"\n\n![image](data:image/png;base64,{image_base64})", 
                                thought_signature=thought_signature
                            )
                            yield sse_string

                if function_call_name:
                    sse_string = await generate_sse_response(timestamp, model, content=None, tools_id="chatcmpl-9inWv0yEtgn873CxMBzHeCeiHctTV", function_call_name=function_call_name, thought_signature=thought_signature)
                    yield sse_string
                if function_full_response:
                    sse_string = await generate_sse_response(timestamp, model, content=None, tools_id="chatcmpl-9inWv0yEtgn873CxMBzHeCeiHctTV", function_call_name=None, function_call_content=function_full_response, thought_signature=thought_signature)
                    yield sse_string

                if parts_json == "[]" or (blockReason and blockReason != "STOP"):
                    msg = _extract_gemini_block_message(response_json) or (blockReason or "Empty Response")
                    yield {"error": f"Gemini Blocked: {blockReason or 'Empty Response'}", "status_code": 400, "details": msg}
                    return
                elif finishReason and finishReason not in ["STOP", "MAX_TOKENS"]:
                    # 非正常结束原因（如 SAFETY, RECITATION 等）
                    yield {"error": f"Gemini Finish Reason: {finishReason}", "status_code": 400, "details": f"{finishReason}"}
                    return
                elif finishReason:
                    # 正常结束（STOP 或 MAX_TOKENS）
                    # 注意：部分上游/模型可能会直接返回 finishReason=STOP 但不包含任何内容。
                    # 若在本次流中没有任何有效内容，则不要先发 stop chunk（否则可能被上层判为“空响应”）。
                    stream_finished_normally = True

                    if has_content or has_reasoning or has_function_call or has_image:
                        sse_string = await generate_sse_response(timestamp, model, stop="stop")
                        yield sse_string

                    break

                parts_json = ""

        # 检查图像生成模型是否实际返回了图片
        # 对于 image 模型，如果只有思维链但没有图片，视为生成失败
        is_image_model = "-image" in model.lower() or "image-generation" in model.lower()
        
        if is_image_model and not has_image:
            # 图像生成模型但没有生成图片
            error_detail = {
                "reason": "no_image_generated",
                "has_reasoning": has_reasoning,
                "has_content": has_content,
                "model": model,
                "stream_finished_normally": stream_finished_normally,
            }
            logger.warning(f"[Gemini] Image model returned no image: {error_detail}")
            yield {
                "error": "Gemini image generation failed: no image was generated",
                "status_code": 502,
                "details": "Gemini image generation failed: no image was generated",
            }
            return
        
        # 检查普通模型是否返回了有效内容
        if not is_image_model and not has_content and not has_reasoning and not has_function_call:
            logger.warning(f"[Gemini] Empty response: no content, reasoning, or function call")
            yield {
                "error": "Gemini returned empty response",
                "status_code": 502,
                "details": "Gemini returned empty response",
            }
            return

        # 如果流没有正常结束（没有收到 finishReason），确保发送 finish_reason
        # 同样：只有当已经产生过有效内容时才补发 stop，避免被上层当成“空响应”。
        if not stream_finished_normally:
            logger.warning(f"[Gemini] Stream ended without finishReason, sending stop signal")
            if has_content or has_reasoning or has_function_call or has_image:
                sse_string = await generate_sse_response(timestamp, model, stop="stop")
                yield sse_string
        
        # 发送 usage chunk（如果有）
        if totalTokenCount > 0:
            sse_string = await generate_sse_response(timestamp, model, None, None, None, None, None, totalTokenCount, promptTokenCount, candidatesTokenCount)
            yield sse_string

    yield "data: [DONE]" + end_of_line


async def fetch_gemini_models(client, provider):
    """获取 Gemini API 的模型列表。

    Gemini 的 `models.list` 接口默认只返回一页（通常约 50 条）。这里显式拉取分页，直到没有 nextPageToken。
    """
    from ..log_config import logger

    base_url = provider.get('base_url', 'https://generativelanguage.googleapis.com/v1beta').rstrip('/')
    api_key = provider.get('api')
    if isinstance(api_key, list):
        api_key = api_key[0] if api_key else None

    url = f"{base_url}/models"

    headers = {
        'Content-Type': 'application/json',
        # 使用请求头认证，避免 URL 参数中的特殊字符问题
        'x-goog-api-key': api_key,
    }

    # 尽量减少请求次数：优先用更大的 pageSize；同时加上上限防止异常循环
    page_size = 1000
    max_pages = 20
    max_total = 5000

    models: list[str] = []
    seen: set[str] = set()

    page_token: str | None = None
    for _ in range(max_pages):
        params: dict[str, str | int] = {'pageSize': page_size}
        if page_token:
            params['pageToken'] = page_token

        response = await client.get(url, headers=headers, params=params)
        response.raise_for_status()

        data = response.json()
        if not isinstance(data, dict):
            break

        # Gemini 返回格式: {"models": [{"name": "models/gemini-pro", ...}], "nextPageToken": "..."}
        for m in data.get('models', []) or []:
            if not isinstance(m, dict):
                continue
            name = m.get('name', '')
            if not isinstance(name, str):
                continue

            # 移除 "models/" 前缀
            if name.startswith('models/'):
                name = name[7:]

            name = name.strip()
            if not name or name in seen:
                continue
            seen.add(name)
            models.append(name)

            if len(models) >= max_total:
                logger.warning(f"[Gemini] models list truncated at {max_total} items")
                return models

        page_token = data.get('nextPageToken') or data.get('next_page_token')
        if not page_token:
            break

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
        passthrough_payload_adapter=patch_passthrough_gemini_payload,
        response_adapter=fetch_gemini_response,
        stream_adapter=fetch_gemini_response_stream,
        models_adapter=fetch_gemini_models,
    )
