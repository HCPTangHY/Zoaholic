import json
import sys
from pathlib import Path

import pytest

# 修改原因：本测试直接导入 core.quota 与 routes 模块，单文件运行时项目根目录可能不在 sys.path 中。
# 修改方式：从测试文件向上查找同时包含 core/ 和 routes/ 的目录，并在缺失时插入导入路径。
# 目的：确保统一额度快照模型、服务层和余额端点测试始终覆盖当前仓库源码。
ROOT = next(
    parent for parent in Path(__file__).resolve().parents
    if (parent / "core").is_dir() and (parent / "routes").is_dir()
)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _json_response_payload(response) -> dict:
    """读取 FastAPI JSONResponse 的 JSON 内容。"""
    # 修改原因：余额端点单元测试直接调用路由函数，不经过 ASGI 测试客户端。
    # 修改方式：解码 JSONResponse.body 并用 json.loads 还原为 dict。
    # 目的：让测试只验证端点返回结构，不额外引入 HTTP 调用层。
    return json.loads(response.body.decode())

