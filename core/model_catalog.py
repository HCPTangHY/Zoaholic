"""模型列表构造工具。"""


# 迁移说明：
# 修改原因：该模块承载业务逻辑，不应继续放在 utils_pkg 这种通用工具包中。
# 修改方式：按照 Scout 的归位方案迁移到 core 对应业务模块，并只调整必要的内部导入路径。
# 目的：让业务代码按领域归属维护，同时保留根 utils.py 和 utils_pkg shim 的旧导入兼容性。
from core.utils import get_model_dict, is_local_api_key, safe_get


def _append_model_info_if_missing(all_models, unique_models, model_id):
    """向 /v1/models 返回值追加一个模型条目。"""
    # 修改原因：真实模型和虚拟模型都需要构造同一种 OpenAI 兼容模型对象。
    # 修改方式：集中去重并生成固定结构的 model_info。
    # 目的：避免新增虚拟模型暴露逻辑时重复拼装字段导致行为不一致。
    if not model_id or model_id in unique_models:
        return
    unique_models.add(model_id)
    all_models.append({
        "id": model_id,
        "object": "model",
        "created": 1720524448858,
        "owned_by": "Zoaholic",
    })


def _append_authorized_virtual_models(all_models, unique_models, config, api_index):
    """按当前 API Key 的 model 授权追加启用的虚拟模型。"""
    # 修改原因：虚拟模型名需要出现在 /v1/models 中，但只能展示当前 API Key 有权限访问的条目。
    # 修改方式：读取 preferences.virtual_models，保留 enabled 不为 false 且被 model 数组或 all 授权的虚拟名。
    # 目的：让客户端可以发现可用虚拟模型，同时不引入新的授权机制。
    virtual_models = safe_get(config, 'preferences', 'virtual_models', default={}) or {}
    if not isinstance(virtual_models, dict):
        return

    model_rules = safe_get(config, 'api_keys', api_index, 'model', default=[]) or []
    normalized_rules = []
    for rule in model_rules:
        if isinstance(rule, dict) and rule:
            rule = next(iter(rule.keys()))
        if isinstance(rule, str):
            normalized_rules.append(rule)

    allow_all = "all" in normalized_rules
    allowed_names = set(normalized_rules)

    # 当前 API Key 允许的分组
    api_key_groups = safe_get(config, 'api_keys', api_index, 'groups', default=['default'])
    if isinstance(api_key_groups, str):
        api_key_groups = [api_key_groups]
    if not isinstance(api_key_groups, list) or not api_key_groups:
        api_key_groups = ['default']
    allowed_groups = set(api_key_groups)

    # 构建 provider_name → groups 映射，供虚拟模型 group 检查
    provider_groups_map = {}
    for p in config.get("providers", []):
        pname = p.get("provider", "")
        if not pname:
            continue
        pg = p.get("groups") or ["default"]
        if isinstance(pg, str):
            pg = [pg] if pg else ["default"]
        if not isinstance(pg, list) or not pg:
            pg = ["default"]
        provider_groups_map[pname] = set(pg)

    for virtual_name, virtual_config in virtual_models.items():
        if not isinstance(virtual_config, dict):
            continue
        enabled_value = virtual_config.get("enabled", True)
        if isinstance(enabled_value, str):
            enabled_value = enabled_value.strip().lower() not in {"false", "0", "no", "off"}
        if enabled_value is False:
            continue
        virtual_name = str(virtual_name).strip()
        if not (allow_all or virtual_name in allowed_names):
            continue
        # 检查虚拟模型是否对当前 key 可达：
        # 1. chain 中 channel 节点的 provider group 与 key 有交集
        # 2. chain 中 model 节点引用的模型所在 provider group 与 key 有交集
        # 3. fallthrough：虚拟模型名在常规渠道中存在且 group 匹配
        chain = virtual_config.get("chain") or virtual_config.get("targets") or []
        if chain:
            has_accessible_provider = False
            for target in chain:
                if not isinstance(target, dict):
                    continue
                node_type = target.get("type", "model")
                if node_type == "channel":
                    target_provider = target.get("value", "")
                    target_groups = provider_groups_map.get(target_provider)
                    if target_groups and allowed_groups.intersection(target_groups):
                        has_accessible_provider = True
                        break
                elif node_type == "model":
                    model_value = target.get("value", "")
                    if model_value:
                        for p in config.get("providers", []):
                            pname = p.get("provider", "")
                            p_model_dict = p.get("_model_dict_cache") or {}
                            if model_value in p_model_dict:
                                p_grps = provider_groups_map.get(pname)
                                if p_grps and allowed_groups.intersection(p_grps):
                                    has_accessible_provider = True
                                    break
                    if has_accessible_provider:
                        break
            # fallthrough：虚拟路由 chain 全部被分组过滤后，运行时回退到常规路由。
            # 如果虚拟模型名在常规渠道中存在且分组匹配，模型仍然可达。
            if not has_accessible_provider:
                for p in config.get("providers", []):
                    pname = p.get("provider", "")
                    p_model_dict = p.get("_model_dict_cache") or {}
                    if virtual_name in p_model_dict:
                        p_grps = provider_groups_map.get(pname)
                        if p_grps and allowed_groups.intersection(p_grps):
                            has_accessible_provider = True
                            break
            if not has_accessible_provider:
                continue
        _append_model_info_if_missing(all_models, unique_models, virtual_name)


