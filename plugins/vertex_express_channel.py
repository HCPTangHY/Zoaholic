"""
Vertex Express 渠道插件

这是一个 Vertex Express API 的渠道插件，支持通过 Express Key 访问 Google Vertex AI。
与标准 Vertex AI 不同，Express Key 不需要 OAuth2 认证，只需简单的 API Key。

特点：
1. 自动从 Express Key 提取 Project ID
2. 支持自定义 base_url
3. 自动设置安全设置为最宽松级别
4. 支持流式和非流式响应

配置示例（在 config.yaml 中）:
```yaml
providers:
  - provider: vertex-express
    base_url: https://aiplatform.googleapis.com  # 可选，默认值
    api:
      - AQ.xxxxx  # Vertex Express Key
    model:
      - gemini-2.5-flash-preview-05-20
      - gemini-2.5-pro-preview-06-05
```
"""

import re
import json
import copy
import asyncio
from datetime import datetime
from uuid import uuid4
from typing import TYPE_CHECKING, Dict, Any, Optional

import httpx

if TYPE_CHECKING:
    from core.plugins import PluginManager

# ==================== 插件元信息 ====================

PLUGIN_INFO = {
    "name": "vertex_express_channel",
    "version": "1.0.0",
    "description": "Vertex Express 渠道插件，支持通过 Express Key 访问 Google Vertex AI",
    "author": "Zoaholic",
    "dependencies": [],
    "metadata": {
        "category": "channel",
        "tags": ["vertex", "google", "gemini", "express"],
    },
}

# 声明此插件提供的扩展
EXTENSIONS = ["channels:vertex-express"]

# ==================== Project ID 缓存 ====================

# 缓存格式: (base_url, key) -> project_id
_project_id_cache: Dict[tuple, str] = {}


async def get_project_id(key: str, base_url: str = "https://aiplatform.googleapis.com") -> str:
    """
    从 Express Key 中提取 Project ID
    
    通过发送一个轻量级请求并从错误信息中提取 Project ID
    
    Args:
        key: Express API Key
        base_url: API 基础 URL，默认为 https://aiplatform.googleapis.com
    """
    cache_key = (base_url, key)
    if cache_key in _project_id_cache:
        return _project_id_cache[cache_key]

    # 使用一个轻量级的 endpoint 来探测 project ID
    # 确保 base_url 格式正确
    base_url = base_url.rstrip('/')
    url = f"{base_url}/v1/publishers/google/models/gemini-2.0-flash:generateContent?key={key}"
    headers = {'Content-Type': 'application/json'}
    data = '{}'

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, headers=headers, content=data, timeout=30)
            
            # 我们期望这里报错，并从报错信息中提取 Project ID
            if response.status_code in (400, 403, 404):
                try:
                    error_data = response.json()
                    error_message = error_data.get("error", {}).get("message", "")
                    
                    # 尝试匹配 projects/YOUR_PROJECT_ID/locations/
                    match = re.search(r'projects/([^/]+)/locations/', error_message)
                    if match:
                        project_id = match.group(1)
                        _project_id_cache[cache_key] = project_id
                        return project_id
                except json.JSONDecodeError:
                    pass
            
            raise ValueError(f"Failed to extract project ID: status={response.status_code}")
            
        except httpx.HTTPError as e:
            raise ValueError(f"Failed to extract project ID: {e}")

    raise ValueError("Could not extract project ID from key response.")


# ==================== 格式化函数 ====================

def format_text_message(text: str) -> dict:
    """格式化文本消息为 Gemini 格式"""
    return {"text": text}


async def format_image_message(image_url: str) -> dict:
    """格式化图片消息为 Gemini 格式"""
    from core.utils import get_base64_image
    
    base64_image, image_type = await get_base64_image(image_url)
    return {
        "inlineData": {
            "mimeType": image_type,
            "data": base64_image.split(",")[1],
        }
    }


# ==================== 渠道适配器实现 ====================

# 需要 65k token 限制的模型
GEMINI_MAX_TOKEN_65K_MODELS = [
    "gemini-2.5-pro", "gemini-2.0-pro", 
    "gemini-2.0-flash-thinking", "gemini-2.5-flash"
]


