"""routes.system 源码目录与同步目标的回归测试。

背景：_get_source_dir 不能猜测服务器上的固定路径或向上查找无关仓库；
_update_git 只有在显式配置 ZOAHOLIC_PROD_DIR 时才同步到其他目录。
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import routes.system as system


PROJECT_ROOT = str(Path(system.__file__).resolve().parent.parent)


def test_source_dir_defaults_to_project_root(monkeypatch):
    monkeypatch.delenv("ZOAHOLIC_SOURCE_DIR", raising=False)
    assert system._get_source_dir() == PROJECT_ROOT


def test_source_dir_uses_env_when_it_is_a_git_repo(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("ZOAHOLIC_SOURCE_DIR", str(tmp_path))
    assert system._get_source_dir() == str(tmp_path)


def test_source_dir_ignores_env_without_git(monkeypatch, tmp_path):
    monkeypatch.setenv("ZOAHOLIC_SOURCE_DIR", str(tmp_path))
    assert system._get_source_dir() == PROJECT_ROOT


def _run_update_git(monkeypatch, source_dir: Path):
    synced = []

    def fake_run(cmd, *args, **kwargs):
        stdout = "" if isinstance(cmd, list) and cmd[:2] == ["git", "diff"] else "ok"
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    async def fake_sync(src, dst, exclude=None):
        synced.append((src, dst))
        return True

    async def fake_restart(new_version):
        return None

    monkeypatch.setenv("ZOAHOLIC_SOURCE_DIR", str(source_dir))
    monkeypatch.setattr(system.subprocess, "run", fake_run)
    monkeypatch.setattr(system, "_sync_dirs", fake_sync)
    monkeypatch.setattr(system, "_schedule_restart", fake_restart)
    steps = []
    asyncio.run(system._update_git(steps))
    return synced, steps


def test_update_git_does_not_sync_without_prod_dir(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    monkeypatch.delenv("ZOAHOLIC_PROD_DIR", raising=False)

    synced, steps = _run_update_git(monkeypatch, tmp_path)

    assert synced == []
    assert {"step": "sync_backend", "success": True, "output": "same dir, skipped"} in steps


def test_update_git_syncs_to_configured_prod_dir(monkeypatch, tmp_path):
    source = tmp_path / "src"
    prod = tmp_path / "prod"
    (source / ".git").mkdir(parents=True)
    prod.mkdir()
    monkeypatch.setenv("ZOAHOLIC_PROD_DIR", str(prod))

    synced, _ = _run_update_git(monkeypatch, source)

    assert synced == [(str(source), str(prod))]
