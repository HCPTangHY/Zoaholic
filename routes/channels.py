"""
Channels 管理路由
"""

import os
import json
import copy

from core.env import env_bool
import httpx
from time import time
from typing import Optional, Any

from fastapi import APIRouter, Depends, HTTPException, Body
from fastapi.responses import JSONResponse

from core.channels import list_channels, get_channel
from core.log_config import logger
from utils import safe_get
from routes.deps import rate_limit_dependency, verify_admin_api_key, get_app

router = APIRouter()
is_debug = env_bool("DEBUG", False)


@router.get("/v1/channels", dependencies=[Depends(rate_limit_dependency)])
async def get_channels(token: str = Depends(verify_admin_api_key)):
    """
    获取所有已注册的渠道类型列表。
    返回每个渠道的 id, type_name, default_base_url, auth_header, description, has_models_adapter。
    """
    channels = list_channels()
    channel_list = [ch.to_dict() for ch in channels]
    return JSONResponse(content={"channels": channel_list})


@router.post("/v1/channels/fetch_models", dependencies=[Depends(rate_limit_dependency)])
async def fetch_channel_models(
    token: str = Depends(verify_admin_api_key),
    provider_config: dict = Body(..., description="Provider configuration including engine, base_url, api_key, etc.")
):
    """
    根据渠道配置获取可用的模型列表。
    
    请求体示例:
    {
        "engine": "gpt",  // 渠道类型 ID
        "base_url": "https://api.openai.com/v1",
        "api_key": "sk-xxx",
        // 其他渠道特定配置...
    }
    
    返回:
    {
        "models": ["gpt-4", "gpt-3.5-turbo", ...]
    }
    """
    app = get_app()
    
    engine = provider_config.get("engine") or provider_config.get("type") or "openai"
    
    # 获取渠道定义
    channel = get_channel(engine)
    if not channel:
        raise HTTPException(status_code=404, detail=f"Channel type '{engine}' not found")
    
    if not channel.models_adapter:
        raise HTTPException(status_code=400, detail=f"Channel '{engine}' does not support fetching models")
    
    # 构建 provider 配置，如果 base_url 为空则使用渠道默认值
    config_base_url = provider_config.get("base_url", "")
    provider = {
        "base_url": config_base_url if config_base_url else (channel.default_base_url or ""),
        "api": provider_config.get("api_key") or provider_config.get("api") or "",
        # Vertex AI 特定配置
        "project_id": provider_config.get("project_id", ""),
        "client_email": provider_config.get("client_email", ""),
        "private_key": provider_config.get("private_key", ""),
        # AWS 特定配置
        "aws_access_key": provider_config.get("aws_access_key", ""),
        "aws_secret_key": provider_config.get("aws_secret_key", ""),
        # Cloudflare 特定配置
        "cf_account_id": provider_config.get("cf_account_id", ""),
    }
    
    # 获取代理配置
    proxy = provider_config.get("proxy") or safe_get(app.state.config, "preferences", "proxy")
    
    # 验证 base_url 格式
    base_url = provider.get("base_url", "")
    if base_url and not base_url.startswith(("http://", "https://")):
        # 自动添加 https:// 前缀
        provider["base_url"] = f"https://{base_url}"
        logger.info(f"Auto-prefixed base_url: {provider['base_url']}")
    
    try:
        async with app.state.client_manager.get_client(provider["base_url"], proxy) as client:
            # 设置超时，避免请求卡死
            import asyncio
            models = await asyncio.wait_for(
                channel.models_adapter(client, provider),
                timeout=30.0
            )
            return JSONResponse(content={"models": models})
    except Exception as e:
        # 尽量提取并返回上游的错误信息
        upstream_status = None
        upstream_message: Optional[str] = None

        response = getattr(e, "response", None)
        if response is not None:
            try:
                upstream_status = response.status_code
            except Exception:
                upstream_status = None

            try:
                data = response.json()
                if isinstance(data, dict):
                    upstream_message = (
                        data.get("error")
                        or data.get("message")
                        or data.get("detail")
                    )
                else:
                    upstream_message = str(data)
            except Exception:
                try:
                    upstream_message = response.text
                except Exception:
                    upstream_message = None

        if not upstream_message:
            upstream_message = str(e).split("For more information")[0].strip()

        logger.error(
            f"Failed to fetch models for channel '{engine}': "
            f"status={upstream_status}, error={upstream_message}, raw_exception={repr(e)}"
        )
        if is_debug:
            import traceback
            traceback.print_exc()

        status_code = upstream_status or 502
        raise HTTPException(
            status_code=status_code,
            detail=f"上游接口返回错误 ({status_code}): {upstream_message}"
        )


