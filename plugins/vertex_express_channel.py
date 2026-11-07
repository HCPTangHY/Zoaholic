"""
Vertex Express 渠道插件

这是一个 Vertex Express API 的渠道插件，支持通过 Express Key 访问 Google Vertex AI。
与标准 Vertex AI 不同，Express Key 不需要 OAuth2 认证，只需简单的 API Key。

特点：
1. 自动从 Express Key 提取 Project ID
2. 支持自定义 base_url
3. 完整复用 gemini_channel 的请求构建、流式与非流式响应处理逻辑
"""

import re
from contextlib import aclosing
from typing import TYPE_CHECKING, Dict

if TYPE_CHECKING:
    from core.plugins import PluginManager

# ==================== 插件元信息 ====================

PLUGIN_INFO = {
    "name": "vertex_express_channel",
    "version": "1.1.0",
    "description": "Vertex Express 渠道插件，支持通过 Express Key 访问 Google Vertex AI",
    "author": "Zoaholic",
    "dependencies": [],
    "metadata": {
        "category": "channel",
        "tags": ["vertex", "google", "gemini", "express"],
    },
}

EXTENSIONS = ["channels:vertex-express"]

# ==================== 渠道适配器实现 ====================

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
    """处理 Vertex Express 流式响应（直接复用 gemini_channel）"""
    from core.channels.gemini_channel import fetch_gemini_response_stream

    if "streamGenerateContent" in url and "alt=sse" not in url:
        url += "&alt=sse"

    async with aclosing(fetch_gemini_response_stream(client, url, headers, payload, model, timeout)) as source:
        async for chunk in source:
            yield chunk


async def fetch_vertex_express_response(client, url, headers, payload, model, timeout):
    """处理 Vertex Express 非流式响应（直接复用 gemini_channel）"""
    from core.channels.gemini_channel import fetch_gemini_response

    if "streamGenerateContent" in url:
        url = url.replace("streamGenerateContent", "generateContent")

    async with aclosing(fetch_gemini_response(client, url, headers, payload, model, timeout)) as source:
        async for chunk in source:
            yield chunk


# ==================== 渠道定义 ====================

class VertexExpressChannelAdapter:
    """Vertex Express 渠道适配器类"""

    id = "vertex-express"
    type_name = "vertex-express"

    request_adapter = staticmethod(get_vertex_express_payload)
    stream_adapter = staticmethod(fetch_vertex_express_response_stream)
    response_adapter = staticmethod(fetch_vertex_express_response)


# ==================== 插件生命周期函数 ====================

def setup(manager: "PluginManager"):
    """插件初始化"""
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

    from core.channels.registry import register_channel, unregister_channel
    from core.channels.gemini_channel import patch_passthrough_gemini_payload, gemini_stream_classifier

    try:
        unregister_channel(VertexExpressChannelAdapter.id)
        register_channel(
            id=VertexExpressChannelAdapter.id,
            type_name="gemini",
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
    """插件清理"""
    manager.unregister_extension("channels", "vertex-express")
    from core.channels.registry import unregister_channel
    unregister_channel("vertex-express")
    print(f"[{PLUGIN_INFO['name']}] Channel 'vertex-express' unregistered!")


def unload():
    """插件卸载回调"""
    print(f"[{PLUGIN_INFO['name']}] Plugin unloading...")
