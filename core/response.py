"""
响应处理模块

负责处理 API 响应的流式和非流式数据
所有流式响应渠道通过 channels 模块的注册中心获取适配器
"""

import json
import asyncio
from datetime import datetime
from typing import Optional, List, Any

from .log_config import logger
from .middleware import request_info
from .utils import safe_get, truncate_for_logging


async def check_response(response, error_log):
    """
    检查 HTTP 响应状态码，如果不是 2xx 则返回错误信息
    同时：
    - 记录上游失败响应到 request_info
    - 对于成功响应，自动包装 aiter_text 方法以记录上游响应
    
    Args:
        response: httpx 响应对象
        error_log: 错误日志前缀
        
    Returns:
        dict 或 None: 如果有错误返回错误字典，否则返回 None
    """
    if response and not (200 <= response.status_code < 300):
        error_message = await response.aread()
        error_str = error_message.decode('utf-8', errors='replace')
        
        # 记录失败的上游响应（使用深度截断，保留结构同时限制大小）
        try:
            current_info = request_info.get()
            if current_info and current_info.get("raw_data_expires_at") is not None:
                current_info["upstream_response_body"] = truncate_for_logging(error_str)
        except Exception as e:
            logger.error(f"Error saving upstream error response: {str(e)}")
        
        try:
            error_json = await asyncio.to_thread(json.loads, error_str)
        except json.JSONDecodeError:
            error_json = error_str
        return {"error": f"{error_log} HTTP Error", "status_code": response.status_code, "details": error_json}
    
    # 成功响应：包装 aiter_text 方法以自动记录上游响应
    if response:
        _wrap_response_aiter_text(response)
    
    return None


def _wrap_response_aiter_text(response):
    """
    包装 httpx response 的 aiter_text 方法，自动记录上游原始响应
    """
    original_aiter_text = response.aiter_text
    
    try:
        captured_info = request_info.get()
    except Exception:
        captured_info = None
    
    should_save = captured_info and captured_info.get("raw_data_expires_at") is not None
    
    if not should_save:
        return
    
    async def logging_aiter_text():
        """包装后的 aiter_text，自动记录数据"""
        upstream_chunks = []
        max_size = 100 * 1024  # 100KB
        total_size = 0
        
        try:
            async for chunk in original_aiter_text():
                if total_size < max_size:
                    upstream_chunks.append(chunk)
                    total_size += len(chunk.encode('utf-8'))
                
                yield chunk
        except GeneratorExit:
            # 调用者关闭生成器时触发（如客户端断开连接）
            # 需要重新抛出以确保上层正确处理
            logger.debug("Generator closed by caller (GeneratorExit)")
            raise
        except Exception as e:
            logger.error(f"Error during upstream response iteration: {str(e)}")
            raise
        finally:
            if upstream_chunks and captured_info:
                try:
                    upstream_response = "".join(upstream_chunks)
                    captured_info["upstream_response_body"] = truncate_for_logging(upstream_response)
                except Exception as e:
                    logger.error(f"Error saving upstream response body: {str(e)}")
    
    try:
        response.aiter_text = logging_aiter_text
    except AttributeError:
        try:
            object.__setattr__(response, 'aiter_text', logging_aiter_text)
        except Exception as e:
            logger.error(f"Failed to wrap response.aiter_text: {str(e)}")


def _save_upstream_response_for_non_stream(response):
    """
    保存非流式响应的原始上游响应体
    """
    try:
        captured_info = request_info.get()
    except Exception:
        captured_info = None
    
    if not captured_info or captured_info.get("raw_data_expires_at") is None:
        return
    
    try:
        if hasattr(response, '_content') and response._content:
            captured_info["upstream_response_body"] = truncate_for_logging(response._content)
    except Exception as e:
        logger.error(f"Error saving upstream response for non-stream: {str(e)}")


async def fetch_response(client, url, headers, payload, engine, model, timeout=200):
    """
    处理非流式 API 响应，通过渠道适配器进行分发
    """
    from .channels import get_channel
    
    channel = get_channel(engine)
    if channel and channel.response_adapter:
        async for chunk in channel.response_adapter(client, url, headers, payload, model, timeout):
            # 如果适配器返回的是字典且包含 error，则它是一个预处理过的错误
            if isinstance(chunk, dict) and "error" in chunk:
                yield chunk
                return
            yield chunk
        return

    # 回退逻辑：如果渠道没有适配器，执行默认的 OpenAI 兼容逻辑
    if payload.get("file"):
        file = payload.pop("file")
        response = await client.post(url, headers=headers, data=payload, files={"file": file}, timeout=timeout)
    else:
        json_payload = await asyncio.to_thread(json.dumps, payload)
        response = await client.post(url, headers=headers, content=json_payload, timeout=timeout)
    
    error_message = await check_response(response, "fetch_response_fallback")
    if error_message:
        yield error_message
        return
    
    _save_upstream_response_for_non_stream(response)
    
    if engine == "tts":
        yield response.read()
    else:
        response_bytes = await response.aread()
        response_json = await asyncio.to_thread(json.loads, response_bytes)
        yield response_json


async def fetch_response_stream(
    client,
    url,
    headers,
    payload,
    engine,
    model,
    timeout=200,
    enabled_plugins: Optional[List[str]] = None,
):
    """
    通过渠道注册中心获取流式响应适配器并处理响应流
    """
    from .channels import get_channel
    from .plugins.interceptors import apply_response_interceptors
    
    channel = get_channel(engine)
    if channel and channel.stream_adapter:
        async for chunk in channel.stream_adapter(client, url, headers, payload, model, timeout):
            # 如果适配器返回的是字典且包含 error，则它是一个预处理过的错误
            if isinstance(chunk, dict) and "error" in chunk:
                yield chunk
                continue
                
            # 应用响应拦截器
            chunk = await apply_response_interceptors(chunk, engine, model, is_stream=True, enabled_plugins=enabled_plugins)
            yield chunk
        
        return
    
    raise ValueError(f"Unknown engine: {engine}")