async def get_vertex_express_payload(request, engine, provider, api_key=None):
    """
    构建 Vertex Express API 的请求 payload
    
    Args:
        request: 请求对象 (RequestModel)
        engine: 引擎类型
        provider: 渠道提供者配置
        api_key: Express API Key
        
    Returns:
        tuple: (url, headers, payload)
    """
    from core.utils import get_model_dict, safe_get
    
    headers = {
        'Content-Type': 'application/json'
    }

    # 获取映射后的实际模型ID
    model_dict = get_model_dict(provider)
    original_model = model_dict.get(request.model, request.model)

    # 构建 URL 基础部分
    base_url = provider.get('base_url', 'https://aiplatform.googleapis.com').rstrip('/')

    # 获取 Project ID（传入 base_url 用于试探请求）
    project_id = await get_project_id(api_key, base_url)

    # 流式/非流式端点
    if request.stream:
        gemini_stream = "streamGenerateContent"
    else:
        gemini_stream = "generateContent"

    # 模型名称映射处理
    if "gemini-2.0-flash-exp-image-generation" in original_model:
        original_model = original_model.replace(
            "gemini-2.0-flash-exp-image-generation", 
            "gemini-2.5-flash-image-preview"
        )

    # 构建完整 URL
    url = f"{base_url}/v1/projects/{project_id}/locations/global/publishers/google/models/{original_model}:{gemini_stream}?key={api_key}"

    # 构建消息
    messages = []
    systemInstruction = None
    system_prompt = ""
    function_arguments = None

    try:
        from core.models import Message
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
            # 确保 role 字段存在
            if not hasattr(msg, 'role') or not msg.role:
                messages.append({"role": "user", "parts": parts})
            else:
                messages.append({"role": msg.role, "parts": parts})
        elif msg.role == "system":
            # 系统提示词处理逻辑
            sys_text = "".join([p.get("text", "") for p in parts if "text" in p])
            sys_text = re.sub(r"_+", "_", sys_text)
            system_prompt = system_prompt + "\n\n" + sys_text

    if system_prompt.strip():
        systemInstruction = {"parts": [{"text": system_prompt}]}

    # 强制设置安全设置为最宽松级别
    safety_settings = [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_CIVIC_INTEGRITY", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_UNSPECIFIED", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_IMAGE_HATE", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_IMAGE_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_IMAGE_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_IMAGE_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_JAILBREAK", "threshold": "BLOCK_NONE"},
    ]

    payload = {
        "contents": messages or [{"role": "user", "parts": [{"text": "No messages"}]}],
        "safetySettings": safety_settings,
    }

    if systemInstruction:
        payload["systemInstruction"] = systemInstruction

    # 生成配置
    generation_config = {}

    # 不需要转发的字段
    miss_fields = [
        'model', 'messages', 'stream', 'tool_choice', 'presence_penalty',
        'frequency_penalty', 'n', 'user', 'include_usage', 'logprobs',
        'top_logprobs', 'response_format', 'stream_options', 'prompt',
        'size', 'max_completion_tokens', 'extra_body', 'thinking',
        'chat_template_kwargs', 'min_p', 'reasoning_effort',
    ]

    def process_tool_parameters(data):
        """处理工具参数，移除 Gemini 不支持的字段"""
        if isinstance(data, dict):
            data.pop("additionalProperties", None)
            if "default" in data:
                default_value = data.pop("default")
                description = data.get("description", "")
                data["description"] = f"{description}\nDefault: {default_value}"
            for value in data.values():
                process_tool_parameters(value)
        elif isinstance(data, list):
            for item in data:
                process_tool_parameters(item)

    for field, value in request.model_dump(exclude_unset=True).items():
        if field not in miss_fields and value is not None:
            # 跳过某些模型的 tools
            if field == "tools" and any(m in original_model for m in [
                "gemini-2.0-flash-thinking", 
                "gemini-2.5-flash-image", 
                "gemini-3-pro-image"
            ]):
                continue
            
            if field == "tools":
                processed_tools = []
                for tool in value:
                    function_def = tool["function"]
                    if "parameters" in function_def:
                        process_tool_parameters(function_def["parameters"])
                    if function_def["name"] not in ["googleSearch"]:
                        processed_tools.append({"function": function_def})

                if processed_tools:
                    payload.update({
                        "tools": [{
                            "function_declarations": [tool["function"] for tool in processed_tools]
                        }],
                        "tool_config": {
                            "function_calling_config": {
                                "mode": "AUTO"
                            }
                        }
                    })
            elif field == "temperature":
                # 特定模型的温度限制
                if any(m in original_model for m in ["gemini-2.5-flash-image", "gemini-3-pro-image"]):
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

    # 设置默认的 maxOutputTokens
    if "maxOutputTokens" not in generation_config:
        payload["generationConfig"]["maxOutputTokens"] = 32768

    # 处理图片生成模型
    if "-image" in original_model:
        payload["generationConfig"]["responseModalities"] = ["TEXT", "IMAGE"]
        # 移除不兼容的配置
        if "thinkingConfig" in payload["generationConfig"]:
            del payload["generationConfig"]["thinkingConfig"]
        if "responseMimeType" in payload["generationConfig"]:
            del payload["generationConfig"]["responseMimeType"]

    # 处理 OpenAI extra_body.google 配置
    request_data = request.model_dump(exclude_unset=True)
    extra_body = request_data.get('extra_body')
    
    if isinstance(extra_body, dict):
        google_config = extra_body.get('google', {})
        if isinstance(google_config, dict) and google_config:
            def _snake_to_camel(s: str) -> str:
                parts = s.split('_')
                return parts[0] + ''.join(word.capitalize() for word in parts[1:])
            
            def _convert_keys(obj):
                if isinstance(obj, dict):
                    return {_snake_to_camel(k): _convert_keys(v) for k, v in obj.items()}
                elif isinstance(obj, list):
                    return [_convert_keys(item) for item in obj]
                else:
                    return obj
            
            def _deep_merge(target, source):
                for key, value in source.items():
                    if key in target and isinstance(target[key], dict) and isinstance(value, dict):
                        _deep_merge(target[key], value)
                    else:
                        target[key] = value
            
            converted_config = _convert_keys(google_config)
            _deep_merge(payload["generationConfig"], converted_config)

    # 处理 gemini-2.5 系列的思考配置
    if "gemini-2.5" in original_model and "gemini-2.5-flash-image" not in original_model:
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

    return url, headers, payload


