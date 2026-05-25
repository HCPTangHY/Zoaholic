from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from core.log_config import logger


@dataclass
class UiSlotContribution:
    """一个 UI slot 贡献。"""

    # 修改原因：插件和渠道需要用同一套结构声明前端插槽脚本及其生效条件。
    # 修改方式：把 slot 标识、脚本、来源、优先级、模式和 target 条件集中到一个 dataclass。
    # 目的：让后端在输出渠道元数据前可以统一筛选并解析最终 ui_slots。
    slot_id: str          # 全局唯一 ID，如 'oai_tier.quota_display'
    slot: str             # 插槽名：quota_display / key_border / key_background 等
    script: str           # 内联 JS 代码
    source: str = ''      # 来源：渠道名或插件名
    priority: int = 100   # 越大越优先
    mode: str = 'replace' # replace / append
    engines: Optional[List[str]] = None      # 匹配的 engine 列表，None = 所有
    auth_types: Optional[List[str]] = None   # 匹配的认证类型：api_key / oauth
    enabled_plugin: Optional[str] = None     # 要求 provider 启用了某插件


# 修改原因：UI slot 贡献不再只属于 ChannelDefinition，插件也需要在生命周期内动态增删贡献。
# 修改方式：用全局字典按 slot_id 保存贡献，重复注册同一 ID 时直接覆盖。
# 目的：支持插件热重载和渠道扩展，同时保持解析入口简单稳定。
_SLOT_REGISTRY: Dict[str, UiSlotContribution] = {}


def register_ui_slot(
    slot_id: str,
    slot: str,
    script: str,
    *,
    source: str = '',
    priority: int = 100,
    mode: str = 'replace',
    engines: Optional[List[str]] = None,
    auth_types: Optional[List[str]] = None,
    enabled_plugin: Optional[str] = None,
) -> None:
    """注册一个 UI slot 贡献。"""
    # 修改原因：调用方需要一个稳定 API 来声明自身贡献的 UI slot，而不是直接改内部字典。
    # 修改方式：把函数参数组装为 UiSlotContribution，并以 slot_id 写入全局注册表。
    # 目的：保证渠道和插件使用同一条注册路径，后续可以在这里扩展校验或日志。
    contrib = UiSlotContribution(
        slot_id=slot_id,
        slot=slot,
        script=script,
        source=source,
        priority=priority,
        mode=mode,
        engines=engines,
        auth_types=auth_types,
        enabled_plugin=enabled_plugin,
    )
    _SLOT_REGISTRY[slot_id] = contrib
    logger.debug(f"[ui_slots] Registered slot contribution: {slot_id} -> {slot}")


def unregister_ui_slot(slot_id: str) -> None:
    """注销一个 UI slot 贡献。"""
    # 修改原因：插件卸载或热重载时必须移除旧贡献，避免前端继续加载过期脚本。
    # 修改方式：按 slot_id 从全局注册表中删除贡献；不存在时保持幂等。
    # 目的：让插件生命周期可以安全反复执行 setup/teardown。
    if slot_id in _SLOT_REGISTRY:
        del _SLOT_REGISTRY[slot_id]
        logger.debug(f"[ui_slots] Unregistered slot contribution: {slot_id}")


def _slot_output_value(contrib: UiSlotContribution) -> Any:
    """把单个贡献转换为前端可消费的 ui_slots 值。"""
    # 修改原因：/v1/channels 没有单个 provider 的 enabled_plugins 上下文，后端不能提前过滤插件门控 slot。
    # 修改方式：无条件 slot 仍输出脚本字符串；带 enabled_plugin 条件的 slot 输出 script 和 requires_plugin 对象。
    # 目的：让前端按当前 provider.preferences.enabled_plugins 决定是否渲染，同时保持旧无条件 slot 格式不变。
    if contrib.enabled_plugin:
        return {"script": contrib.script, "requires_plugin": contrib.enabled_plugin}
    return contrib.script


def resolve_slots_for_engine(
    engine: str,
    auth_type: str = 'api_key',
    enabled_plugins: Optional[List[str]] = None,
    channel_slots: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """解析某个 engine/provider 最终应该使用的 slots。"""
    # 修改原因：/v1/channels 和 provider 级解析都需要同一套 target 匹配和优先级逻辑，但 provider 插件条件必须保留给前端判断。
    # 修改方式：先复制渠道自带 channel_slots，再收集匹配 engine 和 auth_type 的贡献；enabled_plugin 不再过滤，只写入输出条件。
    # 目的：让 oai_tier 这类插件 slot 能返回给管理端，同时避免没有启用插件的 provider 误渲染。
    result: Dict[str, Any] = dict(channel_slots or {})
    _ = enabled_plugins

    candidates: Dict[str, List[UiSlotContribution]] = {}
    for contrib in _SLOT_REGISTRY.values():
        if contrib.engines and engine not in contrib.engines:
            continue
        if contrib.auth_types and auth_type not in contrib.auth_types:
            continue
        # 修改原因：这里解析的是渠道元数据，不一定知道当前 provider 是否启用了插件。
        # 修改方式：不再按 contrib.enabled_plugin 过滤，稍后把该条件输出为 requires_plugin。
        # 目的：让前端可以用当前 provider.preferences.enabled_plugins 精确决定 slot 是否生效。
        candidates.setdefault(contrib.slot, []).append(contrib)

    # 修改原因：同一 slot 可能被多个插件贡献，注册顺序不应决定最终展示。
    # 修改方式：每个 slot 只取 priority 最大的贡献；replace 贡献覆盖基础值，append 贡献只在没有基础值时落入结果。
    # 目的：保持 P1 阶段行为简单，并为后续 provider 级 slot 解析保留 mode 字段。
    for slot_name, contribs in candidates.items():
        best = max(contribs, key=lambda c: c.priority)
        if best.mode == 'replace' or slot_name not in result:
            result[slot_name] = _slot_output_value(best)

    return result


def get_all_contributions() -> List[UiSlotContribution]:
    """获取所有注册的 slot 贡献。"""
    # 修改原因：测试、调试页或后续管理端需要查看当前内存中的 UI slot 贡献。
    # 修改方式：返回全局注册表 value 的列表副本，不暴露内部字典本身。
    # 目的：避免调用方误改全局注册表，同时保留可观测性。
    return list(_SLOT_REGISTRY.values())

