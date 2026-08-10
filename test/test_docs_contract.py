from pathlib import Path


# 修改原因：本测试直接读取 docs 目录，单文件运行时需要稳定定位仓库根目录。
# 修改方式：从当前测试文件向上查找包含 docs/ 与 core/ 的目录。
# 目的：让文档契约测试在完整测试集和单文件 pytest 中都能使用真实仓库文档。
ROOT = next(
    parent for parent in Path(__file__).resolve().parents
    if (parent / "docs").is_dir() and (parent / "core").is_dir()
)


def _read_doc(name: str) -> str:
    """读取指定开发文档文本。"""
    # 修改原因：三份新增文档都需要检查存在性和关键契约，重复读文件会让失败信息不清楚。
    # 修改方式：封装统一读取函数，并在缺失时让 Path.read_text 抛出明确路径错误。
    # 目的：保持各测试只描述对应文档必须包含的开发要点。
    return (ROOT / "docs" / name).read_text(encoding="utf-8")

