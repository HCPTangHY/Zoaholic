"""
Provider 匹配与调度模块

包含模型规则解析、provider 列表生成、调度算法（加权轮询、彩票调度）、TPR 限制等功能。
"""

import random
from typing import Dict, List, Optional, Any, TYPE_CHECKING

from fastapi import HTTPException

from core.log_config import logger
from core.utils import (
    get_model_dict,
    circular_list_encoder,
    is_local_api_key,
    provider_api_circular_list,
)
from utils import safe_get

if TYPE_CHECKING:
    from fastapi import FastAPI

# 调试模式标志，由 main 模块设置
is_debug = False


def set_debug_mode(debug: bool):
    """设置调试模式"""
    global is_debug
    is_debug = debug


def _is_pool_sharing_enabled(provider: Dict[str, Any]) -> bool:
    """判断渠道是否开启共享路由池。"""
    # 修改原因：pool_sharing 是渠道级显式开关，默认必须关闭，避免旧渠道被无前缀模型名误命中。
    # 修改方式：只读取 provider.preferences.pool_sharing，并兼容少量字符串布尔值。
    # 目的：让带 model_prefix 的渠道只有在明确开启时才参与无前缀请求池。
    preferences = provider.get("preferences") or {}
    if not isinstance(preferences, dict):
        return False
    value = preferences.get("pool_sharing", False)
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "on"}
    return bool(value)


def _get_pool_sharing_model_name(
    provider: Dict[str, Any],
    request_model: str,
    model_dict: Dict[str, str]
) -> Optional[str]:
    """返回可用于共享路由池的带前缀模型名；不满足条件时返回 None。"""
    # 修改原因：无前缀请求需要在 pool_sharing=True 时，用 prefix+request_model 去匹配带前缀渠道。
    # 修改方式：保持 request_model 原样，只把匹配规则转换成带前缀外部模型名。
    # 目的：让用户请求 deepseek-chat 时可以进入 [sili]deepseek-chat 的候选池，但请求 [sili]deepseek-chat 仍精准命中。
    prefix = provider.get("model_prefix", "").strip()
    if not prefix or not _is_pool_sharing_enabled(provider):
        return None
    if request_model.startswith(prefix):
        return None

    prefixed_model = f"{prefix}{request_model}"
    if prefixed_model in model_dict:
        return prefixed_model
    return None


def _append_provider_rule(provider_rules: List[str], provider_name: str, model_name: str) -> None:
    """追加 provider/model 规则，并避免 all 分支和共享分支产生重复项。"""
    # 修改原因：all 规则会枚举带前缀模型，pool_sharing 也会计算同一条规则。
    # 修改方式：集中去重后再追加。
    # 目的：保证后续权重调度不会因为重复规则改变候选池比例。
    rule = provider_name + "/" + model_name
    if rule not in provider_rules:
        provider_rules.append(rule)


def _is_virtual_model_authorized(request_model: str, config: Dict[str, Any], api_index: int) -> bool:
    """判断当前 API Key 是否允许访问虚拟模型名。"""
    # 修改原因：虚拟模型只是新的模型名入口，必须复用 API Key 的 model 授权数组。
    # 修改方式：只接受 "all" 或与虚拟模型名完全相同的 model 规则。
    # 目的：避免虚拟模型绕过现有 API Key 授权边界。
    model_rules = safe_get(config, 'api_keys', api_index, 'model', default=[]) or []
    for rule in model_rules:
        if isinstance(rule, dict) and rule:
            rule = next(iter(rule.keys()))
        if not isinstance(rule, str):
            continue
        if rule == "all" or rule == request_model:
            return True
    return False


