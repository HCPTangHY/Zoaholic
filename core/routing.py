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
                # 过滤掉被重定向的上游原名
                if model in upstream_candidates:
                    continue
                # 如果有前缀，只返回带前缀的模型名
                if prefix and not model.startswith(prefix):
                    continue
                provider_rules.append(provider["provider"] + "/" + model)

    elif "/" in model_rule:
        if model_rule.startswith("<") and model_rule.endswith(">"):
            model_rule = model_rule[1:-1]
            # 处理带斜杠的模型名
            for provider in config['providers']:
                model_dict = provider["_model_dict_cache"]
                if model_rule in model_dict.keys():
                    provider_rules.append(provider['provider'] + "/" + model_rule)
        else:
            provider_name = model_rule.split("/")[0]
            model_name_split = "/".join(model_rule.split("/")[1:])
            models_list = []

            # api_keys 中 api 为 sk- 时，表示继承 api_keys，将 api_keys 中的 api key 当作 渠道
            if provider_name.startswith("sk-") and provider_name in app.state.api_list:
                if app.state.models_list.get(provider_name):
                    models_list = app.state.models_list[provider_name]
                else:
                    models_list = []
            else:
                for provider in config['providers']:
                    model_dict = provider["_model_dict_cache"]
                    if provider['provider'] == provider_name:
                        models_list.extend(list(model_dict.keys()))

            # api_keys 中 model 为 provider_name/* 时，表示所有模型都匹配
            if model_name_split == "*":
                if request_model in models_list:
                    provider_rules.append(provider_name + "/" + request_model)

                # 如果请求模型名： gpt-4* ，则匹配所有以模型名开头且不以 * 结尾的模型
                for models_list_model in models_list:
                    if request_model.endswith("*") and models_list_model.startswith(request_model.rstrip("*")):
                        provider_rules.append(provider_name + "/" + models_list_model)

            # api_keys 中 model 为 provider_name/model_name 时，表示模型名完全匹配
            elif model_name_split == request_model \
            or (request_model.endswith("*") and model_name_split.startswith(request_model.rstrip("*"))):
                # api_keys 中 model 为 provider_name/model_name 时，请求模型名： model_name*
                if model_name_split in models_list:
                    provider_rules.append(provider_name + "/" + model_name_split)

    else:
        for provider in config["providers"]:
            model_dict = provider["_model_dict_cache"]
            if model_rule in model_dict.keys():
                provider_rules.append(provider["provider"] + "/" + model_rule)

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
        if provider_name.startswith("sk-") and provider_name in app.state.api_list:
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
                if "/" in item and provider['provider'] == provider_name and model_name_split in model_dict.keys():
                    if request_model in model_dict.keys() and model_name_split == request_model:
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
    provider_rules = []

    for model_rule in config['api_keys'][api_index]['model']:
        provider_rules.extend(await get_provider_rules(model_rule, config, request_model, app))
    
    provider_list = get_provider_list(provider_rules, config, request_model, app)

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
            if provider_name.startswith("sk-") and provider_name in app.state.api_list:
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
    
    # 核心修复：显式按权重降序排列原始列表
    # 1. 确保在 fixed_priority 模式下权重高的优先
    # 2. 确保在 weighted_round_robin 初始比例相等时权重高的优先（消除 YAML 位置影响）
    matching_providers.sort(key=get_provider_weight, reverse=True)
    
    # 检查是否有任何渠道配置了权重
    has_channel_weights = any(get_provider_weight(p) > 0 for p in matching_providers)
    
    if has_channel_weights:
        # 当有渠道权重时，如果是默认调度算法（fixed_priority），自动切换到加权轮询
        effective_algorithm = scheduling_algorithm
        if scheduling_algorithm == "fixed_priority":
            effective_algorithm = "weighted_round_robin"
        
        if effective_algorithm == "weighted_round_robin":
            # 构建权重字典
            channel_weights = {}
            for provider in matching_providers:
                weight = get_provider_weight(provider)
                if weight > 0:
                    channel_weights[provider['provider']] = weight
            
            if channel_weights:
                weighted_provider_name_list = weighted_round_robin(channel_weights)
                new_matching_providers = []
                for provider_name in weighted_provider_name_list:
                    for provider in matching_providers:
                        if provider['provider'] == provider_name:
                            new_matching_providers.append(provider)
                # 将没有权重的渠道追加到末尾
                for provider in matching_providers:
                    if provider['provider'] not in channel_weights:
                        new_matching_providers.append(provider)
                matching_providers = new_matching_providers
        elif effective_algorithm == "lottery":
            # 构建权重字典
            channel_weights = {}
            for provider in matching_providers:
                weight = get_provider_weight(provider)
                if weight > 0:
                    channel_weights[provider['provider']] = weight
            
            if channel_weights:
                weighted_provider_name_list = lottery_scheduling(channel_weights)
                new_matching_providers = []
                for provider_name in weighted_provider_name_list:
                    for provider in matching_providers:
                        if provider['provider'] == provider_name:
                            new_matching_providers.append(provider)
                # 将没有权重的渠道追加到末尾
                for provider in matching_providers:
                    if provider['provider'] not in channel_weights:
                        new_matching_providers.append(provider)
                matching_providers = new_matching_providers
        # effective_algorithm 不会是 fixed_priority（因为上面已经转换为 weighted_round_robin）
        # 这里不需要 else 分支，所有有权重的情况都会走到上面两个分支

    if is_debug:
        import json
        for provider in matching_providers:
            logger.info(
                "available provider: %s",
                json.dumps(provider, indent=4, ensure_ascii=False, default=circular_list_encoder)
            )

    return matching_providers