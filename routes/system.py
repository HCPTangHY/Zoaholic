"""
系统管理路由 — 版本检查与自动更新

支持三种部署方式：
- git: Python 裸奔 + git repo（git pull + sync + restart）
- docker: Docker 容器（检测新镜像，提供 pull 命令或自动更新）
- pip: pip install（pip install --upgrade）
"""

import os
import subprocess
import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from routes.deps import rate_limit_dependency, verify_admin_api_key, get_app
from core.log_config import logger

router = APIRouter()

# GitHub 仓库信息
GITHUB_REPO = os.getenv("GITHUB_REPO", "HCPTangHY/Zoaholic")
GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}"
GHCR_IMAGE = os.getenv("GHCR_IMAGE", f"ghcr.io/{GITHUB_REPO.lower()}")
PYPI_PACKAGE = os.getenv("PYPI_PACKAGE", "zoaholic")


# ─── 部署类型检测 ───

def _detect_deploy_type() -> str:
    """检测当前部署方式: docker / git / pip / unknown"""
    # Docker 检测
    if os.path.exists("/.dockerenv"):
        return "docker"
    try:
        with open("/proc/1/cgroup", "r") as f:
            if "docker" in f.read() or "containerd" in f.read():
                return "docker"
    except Exception:
        pass
    if os.getenv("DOCKER_CONTAINER") or os.getenv("container"):
        return "docker"

    # Git 检测
    source = _get_source_dir()
    if os.path.isdir(os.path.join(source, ".git")):
        return "git"

    # Pip 检测
    try:
        import importlib.metadata
        importlib.metadata.version(PYPI_PACKAGE)
        return "pip"
    except Exception:
        pass

    return "unknown"


def _get_source_dir() -> str:
    """获取源码 git 目录"""
    env_dir = os.getenv("ZOAHOLIC_SOURCE_DIR")
    if env_dir and os.path.isdir(os.path.join(env_dir, ".git")):
        return env_dir
    d = Path(__file__).resolve().parent.parent
    for _ in range(5):
        if (d / ".git").is_dir():
            return str(d)
        d = d.parent
    for known in ["/www/wwwroot/zoaholic_original", "/opt/zoaholic"]:
        if os.path.isdir(os.path.join(known, ".git")):
            return known
    return str(Path(__file__).resolve().parent.parent)


def _get_current_version(app=None) -> str:
    if app:
        v = getattr(app.state, "version", None)
        if v:
            return v
    try:
        import tomllib
        pyproject = Path(_get_source_dir()) / "pyproject.toml"
        with open(pyproject, "rb") as f:
            return tomllib.load(f)["project"]["version"]
    except Exception:
        pass
    try:
        import importlib.metadata
        return importlib.metadata.version(PYPI_PACKAGE)
    except Exception:
        return "unknown"


def _get_git_info() -> dict:
    try:
        cwd = _get_source_dir()
        if not os.path.isdir(os.path.join(cwd, ".git")):
            return {}
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=cwd, timeout=5
        ).stdout.strip()
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, cwd=cwd, timeout=5
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, cwd=cwd, timeout=5
        ).stdout.strip()
        return {"commit": commit, "branch": branch, "dirty": bool(dirty)}
    except Exception as e:
        return {"error": str(e)}


def _compare_versions(current: str, latest: str) -> bool:
    def parse(v: str):
        try:
            return tuple(int(x) for x in v.lstrip("v").split("."))
        except (ValueError, AttributeError):
            return (0, 0, 0)
    return parse(latest) > parse(current)


# ─── GitHub / Registry 查询 ───

async def _fetch_latest_release() -> dict | None:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{GITHUB_API}/releases/latest",
                headers={"Accept": "application/vnd.github.v3+json"},
            )
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "version": data.get("tag_name", "").lstrip("v"),
                    "tag": data.get("tag_name", ""),
                    "name": data.get("name", ""),
                    "body": data.get("body", ""),
                    "published_at": data.get("published_at", ""),
                    "html_url": data.get("html_url", ""),
                }
            # fallback: 最新 tag
            resp = await client.get(
                f"{GITHUB_API}/tags?per_page=1",
                headers={"Accept": "application/vnd.github.v3+json"},
            )
            if resp.status_code == 200:
                tags = resp.json()
                if tags:
                    tag_name = tags[0].get("name", "")
                    return {
                        "version": tag_name.lstrip("v"),
                        "tag": tag_name,
                        "name": tag_name,
                        "body": "",
                        "published_at": "",
                        "html_url": f"https://github.com/{GITHUB_REPO}/releases/tag/{tag_name}",
                    }
    except Exception as e:
        logger.warning(f"[system] Failed to fetch latest release: {e}")
    return None


