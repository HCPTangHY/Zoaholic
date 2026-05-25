"""UI slot 注册表导出。"""

# 修改原因：UI slot 注册能力需要从渠道注册表中独立出来，供渠道和插件共同使用。
# 修改方式：在 core.ui_slots 包入口导出注册、注销、解析和查询函数。
# 目的：让调用方无需依赖具体 registry.py 路径，也能获得稳定的公共导入入口。
from .registry import (
    UiSlotContribution,
    get_all_contributions,
    register_ui_slot,
    resolve_slots_for_engine,
    unregister_ui_slot,
)

__all__ = [
    "UiSlotContribution",
    "get_all_contributions",
    "register_ui_slot",
    "resolve_slots_for_engine",
    "unregister_ui_slot",
]