@router.post("/v1/channels/test", dependencies=[Depends(rate_limit_dependency)])
async def test_channel(
    token: str = Depends(verify_admin_api_key),
    test_config: dict = Body(..., description="Test configuration including provider snapshot and model to test")
):
    """
    测试特定渠道的连接。

    目标：尽量复用正式请求链路，避免测试链路与生产链路行为不一致。
    - 支持传入 provider_snapshot（完整渠道配置）
    - 保持对旧字段（engine/base_url/api_key/model）的兼容
    - 支持 preferences.headers / post_body_parameter_overrides / enabled_plugins
    
    请求体示例:
    {
        "provider_snapshot": { ...完整渠道配置... },
        "model": "gpt-4o-mini",  // 建议传模型别名
        "upstream_model": "gpt-4o-mini",  // 可选，别名缺失时回退
        "timeout": 30,

        // 兼容旧用法（可选）
        "engine": "openai",
        "base_url": "https://api.openai.com/v1",
        "api_key": "sk-xxx"
    }
    """
    from core.request import get_payload
    from core.models import RequestModel
    from core.utils import get_model_dict

    def _normalize_key_item(item: Any) -> Optional[str]:
        if isinstance(item, str):
            key = item.strip()
            return key or None
        if isinstance(item, dict):
            key = item.get("key")
            if isinstance(key, str) and key.strip():
                key = key.strip()
                if item.get("disabled") and not key.startswith("!"):
                    key = f"!{key}"
                return key
        return None

    def _collect_key_candidates(raw_keys: Any) -> list[str]:
        if raw_keys is None:
            return []
        items = raw_keys if isinstance(raw_keys, list) else [raw_keys]
        results: list[str] = []
        for item in items:
            normalized = _normalize_key_item(item)
            if normalized:
                results.append(normalized)
        return results
    
    app = get_app()

    provider_snapshot = test_config.get("provider_snapshot")
    provider = copy.deepcopy(provider_snapshot) if isinstance(provider_snapshot, dict) else {}
    if not isinstance(provider, dict):
        provider = {}

    engine = (
        test_config.get("engine")
        or provider.get("engine")
        or test_config.get("type")
        or "openai"
    )
    engine = str(engine).strip() if engine is not None else "openai"

    model = (
        test_config.get("model")
        or test_config.get("model_alias")
        or test_config.get("upstream_model")
        or ""
    )
    model = str(model).strip()

    upstream_model_hint = test_config.get("upstream_model")
    upstream_model_hint = str(upstream_model_hint).strip() if upstream_model_hint else None

    if not model:
        raise HTTPException(status_code=400, detail="model 是必填项")

    timeout = test_config.get("timeout", 30)
    try:
        timeout = max(1, int(timeout))
    except Exception:
        timeout = 30

    channel = get_channel(engine)
    if not channel:
        raise HTTPException(status_code=404, detail=f"Channel type '{engine}' not found")

    provider["provider"] = provider.get("provider") or f"test_{engine or 'channel'}"
    provider["engine"] = engine

    base_url = test_config.get("base_url") or provider.get("base_url", "")
    base_url = str(base_url).strip() if base_url else ""

    # 如果 base_url 为空，使用渠道默认值
    if not base_url:
        if channel.default_base_url:
            base_url = channel.default_base_url
            logger.info(f"Using default base_url for channel '{engine}': {base_url}")
        else:
            raise HTTPException(status_code=400, detail="base_url 是必填项（该渠道类型没有默认地址）")

    # 验证 base_url 格式
    if not base_url.startswith(("http://", "https://")):
        # 自动添加 https:// 前缀
        base_url = f"https://{base_url}"
        logger.info(f"Auto-prefixed base_url: {base_url}")
    provider["base_url"] = base_url.rstrip('/')

    # 解析测试使用 API Key：显式传参 > provider.api / provider.api_keys
    explicit_api_key = test_config.get("api_key") or test_config.get("api")
    selected_api_key = None
    if isinstance(explicit_api_key, str) and explicit_api_key.strip():
        selected_api_key = explicit_api_key.strip()
        if selected_api_key.startswith("!"):
            selected_api_key = selected_api_key[1:]
    else:
        candidates = _collect_key_candidates(provider.get("api"))
        candidates.extend(_collect_key_candidates(provider.get("api_keys")))

        for key in candidates:
            if not key.startswith("!"):
                selected_api_key = key
                break

        if not selected_api_key and candidates:
            selected_api_key = candidates[0][1:] if candidates[0].startswith("!") else candidates[0]

    if selected_api_key:
        provider["api"] = selected_api_key

    # 确保模型映射存在，兼容别名测试
    provider_models = provider.get("model")
    if not isinstance(provider_models, list):
        fallback_models = provider.get("models")
        provider_models = copy.deepcopy(fallback_models) if isinstance(fallback_models, list) else []

    if not provider_models:
        if upstream_model_hint and upstream_model_hint != model:
            provider_models = [{upstream_model_hint: model}]
        else:
            provider_models = [model]

    provider["model"] = provider_models
    provider.pop("models", None)

    model_dict = get_model_dict(provider)
    if model not in model_dict:
        if upstream_model_hint and upstream_model_hint != model:
            provider["model"].append({upstream_model_hint: model})
        else:
            provider["model"].append(model)
        model_dict = get_model_dict(provider)

    provider["_model_dict_cache"] = model_dict

    if model not in model_dict:
        raise HTTPException(status_code=400, detail=f"model '{model}' 不在当前渠道模型配置中")

    # 构建测试请求（允许外部覆盖部分参数，便于做 Key/流式等测试）
    # 注意：这里尽量保持「轻量」请求，避免测试时产生较大消耗。
    import asyncio

    prompt = test_config.get("prompt") or test_config.get("text") or "Hi"
    raw_messages = test_config.get("messages")
    if isinstance(raw_messages, list) and raw_messages:
        messages = raw_messages
    else:
        messages = [{"role": "user", "content": str(prompt)}]

    request_patch = test_config.get("request_patch")
    if not isinstance(request_patch, dict):
        request_patch = {}

    # 常用字段（显式字段优先级 > request_patch > 默认值）
    def _get_first(*keys: str, default=None):
        for k in keys:
            if k in test_config and test_config.get(k) is not None:
                return test_config.get(k)
        if keys and keys[0] in request_patch and request_patch.get(keys[0]) is not None:
            return request_patch.get(keys[0])
        return default

    stream = bool(_get_first("stream", default=False))

    # 默认 max_tokens 设小一点即可验证可用性
    max_tokens = _get_first("max_tokens", default=16)
    try:
        max_tokens = int(max_tokens) if max_tokens is not None else None
    except Exception:
        max_tokens = 16

    temperature = _get_first("temperature", default=0.5)
    try:
        temperature = float(temperature) if temperature is not None else None
    except Exception:
        temperature = 0.5

    top_p = _get_first("top_p", default=1.0)
    try:
        top_p = float(top_p) if top_p is not None else None
    except Exception:
        top_p = 1.0

    # 允许额外字段透传（RequestModel extra=allow）
    test_request_payload = {
        **request_patch,
        "model": model,
        "messages": messages,
        "stream": stream,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": top_p,
    }
    test_request = RequestModel(**test_request_payload)

    # 获取代理配置（优先级：test_config > provider > global）
    proxy = test_config.get("proxy")
    if not proxy:
        proxy = safe_get(app.state.config, "preferences", "proxy")
        proxy = safe_get(provider, "preferences", "proxy", default=proxy)

    start_time = time()
    
    try:
        # 使用正式链路的 payload 构建逻辑（包含参数覆写、请求插件）
        url, headers, payload = await get_payload(test_request, engine, provider, selected_api_key)

        # 对齐正式链路：追加渠道自定义 headers
        custom_headers = safe_get(provider, "preferences", "headers", default={})
        if isinstance(custom_headers, dict):
            headers.update({str(k): str(v) for k, v in custom_headers.items() if v is not None})

        # 打印调试信息
        try:
            pretty_payload = json.dumps(payload, ensure_ascii=False)
        except Exception:
            pretty_payload = str(payload)

        print("[CHANNEL_TEST] engine:", engine)
        print("[CHANNEL_TEST] url:", url)
        print("[CHANNEL_TEST] payload:", pretty_payload)

        if is_debug:
            logger.info(f"Channel test - Engine: {engine}")
            logger.info(f"Channel test - URL: {url}")
            logger.info(f"Channel test - Headers: {headers}")
            logger.info(f"Channel test - Payload (truncated): {pretty_payload[:2000]}")
        
        async with app.state.client_manager.get_client(url, proxy) as client:
            # stream 测试：只读取少量响应以验证链路，不让测试请求卡住
            if stream:
                async with client.stream(
                    "POST",
                    url,
                    headers=headers,
                    json=payload,
                    timeout=timeout,
                ) as response:
                    latency_ms = int((time() - start_time) * 1000)

                    upstream_status_code = int(response.status_code)
                    auth_failed = upstream_status_code in (401, 403)

                    preview = ""
                    try:
                        it = response.aiter_text()
                        preview = await asyncio.wait_for(it.__anext__(), timeout=min(10, timeout))
                        preview = (preview or "")[:800]
                    except StopAsyncIteration:
                        preview = ""
                    except Exception as e:
                        preview = f"<stream preview failed: {type(e).__name__}: {e}>"[:800]

                    if 200 <= upstream_status_code < 300:
                        return JSONResponse(
                            content={
                                "success": True,
                                "latency_ms": latency_ms,
                                "message": "测试成功",
                                "upstream_status_code": upstream_status_code,
                                "auth_failed": auth_failed,
                                "stream": True,
                                "response_preview": preview,
                            }
                        )

                    # 尝试读取错误文本（避免读太多）
                    err_text = ""
                    try:
                        err_text = (await response.aread()).decode("utf-8", errors="ignore")[:800]
                    except Exception:
                        err_text = ""
                    if not err_text:
                        err_text = f"HTTP {upstream_status_code}"

                    return JSONResponse(
                        status_code=200,
                        content={
                            "success": False,
                            "latency_ms": latency_ms,
                            "message": f"HTTP {upstream_status_code}",
                            "error": err_text,
                            "upstream_status_code": upstream_status_code,
                            "auth_failed": auth_failed,
                            "stream": True,
                            "response_preview": preview,
                        },
                    )

            # 非 stream：正常 post 并解析 JSON
            response = await client.post(
                url,
                headers=headers,
                json=payload,
                timeout=timeout,
            )

            latency_ms = int((time() - start_time) * 1000)
            upstream_status_code = int(response.status_code)
            auth_failed = upstream_status_code in (401, 403)

            # 统一解析响应体
            resp_json = None
            error_detail = ""
            try:
                resp_json = response.json()
            except Exception:
                resp_json = None

            if isinstance(resp_json, dict):
                if resp_json.get("error") is not None:
                    err_obj = resp_json.get("error")
                    if isinstance(err_obj, dict):
                        error_detail = (
                            err_obj.get("message")
                            or err_obj.get("code")
                            or err_obj.get("status")
                            or str(err_obj)
                        )
                    else:
                        error_detail = str(err_obj)
                elif resp_json.get("detail") and not resp_json.get("choices"):
                    error_detail = str(resp_json.get("detail"))

            if not error_detail:
                try:
                    body_text = response.text
                    if body_text and body_text.strip() and response.status_code >= 400:
                        error_detail = body_text[:800]
                except Exception:
                    pass

            is_success = 200 <= upstream_status_code < 300 and not error_detail

            if is_success:
                return JSONResponse(
                    content={
                        "success": True,
                        "latency_ms": latency_ms,
                        "message": "测试成功",
                        "upstream_status_code": upstream_status_code,
                        "auth_failed": auth_failed,
                        "stream": False,
                    }
                )

            if not error_detail:
                error_detail = f"HTTP {upstream_status_code}"

            return JSONResponse(
                status_code=200,
                content={
                    "success": False,
                    "latency_ms": latency_ms,
                    "message": f"HTTP {upstream_status_code}",
                    "error": error_detail,
                    "upstream_status_code": upstream_status_code,
                    "auth_failed": auth_failed,
                    "stream": False,
                },
            )
                
    except httpx.TimeoutException:
        latency_ms = int((time() - start_time) * 1000)
        return JSONResponse(
            status_code=200,
            content={
                "success": False,
                "latency_ms": latency_ms,
                "message": "请求超时",
                "error": f"请求超时（{timeout}秒）",
                "upstream_status_code": None,
                "auth_failed": False,
                "stream": bool(stream),
            }
        )
    except httpx.ConnectError as e:
        return JSONResponse(
            status_code=200,
            content={
                "success": False,
                "latency_ms": None,
                "message": "连接失败",
                "error": str(e),
                "upstream_status_code": None,
                "auth_failed": False,
                "stream": bool(stream),
            }
        )
    except Exception as e:
        latency_ms = int((time() - start_time) * 1000) if time() - start_time > 0 else None
        
        error_message = str(e)
        if hasattr(e, 'response'):
            try:
                error_data = e.response.json()
                error_message = (
                    error_data.get("error", {}).get("message") or
                    error_data.get("error") or
                    error_data.get("message") or
                    str(error_data)
                )
            except Exception:
                pass
        
        logger.error(f"Channel test failed: {error_message}")
        if is_debug:
            import traceback
            traceback.print_exc()
        
        return JSONResponse(
            status_code=200,
            content={
                "success": False,
                "latency_ms": latency_ms,
                "message": "测试失败",
                "error": error_message,
                "upstream_status_code": None,
                "auth_failed": False,
                "stream": bool(stream),
            }
        )


