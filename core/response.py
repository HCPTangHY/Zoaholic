"""
响应处理模块

负责处理 API 响应的流式和非流式数据
所有流式响应渠道通过 channels 模块的注册中心获取适配器
"""

import json
import asyncio
from datetime import datetime
from typing import Optional, List, Any, Dict

from .log_config import logger
from .middleware import request_info
from .utils import safe_get, truncate_for_logging
from .json_utils import json_loads, json_dumps_text


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
                # 修改原因：日志详情需要在失败响应中同时查看上游响应头和响应体。
                # 修改方式：在保存失败响应体的同一个上下文里保存已脱敏的 response headers。
                # 目的：避免非 2xx 响应提前返回时遗漏上游响应头。
                _save_upstream_response_headers(response, current_info)
                current_info["upstream_response_body"] = truncate_for_logging(error_str)
        except Exception as e:
            logger.error(f"Error saving upstream error response: {str(e)}")
        
        try:
            error_json = json_loads(error_str)
        except json.JSONDecodeError:
            error_json = error_str
        return {"error": f"{error_log} HTTP Error", "status_code": response.status_code, "details": error_json}
    
    # 成功响应：包装 aiter_text / aiter_bytes 方法以自动记录上游响应
    if response:
        _wrap_response_iterators(response)
    
    return None


def _get_response_capture_state():
    try:
        captured_info = request_info.get()
    except Exception:
        captured_info = None

    should_save = captured_info and captured_info.get("raw_data_expires_at") is not None
    return captured_info, should_save


def _save_upstream_response_headers(response, captured_info):
    """保存脱敏后的上游响应头。"""
    if not captured_info or captured_info.get("raw_data_expires_at") is None:
        return

    try:
        resp_headers = dict(getattr(response, "headers", {}) or {})
        # 修改原因：响应头可能包含 Cookie 或上游密钥，不能直接写入可查看日志。
        # 修改方式：按大小写不敏感方式删除敏感响应头，再序列化为 JSON 字符串。
        # 目的：在保留排障所需响应头的同时，降低日志泄露敏感信息的风险。
        for sensitive_key in ("set-cookie", "x-api-key"):
            for key in list(resp_headers.keys()):
                if str(key).lower() == sensitive_key:
                    del resp_headers[key]
        captured_info["upstream_response_headers"] = json.dumps(resp_headers, ensure_ascii=False)
    except Exception:
        pass


def _wrap_response_iterators(response):
    captured_info, should_save = _get_response_capture_state()
    if should_save:
        # 修改原因：流式响应的响应体要等迭代结束才能保存，但响应头在 response 对象创建后已经可用。
        # 修改方式：包装迭代器前先从 response.headers 保存已脱敏响应头。
        # 目的：确保 aiter_text、aiter_bytes 和 aread 三种读取路径都能共享同一份响应头采集逻辑。
        _save_upstream_response_headers(response, captured_info)
    _wrap_response_aiter_text(response)
    _wrap_response_aiter_bytes(response)
    _wrap_response_aread(response)


def _wrap_response_aiter_text(response):
    """
    包装 httpx response 的 aiter_text 方法，自动记录上游原始响应
    """
    original_aiter_text = response.aiter_text
    
    captured_info, should_save = _get_response_capture_state()

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


def _wrap_response_aiter_bytes(response):
    """
    包装 httpx response 的 aiter_bytes 方法，自动记录上游原始响应
    """
    if not hasattr(response, "aiter_bytes"):
        return

    original_aiter_bytes = response.aiter_bytes
    captured_info, should_save = _get_response_capture_state()

    if not should_save:
        return

    async def logging_aiter_bytes():
        upstream_chunks = []
        max_size = 100 * 1024  # 100KB
        total_size = 0

        try:
            async for chunk in original_aiter_bytes():
                if total_size < max_size:
                    upstream_chunks.append(chunk)
                    total_size += len(chunk)

                yield chunk
        except GeneratorExit:
            logger.debug("Generator closed by caller (GeneratorExit)")
            raise
        except Exception as e:
            logger.error(f"Error during upstream byte iteration: {str(e)}")
            raise
        finally:
            if upstream_chunks and captured_info:
                try:
                    upstream_response = b"".join(upstream_chunks)
                    captured_info["upstream_response_body"] = truncate_for_logging(upstream_response)
                except Exception as e:
                    logger.error(f"Error saving upstream byte response body: {str(e)}")

    try:
        response.aiter_bytes = logging_aiter_bytes
    except AttributeError:
        try:
            object.__setattr__(response, 'aiter_bytes', logging_aiter_bytes)
        except Exception as e:
            logger.error(f"Failed to wrap response.aiter_bytes: {str(e)}")


