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
    """从 Express Key 中提取 Project ID（支持 project_id:AQ.xxx 直解析、缓存与代理探测）"""
    from core.channels.vertex_channel import extract_vertex_express_project_id
    pid, _ = await extract_vertex_express_project_id(key, base_url)
    if pid:
        _project_id_cache[(base_url, key)] = pid
    return pid



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
    构建 Vertex Express API 的请求 payload（复用 gemini_channel 最新 Schema/签名/65k/Safety 逻辑，统一走 locations/global）
    """
    from core.channels.gemini_channel import get_gemini_payload
    from core.channels.vertex_channel import extract_vertex_express_project_id
    from core.utils import get_model_dict

    raw_base_url = provider.get("base_url", "https://aiplatform.googleapis.com").rstrip("/")
    ver_match = re.search(r"/(v1(?:beta\d*)?)$", raw_base_url)
    api_ver = ver_match.group(1) if ver_match else "v1beta1"
    api_root = re.sub(r"/v1(?:beta\d*)?$", "", raw_base_url)

    project_id, actual_key = await extract_vertex_express_project_id(api_key or "", api_root)
    if not project_id:
        project_id = str(provider.get("project_id") or (provider.get("preferences") or {}).get("project_id") or "").strip()

    temp_provider = provider.copy()
    temp_provider["base_url"] = f"{api_root}/{api_ver}"
    _, headers, payload = await get_gemini_payload(request, "vertex-express", temp_provider, actual_key)

    headers.pop("x-goog-api-key", None)
    headers.pop("Authorization", None)

    model_dict = get_model_dict(provider)
    original_model = model_dict.get(request.model, request.model)
    if "gemini-2.0-flash-exp-image-generation" in original_model:
        original_model = original_model.replace(
            "gemini-2.0-flash-exp-image-generation",
            "gemini-2.5-flash-image-preview",
        )

    gemini_stream = "streamGenerateContent" if request.stream else "generateContent"
    sse_suffix = "&alt=sse" if request.stream else ""
    if project_id:
        url = (
            f"{api_root}/{api_ver}/projects/{project_id}/locations/global"
            f"/publishers/google/models/{original_model}:{gemini_stream}?key={actual_key}{sse_suffix}"
        )
    else:
        url = (
            f"{api_root}/{api_ver}/publishers/google/models/{original_model}:{gemini_stream}?key={actual_key}{sse_suffix}"
        )

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
        from core.channels.gemini_channel import patch_passthrough_gemini_payload, gemini_stream_classifier
        from core.channels.registry import unregister_channel
        unregister_channel(VertexExpressChannelAdapter.id)
        register_channel(
            id=VertexExpressChannelAdapter.id, # "vertex-express"
            type_name="gemini", # <--- 声明为 gemini 类型，自动命中方言透传
            default_base_url="https://aiplatform.googleapis.com",
            auth_header=None,
            description="Google Vertex AI (Express Key)",
            request_adapter=VertexExpressChannelAdapter.request_adapter,
            passthrough_payload_adapter=patch_passthrough_gemini_payload,
            passthrough_dialects=["gemini"],
            stream_adapter=VertexExpressChannelAdapter.stream_adapter,
            stream_event_classifier=gemini_stream_classifier,
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