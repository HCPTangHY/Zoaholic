from pathlib import Path
import sys

# 修改原因：渠道插槽注册测试从 tests/ 目录单独运行时，项目根目录不一定在 sys.path 中。
# 修改方式：向上查找包含 core/ 和 routes/ 的目录并插入导入路径。
# 目的：让单文件 pytest 与仓库根目录 pytest 都能导入真实渠道注册实现。
ROOT = next(
    parent for parent in Path(__file__).resolve().parents
    if (parent / "core").is_dir() and (parent / "routes").is_dir()
)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_claude_code_registers_merged_quota_display_ui_slots():
    from core.channels import claude_code_channel as cc
    from core.channels.registry import get_channel, unregister_channel

    # 修改原因：Claude Code 的金额标签要合并到 quota_display，渠道元数据不能再注册旧的独立标签插槽。
    # 修改方式：重新注册 claude-code 渠道后断言 quota_display 同时包含订阅 tier、百分比和 extra_usage 金额逻辑。
    # 目的：防止前端删除独立标签挂载点后，Claude Code 的额度金额在渠道元数据中丢失。
    unregister_channel("claude-code")
    cc.register()
    definition = get_channel("claude-code")

    assert definition is not None
    assert definition.ui_slots is not None
    assert {"key_background", "balance_summary", "quota_display", "import_placeholder"}.issubset(set(definition.ui_slots))
    assert "quota_label" not in definition.ui_slots
    display_script = definition.ui_slots["quota_display"]
    assert "extra_usage_monthly_limit" in definition.ui_slots["key_background"]
    assert "extra_usage_monthly_limit" in display_script
    assert "remaining" in display_script
    assert "subscription_type" in display_script
    assert "tierMap" in display_script
    # 修改原因：Claude Code 的 quota_display 会同时挂载到完整行和机房卡片，机房卡片不能复用完整行的药丸和金额样式。
    # 修改方式：断言脚本读取 ctx.context.mode，并在 rack 分支使用更短文本；背景条脚本也必须按 mode 改变填充方向。
    # 目的：防止后续重构时再次把完整行标签渲染进小圆环中心，或让机房卡片背景条横向溢出。
    assert "ctx.context?.mode" in display_script
    assert "mode === 'rack'" in display_script
    assert "text-[9px]" in display_script
    assert "extraUsageLabel" in display_script
    background_script = definition.ui_slots["key_background"]
    assert "extra_usage_monthly_limit" in background_script
    assert "ctx.context?.mode" in background_script
    assert "mode === 'rack'" in background_script
    assert "el.style.height = pct + '%'" in background_script
    assert "el.style.width = pct + '%'" in background_script
    assert "Object.values(accounts).filter" in definition.ui_slots["balance_summary"]
    assert definition.to_dict()["ui_slots"] == definition.ui_slots


def test_codex_registers_merged_quota_display_tier_ui_slot():
    from core.channels import codex_channel
    from core.channels.registry import get_channel, unregister_channel

    # 修改原因：Codex 的 plan_type 要合并回 quota_display，渠道文件不能继续暴露独立标签脚本。
    # 修改方式：重新注册 codex 渠道后断言 quota_display 同时读取百分比和 plan_type，且旧常量已移除。
    # 目的：保证前端只保留一个 quota_display 挂载点时，Codex 仍能显示订阅类型和剩余额度。
    unregister_channel("codex")
    codex_channel.register()
    definition = get_channel("codex")

    assert definition is not None
    assert hasattr(definition, "ui_slots")
    assert definition.ui_slots is not None
    assert set(definition.ui_slots) == {"quota_display", "import_placeholder"}
    assert "quota_label" not in definition.ui_slots
    assert not hasattr(codex_channel, "CODEX_QUOTA_LABEL")
    display_script = definition.ui_slots["quota_display"]
    assert "quota_inner" in display_script
    assert "quota_outer" in display_script
    assert "x-codex-plan-type" in display_script
    assert "raw.plan_type" in display_script
    assert "planType" in display_script
    assert "minPct" in display_script
    # 修改原因：Codex quota_display 同时服务完整行和机房卡片，必须用 mode 分支避免小圆环中心显示 planType 药丸。
    # 修改方式：断言脚本读取 ctx.context.mode，并包含 rack 专用的短文本样式和 row 专用的完整药丸样式。
    # 目的：确保机房卡片只显示百分比或短 planType，完整行继续显示 planType + 百分比。
    assert "ctx.context?.mode" in display_script
    assert "mode === 'rack'" in display_script
    assert "text-[9px]" in display_script
    assert "parts.join(' ')" in display_script
    assert definition.to_dict()["ui_slots"] == definition.ui_slots