def _filter_provider_list(
    provider_list: List[Dict[str, Any]],
    request_model: str,
    config: Dict[str, Any],
    api_index: int,
) -> List[Dict[str, Any]]:
    """复用黑名单和分组过滤逻辑。"""
    # 修改原因：虚拟模型候选池和普通模型候选池都必须经过同一套 API Key 过滤。
    # 修改方式：把 get_matching_providers 中的过滤逻辑抽成独立函数。
    # 目的：让虚拟模型注入点只替换候选池生成，不改变黑名单与分组语义。
    # ── 黑名单过滤 ──
    # excluded_channels: 排除整个渠道
    # excluded_models: 排除模型，支持三种格式：
    #   "模型名"         → 排除所有渠道的该模型
    #   "模型名*"        → 通配符前缀匹配
    #   "渠道名/模型名"   → 只排除指定渠道的指定模型（支持 渠道名/模型名* 通配）
    excluded_channels = safe_get(config, 'api_keys', api_index, 'preferences', 'excluded_channels', default=None) or []
    excluded_models_cfg = safe_get(config, 'api_keys', api_index, 'preferences', 'excluded_models', default=None) or []

    if isinstance(excluded_channels, str):
        excluded_channels = [s.strip() for s in excluded_channels.split(",") if s.strip()]
    if isinstance(excluded_models_cfg, str):
        excluded_models_cfg = [s.strip() for s in excluded_models_cfg.split(",") if s.strip()]

    # 排除整个渠道
    if excluded_channels:
        ch_set = set(excluded_channels)
        provider_list = [p for p in provider_list if p.get('provider') not in ch_set]

    # 排除模型
    if excluded_models_cfg:
        global_exact = set()
        global_prefixes = []
        pair_exact = set()
        pair_prefixes = []

        for item in excluded_models_cfg:
            item = item.strip()
            if not item:
                continue
            if "/" in item:
                ch, mdl = item.split("/", 1)
                if mdl.endswith("*"):
                    pair_prefixes.append((ch, mdl.rstrip("*")))
                else:
                    pair_exact.add((ch, mdl))
            elif item.endswith("*"):
                global_prefixes.append(item.rstrip("*"))
            else:
                global_exact.add(item)

        # 全局模型黑名单命中 → 直接返回空
        if request_model in global_exact:
            logger.info("blacklist: request_model %s hit excluded_models exact match", request_model)
            return []
        for prefix in global_prefixes:
            if request_model.startswith(prefix):
                logger.info("blacklist: request_model %s hit excluded_models prefix '%s*'", request_model, prefix)
                return []

        # 渠道/模型 对过滤
        if pair_exact or pair_prefixes:
            new_list = []
            for p in provider_list:
                pname = p.get('provider', '')
                if (pname, request_model) in pair_exact:
                    continue
                skip = False
                for pair_ch, pair_pfx in pair_prefixes:
                    if pname == pair_ch and request_model.startswith(pair_pfx):
                        skip = True
                        break
                if not skip:
                    new_list.append(p)
            provider_list = new_list

    # 分组过滤：仅保留与 API Key 分组有交集的渠道
    api_key_groups = safe_get(config, 'api_keys', api_index, 'groups', default=['default'])
    if isinstance(api_key_groups, str):
        api_key_groups = [api_key_groups]
    if not isinstance(api_key_groups, list) or not api_key_groups:
        api_key_groups = ['default']
    s_key = set(api_key_groups)

    filtered = []
    for p in provider_list:
        p_groups = p.get('groups', ['default'])
        if isinstance(p_groups, str):
            p_groups = [p_groups]
        if not isinstance(p_groups, list) or not p_groups:
            p_groups = ['default']
        if s_key.intersection(set(p_groups)):
            filtered.append(p)

    return filtered


def weighted_round_robin(weights: Dict[str, int]) -> List[str]:
    """
    加权轮询调度算法
    
    Args:
        weights: 字典，键为 provider 名称，值为权重
        
    Returns:
        按加权轮询顺序排列的 provider 名称列表
    """
    provider_names = list(weights.keys())
    current_weights = {name: 0 for name in provider_names}
    num_selections = total_weight = sum(weights.values())
    weighted_provider_list = []

    for _ in range(num_selections):
        max_ratio = -1
        selected_letter = None

        for name in provider_names:
            current_weights[name] += weights[name]
            ratio = current_weights[name] / weights[name]

            if ratio > max_ratio:
                max_ratio = ratio
                selected_letter = name

        weighted_provider_list.append(selected_letter)
        current_weights[selected_letter] -= total_weight

    return weighted_provider_list


