"""
Models 路由
"""

from typing import Any, Dict, List, Optional, Set

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from core.byok import get_byok_real_key, is_byok_provider
from core.api_key_access import is_admin_api_key
from core.channels import get_channel
from core.http import proxy_context
from core.log_config import logger
from core.utils import get_model_dict, is_local_api_key
from utils import post_all_models, safe_get
from routes.deps import rate_limit_dependency, verify_api_key, get_app

router = APIRouter()


def _append_model_info_if_missing(models: List[Dict[str, Any]], unique: Set[str], model_id: str) -> None:
    """追加 OpenAI models 格式条目，并按 id 去重。"""
    # 修改原因：BYOK 动态模型列表和旧 post_all_models 都返回同一响应格式，去重逻辑需要集中。
    # 修改方式：只在 model_id 非空且未出现时追加标准模型对象。
    # 目的：让静态配置模型和 adapter 动态模型可以安全合并。
    if not model_id or model_id in unique:
        return
    unique.add(model_id)
    models.append({
        "id": model_id,
        "object": "model",
        "created": 1720524448858,
        "owned_by": "Zoaholic",
    })


def _provider_groups_allowed(provider: dict, allowed_groups: set) -> bool:
    """判断 provider 分组是否允许当前 API Key 访问。"""
    # 修改原因：BYOK 动态模型列表不能绕过现有 API Key groups 授权。
    # 修改方式：复用 provider.groups 与 api_keys[].groups 的交集规则。
    # 目的：模型列表展示边界和真实请求路由边界一致。
    p_groups = provider.get("groups") or ["default"]
    if isinstance(p_groups, str):
        p_groups = [p_groups] if p_groups else ["default"]
    if not isinstance(p_groups, list) or not p_groups:
        p_groups = ["default"]
    return bool(allowed_groups.intersection(set(p_groups)))


async def _fetch_byok_provider_models(app, provider: dict, byok_real_key: str) -> List[str]:
    """使用 BYOK 真实上游 key 动态拉取 provider 模型列表。"""
    # 修改原因：provider.model=["*"] 没有可枚举的静态模型，BYOK 请求 /v1/models 时应使用用户真实 key 查询上游。
    # 修改方式：浅拷贝 provider，把 api 临时替换为 byok_real_key，再调用渠道 models_adapter。
    # 目的：不污染运行时配置和 key pool，同时让上游鉴权使用请求中的真实 key。
    engine = provider.get("engine") or "openai"
    channel = get_channel(engine)
    if not channel or not channel.models_adapter:
        return []

    provider_copy = dict(provider)
    provider_copy["api"] = byok_real_key
    proxy = safe_get(app.state.config, "preferences", "proxy", default=None)
    proxy = safe_get(provider_copy, "preferences", "proxy", default=proxy)
    base_url = provider_copy.get("base_url") or (channel.default_base_url or "")
    provider_copy["base_url"] = base_url

    with proxy_context(proxy):
        async with app.state.client_manager.get_client(base_url, proxy) as client:
            try:
                return await channel.models_adapter(client, provider_copy)
            except Exception as exc:
                logger.warning("BYOK models_adapter failed for provider %s: %s", provider.get("provider"), exc)
                return []


async def build_models_for_request(api_index: int, app=None) -> List[Dict[str, Any]]:
    """构建当前请求可见的模型列表，支持 BYOK 动态拉取。"""
    # 修改原因：原 post_all_models 是同步函数，不能在 provider.model=["*"] 时调用上游 adapter。
    # 修改方式：先保留旧静态行为，再在 BYOK 请求中对通配符 BYOK provider 追加 adapter 动态模型。
    # 目的：兼容已有模型列表语义，并补齐 BYOK 模式下 /v1/models 的真实可用模型展示。
    if app is None:
        app = get_app()

    if not hasattr(app.state, "models_list") or app.state.models_list is None:
        app.state.models_list = {}

    models = post_all_models(api_index, app.state.config, app.state.api_list, app.state.models_list)
    unique = {item.get("id") for item in models if isinstance(item, dict) and item.get("id")}

    byok_real_key = get_byok_real_key()
    if not byok_real_key:
        return models

    api_key_groups = safe_get(app.state.config, "api_keys", api_index, "groups", default=["default"])
    if isinstance(api_key_groups, str):
        api_key_groups = [api_key_groups]
    if not isinstance(api_key_groups, list) or not api_key_groups:
        api_key_groups = ["default"]
    allowed_groups = set(api_key_groups)
    admin_access = is_admin_api_key(app.state.config, api_index)

    api_models = safe_get(app.state.config, "api_keys", api_index, "model", default=[]) or []
    authorized_byok_providers = set()
    allow_all = False
    for rule in api_models:
        if isinstance(rule, dict) and rule:
            rule = next(iter(rule.keys()))
        if not isinstance(rule, str):
            continue
        if rule == "all":
            allow_all = True
        elif "/" in rule:
            provider_name, model_rule = rule.split("/", 1)
            if model_rule == "*":
                authorized_byok_providers.add(provider_name)

    for provider in app.state.config.get("providers", []) or []:
        if provider.get("enabled") is False:
            continue
        if not is_byok_provider(provider):
            continue
        if is_local_api_key(provider.get("provider", "")):
            continue
        if not admin_access and not _provider_groups_allowed(provider, allowed_groups):
            continue
        provider_name = provider.get("provider")
        if not allow_all and provider_name not in authorized_byok_providers:
            continue

        model_dict = provider.get("_model_dict_cache") or get_model_dict(provider)
        has_only_wildcard = list(model_dict.keys()) == ["*"] or provider.get("model") == ["*"]
        if not has_only_wildcard:
            continue

        dynamic_models = await _fetch_byok_provider_models(app, provider, byok_real_key)
        prefix = str(provider.get("model_prefix") or "").strip()
        for model_id in dynamic_models:
            if not isinstance(model_id, str):
                continue
            public_model_id = f"{prefix}{model_id}" if prefix and not model_id.startswith(prefix) else model_id
            _append_model_info_if_missing(models, unique, public_model_id)

    models.sort(key=lambda x: x["id"])
    return models


@router.get("/v1/models", dependencies=[Depends(rate_limit_dependency)])
async def list_models(request: Request, api_index: int = Depends(verify_api_key)):
    """列出可用模型。

    返回当前 API Key 可访问的所有模型列表。

    兼容：
    - 管理控制台使用 admin JWT 访问时（Authorization: Bearer <jwt>），
      verify_api_key 会将其映射为配置中的 admin api_key index，从而也能正常拿到模型列表。
    - BYOK 请求使用通配符模板身份计费，provider.model=["*"] 且有 models_adapter 时用真实上游 key 动态拉取模型。
    """
    app = request.app if request is not None else get_app()
    models = await build_models_for_request(api_index, app)
    return JSONResponse(content={
        "object": "list",
        "data": models,
    })