async def fetch_vertex_express_response_stream(client, url, headers, payload, model, timeout):
    """
    处理 Vertex Express 流式响应
    
    复用 Gemini 的流式响应处理逻辑
    """
    from core.channels.gemini_channel import fetch_gemini_response_stream
    
    # 如果 URL 中包含 streamGenerateContent，添加 alt=sse 参数
    if "streamGenerateContent" in url and "alt=sse" not in url:
        url += "&alt=sse"
    
    async for chunk in fetch_gemini_response_stream(client, url, headers, payload, model, timeout):
        yield chunk


async def fetch_vertex_express_response(client, url, headers, payload, model, timeout):
    """
    处理 Vertex Express 非流式响应
    """
    from core.utils import generate_no_stream_response, safe_get
    from core.channels.gemini_channel import gemini_json_process
    
    timestamp = int(datetime.timestamp(datetime.now()))
    
    # 确保不是流式请求
    if "streamGenerateContent" in url:
        url = url.replace("streamGenerateContent", "generateContent")
    
    json_payload = await asyncio.to_thread(json.dumps, payload)
    response = await client.post(url, headers=headers, content=json_payload, timeout=timeout)
    
    if response.status_code != 200:
        # ... 保持错误处理逻辑 ...
        error_data = {
            "error": {
                "message": f"Vertex Express API error: {response.status_code}",
                "type": "api_error",
                "code": response.status_code,
                "details": response.text
            }
        }
        yield f"data: {json.dumps(error_data)}\n\n"
        return
    
    try:
        response_json = response.json()
        # 修改原因：gemini_json_process 现在会返回全部 functionCall，Vertex Express 复用该解析函数时必须同步新返回值。
        # 修改方式：去掉错误的 await，并接收 function_calls_list 后转换为 OpenAI tool_calls_list。
        # 目的：避免多个 Gemini 工具调用在 Vertex Express 非流式路径中丢失或共享索引。
        is_thinking, reasoning_content, content, image_base64, function_call_name, function_full_response, finishReason, blockReason, promptTokenCount, candidatesTokenCount, totalTokenCount, thought_signature, function_calls_list = gemini_json_process(response_json)
        
        if blockReason and blockReason != "STOP":
            yield {"error": f"Vertex Blocked: {blockReason}", "status_code": 400, "details": response_json}
            return

        tool_calls_list = []
        for function_index, function_call in enumerate(function_calls_list):
            arguments = function_call.get("args")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False)
            tool_calls_list.append({
                "index": function_index,
                "id": f"call_{uuid4().hex[:24]}",
                "type": "function",
                "function": {
                    "name": function_call.get("name", ""),
                    "arguments": arguments,
                },
            })

        result = await generate_no_stream_response(
            timestamp, model,
            content=content,
            role="assistant",
            total_tokens=totalTokenCount,
            prompt_tokens=promptTokenCount,
            completion_tokens=candidatesTokenCount,
            reasoning_content=reasoning_content,
            image_base64=image_base64,
            thought_signature=thought_signature,
            tool_calls_list=tool_calls_list or None,
        )
        yield result
        
    except Exception as e:
        yield f"data: {json.dumps({'error': str(e)})}\n\n"