def lottery_scheduling(weights: Dict[str, int]) -> List[str]:
    """
    彩票调度算法
    
    Args:
        weights: 字典，键为 provider 名称，值为权重（彩票数量）
        
    Returns:
        按彩票调度顺序排列的 provider 名称列表
    """
    total_tickets = sum(weights.values())
    selections = []
    for _ in range(total_tickets):
        ticket = random.randint(1, total_tickets)
        cumulative = 0
        for provider, weight in weights.items():
            cumulative += weight
            if ticket <= cumulative:
                selections.append(provider)
                break
    return selections


async def get_provider_rules(
    model_rule: str,
    config: Dict[str, Any],
    request_model: str,
    app: "FastAPI"
) -> List[str]:
    """
    根据模型规则获取 provider 规则列表
    
    Args:
        model_rule: 模型规则字符串（如 "all", "provider/model", "model"）
        config: 配置字典
        request_model: 请求的模型名称
        app: FastAPI 应用实例
        
    Returns:
        provider 规则列表，格式为 ["provider/model", ...]
    """
    provider_rules = []
    
    if model_rule == "all":
        # 如模型名为 all，则返回所有模型
        for provider in config["providers"]:
            # 跳过禁用的渠道
            if provider.get("enabled") is False:
                continue
            model_dict = provider["_model_dict_cache"]
            # 识别被重定向的上游原名
            upstream_candidates = {v for k, v in model_dict.items() if v != k}
            # 如果渠道配置了 model_prefix，只返回带前缀的模型名
            prefix = provider.get('model_prefix', '').strip()
            for model in model_dict.keys():
                # 跳过通配符标记，"*" 渠道不能在 all 模式下枚举
                if model == "*":
                    continue
                # 过滤掉被重定向的上游原名
                if model in upstream_candidates:
                    continue
                # 如果有前缀，只返回带前缀的模型名
                if prefix and not model.startswith(prefix):
                    continue
                _append_provider_rule(provider_rules, provider["provider"], model)
            pool_model = _get_pool_sharing_model_name(provider, request_model, model_dict)
            if pool_model:
                _append_provider_rule(provider_rules, provider["provider"], pool_model)

    elif "/" in model_rule:
        if model_rule.startswith("<") and model_rule.endswith(">"):
            model_rule = model_rule[1:-1]
            # 处理带斜杠的模型名
            for provider in config['providers']:
                # 跳过禁用的渠道
                if provider.get("enabled") is False:
                    continue
                model_dict = provider["_model_dict_cache"]
                if model_rule in model_dict.keys():
                    _append_provider_rule(provider_rules, provider['provider'], model_rule)
                else:
                    pool_model = _get_pool_sharing_model_name(provider, request_model, model_dict)
                    if model_rule == request_model and pool_model:
                        _append_provider_rule(provider_rules, provider['provider'], pool_model)
        else:
            provider_name = model_rule.split("/")[0]
            model_name_split = "/".join(model_rule.split("/")[1:])
            models_list = []
            matched_provider = None

            # api_keys 中 api 为本地 Key 时，表示继承 api_keys，将 api_keys 中的 api key 当作渠道
            if is_local_api_key(provider_name) and provider_name in app.state.api_list:
                if app.state.models_list.get(provider_name):
                    models_list = app.state.models_list[provider_name]
                else:
                    models_list = []
            else:
                for provider in config['providers']:
                    # 跳过禁用的渠道
                    if provider.get("enabled") is False:
                        continue
                    model_dict = provider["_model_dict_cache"]
                    if provider['provider'] == provider_name:
                        models_list.extend(list(model_dict.keys()))
                        matched_provider = provider

            pool_model = None
            if matched_provider is not None:
                # 修改原因：provider/* 和 provider/model 两类显式规则也需要支持共享路由池。
                # 修改方式：只在 pool_sharing=True 且 request_model 为无前缀名时，计算 prefix+request_model。
                # 目的：避免该能力只在 all 规则下生效，保持不同模型规则行为一致。
                pool_model = _get_pool_sharing_model_name(
                    matched_provider,
                    request_model,
                    matched_provider["_model_dict_cache"]
                )

            # api_keys 中 model 为 provider_name/* 时，表示所有模型都匹配
            if model_name_split == "*":
                # 渠道配置了 model: ["*"] 时，接受任意模型名透传
                # 但如果请求模型名本身以 * 结尾（如 gpt-4*），优先走下方的前缀展开逻辑
                if "*" in models_list and not request_model.endswith("*"):
                    _append_provider_rule(provider_rules, provider_name, request_model)
                elif request_model in models_list:
                    _append_provider_rule(provider_rules, provider_name, request_model)
                elif pool_model and pool_model in models_list:
                    _append_provider_rule(provider_rules, provider_name, pool_model)

                # 如果请求模型名： gpt-4* ，则匹配所有以模型名开头且不以 * 结尾的模型
                for models_list_model in models_list:
                    if models_list_model == "*":
                        continue
                    if request_model.endswith("*") and models_list_model.startswith(request_model.rstrip("*")):
                        _append_provider_rule(provider_rules, provider_name, models_list_model)

            # api_keys 中 model 为 provider_name/model_name 时，表示模型名完全匹配
            elif model_name_split == request_model \
            or (request_model.endswith("*") and model_name_split.startswith(request_model.rstrip("*"))):
                # api_keys 中 model 为 provider_name/model_name 时，请求模型名： model_name*
                if model_name_split in models_list:
                    _append_provider_rule(provider_rules, provider_name, model_name_split)
                elif model_name_split == request_model and pool_model:
                    _append_provider_rule(provider_rules, provider_name, pool_model)

    else:
        for provider in config["providers"]:
            # 跳过禁用的渠道
            if provider.get("enabled") is False:
                continue
            model_dict = provider["_model_dict_cache"]
            if model_rule in model_dict.keys():
                _append_provider_rule(provider_rules, provider["provider"], model_rule)
            else:
                pool_model = _get_pool_sharing_model_name(provider, request_model, model_dict)
                if model_rule == request_model and pool_model:
                    _append_provider_rule(provider_rules, provider["provider"], pool_model)

    return provider_rules