async def _fetch_latest_docker_tag() -> dict | None:
    """查询 GHCR 最新镜像 tag"""
    import httpx
    try:
        # GHCR 用 GitHub API 查 package versions
        owner, repo = GITHUB_REPO.split("/")
        url = f"https://api.github.com/orgs/{owner}/packages/container/{repo.lower()}/versions"
        async with httpx.AsyncClient(timeout=10) as client:
            # 先试 org
            resp = await client.get(
                url, headers={"Accept": "application/vnd.github.v3+json"}, params={"per_page": 5}
            )
            if resp.status_code != 200:
                # fallback: user endpoint
                url = f"https://api.github.com/users/{owner}/packages/container/{repo.lower()}/versions"
                resp = await client.get(
                    url, headers={"Accept": "application/vnd.github.v3+json"}, params={"per_page": 5}
                )
            if resp.status_code == 200:
                versions = resp.json()
                for v in versions:
                    tags = v.get("metadata", {}).get("container", {}).get("tags", [])
                    # 找带版本号的 tag（跳过 latest/sha）
                    for t in tags:
                        if t.startswith("v") or any(c.isdigit() for c in t.split(".")):
                            return {
                                "tag": t,
                                "version": t.lstrip("v"),
                                "digest": v.get("name", ""),
                                "updated_at": v.get("updated_at", ""),
                                "image": f"{GHCR_IMAGE}:{t}",
                            }
                # 没找到版本 tag，用 latest
                if versions:
                    tags = versions[0].get("metadata", {}).get("container", {}).get("tags", [])
                    return {
                        "tag": tags[0] if tags else "latest",
                        "version": "",
                        "digest": versions[0].get("name", ""),
                        "updated_at": versions[0].get("updated_at", ""),
                        "image": f"{GHCR_IMAGE}:latest",
                    }
    except Exception as e:
        logger.warning(f"[system] Failed to fetch Docker tag: {e}")
    return None


async def _fetch_pypi_version() -> dict | None:
    """查询 PyPI 最新版本"""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"https://pypi.org/pypi/{PYPI_PACKAGE}/json")
            if resp.status_code == 200:
                data = resp.json()
                version = data.get("info", {}).get("version", "")
                return {
                    "version": version,
                    "summary": data.get("info", {}).get("summary", ""),
                    "html_url": data.get("info", {}).get("project_url", f"https://pypi.org/project/{PYPI_PACKAGE}/"),
                }
    except Exception as e:
        logger.warning(f"[system] Failed to fetch PyPI version: {e}")
    return None


# ─── Docker 环境信息 ───

def _get_docker_info() -> dict:
    """获取容器内能拿到的 Docker 信息"""
    info = {}
    # 当前镜像（通常通过环境变量注入）
    info["image"] = os.getenv("DOCKER_IMAGE", "")
    info["container_id"] = ""
    try:
        with open("/proc/self/cgroup", "r") as f:
            for line in f:
                if "docker" in line:
                    parts = line.strip().split("/")
                    if parts:
                        info["container_id"] = parts[-1][:12]
                    break
    except Exception:
        pass
    # 检查 docker.sock 是否可用
    info["socket_available"] = os.path.exists("/var/run/docker.sock")
    return info


# ─── API 端点 ───

@router.get("/v1/system/version", dependencies=[Depends(rate_limit_dependency)])
async def get_version(api_index: int = Depends(verify_admin_api_key)):
    app = get_app()
    deploy_type = _detect_deploy_type()
    result = {
        "version": _get_current_version(app),
        "deploy_type": deploy_type,
    }
    if deploy_type == "git":
        result["git"] = _get_git_info()
    elif deploy_type == "docker":
        result["docker"] = _get_docker_info()
    return JSONResponse(content=result)


