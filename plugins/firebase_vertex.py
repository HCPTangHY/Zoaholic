"""
Firebase Vertex AI 渠道插件

实现标准 Gemini 格式和透传的渠道 firebaseVertex。
Base URL 格式: https://firebasevertexai.googleapis.com/v1beta/projects/{key}/locations/global/publishers/google/models/{model}:{method}?key={key}
"""

import json
import asyncio
from contextlib import aclosing
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
    async with aclosing(fetch_gemini_response_stream(client, url, headers, payload, model, timeout)) as source:
        async for chunk in source:
            yield chunk


async def fetch_firebase_vertex_response(client, url, headers, payload, model, timeout):
    """复用 Gemini 的非流式处理逻辑"""
    from core.channels.gemini_channel import fetch_gemini_response
    async with aclosing(fetch_gemini_response(client, url, headers, payload, model, timeout)) as source:
        async for chunk in source:
            yield chunk


async def patch_passthrough_firebase_vertex_payload(payload, modifications, request, engine, provider, api_key=None):
    from core.channels.gemini_channel import patch_passthrough_gemini_payload
    return await patch_passthrough_gemini_payload(payload, modifications, request, engine or "firebaseVertex", provider, api_key)


class FirebaseVertexChannelAdapter:
    """
    Firebase Vertex AI 渠道适配器
    """
    id = "firebaseVertex"
    type_name = "firebaseVertex"
    
    request_adapter = staticmethod(get_firebase_vertex_payload)
    passthrough_payload_adapter = staticmethod(patch_passthrough_firebase_vertex_payload)
    stream_adapter = staticmethod(fetch_firebase_vertex_response_stream)
    response_adapter = staticmethod(fetch_firebase_vertex_response)


# ==================== 插件生命周期函数 ====================


def _install_vacuum_hotfix():
    """Zero-restart hotfix for main.py cleanup_expired_raw_data incremental_vacuum cursor consumption."""
    try:
        import aiosqlite
        if getattr(aiosqlite.Connection, "_zoaholic_vacuum_hotfix_v1", False):
            return
        orig_execute = aiosqlite.Connection.execute

        async def _patched_execute(self, sql, parameters=None):
            sql_str = str(sql or "").strip()
            if sql_str.upper().startswith("PRAGMA INCREMENTAL_VACUUM"):
                cur = await orig_execute(self, "PRAGMA incremental_vacuum(50000)", parameters)
                try:
                    await cur.fetchall()
                    cur2 = await orig_execute(self, "PRAGMA wal_checkpoint(PASSIVE)")
                    await cur2.fetchall()
                except Exception:
                    pass
                return cur
            return await orig_execute(self, sql, parameters)

        aiosqlite.Connection._orig_execute = orig_execute
        aiosqlite.Connection.execute = _patched_execute
        aiosqlite.Connection._zoaholic_vacuum_hotfix_v1 = True
        print("[firebase_vertex] Installed zero-restart SQLite incremental_vacuum hotfix.")
    except Exception as e:
        print(f"[firebase_vertex] Vacuum hotfix install skipped: {e}")

