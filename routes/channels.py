"""
Channels 管理路由
"""

import os
import json
import copy
import asyncio

from core.env import env_bool
import httpx
from time import time
from typing import Optional, Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Body, BackgroundTasks
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


@router.get("/v1/channels/key_status", dependencies=[Depends(rate_limit_dependency)])
async def get_key_status(token: str = Depends(verify_admin_api_key)):
    """获取所有渠道的运行时 Key 自动禁用状态。仅反映内存中的实时状态，不修改任何配置。"""
    from core.utils import provider_api_circular_list
    from time import time as _time

    now = _time()
    result = {}
    for provider_name, circular_list in provider_api_circular_list.items():
        auto_disabled = await circular_list.get_auto_disabled_keys()
        cooling = []
        for item in circular_list.items:
            # 只返回普通冷却中（非自动禁用）的 Key
            if item not in circular_list.auto_disabled_info and now < circular_list.cooling_until.get(item, 0):
                until = circular_list.cooling_until[item]
                remaining = -1 if until == float('inf') else int(until - now)
                cooling.append({"key": item, "remaining_seconds": remaining})
        if auto_disabled or cooling:
            result[provider_name] = {
                "auto_disabled": auto_disabled,
                "cooling": cooling,
            }
    return JSONResponse(content=result)


