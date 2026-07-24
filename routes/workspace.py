"""
后端工作区文件管理路由。

提供对后端服务器关键目录/文件的浏览、读取、编辑、删除和下载功能。
仅管理员可访问，通过白名单严格限制可操作的路径范围。
"""

import os
import stat
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from routes.deps import rate_limit_dependency, verify_admin_api_key

router = APIRouter()

# ==================== 配置 ====================

# 项目根目录（main.py 所在目录）
PROJECT_ROOT = Path(os.getcwd()).resolve()

# 白名单规则：(相对路径, 权限)
# r = 可读, w = 可写, d = 可删除
# 目录尾部带 / 表示目录下所有文件
WORKSPACE_RULES: List[tuple] = [
    ("api.yaml", "rw"),
    ("data/", "rd"),
    ("plugins/", "r"),
    ("pyproject.toml", "r"),
    ("docker-compose.yml", "r"),
    ("Dockerfile", "r"),
    (".env", "r"),
    ("docs/", "r"),
]

# 禁止在任何操作中暴露的文件扩展名/文件名
BLOCKED_NAMES = {".git", "__pycache__", ".env.local", ".env.production"}
BLOCKED_EXTENSIONS = {".pyc", ".pyo", ".db-journal", ".db-wal", ".db-shm"}

# 文本文件可读的最大大小（字节）
MAX_READ_SIZE = 2 * 1024 * 1024  # 2MB
# 可写文件的最大大小
MAX_WRITE_SIZE = 1 * 1024 * 1024  # 1MB


# ==================== 权限检查 ====================


def _normalize_rel(rel_path: str) -> str:
    """规范化相对路径，防止路径穿越。"""
    # 用 PurePosixPath 统一处理
    parts = PurePosixPath(rel_path.replace("\\", "/")).parts
    # 过滤掉 .. 和绝对路径开头
    safe_parts = [p for p in parts if p not in ("..", "/", "\\") and not p.startswith("/")]
    return "/".join(safe_parts)


def _resolve_path(rel_path: str) -> Path:
    """将相对路径解析为绝对路径，并验证不超出项目根目录。"""
    normalized = _normalize_rel(rel_path)
    absolute = (PROJECT_ROOT / normalized).resolve()
    # 确保在项目根目录内
    if not str(absolute).startswith(str(PROJECT_ROOT)):
        raise HTTPException(status_code=403, detail="路径超出允许范围")
    return absolute


def _get_permissions(rel_path: str) -> str:
    """获取指定相对路径的权限字符串。无匹配则返回空字符串。"""
    normalized = _normalize_rel(rel_path)
    if not normalized:
        return ""

    # 检查文件名/扩展名是否在黑名单中
    name = Path(normalized).name
    suffix = Path(normalized).suffix.lower()
    if name in BLOCKED_NAMES or suffix in BLOCKED_EXTENSIONS:
        return ""

    best_match = ""
    best_match_len = -1

    for rule_path, perms in WORKSPACE_RULES:
        if rule_path.endswith("/"):
            # 目录规则：匹配该目录下所有文件
            prefix = rule_path.rstrip("/")
            if normalized == prefix or normalized.startswith(prefix + "/"):
                if len(prefix) > best_match_len:
                    best_match = perms
                    best_match_len = len(prefix)
        else:
            # 精确文件匹配
            if normalized == rule_path:
                if len(rule_path) > best_match_len:
                    best_match = perms
                    best_match_len = len(rule_path)

    return best_match


def _check_permission(rel_path: str, required: str) -> str:
    """检查权限，返回规范化后的相对路径。不满足则抛出 403。"""
    normalized = _normalize_rel(rel_path)
    perms = _get_permissions(normalized)
    if required not in perms:
        raise HTTPException(status_code=403, detail=f"无权对 '{normalized}' 执行此操作")
    return normalized


# ==================== 辅助函数 ====================


