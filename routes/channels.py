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


TEST_PROVIDER_RUNTIME_FIELDS = {
    "api",
    "api_keys",
    "sub_channels",
    "_is_sub_channel",
    "_parent_provider",
    "_model_dict_cache",
    "_virtual_route_provider",
    "_virtual_priority",
    "_virtual_route_test",
}


def _normalize_key_item(item: Any) -> Optional[str]:
    """把渠道测试收到的单个 Key 配置归一化为字符串。"""
    # 修改原因：渠道测试入口需要兼容旧字符串、新对象和带标签单键 dict 三种 Key 配置。
    # 修改方式：把可用 Key 统一转换成字符串，并保留前导 ! 作为禁用标记。
    # 目的：让后续 Key 选择逻辑只处理一种数据形态，避免主路由继续堆积格式判断。
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
        if len(item) == 1:
            raw_key = next(iter(item.keys()))
            key = str(raw_key).strip()
            return key or None
    return None


def _collect_key_candidates(raw_keys: Any) -> list[str]:
    """从字符串、dict 或列表形式的 Key 配置中收集候选 Key。"""
    # 修改原因：前端不同入口可能提交 api、api_keys、api_key 等不同形态。
    # 修改方式：把非列表包装成单元素列表，再复用 _normalize_key_item 逐项解析。
    # 目的：保证单渠道测试和逐 Key 测试都能使用相同的候选 Key 选择规则。
    if raw_keys is None:
        return []
    items = raw_keys if isinstance(raw_keys, list) else [raw_keys]
    results: list[str] = []
    for item in items:
        normalized = _normalize_key_item(item)
        if normalized:
            results.append(normalized)
    return results


def _get_test_model_name(test_config: Dict[str, Any]) -> str:
    """读取测试请求模型名，优先使用内部已解析模型名。"""
    # 修改原因：model_prefix 渠道可能需要把前端传入的无前缀模型名解析为带前缀的外部模型名。
    # 修改方式：provider 构建阶段可写入 _resolved_test_model，请求构建阶段优先读取它。
    # 目的：在不改变公开请求体格式的前提下，让拆分后的 provider 构建和 request 构建共享解析结果。
    model = (
        test_config.get("_resolved_test_model")
        or test_config.get("model")
        or test_config.get("model_alias")
        or test_config.get("upstream_model")
        or ""
    )
    model = str(model).strip()
    if not model:
        raise HTTPException(status_code=400, detail="model 是必填项")
    return model


def _get_upstream_model_hint(test_config: Dict[str, Any]) -> Optional[str]:
    """读取前端传入的上游模型提示。"""
    # 修改原因：测试弹窗会同时传别名 model 和 upstream_model，别名缺失时要用它补全映射。
    # 修改方式：集中清洗 upstream_model 字段，空字符串统一视为 None。
    # 目的：让 provider 模型映射补全逻辑不再散落在主路由中。
    upstream_model_hint = test_config.get("upstream_model")
    return str(upstream_model_hint).strip() if upstream_model_hint else None


def _get_test_timeout(test_config: Dict[str, Any]) -> int:
    """读取渠道测试超时时间。"""
    # 修改原因：前端传入的 timeout 需要在 routes/channels.py 侧转为 handler 的 override_timeout。
    # 修改方式：保留原有默认值 30，并把非法值回退为默认值、最小值限制为 1。
    # 目的：不修改 handler.py 的前提下，让测试接口继续尊重前端超时设置。
    timeout = test_config.get("timeout", 30)
    try:
        return max(1, int(timeout))
    except Exception:
        return 30


def _is_virtual_route_test(test_config: Dict[str, Any]) -> bool:
    """判断当前请求是否为虚拟路由测试。"""
    # 修改原因：虚拟路由测试标记可能在请求体顶层，也可能在前端构造的 provider_snapshot 中。
    # 修改方式：在清洗 provider_snapshot 之前单独读取这两个位置。
    # 目的：后续清理 _virtual_route_test 运行时字段时，不破坏已有虚拟路由测试入口。
    provider_snapshot = test_config.get("provider_snapshot")
    return bool(
        test_config.get("virtual_route_test")
        or (isinstance(provider_snapshot, dict) and provider_snapshot.get("_virtual_route_test"))
    )


