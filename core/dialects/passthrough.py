"""
透传检测与轻量级修改

当入口方言与目标上游 engine 匹配时，走透传：
- 不再进行 Canonical -> Native 的二次转换
- 仍允许做轻量级字段修改（model 重命名、system_prompt 注入、overrides 深度合并）
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Dict, Optional

from core.log_config import logger
from core.utils import safe_get


@dataclass
class PassthroughContext:
    """透传上下文，携带原始请求信息和轻量级修改"""

    enabled: bool
    dialect_id: str
    original_payload: Dict[str, Any]
    original_headers: Dict[str, str]
    modifications: Dict[str, Any]


def detect_passthrough(dialect_id: str, target_engine_id: str) -> bool:
    """
    检测是否可透传（宽松模式）

    逻辑：
    1. 检查 dialect.target_engine 是否包含 target_engine_id (渠道 ID)
    2. 检查 dialect.target_engine 是否包含该渠道的 type_name (技术类型)
    """
    dialect = None
    try:
        from .registry import get_dialect
        dialect = get_dialect(dialect_id)
    except Exception:
        dialect = None

    if not dialect:
        return dialect_id == target_engine_id

    # 获取渠道定义以提取其 type_name
    try:
        from core.channels import get_channel
        channel = get_channel(target_engine_id)
        target_type = channel.type_name if channel else target_engine_id
    except Exception:
        target_type = target_engine_id

    expected_engines = getattr(dialect, "target_engine", None) or [dialect_id]
    if not isinstance(expected_engines, (list, tuple, set)):
        expected_engines = [expected_engines]

    # 只要渠道 ID 或 渠道类型 命中方言的目标引擎列表，即可透传
    return target_engine_id in expected_engines or target_type in expected_engines


async def evaluate_passthrough(
    dialect_id: str,
    original_payload: Dict[str, Any],
    original_headers: Dict[str, str],
    target_provider: Dict[str, Any],
    request_model: str,
) -> PassthroughContext:
    """
    评估是否可以透传（宽松模式）

    Returns:
        PassthroughContext: 透传决策 + 原始 payload/headers + modifications
    """
    target_engine = target_provider.get("engine")
    can_passthrough = detect_passthrough(dialect_id, target_engine)

    modifications: Dict[str, Any] = {}
    if can_passthrough:
        # 模型重命名（alias -> upstream）
        model_dict = target_provider.get("_model_dict_cache", {})
        if request_model in model_dict and model_dict[request_model] != request_model:
            modifications["model_rename"] = model_dict[request_model]

        # 渠道 system_prompt 注入
        system_prompt = safe_get(target_provider, "preferences", "system_prompt", default=None)
        if system_prompt:
            modifications["system_prompt"] = system_prompt

        # post_body_parameter_overrides 深度覆写
        overrides = safe_get(target_provider, "preferences", "post_body_parameter_overrides", default=None)
        if isinstance(overrides, dict) and overrides:
            modifications["overrides"] = overrides

    return PassthroughContext(
        enabled=can_passthrough,
        dialect_id=dialect_id,
        original_payload=original_payload,
        original_headers=original_headers or {},
        modifications=modifications,
    )


def apply_passthrough_modifications(
    payload: Dict[str, Any],
    modifications: Dict[str, Any],
    dialect_id: str,
    request_model: str,
    original_model: str,
) -> Dict[str, Any]:
    """
    对原始 payload 应用轻量级修改（不重构 messages 结构）

    Args:
        payload: 原始 native payload
        modifications: evaluate_passthrough 收集的修改项
        dialect_id: 入口方言
        request_model: 请求别名模型（RequestModel.model）
        original_model: 上游真实模型名
    """
    new_payload = copy.deepcopy(payload)

    # model 重命名：Gemini 的 model 在 URL 中处理，这里只处理非 Gemini
    if modifications.get("model_rename") and dialect_id != "gemini":
        new_payload["model"] = modifications["model_rename"]

    # system_prompt 注入
    if modifications.get("system_prompt"):
        _inject_system_prompt(new_payload, modifications["system_prompt"], dialect_id)

    # overrides 合并
    if modifications.get("overrides"):
        _apply_overrides(new_payload, modifications["overrides"], request_model, original_model)

    return new_payload


def _inject_system_prompt(payload: Dict[str, Any], system_prompt: str, dialect_id: str) -> None:
    """按不同方言把 system_prompt 注入到 payload 对应位置"""
    system_prompt_text = str(system_prompt).strip()
    if not system_prompt_text:
        return

    if dialect_id == "openai":
        messages = payload.get("messages")
        if isinstance(messages, list):
            for msg in messages:
                if isinstance(msg, dict) and msg.get("role") == "system":
                    content = msg.get("content") or ""
                    msg["content"] = f"{system_prompt_text}\n\n{content}" if content else system_prompt_text
                    return
            # 无 system 消息则插入
            messages.insert(0, {"role": "system", "content": system_prompt_text})
        return

    if dialect_id == "gemini":
        sys_inst = payload.get("systemInstruction")
        if isinstance(sys_inst, dict):
            parts = sys_inst.get("parts") or []
            if parts and isinstance(parts, list) and isinstance(parts[0], dict):
                old = parts[0].get("text") or ""
                parts[0]["text"] = f"{system_prompt_text}\n\n{old}" if old else system_prompt_text
            else:
                sys_inst["parts"] = [{"text": system_prompt_text}]
        else:
            payload["systemInstruction"] = {"parts": [{"text": system_prompt_text}]}
        return

    if dialect_id == "claude":
        old_system = payload.get("system")
        if isinstance(old_system, str):
            payload["system"] = f"{system_prompt_text}\n\n{old_system}" if old_system else system_prompt_text
        elif old_system is None:
            payload["system"] = system_prompt_text
        return


def _apply_overrides(
    payload: Dict[str, Any],
    overrides: Dict[str, Any],
    request_model: str,
    original_model: str,
) -> None:
    """复用 get_payload 中的 overrides 语义，对 native payload 做深度覆写"""

    def _deep_merge(target: Any, override: Any) -> Any:
        if isinstance(target, dict) and isinstance(override, dict):
            for _k, _v in override.items():
                if isinstance(_v, dict) and isinstance(target.get(_k), dict):
                    _deep_merge(target[_k], _v)
                else:
                    target[_k] = _v
        else:
            return override
        return target

    # 全局 all / * 覆写
    for global_key in ("all", "*"):
        global_override = safe_get(overrides, global_key, default=None)
        if isinstance(global_override, dict):
            _deep_merge(payload, global_override)

    # 模型别名和原始模型名覆写
    for model_key in {request_model, original_model}:
        model_override = safe_get(overrides, model_key, default=None)
        if isinstance(model_override, dict):
            _deep_merge(payload, model_override)

    # 其余键作为顶层字段覆写（保持与旧逻辑兼容）
    for key, value in overrides.items():
        if key in ("all", "*", request_model, original_model):
            continue
        if "-" not in key and " " not in key:
            if key in payload and isinstance(payload.get(key), dict) and isinstance(value, dict):
                _deep_merge(payload[key], value)
            else:
                payload[key] = value