def _is_text_file(path: Path) -> bool:
    """粗略判断是否为文本文件。"""
    text_suffixes = {
        ".py", ".txt", ".md", ".yaml", ".yml", ".json", ".toml",
        ".cfg", ".ini", ".conf", ".env", ".sh", ".bash", ".bat",
        ".csv", ".xml", ".html", ".css", ".js", ".ts", ".tsx",
        ".jsx", ".sql", ".log", ".rst", ".gitignore", ".dockerignore",
    }
    name = path.name.lower()
    suffix = path.suffix.lower()
    # 无扩展名但常见的配置文件
    if name in {"dockerfile", ".gitignore", ".dockerignore", ".env", "makefile", "license"}:
        return True
    return suffix in text_suffixes


def _get_language(path: Path) -> str:
    """根据扩展名返回语言标识（用于前端语法高亮）。"""
    ext_map = {
        ".py": "python", ".yaml": "yaml", ".yml": "yaml",
        ".json": "json", ".toml": "toml", ".md": "markdown",
        ".sh": "bash", ".bash": "bash", ".sql": "sql",
        ".js": "javascript", ".ts": "typescript", ".tsx": "tsx",
        ".jsx": "jsx", ".html": "html", ".css": "css",
        ".xml": "xml", ".csv": "csv", ".txt": "plaintext",
        ".log": "plaintext", ".env": "dotenv",
    }
    name = path.name.lower()
    if name == "dockerfile":
        return "dockerfile"
    return ext_map.get(path.suffix.lower(), "plaintext")


def _stat_info(path: Path, rel: str) -> Dict[str, Any]:
    """获取文件/目录的元信息。"""
    try:
        s = path.stat()
    except OSError:
        return {"path": rel, "exists": False}

    is_dir = stat.S_ISDIR(s.st_mode)
    return {
        "path": rel,
        "name": path.name,
        "is_dir": is_dir,
        "size": s.st_size if not is_dir else None,
        "modified_at": datetime.fromtimestamp(s.st_mtime, timezone.utc).isoformat(),
        "permissions": _get_permissions(rel),
        "is_text": _is_text_file(path) if not is_dir else None,
        "language": _get_language(path) if not is_dir else None,
    }


# ==================== 数据模型 ====================


class WriteFileRequest(BaseModel):
    path: str
    content: str


class DeleteFileRequest(BaseModel):
    path: str


# ==================== 路由端点 ====================


@router.get("/v1/workspace/tree", dependencies=[Depends(rate_limit_dependency)])
async def workspace_tree(
    request: Request,
    path: str = Query("", description="要浏览的目录相对路径，空字符串表示根目录"),
    token: str = Depends(verify_admin_api_key),
):
    """
    获取工作区文件树。返回指定目录下的文件和子目录列表。
    仅返回白名单允许的条目。
    """
    if path:
        normalized = _check_permission(path, "r")
        abs_path = _resolve_path(normalized)
        if not abs_path.is_dir():
            raise HTTPException(status_code=400, detail="指定路径不是目录")
    else:
        abs_path = PROJECT_ROOT
        normalized = ""

    entries: List[Dict[str, Any]] = []

    if not abs_path.exists():
        return JSONResponse(content={"path": normalized, "entries": [], "total": 0})

    try:
        for child in sorted(abs_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            child_rel = f"{normalized}/{child.name}" if normalized else child.name
            child_perms = _get_permissions(child_rel if not child.is_dir() else child_rel + "/x")

            # 对于目录，检查目录本身是否在白名单中（作为前缀）
            if child.is_dir():
                dir_prefix = child_rel
                has_rule = any(
                    rule_path.rstrip("/").startswith(dir_prefix) or dir_prefix.startswith(rule_path.rstrip("/"))
                    for rule_path, _ in WORKSPACE_RULES
                )
                if not has_rule:
                    continue
                # 目录本身的权限
                child_perms = _get_permissions(child_rel + "/")
                if not child_perms:
                    # 即使目录本身没有规则，但子目录下有规则也要显示
                    has_child_rule = any(
                        rule_path.rstrip("/").startswith(dir_prefix + "/")
                        for rule_path, _ in WORKSPACE_RULES
                    )
                    if not has_child_rule:
                        continue
                    child_perms = "r"  # 目录本身只读浏览

            elif not child_perms:
                continue

            # 跳过黑名单
            if child.name in BLOCKED_NAMES or child.suffix.lower() in BLOCKED_EXTENSIONS:
                continue

            info = _stat_info(child, child_rel)
            info["permissions"] = child_perms
            entries.append(info)
    except PermissionError:
        raise HTTPException(status_code=403, detail="操作系统拒绝访问该目录")

    return JSONResponse(content={
        "path": normalized,
        "entries": entries,
        "total": len(entries),
    })


@router.get("/v1/workspace/read", dependencies=[Depends(rate_limit_dependency)])
async def workspace_read(
    request: Request,
    path: str = Query(..., description="文件相对路径"),
    token: str = Depends(verify_admin_api_key),
):
    """
    读取单个文本文件的内容。
    """
    normalized = _check_permission(path, "r")
    abs_path = _resolve_path(normalized)

    if not abs_path.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    if abs_path.is_dir():
        raise HTTPException(status_code=400, detail="不能读取目录")
    if abs_path.stat().st_size > MAX_READ_SIZE:
        raise HTTPException(status_code=413, detail=f"文件过大，最大允许 {MAX_READ_SIZE // 1024 // 1024}MB")

    try:
        content = abs_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=415, detail="不是有效的 UTF-8 文本文件")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"读取文件失败: {e}")

    info = _stat_info(abs_path, normalized)
    info["content"] = content
    info["line_count"] = content.count("\n") + (1 if content and not content.endswith("\n") else 0)

    return JSONResponse(content=info)


