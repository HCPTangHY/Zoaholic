import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import utils


def _write_yaml(path: Path, data: dict) -> None:
    """写入测试用 YAML 配置。"""
    # 修改原因：save_api_yaml 的安全保护必须读取磁盘上的旧配置，不能只测内存对象。
    # 修改方式：用项目真实 yaml helper 写入临时 api.yaml。
    # 目的：让测试覆盖生产函数读取现有文件后的判断逻辑。
    with path.open("w", encoding="utf-8") as file:
        utils.yaml.dump(data, file)


def _read_yaml(path: Path) -> dict:
    """读取测试用 YAML 配置。"""
    # 修改原因：断言必须检查文件内容是否被覆盖，而不是只检查函数返回值。
    # 修改方式：从临时 api.yaml 重新读取并解析 YAML。
    # 目的：确认空 providers 写入被阻止时，原文件完整保留。
    with path.open("r", encoding="utf-8") as file:
        return utils.yaml.load(file)


def test_save_api_yaml_refuses_to_overwrite_existing_providers_with_empty_list(tmp_path, monkeypatch):
    """已有渠道时，新配置 providers 为空应拒绝覆盖 api.yaml。"""
    target = tmp_path / "api.yaml"
    existing_config = {
        "providers": [
            {
                "provider": "stable",
                "engine": "openai",
                "api": "sk-test",
                "model": ["gpt-4"],
            }
        ],
        "api_keys": [{"api": "zk-test", "model": ["all"]}],
        "preferences": {},
    }
    _write_yaml(target, existing_config)
    monkeypatch.setattr(utils, "API_YAML_PATH", str(target))

    utils.save_api_yaml({"providers": [], "api_keys": [], "preferences": {}})

    # 修改原因：手动编辑 YAML 解析异常后，规范化得到的空 providers 不能覆盖原始正确配置。
    # 修改方式：把 API_YAML_PATH 指向临时文件，调用真实 save_api_yaml 后重新读取磁盘内容。
    # 目的：确保启动或同步配置时不会把已有渠道删光。
    assert _read_yaml(target) == existing_config


def test_save_api_yaml_allows_empty_providers_when_existing_file_has_no_providers(tmp_path, monkeypatch):
    """旧文件本来没有渠道时，空 providers 写入不应被安全检查误拦截。"""
    target = tmp_path / "api.yaml"
    _write_yaml(target, {"providers": [], "api_keys": [], "preferences": {"old": True}})
    monkeypatch.setattr(utils, "API_YAML_PATH", str(target))

    new_config = {"providers": [], "api_keys": [], "preferences": {"new": True}}
    utils.save_api_yaml(new_config)

    # 修改原因：安全保护只针对“从有渠道变成零渠道”的异常场景，不能阻止合法空配置保存。
    # 修改方式：用旧 providers 为空的临时文件调用真实保存逻辑。
    # 目的：保持首次初始化或显式清空空配置文件时的兼容性。
    assert _read_yaml(target) == new_config
