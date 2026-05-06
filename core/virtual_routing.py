"""
全局虚拟模型路由。

虚拟模型只改变请求模型名到真实上游模型名的展开过程，不引入新的授权机制。
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Set, Tuple

from core.utils import get_model_dict


def _read_bool(value: Any, default: bool = False) -> bool:
    """读取兼容字符串形式的布尔配置。"""
    # 修改原因：virtual_models.enabled 与 pool_sharing 都可能来自 YAML、JSON 或前端表单。
    # 修改方式：统一兼容 true/false、1/0、yes/no 等字符串形式。
    # 目的：避免同一个配置值因来源不同而产生不同路由结果。
    if value is None:
        return default
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    return bool(value)


def _is_pool_sharing_enabled(provider: Dict[str, Any]) -> bool:
    """判断渠道是否开启共享路由池。"""
    # 修改原因：虚拟模型的 model 节点也需要复用普通路由中的 pool_sharing 语义。
    # 修改方式：只读取 provider.preferences.pool_sharing，并兼容字符串布尔值。
    # 目的：让全局模型名节点可以命中明确开启共享路由池的带前缀渠道。
    preferences = provider.get("preferences") or {}
    if not isinstance(preferences, dict):
        return False
    return _read_bool(preferences.get("pool_sharing"), default=False)


def _get_pool_sharing_model_name(
    provider: Dict[str, Any],
    request_model: str,
    model_dict: Dict[str, str],
) -> Optional[str]:
    """返回共享路由池对应的带前缀模型名。"""
    # 修改原因：虚拟模型不能直接导入 routing.py 的私有函数，否则容易形成循环依赖。
    # 修改方式：在本模块保留同等规则，只做 prefix + request_model 的显式匹配。
    # 目的：保持虚拟模型路由与普通模型路由对 pool_sharing 的行为一致。
    prefix = str(provider.get("model_prefix") or "").strip()
    if not prefix or not _is_pool_sharing_enabled(provider):
        return None
    if request_model.startswith(prefix):
        return None

    prefixed_model = f"{prefix}{request_model}"
    if prefixed_model in model_dict:
        return prefixed_model
    return None


def _get_provider_model_dict(provider: Dict[str, Any]) -> Dict[str, str]:
    """读取 provider 的模型映射缓存，不存在时回退到实时计算。"""
    # 修改原因：虚拟路由依赖运行时已经展开好的 _model_dict_cache。
    # 修改方式：优先使用缓存；缓存缺失时才调用 get_model_dict。
    # 目的：兼容测试构造和少量未经过 update_config 的临时渠道对象。
    cached = provider.get("_model_dict_cache")
    if isinstance(cached, dict):
        return {str(k): str(v) for k, v in cached.items()}
    return {str(k): str(v) for k, v in get_model_dict(provider).items()}


def _match_provider_model(
    provider: Dict[str, Any],
    model_name: str,
) -> Optional[Tuple[str, str]]:
    """在单个 provider 中查找外部模型名，并返回外部名和上游真实名。"""
    # 修改原因：model 节点和 channel 节点都需要先把用户配置的模型名解析成渠道上游真实模型。
    # 修改方式：先精确查 _model_dict_cache，再按 pool_sharing 查带前缀外部名。
    # 目的：虚拟模型最终注入的映射始终是 {virtual_name: upstream_model}。
    if not model_name:
        return None

    model_dict = _get_provider_model_dict(provider)
    if model_name in model_dict:
        return model_name, model_dict[model_name]

    pool_model = _get_pool_sharing_model_name(provider, model_name, model_dict)
    if pool_model and pool_model in model_dict:
        return pool_model, model_dict[pool_model]
    return None


def _build_virtual_provider(
    provider: Dict[str, Any],
    virtual_name: str,
    upstream_model: str,
    virtual_priority: int,
) -> Dict[str, Any]:
    """构造只用于本次请求的 provider 副本。"""
    # 修改原因：链条 fallback 需要把每个 chain 节点识别为独立优先级组。
    # 修改方式：深拷贝原渠道后，除注入虚拟名映射缓存外，再写入 _virtual_priority。
    # 目的：排序和尝试列表构造阶段可以先试完高优先级组，再降级到后续 chain 节点。
    new_provider = copy.deepcopy(provider)
    upstream_model = str(upstream_model)
    virtual_name = str(virtual_name)
    model_dict = _get_provider_model_dict(provider)
    model_dict[virtual_name] = upstream_model
    new_provider["model"] = [{upstream_model: virtual_name}]
    new_provider["_model_dict_cache"] = model_dict
    new_provider["_virtual_route_provider"] = True
    new_provider["_virtual_priority"] = int(virtual_priority)
    return new_provider


def _get_virtual_models(config: Dict[str, Any]) -> Dict[str, Any]:
    """读取全局虚拟模型表。"""
    preferences = config.get("preferences") or {}
    if not isinstance(preferences, dict):
        return {}
    virtual_models = preferences.get("virtual_models") or {}
    if not isinstance(virtual_models, dict):
        return {}
    return virtual_models


def resolve_virtual_model(
    virtual_name: str,
    config: Dict[str, Any],
    api_index: int,
    app: Any,
) -> Optional[List[Dict[str, Any]]]:
    """
    如果 virtual_name 命中虚拟模型表，返回展开后的 provider 列表。
    没命中返回 None，走正常路由。
    """
    # 修改原因：虚拟模型路由只负责候选渠道展开，不负责 API Key 授权和黑名单过滤。
    # 修改方式：按 preferences.virtual_models[virtual_name].chain 顺序解析节点并构造临时 provider。
    # 目的：让 get_matching_providers 可以在原有过滤和调度逻辑前插入虚拟候选池。
    _ = (api_index, app)
    virtual_models = _get_virtual_models(config)
    if virtual_name not in virtual_models:
        return None

    virtual_config = virtual_models.get(virtual_name) or {}
    if not isinstance(virtual_config, dict):
        return []
    if not _read_bool(virtual_config.get("enabled"), default=True):
        return []

    chain = virtual_config.get("chain") or []
    if not isinstance(chain, list):
        return []

    providers = config.get("providers") or []
    if not isinstance(providers, list):
        return []

    resolved: List[Dict[str, Any]] = []
    seen_providers: Set[str] = set()

    def append_match(provider: Dict[str, Any], upstream_model: str, chain_index: int) -> None:
        provider_name = str(provider.get("provider") or "")
        if not provider_name or provider_name in seen_providers:
            return
        seen_providers.add(provider_name)
        resolved.append(_build_virtual_provider(provider, virtual_name, upstream_model, chain_index))

    for chain_index, node in enumerate(chain):
        if not isinstance(node, dict):
            continue
        node_type = str(node.get("type") or "model").strip().lower()

        if node_type == "model":
            model_name = str(node.get("value") or "").strip()
            if not model_name:
                continue
            for provider in providers:
                if not isinstance(provider, dict) or provider.get("enabled") is False:
                    continue
                match = _match_provider_model(provider, model_name)
                if match:
                    _, upstream_model = match
                    append_match(provider, upstream_model, chain_index)

        elif node_type == "channel":
            provider_name = str(node.get("value") or "").strip()
            if not provider_name:
                continue
            for provider in providers:
                if not isinstance(provider, dict) or provider.get("enabled") is False:
                    continue
                if str(provider.get("provider") or "") != provider_name:
                    continue
                model_name = str(node.get("model") or virtual_name).strip()
                match = _match_provider_model(provider, model_name)
                if match:
                    _, upstream_model = match
                    append_match(provider, upstream_model, chain_index)
                break

    return resolved


def _collect_real_model_names(config: Dict[str, Any]) -> Set[str]:
    """收集所有真实渠道对外暴露的模型名。"""
    # 修改原因：虚拟模型名与真实模型名冲突时，请求入口会出现两种解释。
    # 修改方式：基于 provider 的 _model_dict_cache 或实时计算结果收集所有对外模型名。
    # 目的：在启动或保存配置时提前阻止含义不确定的虚拟模型配置。
    names: Set[str] = set()
    providers = config.get("providers") or []
    if not isinstance(providers, list):
        return names

    for provider in providers:
        if not isinstance(provider, dict):
            continue
        model_dict = _get_provider_model_dict(provider)
        for model_name in model_dict.keys():
            model_name = str(model_name).strip()
            if model_name and model_name != "*":
                names.add(model_name)
    return names


def validate_virtual_models_config(config: Dict[str, Any]) -> None:
    """校验虚拟模型配置，供启动和保存配置时调用。"""
    # 修改原因：虚拟模型名现在允许覆盖同名真实模型，请求入口由 get_matching_providers 的虚拟优先逻辑消除歧义。
    # 修改方式：不再收集真实模型名，也不再检查虚拟名与真实模型名冲突，仅禁止链条引用另一个虚拟模型。
    # 目的：让 deepseek-chat 等真实模型名可以被虚拟链条接管，同时继续避免递归虚拟路由。
    preferences = config.get("preferences") or {}
    if not isinstance(preferences, dict):
        return

    virtual_models = preferences.get("virtual_models") or {}
    if not virtual_models:
        return
    if not isinstance(virtual_models, dict):
        raise ValueError("preferences.virtual_models must be a mapping")

    virtual_names = {str(name).strip() for name in virtual_models.keys() if str(name).strip()}

    for virtual_name, virtual_config in virtual_models.items():
        name = str(virtual_name).strip()
        if not name:
            raise ValueError("virtual model name cannot be empty")
        if not isinstance(virtual_config, dict):
            raise ValueError(f"virtual model '{name}' must be a mapping")

        chain = virtual_config.get("chain") or []
        if not isinstance(chain, list):
            raise ValueError(f"virtual model '{name}' chain must be a list")

        for node in chain:
            if not isinstance(node, dict):
                continue
            node_type = str(node.get("type") or "model").strip().lower()
            node_value = str(node.get("value") or "").strip()
            node_model = str(node.get("model") or "").strip()
            # 自引用不算嵌套：同名虚拟模型 chain 里引用自己的名字，实际是引用真实模型
            if node_type == "model" and node_value in virtual_names and node_value != name:
                raise ValueError(f"virtual model '{name}' contains nested virtual model '{node_value}'")
            if node_type == "channel" and node_model and node_model in virtual_names and node_model != name:
                raise ValueError(f"virtual model '{name}' contains nested virtual model '{node_model}'")