def _clean_test_provider_snapshot(provider_snapshot: Any) -> Dict[str, Any]:
    """深拷贝并清理测试用 provider snapshot。"""
    # 修改原因：前端快照可能包含运行时字段，直接交给 handler 会影响 Key 轮换、虚拟路由和子渠道语义。
    # 修改方式：进入 provider 构建的第一步就深拷贝快照，并剥离 api、api_keys、sub_channels 及内部运行时字段。
    # 目的：构造一个只用于本次测试的干净 provider，避免污染正式请求链路。
    provider = copy.deepcopy(provider_snapshot) if isinstance(provider_snapshot, dict) else {}
    if not isinstance(provider, dict):
        return {}
    for field in TEST_PROVIDER_RUNTIME_FIELDS:
        provider.pop(field, None)
    return provider


def _select_test_api_key(test_config: Dict[str, Any], provider_snapshot: Any) -> Optional[str]:
    """选择本次单渠道测试使用的 API Key。"""
    # 修改原因：测试 provider 必须剥离原始 Key 池，但 request_model 仍需要一个明确的 force_api_key。
    # 修改方式：先读取显式 api_key/api，再从原始 snapshot 的 api/api_keys 收集候选，优先选择未禁用 Key。
    # 目的：保留旧格式兼容性，同时避免 handler 根据完整 Key 池误算重试次数。
    explicit_api_key = test_config.get("api_key") or test_config.get("api")
    if isinstance(explicit_api_key, str) and explicit_api_key.strip():
        selected_api_key = explicit_api_key.strip()
        return selected_api_key[1:] if selected_api_key.startswith("!") else selected_api_key

    candidates: list[str] = []
    if not isinstance(explicit_api_key, str):
        candidates.extend(_collect_key_candidates(explicit_api_key))
    if isinstance(provider_snapshot, dict):
        candidates.extend(_collect_key_candidates(provider_snapshot.get("api")))
        candidates.extend(_collect_key_candidates(provider_snapshot.get("api_keys")))
    candidates.extend(_collect_key_candidates(test_config.get("api_keys")))

    for key in candidates:
        if not key.startswith("!"):
            return key
    if candidates:
        first = candidates[0]
        return first[1:] if first.startswith("!") else first
    return None


def _build_test_provider(test_config: Dict[str, Any], app: Any) -> tuple[Dict[str, Any], Optional[str], str]:
    """从测试配置构建干净 provider、选中的 Key 和 engine。"""
    # 修改原因：原 test_channel 同时负责 snapshot 清洗、engine 推断、base_url、模型映射和 Key 选择，难以维护。
    # 修改方式：把单渠道测试 provider 的构建集中到一个模块级函数，返回 handler 所需的最小输入。
    # 目的：让主路由只做调度，同时保留别名、model_prefix、upstream_model_hint 和默认 base_url 行为。
    from core.utils import get_model_dict

    _ = app
    provider_snapshot = test_config.get("provider_snapshot")
    provider = _clean_test_provider_snapshot(provider_snapshot)
    selected_api_key = _select_test_api_key(test_config, provider_snapshot)

    engine = (
        test_config.get("engine")
        or provider.get("engine")
        or test_config.get("type")
        or "openai"
    )
    engine = str(engine).strip() if engine is not None else "openai"

    model = _get_test_model_name(test_config)
    upstream_model_hint = _get_upstream_model_hint(test_config)

    channel = get_channel(engine)
    if not channel:
        raise HTTPException(status_code=404, detail=f"Channel type '{engine}' not found")

    provider["provider"] = provider.get("provider") or f"test_{engine or 'channel'}"
    provider["engine"] = engine

    base_url = test_config.get("base_url") or provider.get("base_url", "")
    base_url = str(base_url).strip() if base_url else ""
    if not base_url:
        if channel.default_base_url:
            base_url = channel.default_base_url
            logger.info(f"Using default base_url for channel '{engine}': {base_url}")
        else:
            raise HTTPException(status_code=400, detail="base_url 是必填项（该渠道类型没有默认地址）")
    if not base_url.startswith(("http://", "https://")):
        base_url = f"https://{base_url}"
        logger.info(f"Auto-prefixed base_url: {base_url}")
    provider["base_url"] = base_url.rstrip('/')

    if selected_api_key:
        provider["api"] = selected_api_key

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
    prefix = str(provider.get('model_prefix') or '').strip()
    resolved_model = model

    if resolved_model not in model_dict:
        prefixed_model = f"{prefix}{model}" if prefix else None
        if prefixed_model and prefixed_model in model_dict:
            resolved_model = prefixed_model
        else:
            if upstream_model_hint and upstream_model_hint != model:
                provider["model"].append({upstream_model_hint: model})
            else:
                provider["model"].append(model)
            model_dict = get_model_dict(provider)
            if prefixed_model and prefixed_model in model_dict:
                resolved_model = prefixed_model

    provider["_model_dict_cache"] = model_dict
    test_config["_resolved_test_model"] = resolved_model

    if resolved_model not in model_dict:
        raise HTTPException(status_code=400, detail=f"model '{model}' 不在当前渠道模型配置中")

    return provider, selected_api_key, engine