def get_provider_list(
    provider_rules: List[str],
    config: Dict[str, Any],
    request_model: str,
    app: "FastAPI"
) -> List[Dict[str, Any]]:
    """
    根据 provider 规则列表生成 provider 配置列表
    
    Args:
        provider_rules: provider 规则列表
        config: 配置字典
        request_model: 请求的模型名称
        app: FastAPI 应用实例
        
    Returns:
        provider 配置列表
    """
    provider_list = []
    
    for item in provider_rules:
        provider_name = item.split("/")[0]
        if is_local_api_key(provider_name) and provider_name in app.state.api_list:
            # 加载本地聚合器 Key 的分组
            try:
                local_index = app.state.api_list.index(provider_name)
                local_groups = safe_get(app.state.api_keys_db, local_index, "groups", default=["default"])
            except ValueError:
                local_groups = ["default"]
            if isinstance(local_groups, str):
                local_groups = [local_groups] if local_groups else ["default"]
            if not isinstance(local_groups, list) or not local_groups:
                local_groups = ["default"]

            provider_list.append({
                "provider": provider_name,
                "base_url": "http://127.0.0.1:8000/v1/chat/completions",
                "model": [{request_model: request_model}],
                "tools": True,
                "_model_dict_cache": {request_model: request_model},
                "groups": local_groups,
            })
        else:
            for provider in config['providers']:
                model_dict = provider["_model_dict_cache"]
                if not model_dict:
                    continue
                model_name_split = "/".join(item.split("/")[1:])
                is_wildcard_channel = "*" in model_dict

                if "/" in item and provider['provider'] == provider_name and (model_name_split in model_dict.keys() or is_wildcard_channel):
                    # 通配符渠道：为未在 model_dict 中列出的模型名构建透传映射
                    if is_wildcard_channel and model_name_split not in model_dict:
                        # 构建临时 model_dict 副本，注入当前请求模型的映射
                        wildcard_model_dict = dict(model_dict)
                        wildcard_model_dict[request_model] = request_model
                        new_provider = {
                            "provider": provider["provider"],
                            "base_url": provider.get("base_url", ""),
                            "api": provider.get("api", None),
                            "model": [{request_model: request_model}],
                            "preferences": provider.get("preferences", {}),
                            "tools": provider.get("tools", False),
                            "_model_dict_cache": wildcard_model_dict,
                            "project_id": provider.get("project_id", None),
                            "private_key": provider.get("private_key", None),
                            "client_email": provider.get("client_email", None),
                            "cf_account_id": provider.get("cf_account_id", None),
                            "aws_access_key": provider.get("aws_access_key", None),
                            "aws_secret_key": provider.get("aws_secret_key", None),
                            "engine": provider.get("engine", None),
                            "groups": provider.get("groups", ["default"]),
                        }
                        provider_list.append(new_provider)
                    elif _get_pool_sharing_model_name(provider, request_model, model_dict) == model_name_split:
                        # 修改原因：pool_sharing 通过带前缀规则命中，但下游请求仍使用用户传入的无前缀模型名。
                        # 修改方式：provider.model 按 {上游原始名: 用户请求名} 构造，缓存按 {用户请求名: 上游原始名} 注入。
                        # 目的：用户看到 deepseek-chat 不变，实际发送给上游的仍是带前缀渠道配置中的原始模型。
                        shared_model_dict = dict(provider["_model_dict_cache"])
                        shared_model_dict[request_model] = model_dict[model_name_split]
                        new_provider = {
                            "provider": provider["provider"],
                            "base_url": provider.get("base_url", ""),
                            "api": provider.get("api", None),
                            "model": [{model_dict[model_name_split]: request_model}],
                            "preferences": provider.get("preferences", {}),
                            "tools": provider.get("tools", False),
                            "_model_dict_cache": shared_model_dict,
                            "project_id": provider.get("project_id", None),
                            "private_key": provider.get("private_key", None),
                            "client_email": provider.get("client_email", None),
                            "cf_account_id": provider.get("cf_account_id", None),
                            "aws_access_key": provider.get("aws_access_key", None),
                            "aws_secret_key": provider.get("aws_secret_key", None),
                            "engine": provider.get("engine", None),
                            "groups": provider.get("groups", ["default"]),
                        }
                        provider_list.append(new_provider)
                    elif request_model in model_dict.keys() and model_name_split == request_model:
                        new_provider = {
                            "provider": provider["provider"],
                            "base_url": provider.get("base_url", ""),
                            "api": provider.get("api", None),
                            "model": [{model_dict[model_name_split]: request_model}],
                            "preferences": provider.get("preferences", {}),
                            "tools": provider.get("tools", False),
                            "_model_dict_cache": provider["_model_dict_cache"],
                            "project_id": provider.get("project_id", None),
                            "private_key": provider.get("private_key", None),
                            "client_email": provider.get("client_email", None),
                            "cf_account_id": provider.get("cf_account_id", None),
                            "aws_access_key": provider.get("aws_access_key", None),
                            "aws_secret_key": provider.get("aws_secret_key", None),
                            "engine": provider.get("engine", None),
                            "groups": provider.get("groups", ["default"]),
                        }
                        provider_list.append(new_provider)

                    elif request_model.endswith("*") and model_name_split.startswith(request_model.rstrip("*")):
                        new_provider = {
                            "provider": provider["provider"],
                            "base_url": provider.get("base_url", ""),
                            "api": provider.get("api", None),
                            "model": [{model_dict[model_name_split]: request_model}],
                            "preferences": provider.get("preferences", {}),
                            "tools": provider.get("tools", False),
                            "_model_dict_cache": provider["_model_dict_cache"],
                            "project_id": provider.get("project_id", None),
                            "private_key": provider.get("private_key", None),
                            "client_email": provider.get("client_email", None),
                            "cf_account_id": provider.get("cf_account_id", None),
                            "aws_access_key": provider.get("aws_access_key", None),
                            "aws_secret_key": provider.get("aws_secret_key", None),
                            "engine": provider.get("engine", None),
                            "groups": provider.get("groups", ["default"]),
                        }
                        provider_list.append(new_provider)
    return provider_list


