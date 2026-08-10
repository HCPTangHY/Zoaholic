import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.handler import _fill_failure_provider_info


def test_fill_failure_provider_info_sets_last_attempt_provider_and_model():
    # 修改原因：错误路径过去没有补写 provider、provider_id 和 model，导致 500 日志显示“未知”或“-”。
    # 修改方式：直接测试 handler 使用的字段补全 helper，覆盖空日志上下文的失败记录。
    # 目的：保证不重试错误和重试耗尽错误都能记录最后尝试渠道与请求模型。
    current_info = {}

    _fill_failure_provider_info(current_info, "openai-backup", "gpt-4o-mini")

    assert current_info["provider"] == "openai-backup"
    assert current_info["provider_id"] == "openai-backup"
    assert current_info["model"] == "gpt-4o-mini"


def test_fill_failure_provider_info_preserves_existing_provider_id_and_model():
    # 修改原因：部分成功前置流程可能已经写入 provider_id 或 model，错误补全不应覆盖更精确的值。
    # 修改方式：传入已有字段后再次调用 helper，只允许 provider 更新为当前失败渠道。
    # 目的：避免修复 500 日志缺失信息时破坏已有的日志字段来源。
    current_info = {"provider_id": "configured-provider-id", "model": "mapped-model"}

    _fill_failure_provider_info(current_info, "openai-backup", "gpt-4o-mini")

    assert current_info["provider"] == "openai-backup"
    assert current_info["provider_id"] == "configured-provider-id"
    assert current_info["model"] == "mapped-model"


def test_fill_failure_provider_info_handles_missing_provider_name():
    # 修改原因：极端异常下 provider_name 可能为空，日志补全仍要稳定设置模型字段。
    # 修改方式：用 None 作为渠道名，断言 provider 相关字段保持空值，model 仍写入。
    # 目的：让“所有重试都失败”的兜底路径不会因空渠道名再次抛错。
    current_info = {}

    _fill_failure_provider_info(current_info, None, "gpt-4o-mini")

    assert current_info["provider"] is None
    assert current_info["provider_id"] is None
    assert current_info["model"] == "gpt-4o-mini"