def post_all_models(api_index, config, api_list, models_list):
    all_models = []
    unique_models = set()

    # 允许分组集合：仅返回与当前 API Key 分组有交集的渠道模型
    api_key_groups = safe_get(config, 'api_keys', api_index, 'groups', default=['default'])
    if isinstance(api_key_groups, str):
        api_key_groups = [api_key_groups]
    if not isinstance(api_key_groups, list) or not api_key_groups:
        api_key_groups = ['default']
    allowed_groups = set(api_key_groups)
    
    if config['api_keys'][api_index]['model']:
        for model in config['api_keys'][api_index]['model']:
            if model == "all":
                # 如果模型名为 all，则返回所有模型并去重，按分组过滤
                all_models = get_all_models(config, allowed_groups)
                unique_models = {item["id"] for item in all_models}
                _append_authorized_virtual_models(all_models, unique_models, config, api_index)
                all_models.sort(key=lambda x: x["id"])
                return all_models
            if "/" in model:
                provider = model.split("/")[0]
                model = model.split("/")[1]
                if model == "*":
                    if is_local_api_key(provider) and provider in api_list:
                        # 分组过滤：仅当本地聚合器 Key 与当前请求 Key 分组有交集时才包含
                        try:
                            local_index = api_list.index(provider)
                            p_groups = safe_get(config, 'api_keys', local_index, 'groups', default=['default'])
                        except ValueError:
                            p_groups = ['default']
                        if isinstance(p_groups, str):
                            p_groups = [p_groups] if p_groups else ['default']
                        if not isinstance(p_groups, list) or not p_groups:
                            p_groups = ['default']
                        if allowed_groups.intersection(set(p_groups)):
                            for model_item in models_list[provider]:
                                if model_item not in unique_models:
                                    unique_models.add(model_item)
                                    model_info = {
                                        "id": model_item,
                                        "object": "model",
                                        "created": 1720524448858,
                                        "owned_by": "Zoaholic"
                                    }
                                    all_models.append(model_info)
                    else:
                        for provider_item in config["providers"]:
                            if provider_item['provider'] != provider:
                                continue
                            # 跳过禁用的渠道
                            if provider_item.get("enabled") is False:
                                continue
                            # 分组过滤：provider 必须与当前 Key 分组有交集
                            p_groups = provider_item.get("groups") or ["default"]
                            if isinstance(p_groups, str):
                                p_groups = [p_groups] if p_groups else ["default"]
                            if not isinstance(p_groups, list) or not p_groups:
                                p_groups = ["default"]
                            if not allowed_groups.intersection(set(p_groups)):
                                continue

                            model_dict = get_model_dict(provider_item)
                            # 识别被重定向的上游原名（出现在映射值中且与键不同的项）
                            upstream_candidates = {v for k, v in model_dict.items() if v != k}
                            # 如果渠道配置了 model_prefix，只展示带前缀的模型名
                            prefix = provider_item.get('model_prefix', '').strip()
                            for model_item in model_dict.keys():
                                # 跳过通配符标记，"*" 不是真实模型名
                                if model_item == "*":
                                    continue
                                # 过滤掉作为别名映射上游的模型名
                                # 比较时去掉 prefix，因为 upstream_candidates 存的是不带 prefix 的上游名
                                bare_name = model_item[len(prefix):] if prefix and model_item.startswith(prefix) else model_item
                                # 修改原因：有 model_prefix 时，get_model_dict 会生成 prefix+model -> model，导致所有模型都像别名映射。
                                # 修改方式：只在没有 prefix 时按 bare_name 过滤纯重定向上游原名。
                                # 目的：保留无前缀别名隐藏规则，同时让带前缀的对外模型名正常出现在列表中。
                                if not prefix and bare_name in upstream_candidates:
                                    continue
                                # 如果有前缀，只返回带前缀的模型名
                                if prefix and not model_item.startswith(prefix):
                                    continue
                                if model_item not in unique_models:
                                    unique_models.add(model_item)
                                    model_info = {
                                        "id": model_item,
                                        "object": "model",
                                        "created": 1720524448858,
                                        "owned_by": "Zoaholic"
                                    }
                                    all_models.append(model_info)
                else:
                    if is_local_api_key(provider) and provider in api_list:
                        # 分组过滤：仅当本地聚合器 Key 与当前请求 Key 分组有交集时才包含
                        try:
                            local_index = api_list.index(provider)
                            p_groups = safe_get(config, 'api_keys', local_index, 'groups', default=['default'])
                        except ValueError:
                            p_groups = ['default']
                        if isinstance(p_groups, str):
                            p_groups = [p_groups] if p_groups else ['default']
                        if not isinstance(p_groups, list) or not p_groups:
                            p_groups = ['default']

                        if allowed_groups.intersection(set(p_groups)):
                            # 直接使用配置的模型名，不做归一化
                            if model in models_list[provider]:
                                if model not in unique_models:
                                    unique_models.add(model)
                                    model_info = {
                                        "id": model,
                                        "object": "model",
                                        "created": 1720524448858,
                                        "owned_by": "Zoaholic"
                                    }
                                    all_models.append(model_info)
                    else:
                        for provider_item in config["providers"]:
                            if provider_item['provider'] != provider:
                                continue
                            # 跳过禁用的渠道
                            if provider_item.get("enabled") is False:
                                continue
                            # 分组过滤：provider 必须与当前 Key 分组有交集
                            p_groups = provider_item.get("groups") or ["default"]
                            if isinstance(p_groups, str):
                                p_groups = [p_groups] if p_groups else ["default"]
                            if not isinstance(p_groups, list) or not p_groups:
                                p_groups = ["default"]
                            if not allowed_groups.intersection(set(p_groups)):
                                continue

                            model_dict = get_model_dict(provider_item)
                            # 识别被重定向的上游原名（出现在映射值中且与键不同的项）
                            upstream_candidates = {v for k, v in model_dict.items() if v != k}
                            # 如果渠道配置了 model_prefix，只展示带前缀的模型名
                            prefix = provider_item.get('model_prefix', '').strip()
                            for model_item in model_dict.keys():
                                # 跳过通配符标记，"*" 不是真实模型名
                                if model_item == "*":
                                    continue
                                # 过滤掉作为别名映射上游的模型名
                                # 比较时去掉 prefix，因为 upstream_candidates 存的是不带 prefix 的上游名
                                bare_name = model_item[len(prefix):] if prefix and model_item.startswith(prefix) else model_item
                                # 修改原因：有 model_prefix 时，get_model_dict 会生成 prefix+model -> model，导致所有模型都像别名映射。
                                # 修改方式：只在没有 prefix 时按 bare_name 过滤纯重定向上游原名。
                                # 目的：保留无前缀别名隐藏规则，同时让带前缀的对外模型名正常出现在列表中。
                                if not prefix and bare_name in upstream_candidates:
                                    continue
                                # 如果有前缀，只返回带前缀的模型名
                                if prefix and not model_item.startswith(prefix):
                                    continue
                                if model_item not in unique_models and model_item == model:
                                    unique_models.add(model_item)
                                    model_info = {
                                        "id": model_item,
                                        "object": "model",
                                        "created": 1720524448858,
                                        "owned_by": "Zoaholic"
                                    }
                                    all_models.append(model_info)
                continue

            if is_local_api_key(model) and model in api_list:
                continue

            virtual_models_cfg = safe_get(config, 'preferences', 'virtual_models', default={}) or {}
            if isinstance(virtual_models_cfg, dict) and model in virtual_models_cfg:
                # 修改原因：虚拟模型是否展示取决于 virtual_models.enabled 和 API Key 授权，不能被普通模型兜底逻辑提前加入。
                # 修改方式：遇到已配置的虚拟模型名时跳过普通追加，统一交给 _append_authorized_virtual_models 处理。
                # 目的：避免 disabled 的虚拟模型仍然出现在 /v1/models 中。
                continue

            # 直接使用配置的模型名，不做归一化
            _append_model_info_if_missing(all_models, unique_models, model)

    _append_authorized_virtual_models(all_models, unique_models, config, api_index)

    # 按模型 ID 进行 Unicode 排序
    all_models.sort(key=lambda x: x["id"])
    return all_models