def _wrap_response_aread(response):
    """
    包装 httpx response 的 aread 方法，自动记录上游原始响应。

    所有渠道适配器的非流式处理函数（如 fetch_claude_response、fetch_openai_response 等）
    都使用 response.aread() 读取完整响应体，而不经过 aiter_bytes/aiter_text。
    此包装器确保 aread() 读取的数据也被采集到 upstream_response_body 中。
    """
    if not hasattr(response, "aread"):
        return

    original_aread = response.aread
    captured_info, should_save = _get_response_capture_state()

    if not should_save:
        return

    async def logging_aread():
        """包装后的 aread，自动记录数据"""
        result = await original_aread()
        if captured_info:
            try:
                captured_info["upstream_response_body"] = truncate_for_logging(result)
            except Exception as e:
                logger.error(f"Error saving upstream response body (aread): {str(e)}")
        return result

    try:
        response.aread = logging_aread
    except AttributeError:
        try:
            object.__setattr__(response, 'aread', logging_aread)
        except Exception as e:
            logger.error(f"Failed to wrap response.aread: {str(e)}")


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
            # 修改原因：部分非流式回退路径会直接读取 _content，需要与响应体一起保留响应头。
            # 修改方式：在保存 _content 前复用统一的响应头脱敏保存函数。
            # 目的：避免绕过 aread 包装器的路径遗漏 upstream_response_headers。
            _save_upstream_response_headers(response, captured_info)
            captured_info["upstream_response_body"] = truncate_for_logging(response._content)
    except Exception as e:
        logger.error(f"Error saving upstream response for non-stream: {str(e)}")


def _log_upstream_request(url, payload):
    """在 fetch 层记录实际发给上游的请求体（force_stream 等插件修改后的真实值）"""
    try:
        info = request_info.get()
        if info.get("raw_data_expires_at") is None:
            return
        upstream_payload = {k: v for k, v in payload.items() if k != 'file'}
        info["upstream_request_body"] = truncate_for_logging(upstream_payload)
        info["upstream_request_url"] = url
    except Exception as e:
        logger.error(f"Error logging upstream request in fetch layer: {e}")


async def _apply_response_path_interceptors(
    response_chunk: Any,
    engine: str,
    model: str,
    is_stream: bool,
    enabled_plugins: Optional[List[str]] = None,
    provider: Optional[Dict[str, Any]] = None,
    api_key_info: Optional[Dict[str, Any]] = None,
    key_enabled_plugins: Optional[List[str]] = None,
) -> Any:
    """依次应用响应、渠道出站和 Key 出站拦截器。"""
    from .plugins.interceptors import (
        apply_response_interceptors,
        apply_channel_outbound_interceptors,
        apply_key_outbound_interceptors,
    )

    # 修改原因：新增出站阶段必须接在既有 response_interceptors 之后，并区分渠道级和 Key 级 enabled_plugins。
    # 修改方式：统一封装响应返回路径，先执行旧响应拦截器，再执行 channel_outbound，最后执行 key_outbound。
    # 目的：保证流式和非流式成功/错误 chunk 都走同一顺序，避免各 fetch 分支遗漏新阶段。
    response_chunk = await apply_response_interceptors(
        response_chunk, engine, model, is_stream=is_stream, enabled_plugins=enabled_plugins
    )
    if isinstance(response_chunk, dict) and "error" in response_chunk:
        # 修改原因：结构化错误 chunk 后续会进入 handler 的 Key Rules，channel_outbound/key_outbound 必须在 Key Rules 之后执行。
        # 修改方式：此处只保留旧 response_interceptors 处理，跳过新增出站阶段；最终错误响应由 handler 收尾时再执行出站阶段。
        # 目的：让错误路径满足“响应拦截器之后、Key Rules 之后、返回客户端前”的阶段顺序。
        return response_chunk
    response_chunk = await apply_channel_outbound_interceptors(
        response_chunk, engine, model, provider or {}, is_stream=is_stream, enabled_plugins=enabled_plugins
    )
    response_chunk = await apply_key_outbound_interceptors(
        response_chunk, engine, model, api_key_info or {}, is_stream=is_stream, enabled_plugins=key_enabled_plugins
    )
    return response_chunk