def _build_test_request(test_config: Dict[str, Any]):
    """从测试配置构建 RequestModel。"""
    # 修改原因：测试请求的 prompt、stream、max_tokens、temperature 默认值原本散在主路由中。
    # 修改方式：集中读取和类型转换，并复用 _get_test_model_name 取得最终请求模型名。
    # 目的：保持 API 请求格式不变，同时让主路由不再承担请求对象细节。
    from core.models import RequestModel

    model = _get_test_model_name(test_config)
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

    return RequestModel(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        stream=stream,
        temperature=temperature,
    )


def _resolve_virtual_test(test_config: Dict[str, Any], app: Any) -> list[Dict[str, Any]]:
    """解析虚拟路由测试候选 provider。"""
    # 修改原因：虚拟路由测试必须经过 preferences.virtual_models 的 chain 展开，不能走单 provider 直连。
    # 修改方式：把虚拟模型解析分支单独封装，主路由只接收展开后的 providers。
    # 目的：保留虚拟模型 fallback 链条，并让 override_auto_retry=True 只作用于虚拟路由测试。
    from core.virtual_routing import resolve_virtual_model

    model = _get_test_model_name(test_config)
    test_config["_resolved_test_model"] = model
    providers = resolve_virtual_model(model, app.state.config, 0, app)
    if providers is None:
        raise HTTPException(status_code=400, detail=f"model '{model}' 不是已配置的虚拟模型")
    if not providers:
        raise HTTPException(status_code=400, detail=f"虚拟模型 '{model}' 没有可用的路由链条")
    return providers


def _strip_gateway_prefix(msg: str) -> str:
    """剥离测试响应中由网关包装出来的错误前缀。"""
    # 修改原因：前端需要看到上游真实错误，而不是 Zoaholic 内部包装文本。
    # 修改方式：保留原有两种前缀规则，并在响应提取阶段统一调用。
    # 目的：让普通 body 和 body_iterator 错误都使用相同格式化结果。
    if not msg:
        return msg
    prefix = "Error: Current provider response failed: "
    if msg.startswith(prefix):
        return msg[len(prefix):]
    if msg.startswith("All ") and " error: " in msg:
        return msg.split(" error: ", 1)[1]
    return msg


async def _read_test_response_preview(response: Any, limit: int = 800) -> str:
    """读取测试响应预览文本。"""
    # 修改原因：request_model 可能返回已经有 body 的响应，也可能返回 StreamingResponse 的 body_iterator。
    # 修改方式：优先读取 body，缺失时最多消费 limit 长度的 body_iterator。
    # 目的：保留原测试接口的 response_preview 和错误解析能力。
    preview = ""
    if hasattr(response, "body"):
        try:
            body = response.body
            if isinstance(body, bytes):
                preview = body.decode("utf-8", errors="ignore")[:limit]
            else:
                preview = str(body)[:limit]
        except Exception:
            pass
    elif hasattr(response, "body_iterator"):
        chunks = []
        total = 0
        try:
            async for chunk in response.body_iterator:
                if isinstance(chunk, bytes):
                    chunk = chunk.decode("utf-8", errors="ignore")
                else:
                    chunk = str(chunk)
                chunks.append(chunk)
                total += len(chunk)
                if total > limit:
                    break
        except Exception:
            pass
        preview = "".join(chunks)[:limit]
    return preview