@router.get("/v1/system/check-update", dependencies=[Depends(rate_limit_dependency)])
async def check_update(api_index: int = Depends(verify_admin_api_key)):
    app = get_app()
    current = _get_current_version(app)
    deploy_type = _detect_deploy_type()

    result = {
        "current_version": current,
        "deploy_type": deploy_type,
        "has_update": False,
        "update_instructions": None,
    }

    if deploy_type == "git":
        result["git"] = _get_git_info()
        latest = await _fetch_latest_release()
        result["latest_release"] = latest
        if latest:
            result["has_update"] = _compare_versions(current, latest["version"])
        # 检查远程 commit
        remote_commits = []
        try:
            cwd = _get_source_dir()
            await asyncio.to_thread(
                subprocess.run,
                ["git", "fetch", "origin", "main", "--quiet"],
                capture_output=True, cwd=cwd, timeout=15
            )
            r = await asyncio.to_thread(
                subprocess.run,
                ["git", "log", "HEAD..origin/main", "--oneline", "--no-decorate"],
                capture_output=True, text=True, cwd=cwd, timeout=5
            )
            if r.stdout.strip():
                remote_commits = r.stdout.strip().split("\n")
        except Exception:
            pass
        result["pending_commits"] = remote_commits
        result["pending_count"] = len(remote_commits)
        if remote_commits:
            result["has_update"] = True

    elif deploy_type == "docker":
        result["docker"] = _get_docker_info()
        # 从 GitHub release 获取最新版本（Docker tag 跟 release 同步）
        latest = await _fetch_latest_release()
        result["latest_release"] = latest
        docker_tag = await _fetch_latest_docker_tag()
        result["latest_docker"] = docker_tag
        if latest:
            result["has_update"] = _compare_versions(current, latest["version"])
        # 生成更新指令
        tag = docker_tag["tag"] if docker_tag else "latest"
        result["update_instructions"] = {
            "type": "docker",
            "commands": [
                f"docker pull {GHCR_IMAGE}:{tag}",
                "docker compose up -d  # 或 docker stop/rm/run",
            ],
            "compose_image": f"{GHCR_IMAGE}:{tag}",
            "can_auto_update": _get_docker_info().get("socket_available", False),
        }

    elif deploy_type == "pip":
        pypi = await _fetch_pypi_version()
        result["latest_pypi"] = pypi
        if pypi:
            result["has_update"] = _compare_versions(current, pypi["version"])
        result["update_instructions"] = {
            "type": "pip",
            "commands": [
                f"pip install --upgrade {PYPI_PACKAGE}",
                "# 重启服务",
            ],
            "can_auto_update": True,
        }

    else:
        # unknown
        latest = await _fetch_latest_release()
        result["latest_release"] = latest
        if latest:
            result["has_update"] = _compare_versions(current, latest["version"])

    return JSONResponse(content=result)


@router.post("/v1/system/update", dependencies=[Depends(rate_limit_dependency)])
async def perform_update(
    api_index: int = Depends(verify_admin_api_key),
):
    deploy_type = _detect_deploy_type()
    steps = []

    try:
        if deploy_type == "git":
            return await _update_git(steps)
        elif deploy_type == "docker":
            return await _update_docker(steps)
        elif deploy_type == "pip":
            return await _update_pip(steps)
        else:
            return JSONResponse(status_code=400, content={
                "error": f"不支持自动更新: deploy_type={deploy_type}",
                "deploy_type": deploy_type,
            })
    except Exception as e:
        logger.error(f"[system] Update failed: {e}")
        steps.append({"step": "error", "success": False, "output": str(e)})
        return JSONResponse(status_code=500, content={"error": str(e), "steps": steps})


# ─── 更新执行器 ───