@router.post("/v1/channels/key_status/re_enable", dependencies=[Depends(rate_limit_dependency)])
async def re_enable_key(token: str = Depends(verify_admin_api_key), body: dict = Body(...)):
    """手动恢复被运行时自动禁用的 Key。

    请求体: { "provider": "渠道名", "key": "api_key_string" }
    """
    from core.utils import provider_api_circular_list

    provider_name = body.get("provider")
    key = body.get("key")
    if not provider_name or not key:
        return JSONResponse(status_code=400, content={"error": "Missing provider or key"})

    circular_list = provider_api_circular_list.get(provider_name)
    if not circular_list:
        return JSONResponse(status_code=404, content={"error": f"Provider '{provider_name}' not found"})

    await circular_list.clear_auto_disabled(key)
    return JSONResponse(content={"status": "re_enabled", "provider": provider_name})


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
        # 透传 preferences（用于插件判断等）
        "preferences": provider_config.get("preferences", {}),
    }
    
    # 获取代理配置
    proxy = safe_get(provider_config, "preferences", "proxy") or provider_config.get("proxy") or safe_get(app.state.config, "preferences", "proxy")
    
    # 验证 base_url 格式
    base_url = provider.get("base_url", "")
    if base_url and not base_url.startswith(("http://", "https://")):
        # 自动添加 https:// 前缀
        provider["base_url"] = f"https://{base_url}"
        logger.info(f"Auto-prefixed base_url: {provider['base_url']}")
    
    try:
        from core.http import proxy_context
        import asyncio

        with proxy_context(proxy):
            async with app.state.client_manager.get_client(provider["base_url"], proxy) as client:
                # 包装 client，让请求拦截器能作用于 models_adapter 的请求
                enabled_plugins = safe_get(provider, "preferences", "enabled_plugins", default=None)
                if enabled_plugins:
                    from core.plugins.interceptors import InterceptedClient
                    client = InterceptedClient(client, engine, provider, enabled_plugins)

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
    from core.models import RequestModel
    from core.utils import get_model_dict

    def _normalize_key_item(item: Any) -> Optional[str]:
        if isinstance(item, str):
            key = item.strip()
            return key or None
        if isinstance(item, dict):
            # 支持 {"key": "sk-xxx", "disabled": true} 格式
            key = item.get("key")
            if isinstance(key, str) and key.strip():
                key = key.strip()
                if item.get("disabled") and not key.startswith("!"):
                    key = f"!{key}"
                return key
            # 支持 {"sk-xxx": "label"} 单键 dict 格式
            if len(item) == 1:
                raw_key = next(iter(item.keys()))
                key = str(raw_key).strip()
                return key or None
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

    # 修改原因：虚拟路由测试不能按单个 provider_snapshot 直连测试，否则不会经过 preferences.virtual_models 的 chain 解析。
    # 修改方式：当前端传 virtual_route_test 或 _virtual_route_test 标记时，直接用后端当前配置解析虚拟模型候选 provider。
    # 目的：ChannelTestDialog 中选择虚拟模型名测试时，能走与正式请求相同的虚拟路由链条。
    virtual_route_test = bool(test_config.get("virtual_route_test") or provider.get("_virtual_route_test"))
    selected_api_key = None
    override_providers = None

    if virtual_route_test:
        from core.virtual_routing import resolve_virtual_model

        override_providers = resolve_virtual_model(model, app.state.config, 0, app)
        if override_providers is None:
            raise HTTPException(status_code=400, detail=f"model '{model}' 不是已配置的虚拟模型")
        if not override_providers:
            raise HTTPException(status_code=400, detail=f"虚拟模型 '{model}' 没有可用的路由链条")
    else:
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
        prefix = provider.get('model_prefix', '').strip()

        if model not in model_dict:
            # 前端可能传了不带前缀的模型名，尝试自动匹配带前缀的版本
            prefixed_model = f"{prefix}{model}" if prefix else None
            if prefixed_model and prefixed_model in model_dict:
                model = prefixed_model
            else:
                if upstream_model_hint and upstream_model_hint != model:
                    provider["model"].append({upstream_model_hint: model})
                else:
                    provider["model"].append(model)
                model_dict = get_model_dict(provider)

        provider["_model_dict_cache"] = model_dict

        if model not in model_dict:
            raise HTTPException(status_code=400, detail=f"model '{model}' 不在当前渠道模型配置中")

        override_providers = [provider]

    # 构建测试请求（允许外部覆盖部分参数，默认保持轻量）
    prompt = test_config.get("prompt") or "Hi"
    messages = [{"role": "user", "content": str(prompt)}]

    stream = bool(test_config.get("stream", False))

    max_tokens = test_config.get("max_tokens", 16)
    try:
        max_tokens = int(max_tokens) if max_tokens is not None else 16
    except Exception:
        max_tokens = 16

    temperature = test_config.get("temperature", 0.5)
    try:
        temperature = float(temperature) if temperature is not None else 0.5
    except Exception:
        temperature = 0.5

    test_request = RequestModel(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        stream=stream,
        temperature=temperature,
    )

    # ── 走 request_model 全链路（日志、统计、插件、重试全由内核管理） ──
    from routes.deps import get_model_handler

    model_handler = get_model_handler()
    if not model_handler:
        raise HTTPException(status_code=500, detail="Model handler not initialized")

    start_time = time()

    try:
        bg_tasks = BackgroundTasks()

        response = await model_handler.request_model(
            request_data=test_request,
            api_index=0,  # override 模式下不使用
            background_tasks=bg_tasks,
            override_providers=override_providers,
            force_api_key=selected_api_key,
            override_auto_retry=virtual_route_test,
        )

        latency_ms = int((time() - start_time) * 1000)

        # 提取响应内容用于预览
        preview = ""
        status_code = response.status_code
        if hasattr(response, "body"):
            try:
                preview = response.body.decode("utf-8", errors="ignore")[:800]
            except Exception:
                pass
        elif hasattr(response, "body_iterator"):
            chunks = []
            total = 0
            try:
                async for chunk in response.body_iterator:
                    if isinstance(chunk, bytes):
                        chunk = chunk.decode("utf-8", errors="ignore")
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > 800:
                        break
            except Exception:
                pass
            preview = "".join(chunks)[:800]

        success = 200 <= status_code < 300
        auth_failed = status_code in (401, 403)

        # 解析错误信息，剥离 Zoaholic 内部前缀
        def _strip_gateway_prefix(msg: str) -> str:
            if not msg:
                return msg
            p = "Error: Current provider response failed: "
            if msg.startswith(p):
                return msg[len(p):]
            if msg.startswith("All ") and " error: " in msg:
                return msg.split(" error: ", 1)[1]
            return msg

        error_detail = None
        if not success and preview:
            try:
                resp_json = json.loads(preview)
                if isinstance(resp_json, dict):
                    err_obj = resp_json.get("error")
                    if isinstance(err_obj, dict):
                        error_detail = _strip_gateway_prefix(err_obj.get("message") or str(err_obj))
                    elif err_obj:
                        error_detail = _strip_gateway_prefix(str(err_obj))
                    elif resp_json.get("detail"):
                        error_detail = str(resp_json["detail"])
            except (json.JSONDecodeError, ValueError):
                error_detail = _strip_gateway_prefix(preview)

        return JSONResponse(content={
            "success": success,
            "latency_ms": latency_ms,
            "message": "测试成功" if success else f"HTTP {status_code}",
            "error": error_detail,
            "upstream_status_code": status_code,
            "auth_failed": auth_failed,
            "response_preview": preview if success else None,
        })

    except HTTPException as he:
        latency_ms = int((time() - start_time) * 1000)
        return JSONResponse(
            status_code=200,
            content={
                "success": False,
                "latency_ms": latency_ms,
                "message": "测试失败",
                "error": str(he.detail) if not isinstance(he.detail, dict) else (he.detail.get("message") or str(he.detail)),
                "upstream_status_code": he.status_code,
                "auth_failed": he.status_code in (401, 403),
            }
        )
    except Exception as e:
        latency_ms = int((time() - start_time) * 1000) if time() - start_time > 0 else None
        error_message = str(e)

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