async def _extract_test_result(response: Any, start_time: float) -> Dict[str, Any]:
    """从 handler 响应中提取渠道测试结果。"""
    # 修改原因：响应预览、JSON 错误解析和认证失败判断原本占据主路由大量代码。
    # 修改方式：把 body/body_iterator 读取、错误前缀剥离和标准字段组装集中到这里。
    # 目的：保持前端响应结构完全不变，同时让 test_channel 成为薄调度层。
    latency_ms = int((time() - start_time) * 1000)
    status_code = response.status_code
    preview = await _read_test_response_preview(response)

    success = 200 <= status_code < 300
    auth_failed = status_code in (401, 403)

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
                    error_detail = _strip_gateway_prefix(str(resp_json["detail"]))
        except (json.JSONDecodeError, ValueError):
            error_detail = _strip_gateway_prefix(preview)

    return {
        "success": success,
        "latency_ms": latency_ms,
        "message": "测试成功" if success else f"HTTP {status_code}",
        "error": error_detail,
        "upstream_status_code": status_code,
        "auth_failed": auth_failed,
        "response_preview": preview if success else None,
    }


@router.get("/v1/playground/keys", dependencies=[Depends(rate_limit_dependency)])
async def get_playground_keys(token: str = Depends(verify_admin_api_key)):
    """返回 Playground 可切换的全局用户 API Key 列表。"""
    # 修改原因：Playground Key 选择需要模拟不同 api.yaml 用户身份，而不是指定 provider 上游密钥。
    # 修改方式：只读取全局 api_keys，返回显示名、脱敏文本和完整 api；该端点仍由管理员认证保护。
    # 目的：让 chat 和 models 请求都能用所选用户的 Bearer token 重新鉴权，并解除 Key 列表与 model 的耦合。
    _ = token
    app = get_app()
    config = getattr(getattr(app, "state", None), "config", None) or {}
    api_keys = config.get("api_keys") or []

    keys = []
    for index, item in enumerate(api_keys):
        if not isinstance(item, dict):
            continue
        api = str(item.get("api", "")).strip()
        if not api:
            continue
        name = item.get("name") or None
        masked = f"{api[:3]}...{api[-4:]}" if len(api) > 7 else "***"
        keys.append({
            "index": index,
            "name": name,
            "masked_key": masked,
            "api": api,
        })
    return JSONResponse(content={"keys": keys})


