"""运行时配置规范化工具。"""


# 迁移说明：
# 修改原因：该模块承载业务逻辑，不应继续放在 utils_pkg 这种通用工具包中。
# 修改方式：按照 Scout 的归位方案迁移到 core 对应业务模块，并只调整必要的内部导入路径。
# 目的：让业务代码按领域归属维护，同时保留根 utils.py 和 utils_pkg shim 的旧导入兼容性。
from datetime import datetime, timezone

from core.byok import is_byok_provider
from core.ip_blacklist import normalize_ip_blacklist
from core.log_config import logger
from core.utils import ThreadSafeCircularList, get_model_dict, provider_api_circular_list, safe_get

from .file_store import save_api_yaml
from .db_store import save_config_to_db


_PROVIDER_STRIP_FIELDS = (
    "provider", "base_url", "engine", "model_prefix",
    "project_id", "client_email", "private_key",
    "cf_account_id", "aws_access_key", "aws_secret_key",
)


def _strip_provider_fields(provider: dict) -> None:
    """去除 provider 配置中字符串字段的首尾空格，防止多余空白导致请求异常。"""
    for field in _PROVIDER_STRIP_FIELDS:
        val = provider.get(field)
        if isinstance(val, str):
            provider[field] = val.strip()

    # 去除模型名称的首尾空格
    models = provider.get("model")
    if isinstance(models, list):
        stripped = []
        for m in models:
            if isinstance(m, str):
                stripped.append(m.strip())
            elif isinstance(m, dict):
                stripped.append(
                    {str(k).strip(): str(v).strip() for k, v in m.items()}
                )
            else:
                stripped.append(m)
        provider["model"] = stripped

    # 去除 API key 的首尾空格
    api_val = provider.get("api")
    if isinstance(api_val, str):
        provider["api"] = api_val.strip()
    elif isinstance(api_val, list):
        provider["api"] = [
            str(k).strip() if isinstance(k, (str, int)) else k
            for k in api_val
        ]


def _expand_sub_channels(providers: list) -> list:
    """展开子渠道：将 sub_channels 配置展开为独立的内部 provider。

    子渠道继承主渠道的 api/base_url/preferences 等配置，
    自己的配置项覆盖继承的值。展开后的 provider 在路由层表现为独立渠道。

    子渠道 provider 名格式：{主渠道名}:{子渠道engine}
    """
    expanded = []
    for provider in providers:
        # 主渠道本身始终保留（即使有 sub_channels）
        expanded.append(provider)

        sub_channels = provider.get("sub_channels")
        if not isinstance(sub_channels, list) or not sub_channels:
            continue

        # 主渠道可继承的字段（子渠道没配的就继承）
        parent_api = provider.get("api")
        parent_base_url = provider.get("base_url", "")
        parent_preferences = provider.get("preferences") or {}
        parent_groups = provider.get("groups") or ["default"]
        parent_enabled = provider.get("enabled", True)
        parent_name = provider.get("provider", "")

        seen_names = set()
        for sub_idx, sub in enumerate(sub_channels):
            if not isinstance(sub, dict):
                continue
            sub_engine = sub.get("engine")
            if not sub_engine:
                continue

            # 子渠道 provider 名：主渠道名:子引擎名（重复时加序号）
            base_name = sub.get("provider") or f"{parent_name}:{sub_engine}"
            sub_name = base_name
            if sub_name in seen_names:
                sub_name = f"{base_name}:{sub_idx}"
            seen_names.add(sub_name)

            # 深合并 preferences：主渠道为底，子渠道覆盖
            merged_prefs = {**parent_preferences}
            sub_prefs = sub.get("preferences")
            if isinstance(sub_prefs, dict):
                merged_prefs.update(sub_prefs)

            sub_provider = {
                "provider": sub_name,
                "engine": sub_engine,
                "api": sub.get("api") or parent_api,
                "base_url": sub.get("base_url") or parent_base_url,
                "model": sub.get("model") or [],
                "preferences": merged_prefs,
                "groups": sub.get("groups") or parent_groups,
                "enabled": sub.get("enabled") if sub.get("enabled") is not None else parent_enabled,
                "remark": sub.get("remark") or f"[子渠道] {parent_name} → {sub_engine}",
                # 标记为子渠道（前端/API 可用来识别）
                "_parent_provider": parent_name,
                "_is_sub_channel": True,
            }

            # 继承其他可选字段
            if sub.get("model_prefix"):
                sub_provider["model_prefix"] = sub["model_prefix"]
            elif provider.get("model_prefix"):
                sub_provider["model_prefix"] = provider["model_prefix"]

            expanded.append(sub_provider)

    return expanded


