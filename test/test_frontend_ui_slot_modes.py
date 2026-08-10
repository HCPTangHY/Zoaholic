from pathlib import Path


# 修改原因：quota_display、key_background 和 key_border 插槽现在同时服务完整行与机房卡片，前端必须把渲染模式交给渠道脚本。
# 修改方式：用源码级测试固定 Channels.tsx 的 UiSlot 调用上下文和圆环中心溢出约束，不依赖浏览器环境。
# 目的：防止后续前端重构遗漏 mode，导致渠道脚本无法区分 row 与 rack 布局。
ROOT = next(
    parent for parent in Path(__file__).resolve().parents
    if (parent / "frontend" / "src" / "pages" / "Channels.tsx").is_file()
)
CHANNELS_TSX = ROOT / "frontend" / "src" / "pages" / "Channels.tsx"


def _read_channels_source() -> str:
    return CHANNELS_TSX.read_text(encoding="utf-8")


def _slice_between(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]

