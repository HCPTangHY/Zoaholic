"""配置持久化前的清理和序列化工具。"""


# 迁移说明：
# 修改原因：该模块承载业务逻辑，不应继续放在 utils_pkg 这种通用工具包中。
# 修改方式：按照 Scout 的归位方案迁移到 core 对应业务模块，并只调整必要的内部导入路径。
# 目的：让业务代码按领域归属维护，同时保留根 utils.py 和 utils_pkg shim 的旧导入兼容性。
from fastapi.encoders import jsonable_encoder

from .codec import _quote_colon_strings, yaml


def _rebuild_api_with_labels(provider: dict) -> None:
    """将运行时 _api_labels 还原到 api 列表中，用于持久化。
    
    有 label 的 key 存成 {key: label} dict，没 label 的保持纯字符串。
    disabled key 保留 ! 前缀。
    """
    labels = provider.get("_api_labels")
    if not labels or not isinstance(labels, dict):
        return
    api = provider.get("api")
    if not api:
        return
    
    raw_list = [api] if isinstance(api, str) else list(api) if isinstance(api, list) else []
    rebuilt = []
    for item in raw_list:
        # 跳过已经是 dict 的（不应该出现在运行时 config，但防御性处理）
        if isinstance(item, dict):
            rebuilt.append(item)
            continue
        key_str = str(item).strip()
        # 提取纯 key（去掉 ! 前缀）
        is_disabled = key_str.startswith("!")
        clean_key = key_str[1:] if is_disabled else key_str
        label = labels.get(clean_key)
        if label:
            persist_key = f"!{clean_key}" if is_disabled else clean_key
            rebuilt.append({persist_key: label})
        else:
            rebuilt.append(key_str)
    
    if len(rebuilt) == 1 and isinstance(rebuilt[0], str):
        provider["api"] = rebuilt[0]
    else:
        provider["api"] = rebuilt


def _sanitize_config_for_persistence(config_data: dict) -> dict:
    """清理配置中的运行时字段，返回可持久化的 dict。

    - 移除 providers/api_keys 中以 "_" 开头的运行时字段
    - 保持其余结构不变
    """

    import copy

    processed_data = copy.deepcopy({k: v for k, v in (config_data or {}).items() if not k.startswith('_')})

    # 持久化前还原 label 到 api 列表
    for provider in processed_data.get("providers", []) or []:
        _rebuild_api_with_labels(provider)

    # 过滤掉子渠道展开生成的 provider（运行时产物，不持久化）
    processed_data['providers'] = [
        p for p in (processed_data.get('providers') or [])
        if not p.get('_is_sub_channel')
    ]

    for provider in processed_data.get("providers", []) or []:
        keys_to_remove = [k for k in list(provider.keys()) if str(k).startswith("_")]
        for k in keys_to_remove:
            provider.pop(k, None)

    if "api_keys" in processed_data:
        from core.key_hierarchy import raw_key_config
        processed_data["api_keys"] = [raw_key_config(key) for key in processed_data["api_keys"] or []]

    return processed_data


def dump_config_to_json_obj(config_data: dict) -> dict:
    """将配置 dict 转为可写入 JSON/JSONB 的对象。

    使用 jsonable_encoder 处理 datetime 等类型，避免 JSON 序列化失败。
    """

    processed_data = _sanitize_config_for_persistence(config_data)
    return jsonable_encoder(processed_data)


def dump_config_to_yaml_text(config_data: dict) -> str:
    """将配置序列化为 YAML 文本（可选：用于导出/排查）。"""

    import io

    processed_data = _sanitize_config_for_persistence(config_data)
    processed_data = _quote_colon_strings(processed_data)

    buf = io.StringIO()
    yaml.dump(processed_data, buf)
    return buf.getvalue()
