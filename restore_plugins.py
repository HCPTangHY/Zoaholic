"""
插件持久化恢复脚本

在无持久化文件系统的平台（如 Render Free Tier）上，
通过环境变量保存和恢复自定义插件文件。

使用方式：
1. 将插件文件 Base64 编码后存入环境变量 CUSTOM_PLUGINS
2. 格式：filename1.py:base64content1,filename2.py:base64content2
3. 启动时运行此脚本自动恢复到 plugins/ 目录

编码工具（在本地执行）：
  python restore_plugins.py --encode plugins/my_plugin.py plugins/another_plugin.py

也可以用 CUSTOM_PLUGINS_JSON 环境变量（JSON 格式，更易读）：
  {"my_plugin.py": "base64...", "another_plugin.py": "base64..."}
"""

import os
import sys
import json
import base64
from pathlib import Path

PLUGINS_DIR = Path(__file__).parent / "plugins"


def encode_plugins(file_paths: list) -> str:
    """将插件文件编码为 JSON 格式字符串，用于存入环境变量。"""
    result = {}
    for fp in file_paths:
        p = Path(fp)
        if not p.exists():
            print(f"Warning: {fp} not found, skipping")
            continue
        content = p.read_bytes()
        filename = p.name
        result[filename] = base64.b64encode(content).decode("ascii")
        print(f"Encoded: {filename} ({len(content)} bytes)")

    json_str = json.dumps(result, ensure_ascii=False)
    print(f"\n=== Copy the following value into CUSTOM_PLUGINS_JSON env var ===")
    print(json_str)
    print(f"\n=== Total: {len(result)} plugins, {len(json_str)} chars ===")
    return json_str


def restore_plugins():
    """从环境变量恢复插件文件到 plugins/ 目录。"""
    restored = 0

    # 方式 1: CUSTOM_PLUGINS_JSON（推荐，JSON 格式）
    json_env = os.environ.get("CUSTOM_PLUGINS_JSON", "").strip()
    if json_env:
        try:
            plugins_dict = json.loads(json_env)
            if isinstance(plugins_dict, dict):
                for filename, b64_content in plugins_dict.items():
                    if not filename.endswith(".py"):
                        print(f"[restore_plugins] Skipping non-py file: {filename}")
                        continue
                    target = PLUGINS_DIR / filename
                    # 不覆盖仓库自带的插件（除非环境变量版本更新）
                    content = base64.b64decode(b64_content)
                    target.write_bytes(content)
                    restored += 1
                    print(f"[restore_plugins] Restored: {filename} ({len(content)} bytes)")
        except (json.JSONDecodeError, Exception) as e:
            print(f"[restore_plugins] Error parsing CUSTOM_PLUGINS_JSON: {e}")

    # 方式 2: CUSTOM_PLUGINS（逗号分隔格式，向后兼容）
    csv_env = os.environ.get("CUSTOM_PLUGINS", "").strip()
    if csv_env and not json_env:
        for entry in csv_env.split(","):
            entry = entry.strip()
            if ":" not in entry:
                continue
            filename, b64_content = entry.split(":", 1)
            filename = filename.strip()
            if not filename.endswith(".py"):
                continue
            try:
                target = PLUGINS_DIR / filename
                content = base64.b64decode(b64_content)
                target.write_bytes(content)
                restored += 1
                print(f"[restore_plugins] Restored: {filename} ({len(content)} bytes)")
            except Exception as e:
                print(f"[restore_plugins] Error restoring {filename}: {e}")

    if restored > 0:
        print(f"[restore_plugins] Total restored: {restored} plugin(s)")
    else:
        if not json_env and not csv_env:
            pass  # 没配置环境变量，静默跳过
        else:
            print("[restore_plugins] No plugins restored (check env var format)")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--encode":
        # 编码模式：python restore_plugins.py --encode file1.py file2.py
        if len(sys.argv) < 3:
            print("Usage: python restore_plugins.py --encode <plugin1.py> [plugin2.py ...]")
            sys.exit(1)
        encode_plugins(sys.argv[2:])
    else:
        # 恢复模式
        restore_plugins()