@router.get("/v1/channels", dependencies=[Depends(rate_limit_dependency)])
async def get_channels(token: str = Depends(verify_admin_api_key)):
    """
    获取所有已注册的渠道类型列表。
    返回每个渠道的 id, type_name, default_base_url, auth_header, description, has_models_adapter。
    """
    channels = list_channels()
    # 修改原因：渠道元数据中的 ui_slots 现在需要包含独立注册表叠加后的 resolved slots。
    # 修改方式：继续调用 ChannelDefinition.to_dict()，由渠道定义层统一执行 resolve_slots_for_engine。
    # 目的：保持 /v1/channels 端点结构不变，同时让前端获得 P1 阶段可解析的全局 slot。
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
    
    # OAuth 渠道：api 字段是邮箱/key_id，路由层统一 resolve 成 access_token
    if getattr(channel, "is_oauth", False) and hasattr(app.state, "oauth_manager"):
        # 修改原因：oauth_state 第一层 key 是用户配置的 provider 名，不是 engine；Antigravity 的 key 可能是“反重力oauth”。
        # 修改方式：先把 api/api_key 归一为 key_id 候选，再遍历 OAuthManager._state 的所有 channel_id 调 resolve。
        # 目的：models_adapter 只接收已经解析好的 access_token，保持签名为 (client, provider) 且不自行做 OAuth 查找。
        _om = app.state.oauth_manager
        _state = getattr(_om, "_state", {})
        _resolved_token = None
        for _key_id in _collect_key_candidates(provider.get("api")):
            if _key_id.startswith("!"):
                continue
            for _ch_id in list(_state.keys()) if isinstance(_state, dict) else []:
                _resolved = await _om.resolve(_ch_id, _key_id)
                if _resolved:
                    _resolved_token = _resolved
                    break
            if _resolved_token:
                provider["api"] = _resolved_token
                break

    try:
        from core.http import proxy_context
        import asyncio

        with proxy_context(proxy):
            async with app.state.client_manager.get_client(provider["base_url"], proxy) as client:
                intercepted_client = None
                try:
                    # 修改原因：InterceptedClient 会持有底层 client、provider 和插件列表，必须在请求结束时断开引用。
                    # 修改方式：保存包装器实例，并在 finally 中调用 close；业务代码仍使用 client 变量。
                    # 目的：让 models 查询完成或异常中断后都释放包装器持有的请求级对象。
                    enabled_plugins = safe_get(provider, "preferences", "enabled_plugins", default=None)
                    if enabled_plugins:
                        from core.plugins.interceptors import InterceptedClient
                        intercepted_client = InterceptedClient(client, engine, provider, enabled_plugins)
                        client = intercepted_client

                    models = await asyncio.wait_for(
                        channel.models_adapter(client, provider),
                        timeout=30.0
                    )
                    return JSONResponse(content={"models": models})
                finally:
                    if intercepted_client is not None:
                        intercepted_client.close()
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
    app = get_app()
    timeout = _get_test_timeout(test_config)
    virtual_route_test = _is_virtual_route_test(test_config)

    # 修改原因：普通单渠道测试和虚拟路由测试需要不同的 provider 来源。
    # 修改方式：虚拟路由测试走 resolve_virtual_model 展开链条；普通测试只构建一个干净 provider。
    # 目的：主函数只保留调度语义，不再混合 provider 构造细节。
    selected_api_key = None
    if virtual_route_test:
        override_providers = _resolve_virtual_test(test_config, app)
    else:
        provider, selected_api_key, engine = _build_test_provider(test_config, app)
        _ = engine
        override_providers = [provider]

    test_request = _build_test_request(test_config)

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
            # 修改原因：handler.py 的 override 重试语义已经稳定，不应再在 handler 内增加补丁。
            # 修改方式：单渠道测试显式传 False，虚拟路由测试显式传 True。
            # 目的：单渠道测试只测当前渠道，虚拟路由测试可以按 chain 候选 fallback。
            override_auto_retry=virtual_route_test,
            override_timeout=timeout,
        )

        return JSONResponse(content=await _extract_test_result(response, start_time))

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
    # 修改原因：前端余额展示读取 value_type、percent、available 等通用字段，而 OAuth quota 使用 quota_inner/quota_outer。
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
            "quota_inner": None,
            "quota_outer": None,
            "raw": None,
            "error": error or "OAuth 额度不可用",
        }

    quota_inner = _coerce_oauth_percent(quota.get("quota_inner"))
    quota_outer = _coerce_oauth_percent(quota.get("quota_outer"))
    percentages = [pct for pct in (quota_inner, quota_outer) if pct is not None]
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
        "quota_inner": quota_inner,
        "quota_outer": quota_outer,
        **extra,
        "raw": quota.get("raw") if isinstance(quota.get("raw"), dict) else None,
        "error": None,
    }


