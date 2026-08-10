import json
from pathlib import Path
import sys

import pytest

# 修改原因：UI slot 注册表测试会直接导入 core 和 routes，单文件运行时项目根目录可能不在 sys.path 中。
# 修改方式：从测试文件向上查找同时包含 core/ 和 routes/ 的目录，并在缺失时插入导入路径。
# 目的：让新增注册表测试在完整测试集和单文件 pytest 两种方式下都能稳定运行。
ROOT = next(
    parent for parent in Path(__file__).resolve().parents
    if (parent / "core").is_dir() and (parent / "routes").is_dir()
)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _json_response_payload(response) -> dict:
    """读取 FastAPI JSONResponse 的 JSON 内容。"""
    # 修改原因：/v1/channels 路由测试直接调用处理函数，不经过 ASGI 客户端时也需要读取 JSONResponse。
    # 修改方式：对 response.body 解码后用 json.loads 还原为 dict。
    # 目的：让测试只关注渠道元数据中的 ui_slots 解析结果，不引入额外 HTTP 测试栈。
    return json.loads(response.body.decode())


def test_ui_slot_registry_resolves_targets_and_marks_plugin_gate():
    from core.ui_slots.registry import register_ui_slot, resolve_slots_for_engine, unregister_ui_slot

    # 修改原因：/v1/channels 没有单个 provider 的 enabled_plugins 上下文，不能在后端提前过滤插件门控插槽。
    # 修改方式：注册三个不同条件的贡献，并断言需要插件的 slot 会以 requires_plugin 条件对象输出。
    # 目的：保证前端能拿到 oai_tier 这类插件 slot，同时仍可按当前 provider 的 enabled_plugins 决定是否渲染。
    slot_ids = ["test.openai_quota", "test.oauth_quota", "test.plugin_background"]
    for slot_id in slot_ids:
        unregister_ui_slot(slot_id)

    try:
        register_ui_slot(
            "test.openai_quota",
            "quota_display",
            "export default function render() { return 'openai'; }",
            engines=["openai"],
            auth_types=["api_key"],
            priority=10,
        )
        register_ui_slot(
            "test.oauth_quota",
            "quota_display",
            "export default function render() { return 'oauth'; }",
            engines=["openai"],
            auth_types=["oauth"],
            priority=20,
        )
        register_ui_slot(
            "test.plugin_background",
            "key_background",
            "export default function render() { return 'plugin'; }",
            enabled_plugin="needs_plugin",
        )

        resolved = resolve_slots_for_engine(
            "openai",
            auth_type="api_key",
            channel_slots={"key_hint": "base-script"},
        )
        assert resolved["key_hint"] == "base-script"
        assert "openai" in resolved["quota_display"]
        assert resolved["key_background"] == {
            "script": "export default function render() { return 'plugin'; }",
            "requires_plugin": "needs_plugin",
        }

        with_plugin = resolve_slots_for_engine(
            "openai",
            auth_type="api_key",
            enabled_plugins=["needs_plugin"],
        )
        assert with_plugin["key_background"] == resolved["key_background"]
    finally:
        for slot_id in slot_ids:
            unregister_ui_slot(slot_id)


def test_ui_slot_registry_picks_highest_priority_for_same_slot():
    from core.ui_slots.registry import register_ui_slot, resolve_slots_for_engine, unregister_ui_slot

    # 修改原因：多个插件可能贡献同一个 slot，解析时必须有稳定的优先级规则。
    # 修改方式：给同一 slot 注册高低两个 priority，再断言最终脚本来自更高 priority 的贡献。
    # 目的：防止注册顺序影响管理端最终加载的 slot 脚本。
    slot_ids = ["test.low_priority", "test.high_priority"]
    for slot_id in slot_ids:
        unregister_ui_slot(slot_id)

    try:
        register_ui_slot("test.low_priority", "quota_display", "low-script", priority=10)
        register_ui_slot("test.high_priority", "quota_display", "high-script", priority=200)

        resolved = resolve_slots_for_engine("openai")
        assert resolved["quota_display"] == "high-script"
    finally:
        for slot_id in slot_ids:
            unregister_ui_slot(slot_id)


@pytest.mark.asyncio
async def test_channels_endpoint_outputs_resolved_global_slots(monkeypatch):
    from core.channels.registry import get_channel, register_channel, unregister_channel
    from core.ui_slots.registry import register_ui_slot, unregister_ui_slot
    from routes import channels as channels_route

    # 修改原因：/v1/channels 只知道 channel 级元数据，但前端需要收到插件门控 slot 才能按 provider 判断。
    # 修改方式：注册一个临时渠道、一个全局贡献和一个插件门控贡献，并让路由只返回该临时渠道。
    # 目的：固定端点输出 resolved ui_slots，同时把 enabled_plugin 条件随 slot 一起返回给前端。
    engine = "ui-slot-route-test"
    slot_ids = ["test.route_global", "test.route_plugin"]
    unregister_channel(engine)
    for slot_id in slot_ids:
        unregister_ui_slot(slot_id)

    try:
        register_channel(
            id=engine,
            type_name="openai",
            ui_slots={"key_hint": "channel-script"},
            source="test",
        )
        register_ui_slot(
            "test.route_global",
            "quota_display",
            "global-script",
            engines=[engine],
            auth_types=["api_key"],
        )
        register_ui_slot(
            "test.route_plugin",
            "key_background",
            "plugin-script",
            engines=[engine],
            enabled_plugin="needs_provider_context",
        )

        channel = get_channel(engine)
        monkeypatch.setattr(channels_route, "list_channels", lambda: [channel])
        response = await channels_route.get_channels(token="admin")
        payload = _json_response_payload(response)

        slots = payload["channels"][0]["ui_slots"]
        assert slots["key_hint"] == "channel-script"
        assert slots["quota_display"] == "global-script"
        assert slots["key_background"] == {
            "script": "plugin-script",
            "requires_plugin": "needs_provider_context",
        }
    finally:
        unregister_channel(engine)
        for slot_id in slot_ids:
            unregister_ui_slot(slot_id)


def test_oai_tier_setup_registers_quota_display_slot():
    from core.ui_slots.registry import get_all_contributions, unregister_ui_slot
    from plugins import oai_tier

    # 修改原因：oai_tier 的 tier 展示要从 Channels.tsx 硬编码迁移到插件贡献的 quota_display slot。
    # 修改方式：直接执行插件 setup，检查注册贡献的 slot、target、脚本内容，并在 teardown 后确认注销。
    # 目的：保证插件生命周期能完整接入独立 UI slot 注册表。
    unregister_ui_slot("oai_tier.quota_display")

    try:
        oai_tier.setup(None)
        contributions = {item.slot_id: item for item in get_all_contributions()}
        contribution = contributions["oai_tier.quota_display"]

        assert contribution.slot == "quota_display"
        assert contribution.source == "oai_tier"
        assert contribution.priority == 50
        assert contribution.engines == ["openai", "openai-responses"]
        assert contribution.auth_types == ["api_key"]
        assert contribution.enabled_plugin == "oai_tier"
        assert "data?.tier" in contribution.script
        assert "percent.toFixed(1)" in contribution.script
    finally:
        oai_tier.teardown(None)

    assert all(item.slot_id != "oai_tier.quota_display" for item in get_all_contributions())