def _install_gemini_vertex_channel_hotfix():
    """Zero-restart hot reload of gemini_channel & vertex_channel request/passthrough adapters."""
    try:
        import importlib, sys
        from core.channels.registry import get_channel

        gc_mod = sys.modules.get("core.channels.gemini_channel")
        if gc_mod is not None:
            gc_mod = importlib.reload(gc_mod)
        else:
            from core.channels import gemini_channel as gc_mod

        vc_mod = sys.modules.get("core.channels.vertex_channel")
        if vc_mod is not None:
            vc_mod = importlib.reload(vc_mod)
        else:
            from core.channels import vertex_channel as vc_mod

        gem_ch = get_channel("gemini")
        if gem_ch is not None:
            gem_ch.request_adapter = gc_mod.get_gemini_payload
            gem_ch.passthrough_payload_adapter = gc_mod.patch_passthrough_gemini_payload
            gem_ch.stream_event_classifier = gc_mod.gemini_stream_classifier

        vert_ch = get_channel("vertex-gemini")
        if vert_ch is not None:
            vert_ch.request_adapter = vc_mod.get_vertex_gemini_payload
            vert_ch.passthrough_payload_adapter = gc_mod.patch_passthrough_gemini_payload
            vert_ch.stream_event_classifier = gc_mod.gemini_stream_classifier

        for mod_name in ("core.channels.gemini_cli_channel", "core.channels.antigravity_channel"):
            m = sys.modules.get(mod_name)
            if m is not None and hasattr(m, "get_gemini_payload"):
                m.get_gemini_payload = gc_mod.get_gemini_payload

        import core.dialects.passthrough as dp_mod
        import core.dialects.gemini as dg_mod
        from core.dialects.registry import get_dialect
        importlib.reload(dp_mod)
        importlib.reload(dg_mod)
        gem_dialect = get_dialect("gemini")
        if gem_dialect is not None:
            gem_dialect.parse_usage = dg_mod.parse_gemini_usage
        ve_mod = sys.modules.get("plugins.vertex_express_channel") or sys.modules.get("vertex_express_channel")
        if ve_mod is not None and hasattr(ve_mod, "gemini_json_process"):
            ve_mod.gemini_json_process = gc_mod.gemini_json_process
        import core.streaming as st_mod
        orig_lsr = st_mod.LoggingStreamingResponse
        importlib.reload(st_mod)
        for _attr in ("_try_extract_usage", "_try_parse_line"):
            if hasattr(st_mod.LoggingStreamingResponse, _attr):
                setattr(orig_lsr, _attr, getattr(st_mod.LoggingStreamingResponse, _attr))
        st_mod.LoggingStreamingResponse = orig_lsr
        import core.channels.openrouter_channel as orc_mod
        importlib.reload(orc_mod)
        or_ch = get_channel("openrouter")
        if or_ch is not None:
            or_ch.stream_adapter = orc_mod.fetch_openrouter_response_stream
            or_ch.response_adapter = orc_mod.fetch_openrouter_response

        # Zero-restart hot-reload of core.utils.truncate_for_logging & core.response._wrap_response_aread
        import core.utils as u_mod
        orig_trunc = getattr(u_mod, "truncate_for_logging", None)
        importlib.reload(u_mod)
        if orig_trunc is not None and hasattr(u_mod, "truncate_for_logging"):
            orig_trunc.__code__ = u_mod.truncate_for_logging.__code__
            u_mod.truncate_for_logging = orig_trunc

        import core.response as resp_mod
        orig_wrap_aread = getattr(resp_mod, "_wrap_response_aread", None)
        importlib.reload(resp_mod)
        if orig_wrap_aread is not None and hasattr(resp_mod, "_wrap_response_aread"):
            orig_wrap_aread.__code__ = resp_mod._wrap_response_aread.__code__
            resp_mod._wrap_response_aread = orig_wrap_aread

        if gem_ch is not None and hasattr(gc_mod, "fetch_gemini_response"):
            gem_ch.response_adapter = gc_mod.fetch_gemini_response
            gem_ch.stream_adapter = gc_mod.fetch_gemini_response_stream

        # Zero-restart periodic gc.collect() + malloc_trim(0) maintenance loop
        import asyncio as _aio_gc, gc as _gc, ctypes as _ctypes
        _old_gc_task = getattr(sys, "_zoaholic_hot_gc_task", None)
        if _old_gc_task is not None and not _old_gc_task.done():
            _old_gc_task.cancel()
        async def _hot_gc_trim_loop():
            try:
                _libc = _ctypes.CDLL("libc.so.6")
                _libc.malloc_trim.argtypes = [_ctypes.c_size_t]
                _libc.malloc_trim.restype = _ctypes.c_int
            except Exception:
                _libc = None
            while True:
                await _aio_gc.sleep(180)
                try:
                    with open("/proc/self/statm", "r") as _sf:
                        _rss = (int(_sf.read().split()[1]) * 4096) // (1024 * 1024)
                    _gc.collect()
                    if _libc is not None:
                        _libc.malloc_trim(0)
                except Exception:
                    pass
        try:
            _loop = _aio_gc.get_running_loop()
            sys._zoaholic_hot_gc_task = _loop.create_task(_hot_gc_trim_loop())
            _gc.collect()
            _ctypes.CDLL("libc.so.6").malloc_trim(0)
        except Exception:
            pass

        print("[firebase_vertex] Hot-reloaded gemini_channel, vertex_channel, openrouter_channel, utils.truncate_for_logging, response._wrap_response_aread, streaming & GC trim loop in-place.")
    except Exception as e:
        print(f"[firebase_vertex] Channel hotfix failed: {e}")


def setup(manager: "PluginManager"):
    """插件初始化"""
    _install_vacuum_hotfix()
    _install_gemini_vertex_channel_hotfix()
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
            passthrough_payload_adapter=FirebaseVertexChannelAdapter.passthrough_payload_adapter,
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
