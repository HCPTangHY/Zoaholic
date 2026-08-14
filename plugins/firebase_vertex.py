"""
Firebase Vertex AI 渠道插件

实现标准 Gemini 格式和透传的渠道 firebaseVertex。
Base URL 格式: https://firebasevertexai.googleapis.com/v1beta/projects/{key}/locations/global/publishers/google/models/{model}:{method}?key={key}
"""

import json
import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Dict, Any, Optional

if TYPE_CHECKING:
    from core.plugins import PluginManager

# ==================== 插件元信息 ====================

PLUGIN_INFO = {
    "name": "firebase_vertex",
    "version": "1.0.0",
    "description": "Firebase Vertex AI 渠道插件，支持通过 API Key 访问并自动构建 Project URL",
    "author": "Zoaholic",
    "metadata": {
        "category": "channel",
        "tags": ["firebase", "vertex", "google", "gemini"],
    },
}

# 声明此插件提供的扩展
EXTENSIONS = ["channels:firebaseVertex"]

# ==================== 渠道适配器实现 ====================

async def get_firebase_vertex_payload(request, engine, provider, api_key=None):
    """
    构建 Firebase Vertex AI 的请求 payload
    支持 key 格式: project_id:api_key
    """
    from core.channels.gemini_channel import get_gemini_payload
    from core.utils import get_model_dict
    
    # 解析 key 格式: project_id:api_key
    if api_key and ":" in api_key:
        project_id, actual_api_key = api_key.split(":", 1)
    else:
        # 降级处理
        project_id = api_key
        actual_api_key = api_key

    # 1. 调用标准的 gemini payload 构建逻辑
    temp_provider = provider.copy()
    if 'base_url' not in temp_provider or not temp_provider['base_url']:
        temp_provider['base_url'] = "https://firebasevertexai.googleapis.com/v1beta"
        
    _, headers, payload = await get_gemini_payload(request, engine, temp_provider, actual_api_key)
    
    # 2. 获取映射后的实际模型 ID
    model_dict = get_model_dict(provider)
    original_model = model_dict.get(request.model, request.model)
    
    # 3. 构建符合要求的 URL
    # 用户要求的结构: projects 部分用 project_id 填充
    if request.stream:
        method = "streamGenerateContent"
        params = "?alt=sse"
    else:
        method = "generateContent"
        params = ""
        
    # 构建完整 URL: https://firebasevertexai.googleapis.com/v1beta/projects/{project_id}/locations/global/publishers/google/models/{model}:{method}
    url = f"https://firebasevertexai.googleapis.com/v1beta/projects/{project_id}/locations/global/publishers/google/models/{original_model}:{method}{params}"
    
    # 4. 确保认证头存在，使用真实的 api_key，伪装成真实请求
    headers['x-goog-api-key'] = actual_api_key
    # 移除可能存在的 authorization 头以避免冲突（Firebase 通常只用 x-goog-api-key）
    headers.pop('Authorization', None)
    
    return url, headers, payload


async def fetch_firebase_vertex_response_stream(client, url, headers, payload, model, timeout):
    """复用 Gemini 的流式处理逻辑"""
    from core.channels.gemini_channel import fetch_gemini_response_stream
    async for chunk in fetch_gemini_response_stream(client, url, headers, payload, model, timeout):
        yield chunk


async def fetch_firebase_vertex_response(client, url, headers, payload, model, timeout):
    """复用 Gemini 的非流式处理逻辑"""
    from core.channels.gemini_channel import fetch_gemini_response
    async for chunk in fetch_gemini_response(client, url, headers, payload, model, timeout):
        yield chunk


class FirebaseVertexChannelAdapter:
    """
    Firebase Vertex AI 渠道适配器
    """
    id = "firebaseVertex"
    type_name = "firebaseVertex"
    
    request_adapter = staticmethod(get_firebase_vertex_payload)
    stream_adapter = staticmethod(fetch_firebase_vertex_response_stream)
    response_adapter = staticmethod(fetch_firebase_vertex_response)


# ==================== 插件生命周期函数 ====================

def setup(manager: "PluginManager"):
    """插件初始化"""
    # 1. 注册扩展到插件管理器
    manager.register_extension(
        extension_point="channels",
        extension_id="firebaseVertex",
        implementation=FirebaseVertexChannelAdapter,
        metadata={
            "description": "Firebase Vertex AI 渠道，通过 API Key 自动路由",
            "supported_features": ["chat", "stream", "vision", "tools"],
        },
        plugin_name=PLUGIN_INFO["name"],
    )
    
    # 2. 注册到核心渠道注册表，使其在界面可选
    from core.channels.registry import register_channel
    try:
        register_channel(
            id="firebaseVertex",
            type_name="gemini",  # 声明为 gemini 类型，利用现有的 Gemini 方言处理逻辑
            default_base_url="https://firebasevertexai.googleapis.com/v1beta",
            auth_header="x-goog-api-key: {api_key}",
            description="Firebase Vertex AI",
            request_adapter=FirebaseVertexChannelAdapter.request_adapter,
            stream_adapter=FirebaseVertexChannelAdapter.stream_adapter,
            response_adapter=FirebaseVertexChannelAdapter.response_adapter,
            passthrough_dialects=["gemini"],
        )
        print(f"[{PLUGIN_INFO['name']}] Channel 'firebaseVertex' registered successfully.")
    except Exception as e:
        print(f"[{PLUGIN_INFO['name']}] Channel registration failed: {e}")


def teardown(manager: "PluginManager"):
    """插件清理"""
    # 1. 从插件管理器注销
    manager.unregister_extension("channels", "firebaseVertex")
    
    # 2. 从核心渠道注册表注销
    from core.channels.registry import unregister_channel
    try:
        unregister_channel("firebaseVertex")
        print(f"[{PLUGIN_INFO['name']}] Channel 'firebaseVertex' unregistered.")
    except:
        pass
