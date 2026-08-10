import asyncio
import sys
import uuid
from pathlib import Path

# 修改原因：本测试直接覆盖 claude_code_compat 插件的新 7 层清洗行为，pytest 单文件运行时需要稳定导入项目代码。
# 修改方式：从测试文件向上查找包含 core/ 和 plugins/ 的项目根目录，并放入 sys.path。
# 目的：在仓库根目录或 tests/ 子目录运行时，都验证真实插件实现而不是外部同名模块。
ROOT = next(
    parent for parent in Path(__file__).resolve().parents
    if (parent / "core").is_dir() and (parent / "plugins").is_dir()
)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plugins import claude_code_compat as compat


def provider_with_plugin(entry):
    # 修改原因：插件参数通过 provider.preferences.enabled_plugins 传入，测试需要复用同一种配置结构。
    # 修改方式：构造最小 provider，仅保留插件解析需要的 preferences 字段。
    # 目的：固定 claude_code_compat、版本、entrypoint、salt 的向后兼容解析行为。
    return {"preferences": {"enabled_plugins": [entry]}}


def test_sanitize_payload_injects_identity_when_system_is_empty():
    payload = {"messages": [{"role": "user", "content": "hello"}]}

    # 修改原因：核心 channel 在 system 缺失时会补 Claude Code 身份声明，插件重写后也应保留这个兼容行为。
    # 修改方式：使用没有 system 字段的最小 messages payload 调用清洗入口。
    # 目的：防止仅注入 billing header 而遗漏 Claude Code 身份声明。
    compat.sanitize_payload(payload, {"User-Agent": "claude-code/2.1.97"})

    assert payload["system"][0]["text"].startswith("x-anthropic-billing-header:")
    assert payload["system"][1]["text"] == "You are Claude Code, Anthropic's official CLI for Claude."