async def get_matching_providers(
    request_model: str,
    config: Dict[str, Any],
    api_index: int,
    app: "FastAPI"
) -> List[Dict[str, Any]]:
    """
    获取与请求模型匹配的所有 provider
    
    Args:
        request_model: 请求的模型名称
        config: 配置字典
        api_index: API key 索引
        app: FastAPI 应用实例
        
    Returns:
        匹配的 provider 配置列表
    """
    # 修改原因：虚拟模型名在真实 provider 模型表中不存在，需要先展开成临时 provider 候选池。
    # 修改方式：命中 preferences.virtual_models 后，先校验 API Key model 授权，再复用普通过滤逻辑。
    # 目的：让虚拟模型只替换“候选渠道生成”阶段，不改变黑名单、分组和后续调度行为。
    from core.virtual_routing import resolve_virtual_model
    virtual_providers = resolve_virtual_model(request_model, config, api_index, app)
    if virtual_providers is not None:
        if not _is_virtual_model_authorized(request_model, config, api_index):
            return []
        return _filter_provider_list(virtual_providers, request_model, config, api_index)

    provider_rules = []

    for model_rule in config['api_keys'][api_index]['model']:
        provider_rules.extend(await get_provider_rules(model_rule, config, request_model, app))
    
    provider_list = get_provider_list(provider_rules, config, request_model, app)
    return _filter_provider_list(provider_list, request_model, config, api_index)


