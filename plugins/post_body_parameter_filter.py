"""请求体参数过滤插件（post_body_parameter_filter）

定位：
- 作为“请求拦截器插件”运行（不侵入 core/request.py）。
- 仅当某个渠道在 provider.preferences.enabled_plugins 显式启用本插件时生效。

配置位置：
- provider.preferences.post_body_parameter_filter
  支持 list / dict（deny/allow / all/* / model_key 等），详见 core.payload_filter 文档。

建议：
- 本插件优先级设置为 999（尽量在其他拦截器之后执行），以便在最终转发前进行兜底清理。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from core.log_config import logger
from core.payload_filter import filter_payload_parameters
from core.plugins import register_request_interceptor, unregister_request_interceptor
from core.utils import get_model_dict


PLUGIN_INFO = {
    "name": "post_body_parameter_filter",
    "version": "1.0.0",
    "description": "请求体参数过滤插件 - 按配置移除上游不支持字段，避免 unknown field/validation error",
    "author": "Zoaholic Team",
    "dependencies": [],
    "metadata": {
        "category": "interceptors",
        "tags": ["payload", "filter", "compat"],
        "params_hint": "配置在 provider.preferences.post_body_parameter_filter；插件需在 enabled_plugins 中启用。",
        "provider_config": {
            "key": "post_body_parameter_filter",
            "type": "json",
            "title": "请求体参数过滤",
            "description": "按规则移除上游渠道不支持的字段（deny/allow + dot-path），避免 unknown field / validation error。",
            "example": {
                "mode": "deny",
                "use_defaults": True,
                "deny": [
                    "thinking",
                    "min_p",
                    "top_k",
                    "stream_options.include_usage",
                ],
            },
        },
    },
}

EXTENSIONS = [
    "interceptors:post_body_parameter_filter_request",
]


def _resolve_original_model(provider: Dict[str, Any], model: Optional[str]) -> Optional[str]:
    if not model or not isinstance(provider, dict):
        return None

    # 避免依赖 provider 内部/私有缓存字段（例如 _model_dict_cache），统一走公开工具函数。
    try:
        model_dict = get_model_dict(provider)
    except Exception:
        model_dict = None

    if isinstance(model_dict, dict):
        original_model = model_dict.get(model)
        return str(original_model) if original_model else None

    return None


async def post_body_parameter_filter_request_interceptor(
    request: Any,
    engine: str,
    provider: Dict[str, Any],
    api_key: Optional[str],
    url: str,
    headers: Dict[str, Any],
    payload: Dict[str, Any],
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    if not isinstance(payload, dict) or not payload:
        return url, headers, payload

    # request 可能是 pydantic 模型，也可能是 dict-like
    req_model = None
    try:
        req_model = getattr(request, "model", None)
    except Exception:
        req_model = None

    model = payload.get("model") or req_model
    original_model = _resolve_original_model(provider, model)

    filtered = filter_payload_parameters(
        payload,
        engine=str(engine or "").lower(),
        provider=provider,
        model=str(model) if model else None,
        original_model=original_model,
    )

    logger.debug(
        f"[post_body_parameter_filter] applied. engine={engine}, model={model}, original_model={original_model}"
    )

    return url, headers, filtered


def setup(manager):
    logger.info(f"[{PLUGIN_INFO['name']}] 正在初始化...")

    register_request_interceptor(
        interceptor_id="post_body_parameter_filter_request",
        callback=post_body_parameter_filter_request_interceptor,
        priority=999,
        plugin_name=PLUGIN_INFO["name"],
        metadata={"description": "请求体参数过滤（按配置移除上游不支持字段）"},
    )

    logger.info(f"[{PLUGIN_INFO['name']}] 已注册请求拦截器")


def teardown(manager):
    logger.info(f"[{PLUGIN_INFO['name']}] 正在清理...")
    unregister_request_interceptor("post_body_parameter_filter_request")
    logger.info(f"[{PLUGIN_INFO['name']}] 已清理完成")