def _normalize_oauth_balance_key_ids(api_value: Any) -> list[str]:
    """把 provider.api / api_key 归一化为 OAuth key_id 列表。"""
    # 修改原因：余额入口可能收到前端逐 Key 传入的字符串，也可能收到完整 provider.api 列表。
    # 修改方式：统一遍历列表或单值，去掉空白，并跳过以 ! 开头的禁用账号标识。
    # 目的：OAuth 余额分流可以复用同一套遍历逻辑，且不会查询已禁用账号。
    if isinstance(api_value, list):
        raw_items = api_value
    elif api_value:
        raw_items = [api_value]
    else:
        raw_items = []

    key_ids: list[str] = []
    for item in raw_items:
        # dict 格式: {"email@example.com": "label"}
        if isinstance(item, dict) and len(item) == 1:
            key_id = str(next(iter(item.keys())) or "").strip()
        else:
            key_id = str(item or "").strip()
        if not key_id or key_id.startswith("!"):
            continue
        key_ids.append(key_id)
    return key_ids


def _coerce_oauth_percent(value: Any) -> Optional[float]:
    """把 OAuth quota 百分比转换成 0 到 100 之间的浮点数。"""
    # 修改原因：OAuthManager 缓存可能来自 JSON 文件或响应头解析，数值类型不一定稳定。
    # 修改方式：尝试转成 float，失败返回 None，成功后裁剪到百分比范围。
    # 目的：保持 BalanceResult.percent 对前端始终是安全可展示的数值或空值。
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(max(0.0, min(100.0, number)), 10)


def _oauth_quota_to_balance_result(quota: Any, error: Optional[str] = None) -> Dict[str, Any]:
    """把 OAuthManager.fetch_quota 的返回值转换为前端现有 BalanceResult 形状。"""
    # 修改原因：前端余额展示读取 value_type、percent、available 等通用字段，而 OAuth quota 使用 quota_5h/quota_7d。
    # 修改方式：保留 OAuth 原字段，同时用两个窗口中的最低剩余额度作为兼容的 percent 和 available。
    # 目的：OAuth 渠道可以复用 /v1/channels/balance 和现有余额展示，不要求用户配置 endpoint/mapping。
    if error or not isinstance(quota, dict):
        return {
            "supported": True,
            "value_type": "percent",
            "total": None,
            "used": None,
            "available": None,
            "percent": None,
            "quota_5h": None,
            "quota_7d": None,
            "raw": None,
            "error": error or "OAuth 额度不可用",
        }

    quota_5h = _coerce_oauth_percent(quota.get("quota_5h"))
    quota_7d = _coerce_oauth_percent(quota.get("quota_7d"))
    percentages = [pct for pct in (quota_5h, quota_7d) if pct is not None]
    percent = min(percentages) if percentages else None
    total = 100.0 if percent is not None else None
    used = round(100.0 - percent, 10) if percent is not None else None
    # extra_usage（如 Claude Code 的额外消费额度）
    extra = {}
    if quota.get("extra_usage_enabled"):
        extra["extra_usage_enabled"] = True
        extra["extra_usage_limit"] = quota.get("extra_usage_monthly_limit")
        extra["extra_usage_used"] = quota.get("extra_usage_used")
        extra["extra_usage_utilization"] = quota.get("extra_usage_utilization")

    return {
        "supported": True,
        "value_type": "percent",
        "total": total,
        "used": used,
        "available": percent,
        "percent": percent,
        "quota_5h": quota_5h,
        "quota_7d": quota_7d,
        **extra,
        "raw": quota.get("raw") if isinstance(quota.get("raw"), dict) else None,
        "error": None,
    }