def get_all_models(config, allowed_groups=None):
    """
    获取所有模型列表。
    
    逻辑：
    1. 遍历所有可用渠道
    2. 对每个渠道，读取 model_dict
    3. 过滤掉作为别名映射上游的模型名（只保留别名）
    4. 遍历全部渠道后，去重
    """
    all_models = []
    unique_models = set()
    
    for provider in config["providers"]:
        # 跳过禁用的渠道
        if provider.get("enabled") is False:
            continue
            
        # 分组过滤：如果提供了允许分组集合，需存在交集
        if allowed_groups is not None:
            p_groups = provider.get("groups") or ["default"]
            if isinstance(p_groups, str):
                p_groups = [p_groups] if p_groups else ["default"]
            if not isinstance(p_groups, list) or not p_groups:
                p_groups = ["default"]
            if not allowed_groups.intersection(set(p_groups)):
                continue

        # 使用映射缓存（若不存在则回退到实时计算）
        model_dict = provider.get("_model_dict_cache") or get_model_dict(provider)
        
        # 识别被重定向的上游原名（出现在映射值中且与键不同的项）
        # 这些上游模型名不应该出现在模型列表中，只展示别名
        upstream_candidates = {v for k, v in model_dict.items() if v != k}
        
        # 如果渠道配置了 model_prefix，只展示带前缀的模型名
        prefix = provider.get('model_prefix', '').strip()
        
        for model in model_dict.keys():
            # 跳过通配符标记，"*" 不是真实模型名
            if model == "*":
                continue
            # 过滤掉作为别名映射上游的模型名
            # 比较时去掉 prefix，因为 upstream_candidates 存的是不带 prefix 的上游名
            bare_name = model[len(prefix):] if prefix and model.startswith(prefix) else model
            # 修改原因：有 model_prefix 时，get_model_dict 会生成 prefix+model -> model，导致所有模型都像别名映射。
            # 修改方式：只在没有 prefix 时按 bare_name 过滤纯重定向上游原名。
            # 目的：保留无前缀别名隐藏规则，同时让带前缀的对外模型名正常出现在列表中。
            if not prefix and bare_name in upstream_candidates:
                continue
            # 如果有前缀，只返回带前缀的模型名，过滤掉不带前缀的原始模型名
            if prefix and not model.startswith(prefix):
                continue
            if model not in unique_models:
                unique_models.add(model)
                model_info = {
                    "id": model,
                    "object": "model",
                    "created": 1720524448858,
                    "owned_by": "Zoaholic"
                }
                all_models.append(model_info)
    
    # 按模型 ID 进行 Unicode 排序
    all_models.sort(key=lambda x: x["id"])
    return all_models