async def get_right_order_providers(
    request_model: str,
    config: Dict[str, Any],
    api_index: int,
    scheduling_algorithm: str,
    app: "FastAPI",
    request_total_tokens: Optional[int] = None
) -> List[Dict[str, Any]]:
    """
    获取按正确顺序排列的 provider 列表（应用调度算法和过滤）
    
    Args:
        request_model: 请求的模型名称
        config: 配置字典
        api_index: API key 索引
        scheduling_algorithm: 调度算法名称
        app: FastAPI 应用实例
        request_total_tokens: 请求的总 token 数（可选，用于 TPR 限制）
        
    Returns:
        按调度顺序排列的 provider 配置列表
        
    Raises:
        HTTPException: 当没有可用的 provider 时
    """
    matching_providers = await get_matching_providers(request_model, config, api_index, app)

    # 筛查是否该请求token数量超过渠道tpr
    if request_total_tokens and matching_providers:
        available_providers = []
        for provider in matching_providers:
            model_dict = get_model_dict(provider)
            original_model = model_dict[request_model]
            provider_name = provider['provider']
            if is_local_api_key(provider_name) and provider_name in app.state.api_list:
                # Local API keys are added directly as their limits are handled elsewhere
                available_providers.append(provider)
                continue

            # First, check TPR limit
            is_tpr_exceeded = await provider_api_circular_list[provider_name].is_tpr_exceeded(
                original_model, tokens=request_total_tokens
            )
            if is_tpr_exceeded:
                continue
            available_providers.append(provider)

        matching_providers = available_providers

        if not matching_providers:
            raise HTTPException(
                status_code=413,
                detail=f"The request body is too long, No available providers at the moment: {request_model}"
            )

    if not matching_providers:
        raise HTTPException(status_code=404, detail=f"No available providers at the moment: {request_model}")

    num_matching_providers = len(matching_providers)
    
    # 如果某个渠道的一个模型报错，这个渠道会被排除
    if app.state.channel_manager.cooldown_period > 0 and num_matching_providers > 1:
        matching_providers = await app.state.channel_manager.get_available_providers(matching_providers)
        num_matching_providers = len(matching_providers)
        if not matching_providers:
            raise HTTPException(status_code=503, detail="No available providers at the moment")

    # 检查是否启用轮询
    if scheduling_algorithm == "random":
        matching_providers = random.sample(matching_providers, num_matching_providers)

    # 使用渠道级别的 preferences.weight 进行排序
    # 权重高的渠道排在前面（降序排列）
    def get_provider_weight(provider):
        return provider.get('preferences', {}).get('weight', 0) or 0

    def get_virtual_priority(provider: Dict[str, Any]) -> int:
        """读取虚拟路由优先级；普通渠道默认归入 0 组。"""
        # 修改原因：虚拟模型 chain 节点现在代表 fallback 优先级组。
        # 修改方式：从临时 provider 的 _virtual_priority 读取整数，异常值回退为 0。
        # 目的：排序阶段可以稳定地先排列高优先级节点，再排列后续 fallback 节点。
        try:
            return int(provider.get("_virtual_priority", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def apply_weight_order(provider_group: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """对一个优先级组应用原有权重排序和权重展开逻辑。"""
        # 修改原因：虚拟链条的组间顺序不能被全局权重排序打散。
        # 修改方式：把原来的排序和 weighted_round_robin/lottery 逻辑封装为组内操作。
        # 目的：普通路由保持原行为，虚拟路由只在同一 _virtual_priority 内按权重调度。
        ordered_group = list(provider_group)
        # 核心修复：显式按权重降序排列原始列表
        # 1. 确保在 fixed_priority 模式下权重高的优先
        # 2. 确保在 weighted_round_robin 初始比例相等时权重高的优先（消除 YAML 位置影响）
        ordered_group.sort(key=get_provider_weight, reverse=True)

        # 检查是否有任何渠道配置了权重
        has_channel_weights = any(get_provider_weight(p) > 0 for p in ordered_group)
        if not has_channel_weights:
            return ordered_group

        # 当有渠道权重时，如果是默认调度算法（fixed_priority），自动切换到加权轮询
        effective_algorithm = scheduling_algorithm
        if scheduling_algorithm == "fixed_priority":
            effective_algorithm = "weighted_round_robin"

        if effective_algorithm == "weighted_round_robin":
            # 构建权重字典
            channel_weights = {}
            for provider in ordered_group:
                weight = get_provider_weight(provider)
                if weight > 0:
                    channel_weights[provider['provider']] = weight

            if channel_weights:
                weighted_provider_name_list = weighted_round_robin(channel_weights)
                new_matching_providers = []
                for provider_name in weighted_provider_name_list:
                    for provider in ordered_group:
                        if provider['provider'] == provider_name:
                            new_matching_providers.append(provider)
                # 将没有权重的渠道追加到末尾
                for provider in ordered_group:
                    if provider['provider'] not in channel_weights:
                        new_matching_providers.append(provider)
                ordered_group = new_matching_providers
        elif effective_algorithm == "lottery":
            # 构建权重字典
            channel_weights = {}
            for provider in ordered_group:
                weight = get_provider_weight(provider)
                if weight > 0:
                    channel_weights[provider['provider']] = weight

            if channel_weights:
                weighted_provider_name_list = lottery_scheduling(channel_weights)
                new_matching_providers = []
                for provider_name in weighted_provider_name_list:
                    for provider in ordered_group:
                        if provider['provider'] == provider_name:
                            new_matching_providers.append(provider)
                # 将没有权重的渠道追加到末尾
                for provider in ordered_group:
                    if provider['provider'] not in channel_weights:
                        new_matching_providers.append(provider)
                ordered_group = new_matching_providers
        # effective_algorithm 不会是 fixed_priority（因为上面已经转换为 weighted_round_robin）
        # 这里不需要 else 分支，所有有权重的情况都会走到上面两个分支
        return ordered_group

    has_virtual_priorities = any(
        provider.get("_virtual_route_provider") and "_virtual_priority" in provider
        for provider in matching_providers
    )
    if has_virtual_priorities:
        # 修改原因：虚拟模型 chain 的 fallback 语义要求后续节点不能被高权重提前。
        # 修改方式：先按 _virtual_priority 拆组，每组内部复用原有权重调度，再按 priority 升序合并。
        # 目的：handler 后续重试时会自然先耗尽当前优先级组，再降级到下一组。
        priority_groups: Dict[int, List[Dict[str, Any]]] = {}
        for provider in matching_providers:
            priority_groups.setdefault(get_virtual_priority(provider), []).append(provider)
        matching_providers = []
        for priority in sorted(priority_groups.keys()):
            matching_providers.extend(apply_weight_order(priority_groups[priority]))
    else:
        matching_providers = apply_weight_order(matching_providers)

    if is_debug:
        import json
        for provider in matching_providers:
            logger.info(
                "available provider: %s",
                json.dumps(provider, indent=4, ensure_ascii=False, default=circular_list_encoder)
            )

    return matching_providers