async def fetch_response(
    client,
    url,
    headers,
    payload,
    engine,
    model,
    timeout=200,
    enabled_plugins=None,
    provider=None,
    api_key_info=None,
    key_enabled_plugins=None,
):
    """
    处理非流式 API 响应，通过渠道适配器进行分发
    """
    from .channels import get_channel
    
    _log_upstream_request(url, payload)
    
    channel = get_channel(engine)
    if channel and channel.response_adapter:
        async for chunk in channel.response_adapter(client, url, headers, payload, model, timeout):
            # 如果适配器返回的是字典且包含 error，则它是一个预处理过的错误
            chunk = await _apply_response_path_interceptors(
                chunk, engine, model, is_stream=False,
                enabled_plugins=enabled_plugins,
                provider=provider,
                api_key_info=api_key_info,
                key_enabled_plugins=key_enabled_plugins,
            )
            yield chunk
            if isinstance(chunk, dict) and "error" in chunk:
                return
        return

    # 回退逻辑：如果渠道没有适配器，执行默认的 OpenAI 兼容逻辑
    if payload.get("file"):
        file = payload.pop("file")
        response = await client.post(url, headers=headers, data=payload, files={"file": file}, timeout=timeout)
    else:
        json_payload = await asyncio.to_thread(json_dumps_text, payload)
        response = await client.post(url, headers=headers, content=json_payload, timeout=timeout)
    
    error_message = await check_response(response, "fetch_response_fallback")
    if error_message:
        error_message = await _apply_response_path_interceptors(
            error_message, engine, model, is_stream=False,
            enabled_plugins=enabled_plugins,
            provider=provider,
            api_key_info=api_key_info,
            key_enabled_plugins=key_enabled_plugins,
        )
        yield error_message        
        return
    
    _save_upstream_response_for_non_stream(response)
    
    if engine == "tts":
        yield response.read()
    else:
        response_bytes = await response.aread()
        response_json = await asyncio.to_thread(json_loads, response_bytes)
        # 修改原因：默认 fallback 成功响应此前绕过 response_interceptors，新出站阶段也会因此遗漏。
        # 修改方式：在 yield 前统一调用 _apply_response_path_interceptors。
        # 目的：让无专用 channel adapter 的非流式成功响应也覆盖响应、渠道出站和 Key 出站阶段。
        response_json = await _apply_response_path_interceptors(
            response_json, engine, model, is_stream=False,
            enabled_plugins=enabled_plugins,
            provider=provider,
            api_key_info=api_key_info,
            key_enabled_plugins=key_enabled_plugins,
        )
        yield response_json


# stream_adapter 是否接受 provider 关键字的签名缓存（key: id(adapter)）
_stream_adapter_provider_support: dict = {}


async def fetch_response_stream(
    client,
    url,
    headers,
    payload,
    engine,
    model,
    timeout=200,
    enabled_plugins: Optional[List[str]] = None,
    provider: Optional[Dict[str, Any]] = None,
    api_key_info: Optional[Dict[str, Any]] = None,
    key_enabled_plugins: Optional[List[str]] = None,
):
    """
    通过渠道注册中心获取流式响应适配器并处理响应流
    """
    from .channels import get_channel
    
    _log_upstream_request(url, payload)
    
    channel = get_channel(engine)
    if channel and channel.stream_adapter:
        # 修改原因：部分渠道（如 openai-responses/codex）需要根据 provider.preferences 选择传输方式（HTTP/WebSocket）。
        # 修改方式：对声明了 provider 形参的 stream_adapter 传入当前 provider；签名缓存避免每次反射。
        # 目的：传输选择逻辑下沉到渠道内部，不影响现有六参数 adapter。
        adapter = channel.stream_adapter
        support_key = id(adapter)
        accepts_provider = _stream_adapter_provider_support.get(support_key)
        if accepts_provider is None:
            try:
                import inspect
                accepts_provider = "provider" in inspect.signature(adapter).parameters
            except Exception:
                accepts_provider = False
            _stream_adapter_provider_support[support_key] = accepts_provider
        if accepts_provider:
            chunk_iter = adapter(client, url, headers, payload, model, timeout, provider=provider)
        else:
            chunk_iter = adapter(client, url, headers, payload, model, timeout)
        async for chunk in chunk_iter:
            # 应用响应拦截器和新增出站阶段
            chunk = await _apply_response_path_interceptors(
                chunk, engine, model, is_stream=True,
                enabled_plugins=enabled_plugins,
                provider=provider,
                api_key_info=api_key_info,
                key_enabled_plugins=key_enabled_plugins,
            )
            yield chunk
            # 如果适配器返回的是字典且包含 error，则它是一个预处理过的错误
            if isinstance(chunk, dict) and "error" in chunk:
                return
        
        return
    
    raise ValueError(f"Unknown engine: {engine}")