def _append_quota_snapshot_fields(result: Dict[str, Any]) -> Dict[str, Any]:
    """在旧 BalanceResult 响应上追加 QuotaSnapshot 新字段。"""
    # 修改原因：/v1/channels/balance 需要继续返回旧字段，同时让前端可以开始读取统一 quota 字段。
    # 修改方式：在所有返回分支末尾把旧结果转成 QuotaSnapshot，只追加 gauges、badges、metrics、extensions 四类新字段。
    # 目的：不改变现有余额查询逻辑和旧字段含义，降低 P0 接入风险。
    if not isinstance(result, dict):
        return result

    from core.quota.normalizers import from_balance_result, from_oauth_account

    balance_snapshot = from_balance_result(result)
    if result.get("quota_inner") is not None or result.get("quota_outer") is not None:
        snapshot = from_oauth_account(result)
        snapshot.badges.extend(balance_snapshot.badges)
        if not snapshot.gauges:
            snapshot.gauges.extend(balance_snapshot.gauges)
    else:
        snapshot = balance_snapshot

    snapshot_fields = snapshot.to_dict()
    merged = dict(result)
    for key, default in (("gauges", []), ("badges", []), ("metrics", {}), ("extensions", {})):
        if merged.get(key) is None:
            merged[key] = snapshot_fields.get(key, default.copy())
    return merged


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
            "quota_inner": None,
            "quota_outer": None,
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
        "quota_inner": None,
        "quota_outer": None,
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
            quota = await fetch_quota(channel_id, key_id, force=True)
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
        # 修改原因：OAuth 余额结果也可能需要插件补充 tier、rpm、tpm 等被动采集字段。
        # 修改方式：从原始 provider_config 读取 enabled_plugins，并在返回前调用 balance_enricher 链。
        # 目的：让 oai_tier 可以在不改 OAuth 查询逻辑的情况下补充 balance result。
        from core.plugins.interceptors import apply_balance_enrichers
        enabled_plugins = safe_get(provider_config, "preferences", "enabled_plugins", default=None)
        result = await apply_balance_enrichers(result, engine, provider, enabled_plugins)
        return JSONResponse(content=_append_quota_snapshot_fields(result))

    from core.balance import query_provider_balance, build_balance_config

    # 验证是否配置了 balance
    balance_cfg = build_balance_config(provider)
    if not balance_cfg:
        # 没有 balance 模板，但插件可能有数据（如 oai_tier 的被动采集）
        enabled_plugins = safe_get(provider_config, "preferences", "enabled_plugins", default=None)
        if enabled_plugins:
            from core.plugins.interceptors import apply_balance_enrichers
            fallback = {"supported": True, "value_type": "amount", "raw": None, "error": None}
            fallback = await apply_balance_enrichers(fallback, engine, provider, enabled_plugins)
            # enricher 补了有效字段（如 tier）就返回，否则返回提示
            if any(k not in ("supported", "value_type", "raw", "error") for k in fallback):
                return JSONResponse(content=_append_quota_snapshot_fields(fallback))
            # 有插件但缓存还没数据，提示用户发请求触发采集
            return JSONResponse(content=_append_quota_snapshot_fields({
                "supported": True,
                "error": "尚未采集到数据，请先通过该渠道发送一次请求",
            }))
        return JSONResponse(content=_append_quota_snapshot_fields({
            "supported": False,
            "error": "该渠道未配置余额查询（preferences.balance）",
        }))

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
                intercepted_client = None
                try:
                    # 修改原因：余额查询路径也会创建 InterceptedClient，异常返回时同样需要释放引用。
                    # 修改方式：将包装器生命周期限定在 try/finally 中，并在 finally 调用 close。
                    # 目的：避免余额查询结束后继续持有底层 client、provider 和插件配置。
                    enabled_plugins = safe_get(provider, "preferences", "enabled_plugins", default=None)
                    if enabled_plugins:
                        from core.plugins.interceptors import InterceptedClient
                        intercepted_client = InterceptedClient(client, engine, provider, enabled_plugins)
                        client = intercepted_client

                    result = await query_provider_balance(client, provider)
                    # 修改原因：普通渠道的余额查询返回后需要允许插件补充 OpenAI Tier 等被动信息。
                    # 修改方式：复用上方已解析的 enabled_plugins，对 result 调用 balance_enricher 链。
                    # 目的：不改 core.balance 模板，也能把 oai_tier 缓存的信息带回前端。
                    from core.plugins.interceptors import apply_balance_enrichers
                    result = await apply_balance_enrichers(result, engine, provider, enabled_plugins)
                    return JSONResponse(content=_append_quota_snapshot_fields(result))
                finally:
                    if intercepted_client is not None:
                        intercepted_client.close()

    except Exception as e:
        logger.error(f"Balance query error: {e}")
        return JSONResponse(
            status_code=200,
            content=_append_quota_snapshot_fields({
                "supported": True,
                "error": f"查询失败: {str(e)}"[:500],
                "raw": None,
            }),
        )


@router.get("/v1/channels/balance_templates", dependencies=[Depends(rate_limit_dependency)])
async def get_balance_templates(token: str = Depends(verify_admin_api_key)):
    """
    获取所有预置的余额查询模板列表，供前端展示选择。
    """
    from core.balance import list_balance_templates
    return JSONResponse(content={"templates": list_balance_templates()})
