"""
透传检测与轻量级修改

当入口方言与目标上游 engine 匹配时，走透传：
- 不再进行 Canonical -> Native 的二次转换
- 仍允许做轻量级字段修改（model 重命名、overrides 深度合并）
- system_prompt 等“协议/渠道相关”的注入逻辑，交由各自渠道的 passthrough_payload_adapter 处理
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Dict, Optional

from core.log_config import logger
from core.usage import extract_cache_usage
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

    # overrides 合并
    if modifications.get("overrides"):
        _apply_overrides(new_payload, modifications["overrides"], request_model, original_model)

    return new_payload




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


def parse_passthrough_usage(data: Any) -> Optional[Dict[str, int]]:
    """从透传响应中宽松提取 usage，并统一缓存字段。

    透传路径不会经过通道适配器重组，因此这里同时兼容 OpenAI、Responses、Claude 和 Gemini 的
    原始 usage 位置，目的在于让 LoggingStreamingResponse 能直接写入 current_info。
    """
    if not isinstance(data, dict):
        return None

    gemini_usage = data.get("usageMetadata")
    if isinstance(gemini_usage, dict):
        cache_usage = extract_cache_usage(gemini_usage)
        prompt = gemini_usage.get("promptTokenCount", 0) or 0
        completion = gemini_usage.get("candidatesTokenCount", 0) or 0
        total = gemini_usage.get("totalTokenCount", 0) or (prompt + completion)
        if prompt or completion or cache_usage["cached_tokens"] or cache_usage["cache_creation_tokens"]:
            return {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": total,
                **cache_usage,
            }

    usage = data.get("usage")
    if not isinstance(usage, dict):
        message = data.get("message")
        if isinstance(message, dict):
            usage = message.get("usage")
    if not isinstance(usage, dict):
        response = data.get("response")
        if isinstance(response, dict):
            usage = response.get("usage")
    if not isinstance(usage, dict):
        return None

    cache_usage = extract_cache_usage(usage)
    if "cache_read_input_tokens" in usage or "cache_creation_input_tokens" in usage:
        # Claude 原生 input_tokens 不含缓存部分；透传统计需要还原为统一 prompt_tokens 口径。
        prompt = (usage.get("input_tokens", 0) or 0) + cache_usage["cached_tokens"] + cache_usage["cache_creation_tokens"]
    else:
        prompt = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    completion = usage.get("completion_tokens") or usage.get("output_tokens") or 0
    total = usage.get("total_tokens") or (prompt + completion)

    if prompt or completion or cache_usage["cached_tokens"] or cache_usage["cache_creation_tokens"]:
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
            **cache_usage,
        }
    return None