@router.post("/v1/workspace/write", dependencies=[Depends(rate_limit_dependency)])
async def workspace_write(
    request: Request,
    body: WriteFileRequest,
    token: str = Depends(verify_admin_api_key),
):
    """
    写入文件内容。仅允许白名单中标记为可写（w）的文件。
    """
    normalized = _check_permission(body.path, "w")
    abs_path = _resolve_path(normalized)

    if abs_path.is_dir():
        raise HTTPException(status_code=400, detail="不能写入目录")

    content_bytes = body.content.encode("utf-8")
    if len(content_bytes) > MAX_WRITE_SIZE:
        raise HTTPException(status_code=413, detail=f"内容过大，最大允许 {MAX_WRITE_SIZE // 1024 // 1024}MB")

    # 备份原文件（如果存在）
    backup_content = None
    if abs_path.exists():
        try:
            backup_content = abs_path.read_text(encoding="utf-8")
        except Exception:
            pass

    try:
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(body.content, encoding="utf-8")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"写入文件失败: {e}")

    info = _stat_info(abs_path, normalized)
    info["message"] = "文件已保存"
    info["had_backup"] = backup_content is not None

    return JSONResponse(content=info)


@router.post("/v1/workspace/delete", dependencies=[Depends(rate_limit_dependency)])
async def workspace_delete(
    request: Request,
    body: DeleteFileRequest,
    token: str = Depends(verify_admin_api_key),
):
    """
    删除文件。仅允许白名单中标记为可删除（d）的路径。
    """
    normalized = _check_permission(body.path, "d")
    abs_path = _resolve_path(normalized)

    if not abs_path.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    if abs_path.is_dir():
        raise HTTPException(status_code=400, detail="不能通过此接口删除目录")

    try:
        abs_path.unlink()
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"删除文件失败: {e}")

    return JSONResponse(content={
        "path": normalized,
        "message": "文件已删除",
    })


@router.get("/v1/workspace/download", dependencies=[Depends(rate_limit_dependency)])
async def workspace_download(
    request: Request,
    path: str = Query(..., description="文件相对路径"),
    token: str = Depends(verify_admin_api_key),
):
    """
    下载文件。
    """
    normalized = _check_permission(path, "r")
    abs_path = _resolve_path(normalized)

    if not abs_path.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    if abs_path.is_dir():
        raise HTTPException(status_code=400, detail="不能下载目录")

    return FileResponse(
        path=str(abs_path),
        filename=abs_path.name,
        media_type="application/octet-stream",
    )