def _aggregate_oauth_balance_results(results: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """合并多个 OAuth 账号的 BalanceResult，并保留逐账号 results。"""
    # 修改原因：用户可能一次请求整个 provider.api 列表，也可能逐 Key 请求；两种情况都要兼容。
    # 修改方式：单账号时把该账号结果提升到顶层，多账号时用最低 percent 作为顶层汇总值。
    # 目的：既满足前端旧的单行 BalanceResult 读取方式，也能让新入口返回完整账号映射。
    if not results:
        return {
            "supported": False,
            "value_type": "percent",
            "total": None,
            "used": None,
            "available": None,
            "percent": None,
            "quota_5h": None,
            "quota_7d": None,
            "raw": None,
            "error": "未配置 OAuth 账号标识",
            "results": {},
        }

    if len(results) == 1:
        key_id, result = next(iter(results.items()))
        merged = dict(result)
        merged["results"] = {key_id: result}
        return merged

    percentages = [
        result.get("percent")
        for result in results.values()
        if result.get("error") is None and result.get("percent") is not None
    ]
    percent = min(percentages) if percentages else None
    total = 100.0 if percent is not None else None
    used = round(100.0 - percent, 10) if percent is not None else None
    has_success = any(result.get("error") is None for result in results.values())
    return {
        "supported": True,
        "value_type": "percent",
        "total": total,
        "used": used,
        "available": percent,
        "percent": percent,
        "quota_5h": None,
        "quota_7d": None,
        "raw": results,
        "error": None if has_success else "OAuth 额度不可用",
        "results": results,
    }


async def _query_oauth_channel_balance(app: Any, provider: Dict[str, Any]) -> Dict[str, Any]:
    """通过 OAuthManager 查询 OAuth 渠道账号额度。"""
    # 修改原因：OAuth 渠道没有用户可配置的余额 endpoint，额度统一由 OAuthManager.fetch_quota 维护，且 state 已按 provider name 分层。
    # 修改方式：遍历 provider.api 中的 key_id，并把 provider.provider 作为 channel_id 一起传给 fetch_quota。
    # 目的：让 /v1/channels/balance 成为普通渠道和 OAuth 渠道共同使用的后端入口，同时避免同邮箱跨渠道串读。
    channel_id = str(provider.get("provider") or "").strip()
    key_ids = _normalize_oauth_balance_key_ids(provider.get("api"))
    if not key_ids:
        return _aggregate_oauth_balance_results({})
    if not channel_id:
        return _aggregate_oauth_balance_results({
            key_id: _oauth_quota_to_balance_result(None, "未配置 OAuth 渠道名")
            for key_id in key_ids
        })

    oauth_manager = getattr(getattr(app, "state", None), "oauth_manager", None)
    fetch_quota = getattr(oauth_manager, "fetch_quota", None)
    if not callable(fetch_quota):
        return _aggregate_oauth_balance_results({
            key_id: _oauth_quota_to_balance_result(None, "OAuth 管理器不可用")
            for key_id in key_ids
        })

    async def fetch_one(key_id: str) -> tuple[str, Dict[str, Any]]:
        try:
            quota = await fetch_quota(channel_id, key_id)
            return key_id, _oauth_quota_to_balance_result(quota)
        except Exception as exc:
            logger.warning(f"OAuth balance query failed for {key_id}: {exc}")
            return key_id, _oauth_quota_to_balance_result(None, f"OAuth 额度查询失败: {str(exc)}"[:500])

    pairs = await asyncio.gather(*(fetch_one(key_id) for key_id in key_ids))
    return _aggregate_oauth_balance_results(dict(pairs))


@router.post("/v1/channels/balance", dependencies=[Depends(rate_limit_dependency)])
async def query_channel_balance(
    token: str = Depends(verify_admin_api_key),
    provider_config: dict = Body(..., description="Provider configuration for balance query")
):
    """
    查询渠道余额。

    根据 provider 配置中的 preferences.balance 规则，
    向上游余额接口发请求并返回标准化的余额信息。

    请求体示例:
    {
        "engine": "openai",
        "base_url": "https://example.com/v1",
        "api_key": "sk-xxx",
        "preferences": {
            "balance": {
                "template": "new-api"
            }
        }
    }
    """
    app = get_app()

    engine = provider_config.get("engine") or provider_config.get("type") or "openai"

    # 构建 provider 配置
    provider = {
        # 修改原因：OAuthManager.fetch_quota 新增 channel_id，余额查询路由需要保留前端传入的 provider name。
        # 修改方式：在内部 provider 配置中加入 provider 字段，普通渠道不会使用该字段。
        # 目的：让 OAuth 余额查询能按渠道名读取 oauth_state.json。
        "provider": provider_config.get("provider") or provider_config.get("name") or "",
        "base_url": provider_config.get("base_url", ""),
        "api": provider_config.get("api_key") or provider_config.get("api") or "",
        "engine": engine,
        "preferences": provider_config.get("preferences", {}),
        # Vertex AI
        "project_id": provider_config.get("project_id", ""),
        "client_email": provider_config.get("client_email", ""),
        "private_key": provider_config.get("private_key", ""),
        # AWS
        "aws_access_key": provider_config.get("aws_access_key", ""),
        "aws_secret_key": provider_config.get("aws_secret_key", ""),
    }

    channel = get_channel(engine)
    if channel and getattr(channel, "is_oauth", False):
        # 修改原因：OAuth 渠道的余额来自 OAuth 账号 quota，不存在 preferences.balance 配置。
        # 修改方式：在普通 balance.py 逻辑之前按渠道注册表标记分流到 OAuthManager.fetch_quota。
        # 目的：让管理端余额按钮对 Codex 等 OAuth 渠道可用，同时不影响普通 API Key 渠道。
        result = await _query_oauth_channel_balance(app, provider)
        return JSONResponse(content=result)

    from core.balance import query_provider_balance, build_balance_config

    # 验证是否配置了 balance
    balance_cfg = build_balance_config(provider)
    if not balance_cfg:
        return JSONResponse(content={
            "supported": False,
            "error": "该渠道未配置余额查询（preferences.balance）",
        })

    # 验证 base_url
    base_url = provider.get("base_url", "")
    if base_url and not base_url.startswith(("http://", "https://")):
        provider["base_url"] = f"https://{base_url}"

    # 代理配置
    proxy = (
        safe_get(provider_config, "preferences", "proxy")
        or provider_config.get("proxy")
        or safe_get(app.state.config, "preferences", "proxy")
    )

    try:
        from core.http import proxy_context

        with proxy_context(proxy):
            target_url = provider.get("base_url") or "https://localhost"
            async with app.state.client_manager.get_client(target_url, proxy) as client:
                # 插件拦截器（和 fetch_models 同样的逻辑）
                enabled_plugins = safe_get(provider, "preferences", "enabled_plugins", default=None)
                if enabled_plugins:
                    from core.plugins.interceptors import InterceptedClient
                    client = InterceptedClient(client, engine, provider, enabled_plugins)

                result = await query_provider_balance(client, provider)
                return JSONResponse(content=result)

    except Exception as e:
        logger.error(f"Balance query error: {e}")
        return JSONResponse(
            status_code=200,
            content={
                "supported": True,
                "error": f"查询失败: {str(e)}"[:500],
                "raw": None,
            },
        )


@router.get("/v1/channels/balance_templates", dependencies=[Depends(rate_limit_dependency)])
async def get_balance_templates(token: str = Depends(verify_admin_api_key)):
    """
    获取所有预置的余额查询模板列表，供前端展示选择。
    """
    from core.balance import list_balance_templates
    return JSONResponse(content={"templates": list_balance_templates()})