# ==================== 渠道定义 ====================

class VertexExpressChannelAdapter:
    """
    Vertex Express 渠道适配器类
    """
    
    id = "vertex-express"
    type_name = "vertex-express"
    
    # 适配器函数
    request_adapter = staticmethod(get_vertex_express_payload)
    stream_adapter = staticmethod(fetch_vertex_express_response_stream)
    response_adapter = staticmethod(fetch_vertex_express_response)


# ==================== 插件生命周期函数 ====================

def setup(manager: "PluginManager"):
    """
    插件初始化
    """
    # 注册到插件系统
    manager.register_extension(
        extension_point="channels",
        extension_id="vertex-express",
        implementation=VertexExpressChannelAdapter,
        priority=100,
        metadata={
            "description": "Vertex Express 渠道适配器，支持通过 Express Key 访问 Google Vertex AI",
            "supported_features": ["chat", "stream", "vision", "tools"],
        },
        plugin_name=PLUGIN_INFO["name"],
    )
    
    # 注册到渠道注册表
    from core.channels.registry import register_channel
    
    try:
        register_channel(
            id=VertexExpressChannelAdapter.id, # "vertex-express"
            type_name="gemini", # <--- 声明为 gemini 类型，自动命中方言透传
            default_base_url="https://aiplatform.googleapis.com",
            auth_header=None,
            description="Google Vertex AI (Express Key)",
            request_adapter=VertexExpressChannelAdapter.request_adapter,
            stream_adapter=VertexExpressChannelAdapter.stream_adapter,
            response_adapter=VertexExpressChannelAdapter.response_adapter,
            models_adapter=None,
        )
        print(f"[{PLUGIN_INFO['name']}] Channel 'vertex-express' registered successfully!")
    except ValueError as e:
        print(f"[{PLUGIN_INFO['name']}] Channel registration skipped: {e}")


def teardown(manager: "PluginManager"):
    """
    插件清理
    
    当插件被卸载时调用。
    """
    # 注销扩展
    manager.unregister_extension("channels", "vertex-express")
    
    # 从渠道注册表注销
    from core.channels.registry import unregister_channel
    unregister_channel("vertex-express")
    
    # 清理缓存
    _project_id_cache.clear()
    
    print(f"[{PLUGIN_INFO['name']}] Channel 'vertex-express' unregistered!")


def unload():
    """
    插件卸载回调
    """
    _project_id_cache.clear()
    print(f"[{PLUGIN_INFO['name']}] Plugin unloading...")