async def _update_git(steps: list) -> JSONResponse:
    """Git 部署更新：pull + sync + build + restart"""
    cwd = _get_source_dir()
    prod_dir = os.getenv("ZOAHOLIC_PROD_DIR", "/www/wwwroot/Zoaholic")

    # Step 1: git pull
    result = await asyncio.to_thread(
        subprocess.run,
        ["git", "pull", "origin", "main", "--ff-only"],
        capture_output=True, text=True, cwd=cwd, timeout=30
    )
    steps.append({"step": "git_pull", "success": result.returncode == 0,
                  "output": result.stdout.strip() or result.stderr.strip()})
    if result.returncode != 0:
        return JSONResponse(status_code=500, content={"error": "git pull failed", "steps": steps})

    # Step 2: 读新版本
    new_version = _read_version_from_pyproject(cwd)
    steps.append({"step": "read_version", "success": True, "version": new_version})

    # Step 3: 同步后端
    if os.path.isdir(prod_dir) and os.path.realpath(cwd) != os.path.realpath(prod_dir):
        sync_ok = await _sync_dirs(cwd, prod_dir, exclude=[".git", "node_modules", "frontend", "__pycache__", "*.pyc", "data"])
        steps.append({"step": "sync_backend", "success": sync_ok})
    else:
        steps.append({"step": "sync_backend", "success": True, "output": "same dir, skipped"})

    # Step 4: 前端 build（如有变更）
    frontend_changed = False
    try:
        r = await asyncio.to_thread(
            subprocess.run,
            ["git", "diff", "HEAD~1", "--name-only", "--", "frontend/"],
            capture_output=True, text=True, cwd=cwd, timeout=5
        )
        frontend_changed = bool(r.stdout.strip())
    except Exception:
        pass

    if frontend_changed:
        frontend_dir = os.path.join(cwd, "frontend")
        if os.path.isdir(frontend_dir):
            build_result = await asyncio.to_thread(
                subprocess.run,
                "npm install --prefer-offline && npx vite build",
                shell=True, capture_output=True, text=True,
                cwd=frontend_dir, timeout=120
            )
            steps.append({"step": "frontend_build", "success": build_result.returncode == 0,
                          "output": (build_result.stdout or build_result.stderr)[-500:]})
            if build_result.returncode == 0:
                static_src = os.path.join(cwd, "static")
                static_dst = os.path.join(prod_dir, "static")
                if os.path.isdir(static_src):
                    sync_ok = await _sync_dirs(static_src, static_dst)
                    steps.append({"step": "sync_frontend", "success": sync_ok})
    else:
        steps.append({"step": "frontend_build", "success": True, "output": "no changes, skipped"})

    # Step 5: 重启
    await _schedule_restart(new_version)
    steps.append({"step": "restart_scheduled", "success": True})

    return JSONResponse(content={"success": True, "new_version": new_version, "steps": steps, "deploy_type": "git"})


async def _update_docker(steps: list) -> JSONResponse:
    """Docker 部署更新"""
    docker_info = _get_docker_info()

    if not docker_info.get("socket_available"):
        # 没有 docker.sock，只能给指令
        latest = await _fetch_latest_release()
        tag = latest["tag"] if latest else "latest"
        return JSONResponse(content={
            "success": False,
            "error": "auto_update_unavailable",
            "message": "容器未挂载 docker.sock，无法自动更新",
            "manual_commands": [
                f"docker pull {GHCR_IMAGE}:{tag}",
                "docker compose up -d",
            ],
            "deploy_type": "docker",
        })

    # 有 docker.sock，尝试自动 pull + recreate
    latest = await _fetch_latest_release()
    tag = latest["tag"] if latest else "latest"
    image = f"{GHCR_IMAGE}:{tag}"

    # Pull 新镜像
    result = await asyncio.to_thread(
        subprocess.run,
        ["docker", "pull", image],
        capture_output=True, text=True, timeout=300
    )
    steps.append({"step": "docker_pull", "success": result.returncode == 0,
                  "output": result.stdout.strip()[-500:] or result.stderr.strip()[-500:]})
    if result.returncode != 0:
        return JSONResponse(status_code=500, content={"error": "docker pull failed", "steps": steps})

    new_version = latest["version"] if latest else "unknown"
    steps.append({"step": "image_ready", "success": True, "image": image, "version": new_version})

    # 尝试用 docker compose 重建
    compose_file = None
    for name in ["docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"]:
        if os.path.exists(f"/{name}") or os.path.exists(f"/app/{name}"):
            compose_file = name
            break

    if compose_file:
        # compose recreate（延迟执行，让响应先返回）
        async def _delayed_compose():
            await asyncio.sleep(2)
            logger.info(f"[system] Docker compose recreate with {image}...")
            subprocess.run(
                ["docker", "compose", "up", "-d", "--force-recreate"],
                capture_output=True, timeout=120
            )
        asyncio.create_task(_delayed_compose())
        steps.append({"step": "compose_recreate_scheduled", "success": True})
    else:
        # 没有 compose，给手动指令
        steps.append({"step": "compose_not_found", "success": True,
                      "output": f"Image pulled. Run: docker compose up -d"})

    return JSONResponse(content={"success": True, "new_version": new_version, "steps": steps, "deploy_type": "docker"})