@router.post("/v1/channels/models_by_groups", dependencies=[Depends(rate_limit_dependency)])
async def get_models_by_groups(
    token: str = Depends(verify_admin_api_key),
    request_body: dict = Body(..., description="Request body containing groups array")
):
    """
    根据分组获取可用的模型列表。
    
    请求体示例:
    {
        "groups": ["default", "premium"]  // 分组数组
    }
    
    返回:
    {
        "models": [
            {"id": "gpt-4o", "object": "model", "owned_by": "Zoaholic"},
            ...
        ]
    }
    """
    from core.utils import get_model_dict
    
    app = get_app()
    config = app.state.config
    providers = config.get("providers", [])
    
    # 获取请求的分组
    requested_groups = request_body.get("groups", [])
    if isinstance(requested_groups, str):
        requested_groups = [requested_groups]
    if not requested_groups:
        requested_groups = ["default"]
    
    allowed_groups = set(requested_groups)
    
    # 收集符合分组条件的模型
    all_models = []
    unique_models = set()
    
    for provider in providers:
        # 检查渠道是否启用
        if provider.get("enabled") is False:
            continue
        
        # 分组过滤：provider 必须与请求的分组有交集
        p_groups = provider.get("groups") or ["default"]
        if isinstance(p_groups, str):
            p_groups = [p_groups] if p_groups else ["default"]
        if not isinstance(p_groups, list) or not p_groups:
            p_groups = ["default"]
        
        if not allowed_groups.intersection(set(p_groups)):
            continue
        
        # 获取模型字典
        model_dict = provider.get("_model_dict_cache") or get_model_dict(provider)
        
        # 识别被重定向的上游原名（在此渠道内，出现在映射值中且与键不同的项）
        # 例如: {"pro": "pro", "pronothink": "pro"} 中，"pro" 作为值被 "pronothink" 重定向
        # 所以应该过滤掉 "pro"，只保留 "pronothink"
        redirected_upstreams = {v for k, v in model_dict.items() if v != k}
        
        # 如果渠道配置了 model_prefix，只展示带前缀的模型名
        prefix = provider.get('model_prefix', '').strip()
        
        for alias, upstream in model_dict.items():
            # 如果别名同时也是其他映射的上游目标，说明它被重定向了，跳过
            if alias in redirected_upstreams:
                continue
            # 如果有前缀，只返回带前缀的模型名
            if prefix and not alias.startswith(prefix):
                continue
            
            if alias not in unique_models:
                unique_models.add(alias)
                model_info = {
                    "id": alias,
                    "object": "model",
                    "created": 1720524448858,
                    "owned_by": "Zoaholic"
                }
                all_models.append(model_info)
    
    # 按模型名排序
    all_models.sort(key=lambda x: x["id"])
    
    return JSONResponse(content={"models": all_models})