def test_antigravity_quota_display_slot_supports_row_and_rack_modes():
    from core.channels import antigravity_channel

    # 修改原因：Antigravity quota_display 会在机房卡片圆环中心渲染，旧 compactWidth 判断无法稳定区分完整行和机房卡片。
    # 修改方式：直接检查内联脚本使用 ctx.context.mode，并把 rack 分支限制为百分比或 tier 缩写，不走 credits 金额展示。
    # 目的：避免机房卡片中心出现长 tier 文本、credits 金额或完整行药丸样式。
    display_script = antigravity_channel.QUOTA_UI
    assert "ctx.context?.mode" in display_script
    assert "mode === 'rack'" in display_script
    assert "shortTierName" in display_script
    assert "displayAmount" in display_script
    assert display_script.index("mode === 'rack'") < display_script.index("displayAmount")


def test_builtin_oauth_channels_register_import_placeholders():
    from core.channels import get_channel

    # 修改原因：OAuth 渠道的手动导入入口依赖 import_placeholder，缺失时用户难以判断应粘贴哪种凭据。
    # 修改方式：逐一检查当前内置 OAuth 渠道的 ui_slots，断言都注册了纯文本导入占位提示。
    # 目的：防止后续新增或重构 OAuth 渠道时遗漏导入占位插槽。
    expected_placeholders = {
        "codex": "rt_xxxxxxxx...",
        "claude-code": "sk-ant-oat01-xxxxxxxx...",
        "antigravity": "1//0xxxxxxxx...",
        "gemini-cli": "1//0xxxxxxxx...",
        "vertex-gemini": '{"type": "service_account", "project_id": "...", ...}',
        "vertex-claude": '{"type": "service_account", "project_id": "...", ...}',
    }

    for channel_id, placeholder in expected_placeholders.items():
        definition = get_channel(channel_id)
        assert definition is not None
        assert definition.is_oauth is True
        assert definition.ui_slots is not None
        assert definition.ui_slots.get("import_placeholder") == placeholder


def test_builtin_oauth_channels_with_real_quota_fetch_register_quota_display():
    from core.channels import get_channel
    from core.oauth.providers.base import OAuthProvider

    # 修改原因：只有覆盖基类空实现并能返回额度数据的 OAuth provider 才需要 quota_display。
    # 修改方式：把当前内置 provider 的 fetch_quota 覆盖状态和插槽注册结果固定为显式映射。
    # 目的：保证 Codex、Claude Code、Antigravity 的额度脚本不丢失，同时不强迫 Gemini CLI 和 Vertex 伪造额度展示。
    expected = {
        "codex": True,
        "claude-code": True,
        "antigravity": True,
        "gemini-cli": False,
        "vertex-gemini": False,
        "vertex-claude": False,
    }

    for channel_id, should_have_quota_display in expected.items():
        definition = get_channel(channel_id)
        assert definition is not None
        provider = definition.oauth_provider
        slots = definition.ui_slots or {}
        # 修改原因：OAuthProvider 基类提供 fetch_quota 空实现，单看 callable 会把不支持额度的渠道误判为已实现。
        # 修改方式：比较 provider 类上的 fetch_quota 是否仍是基类函数，从而识别真实覆盖。
        # 目的：让测试语义与“后端 fetch_quota 有返回额度数据才注册 quota_display”的要求一致。
        has_real_quota_fetch = provider.__class__.fetch_quota is not OAuthProvider.fetch_quota
        assert has_real_quota_fetch is should_have_quota_display
        assert ("quota_display" in slots) is should_have_quota_display