async def _update_pip(steps: list) -> JSONResponse:
    """Pip 部署更新"""
    # pip install --upgrade
    result = await asyncio.to_thread(
        subprocess.run,
        ["pip", "install", "--upgrade", PYPI_PACKAGE],
        capture_output=True, text=True, timeout=120
    )
    steps.append({"step": "pip_upgrade", "success": result.returncode == 0,
                  "output": result.stdout.strip()[-500:] or result.stderr.strip()[-500:]})
    if result.returncode != 0:
        return JSONResponse(status_code=500, content={"error": "pip upgrade failed", "steps": steps})

    # 读新版本
    new_version = "unknown"
    try:
        import importlib.metadata
        importlib.metadata.invalidate_caches()
        # 需要重新导入才能读到新版本
        r = await asyncio.to_thread(
            subprocess.run,
            ["python", "-c", f"import importlib.metadata; print(importlib.metadata.version('{PYPI_PACKAGE}'))"],
            capture_output=True, text=True, timeout=10
        )
        new_version = r.stdout.strip() or "unknown"
    except Exception:
        pass
    steps.append({"step": "read_version", "success": True, "version": new_version})

    # 重启
    await _schedule_restart(new_version)
    steps.append({"step": "restart_scheduled", "success": True})

    return JSONResponse(content={"success": True, "new_version": new_version, "steps": steps, "deploy_type": "pip"})


# ─── 工具函数 ───

def _read_version_from_pyproject(cwd: str) -> str:
    try:
        import tomllib
        with open(Path(cwd) / "pyproject.toml", "rb") as f:
            return tomllib.load(f)["project"]["version"]
    except Exception:
        return "unknown"


async def _sync_dirs(src: str, dst: str, exclude: list[str] | None = None) -> bool:
    """同步目录，优先 rsync，fallback cp"""
    src = src.rstrip("/") + "/"
    dst = dst.rstrip("/") + "/"

    # 尝试 rsync
    try:
        exclude_args = ""
        if exclude:
            exclude_args = " ".join(f"--exclude='{e}'" for e in exclude)
        cmd = f"rsync -a {exclude_args} {src} {dst}"
        result = await asyncio.to_thread(
            subprocess.run, cmd, shell=True,
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            return True
    except Exception:
        pass

    # fallback: cp（不支持 exclude，直接全量覆盖）
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            f"cp -r {src}* {dst}",
            shell=True, capture_output=True, text=True, timeout=30
        )
        return result.returncode == 0
    except Exception:
        return False


async def _schedule_restart(new_version: str):
    """延迟 2 秒重启，让 HTTP 响应先返回"""
    async def _do():
        await asyncio.sleep(2)
        logger.info(f"[system] Auto-update to {new_version}, restarting...")
        # 尝试 pm2
        try:
            r = subprocess.run(["pm2", "restart", "zoaholic"], capture_output=True, timeout=10)
            if r.returncode == 0:
                return
        except Exception:
            pass
        # 尝试 systemctl
        try:
            r = subprocess.run(["systemctl", "restart", "zoaholic"], capture_output=True, timeout=10)
            if r.returncode == 0:
                return
        except Exception:
            pass
        # fallback: 退出让进程管理器重启
        os._exit(0)
    asyncio.create_task(_do())
