"""OAuth 运行时状态文件读写。"""

import json
import logging
import os
import shutil
import tempfile
import time
from typing import Any

logger = logging.getLogger(__name__)

STATE_PATH = os.path.join(os.getenv("DATA_DIR", "data"), "oauth_state.json")


def load_state(path: str = STATE_PATH) -> dict:
    """加载 oauth_state.json，损坏时备份并返回空状态。"""
    # 修改原因：旧的非原子写或手工编辑可能留下损坏 JSON，启动时直接抛错会使服务不可用。
    # 修改方式：读取和类型校验放入 try，任何异常都会把原文件复制到 .corrupt.<timestamp> 备份。
    # 目的：保留人工恢复凭据的线索，同时让 OAuthManager 以空状态继续启动。
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data: Any = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("oauth_state.json root is not a dict")
        return data
    except Exception as e:
        backup_path = path + f".corrupt.{int(time.time())}"
        try:
            shutil.copy2(path, backup_path)
        except Exception:
            pass
        logger.error(f"Failed to load oauth_state.json, backed up to {backup_path}: {e}")
        return {}


def save_state(path: str, data: dict) -> None:
    """原子保存 OAuth 凭据状态。"""
    # 修改原因：调用方可能仍直接使用 save_state，不能保留会截断正式文件的旧 open("w") 写法。
    # 修改方式：同目录创建临时文件，写入 JSON、flush、fsync 后用 os.replace 原子替换正式文件。
    # 目的：让所有 OAuth 状态保存入口都具备崩溃安全性，并继续收敛文件权限到 600。
    dir_path = os.path.dirname(path) or "."
    os.makedirs(dir_path, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False)
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