async def update_config(config_data, use_config_url=False, skip_model_fetch=False, save_to_file=True, save_to_db: bool = False, changed_providers=None):
    # 修改原因：IP 黑名单支持精确 IP 和 CIDR，必须在启动和热更新时统一规范并校验。
    # 修改方式：顶层 ip_blacklist 规范为字符串数组，非法条目直接抛出 ValueError 阻止保存。
    # 目的：避免请求期才发现格式错误，并为运行时预解析缓存提供稳定输入。
    config_data['ip_blacklist'] = normalize_ip_blacklist(config_data.get('ip_blacklist'))

    # 修改原因：/v1/api_config/update 可以只保存 preferences，此时传入的是已经包含运行时子渠道的 app.state.config。
    # 修改方式：展开子渠道前先移除 _is_sub_channel 运行时 provider，再从主渠道重新展开。
    # 目的：避免多次保存全局设置后，子渠道在运行时 providers 中重复累积。
    base_providers = [
        p for p in (config_data.get('providers') or [])
        if not (isinstance(p, dict) and p.get('_is_sub_channel'))
    ]
    # 展开子渠道为独立 provider（路由层无感知）
    config_data['providers'] = _expand_sub_channels(base_providers)

    for index, provider in enumerate(config_data['providers']):
        _strip_provider_fields(provider)

        # 修改原因：用户已填写的 base_url 不应被 project_id 或 cf_account_id 的默认值覆盖。
        # 修改方式：只有 base_url 为空时才补对应服务的默认地址。
        # 目的：保留用户自定义网关或代理地址。
        if provider.get('project_id') and not provider.get('base_url'):
            provider['base_url'] = 'https://aiplatform.googleapis.com/'
        if provider.get('cf_account_id') and not provider.get('base_url'):
            provider['base_url'] = 'https://api.cloudflare.com/'

        if isinstance(provider['provider'], int):
            provider['provider'] = str(provider['provider'])

        provider_api = provider.get('api', None)
        provider_is_byok = is_byok_provider(provider)
        # 修改原因：子渠道共享父渠道 key pool 后会把 provider_api 置空，后续清理逻辑会误认为该渠道没有本地 key。
        # 修改方式：在每个 provider 循环迭代开始时初始化共享标志，并在共享成功后设置为 True。
        # 目的：保留共享的父渠道 key pool，同时继续执行后续运行时规范化逻辑。
        shared_parent_key_pool = False
        if provider_is_byok:
            # 修改原因：BYOK provider 现在用 api: ["*"] 显式标记，"*" 不是可轮换的上游 key。
            # 修改方式：构建 key pool 前移除该 provider 的旧 circular list，并把 provider_api 置空跳过后续创建逻辑。
            # 目的：防止 "*" 进入 ThreadSafeCircularList，也避免热更新后沿用旧本地 key 池。
            provider_api_circular_list.pop(provider['provider'], None)
            provider_api = None

        if provider_api:
            if isinstance(provider_api, int):
                provider_api = str(provider_api)

            # 子渠道共享主渠道的 key circular list（保证 round_robin 和禁用状态一致）
            parent_name = provider.get('_parent_provider')
            # 修改原因：provider_api_circular_list 已改为普通 dict，读取父渠道时不能再依赖 [] 自动创建。
            # 修改方式：用 get 取得父渠道现有循环列表，只有存在时才让子渠道共享同一实例。
            # 目的：保持子渠道共享 key 状态的行为，同时避免配置顺序或名称错误造成空 key 池泄漏。
            parent_circular_list = provider_api_circular_list.get(parent_name) if parent_name else None
            if parent_circular_list:
                provider_api_circular_list[provider['provider']] = parent_circular_list
                # 修改原因：共享父渠道 key pool 后 provider_api 会被置空，必须标记该置空不是无 key 状态。
                # 修改方式：记录 shared_parent_key_pool=True，再跳过后面的 circular list 创建。
                # 目的：防止清理分支把刚共享的父渠道 key pool 从子渠道映射中移除。
                shared_parent_key_pool = True
                provider_api = None

        if (
            not provider_is_byok
            and not provider_api
            and not shared_parent_key_pool
            and (changed_providers is None or provider['provider'] in changed_providers)
        ):
            # 修改原因：热更新时若渠道从静态 key 改成无本地 api，旧 provider_api_circular_list 会残留。
            # 修改方式：在本次需要重建的非 BYOK provider 没有本地 api 时，主动移除已有 key 池。
            # 目的：确保无本地 key 的 provider 不会继续使用修改前的本地上游 key，也不会被自动禁用逻辑误处理。
            provider_api_circular_list.pop(provider['provider'], None)

        # 修改原因：子渠道已经共享父渠道 key pool 时，不应再为该子渠道创建独立 key pool。
        # 修改方式：创建分支增加 shared_parent_key_pool 排除条件。
        # 目的：保持子渠道与父渠道使用同一个 ThreadSafeCircularList 实例。
        if provider_api and not shared_parent_key_pool and (changed_providers is None or provider['provider'] in changed_providers):
            # 解析 API key 列表，支持 ! 前缀标记禁用的 key
            # 格式：正常 key 直接使用，以 ! 开头的 key 表示禁用
            def parse_api_keys(api_list):
                """解析 API key 列表，返回 (items, disabled_keys, labels)
                
                支持三种元素格式：
                - str: "sk-xxx" 或 "!sk-xxx"(禁用)
                - dict: {"sk-xxx": "label"} 或 {"!sk-xxx": "label"}(禁用+label)
                - int: 自动转 str
                """
                items = []
                disabled_keys = set()
                labels = {}
                for key in api_list:
                    # dict 格式: {"sk-xxx": "label"}
                    if isinstance(key, dict) and len(key) == 1:
                        raw_key, label = next(iter(key.items()))
                        key_str = str(raw_key).strip()
                        if key_str.startswith('!'):
                            clean_key = key_str[1:]
                            items.append(clean_key)
                            disabled_keys.add(clean_key)
                        else:
                            clean_key = key_str
                            items.append(clean_key)
                        if label and str(label).strip():
                            labels[clean_key] = str(label).strip()
                    else:
                        key_str = str(key).strip()
                        if key_str.startswith('!'):
                            clean_key = key_str[1:]
                            items.append(clean_key)
                            disabled_keys.add(clean_key)
                        else:
                            items.append(key_str)
                return items, disabled_keys, labels
            
            # 保存旧实例的自动禁用状态，用于热重载后恢复
            old_circular = provider_api_circular_list.get(provider['provider'])
            old_auto_disabled = {}
            old_auto_cooling = {}
            # 注意: 此处直接读旧实例的共享状态未加锁，但热重载窗口极短且为只读快照，风险可接受
            if old_circular and hasattr(old_circular, 'auto_disabled_info'):
                old_auto_disabled = dict(old_circular.auto_disabled_info)
                old_auto_cooling = {k: old_circular.cooling_until[k] for k in old_auto_disabled}

            if isinstance(provider_api, str):
                items, disabled_keys, labels = parse_api_keys([provider_api])
                if labels:
                    provider.setdefault('_api_labels', {}).update(labels)
                provider_api_circular_list[provider['provider']] = ThreadSafeCircularList(
                    items=items,
                    rate_limit=safe_get(provider, "preferences", "api_key_rate_limit", default={"default": "999999/min"}),
                    schedule_algorithm=safe_get(provider, "preferences", "api_key_schedule_algorithm", default="round_robin"),
                    provider_name=provider['provider'],
                    disabled_keys=disabled_keys
                )
            if isinstance(provider_api, list):
                items, disabled_keys, labels = parse_api_keys(provider_api)
                if labels:
                    provider.setdefault('_api_labels', {}).update(labels)
                provider_api_circular_list[provider['provider']] = ThreadSafeCircularList(
                    items=items,
                    rate_limit=safe_get(provider, "preferences", "api_key_rate_limit", default={"default": "999999/min"}),
                    schedule_algorithm=safe_get(provider, "preferences", "api_key_schedule_algorithm", default="round_robin"),
                    provider_name=provider['provider'],
                    disabled_keys=disabled_keys
                )

            # 恢复自动禁用状态（仅恢复新实例中仍存在的 Key）
            if old_auto_disabled:
                new_circular = provider_api_circular_list.get(provider['provider'])
                if new_circular:
                    from time import time as _time_now
                    now = _time_now()
                    for k, info in old_auto_disabled.items():
                        until = old_auto_cooling.get(k, 0)
                        if k in new_circular.items and k not in new_circular.disabled_keys:
                            if until == float('inf') or until > now:
                                new_circular.cooling_until[k] = until
                                new_circular.auto_disabled_info[k] = info

        if "models.inference.ai.azure.com" in provider['base_url'] and not provider.get("model"):
            provider['model'] = [
                "gpt-4o",
                "gpt-4.1",
                "gpt-4o-mini",
                "o4-mini",
                "o3",
                "text-embedding-3-small",
                "text-embedding-3-large",
            ]

        if provider.get("tools") is None:
            provider["tools"] = True

        provider["_model_dict_cache"] = get_model_dict(provider)
        
        # 规范化渠道分组字段，支持单值与多值
        groups = provider.get("groups")
        if groups is None:
            if isinstance(provider.get("group"), (str, list)):
                groups = provider.get("group")
            elif safe_get(provider, "preferences", "group", default=None):
                groups = safe_get(provider, "preferences", "group", default=None)
        if isinstance(groups, str):
            groups = [groups]
        elif not isinstance(groups, list):
            groups = ["default"]
        if not groups:
            groups = ["default"]
        provider["groups"] = groups
        
        config_data['providers'][index] = provider

    for index, api_key in enumerate(config_data['api_keys']):
        if "api" in api_key:
            config_data['api_keys'][index]["api"] = str(api_key["api"]).strip()

        # 修改原因：每个 API Key 可独立配置 IP 黑名单，且需要和顶层字段使用同一套校验规则。
        # 修改方式：把 api_keys[].ip_blacklist 统一规范为字符串数组，非法 IP/CIDR 在保存阶段报错。
        # 目的：让 Key 级黑名单在热更新后能安全预解析并立即生效。
        config_data['api_keys'][index]['ip_blacklist'] = normalize_ip_blacklist(api_key.get('ip_blacklist'))

        # 兼容 JSON/JSONB：把 created_at 从字符串恢复为 datetime（用于余额/账期逻辑）
        try:
            pref = config_data['api_keys'][index].get('preferences') or {}
            ca = pref.get('created_at')
            if isinstance(ca, str) and ca.strip():
                s = ca.strip()
                if s.endswith('Z'):
                    s = s[:-1] + '+00:00'
                dt_obj = datetime.fromisoformat(s)
                if dt_obj.tzinfo is None:
                    dt_obj = dt_obj.replace(tzinfo=timezone.utc)
                pref['created_at'] = dt_obj
                config_data['api_keys'][index]['preferences'] = pref
        except Exception:
            pass

    api_keys_db = config_data['api_keys']

    for index, api_key in enumerate(config_data['api_keys']):
        models = []
        
        # 规范化 API Key 分组字段，支持单值与多值
        key_groups = api_key.get("groups")
        if key_groups is None:
            if isinstance(api_key.get("group"), (str, list)):
                key_groups = api_key.get("group")
            elif safe_get(api_key, "preferences", "group", default=None):
                key_groups = safe_get(api_key, "preferences", "group", default=None)
        if isinstance(key_groups, str):
            key_groups = [key_groups]
        elif not isinstance(key_groups, list):
            key_groups = ["default"]
        if not key_groups:
            key_groups = ["default"]
        config_data['api_keys'][index]['groups'] = key_groups

        # 确保api字段为字符串类型
        if "api" in api_key:
            config_data['api_keys'][index]["api"] = str(api_key["api"]).strip()

        if api_key.get('model'):
            for model in api_key.get('model'):
                if isinstance(model, dict):
                    # 只提取模型名，忽略权重值（权重现在在渠道级别配置）
                    key = list(model.keys())[0]
                    models.append(str(key).strip())
                if isinstance(model, str):
                    models.append(model.strip())
            config_data['api_keys'][index]['model'] = models
            api_keys_db[index]['model'] = models
        else:
            # Default to all models if 'model' field is not set
            config_data['api_keys'][index]['model'] = ["all"]
            api_keys_db[index]['model'] = ["all"]

    api_list = [item["api"] for item in api_keys_db]
    # 修改原因：BYOK 模式不新增配置字段，只能从 api_keys[].api 的通配符后缀推断本地身份前缀。
    # 修改方式：配置规范化完成后构建最长优先的 BYOK 前缀表，交给启动和热更新写入 app.state。
    # 目的：让标准鉴权、依赖鉴权和方言鉴权共享同一份前缀匹配规则。
    from core.byok import build_byok_prefixes
    byok_prefixes = build_byok_prefixes(api_keys_db)
    # logger.info(json.dumps(config_data, indent=4, ensure_ascii=False))

    # 修改原因：虚拟模型允许覆盖同名真实模型，但链条递归等结构性错误仍必须在启动或保存时被发现。
    # 修改方式：在 provider 模型缓存和 API Key 模型数组都完成规范化之后执行集中校验。
    # 目的：允许同名覆盖普通路由，同时阻止链条中出现嵌套虚拟模型。
    from core.virtual_routing import validate_virtual_models_config
    validate_virtual_models_config(config_data)

    # 管理阶段：只在显式请求保存时（save_to_file=True）才同步写回本地 api.yaml。
    if not use_config_url and save_to_file:
        save_api_yaml(config_data)

    # 可选：写入数据库（将 DB 作为权威配置）
    if save_to_db:
        try:
            await save_config_to_db(config_data)
        except Exception as e:
            logger.warning(f"Failed to save config to DB: {e}")

    return config_data, api_keys_db, api_list
