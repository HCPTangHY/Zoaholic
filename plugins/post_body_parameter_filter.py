"""请求体参数过滤插件（post_body_parameter_filter）

定位：
- 作为“请求拦截器插件”运行（不侵入 core/request.py）。
- 仅当某个渠道在 provider.preferences.enabled_plugins 显式启用本插件时生效。

配置方式（两种，可同时使用，最终会合并）：

1) 声明式 UI（推荐，新版）——通过 enabled_plugins 的 key=value 参数
   - 在渠道流水线 / 插件配置面板里直接用可视化控件填写：
       mode         过滤模式（deny / allow）
       use_defaults 是否叠加内置默认过滤（开关）
       deny         需要移除的字段（每行一个，支持 dot-path，如 stream_options.include_usage）
       allow        allow 模式下需要保留的字段（每行一个）
   - 对应 enabled_plugins 字符串形如：
       post_body_parameter_filter:mode=deny,use_defaults=true,deny=thinking\ntop_k

2) 渠道级 JSON（高级 / 兼容旧配置）——provider.preferences.post_body_parameter_filter
   - 支持 list / dict（deny/allow / all/* / model_key 等），详见 core.payload_filter 文档。

合并规则：
- 两处配置会被合并：deny / allow 取并集；mode / use_defaults 以“声明式 UI 的 options”为优先，
  未在 options 中显式设置时回退到 JSON 配置，再回退到默认值。

建议：
- 本插件优先级设置为 999（尽量在其他拦截器之后执行），以便在最终转发前进行兜底清理。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from core.log_config import logger
from core.payload_filter import filter_payload_parameters
from core.plugins import (
    get_plugin_options,
    register_request_interceptor,
    unregister_request_interceptor,
)
from core.utils import get_model_dict


PLUGIN_INFO = {
    "name": "post_body_parameter_filter",
    "version": "1.1.0",
    "description": "请求体参数过滤插件 - 按配置移除上游不支持字段，避免 unknown field/validation error",
    "author": "Zoaholic Team",
    "dependencies": [],
    "metadata": {
        "category": "interceptors",
        "tags": ["payload", "filter", "compat"],
        "params_hint": (
            "可视化配置过滤模式与字段；或在下方渠道配置里写完整 JSON。"
            "deny/allow 每行一个字段，支持 dot-path（如 stream_options.include_usage）。"
        ),
        # 修改原因：旧版插件仅提供 provider_config（type=json），前端在流水线里没有声明式控件，
        #           只能回退成一个自由文本输入框（compact 模式下呈现为蓝色输入框）。
        # 修改方式：新增 metadata.params_schema，让前端 PluginParamsForm 渲染下拉/开关/文本域，
        #           并通过 serialize=key_value 写回 enabled_plugins 的 key=value 参数。
        # 目的：与 error_mask / image_filter / key_guard 等已适配插件保持一致的声明式 UI 体验。
        "params_schema": [
            {
                "key": "mode",
                "label": "过滤模式",
                "type": "select",
                "options": [
                    {"value": "deny", "label": "deny（移除黑名单字段，默认）"},
                    {"value": "allow", "label": "allow（仅保留白名单字段）"},
                ],
                "default": "deny",
                "serialize": "key_value",
            },
            {
                "key": "use_defaults",
                "label": "叠加内置默认过滤",
                "type": "toggle",
                "default": True,
                "serialize": "key_value",
            },
            {
                "key": "deny",
                "label": "移除字段（每行一个）",
                "type": "textarea",
                "default": "",
                "placeholder": "thinking\nmin_p\ntop_k\nstream_options.include_usage",
                "serialize": "key_value",
                "visible_when": {"mode": "deny"},
            },
            {
                "key": "allow",
                "label": "保留字段（每行一个）",
                "type": "textarea",
                "default": "",
                "placeholder": "temperature\ntop_p\nmax_tokens",
                "serialize": "key_value",
                "visible_when": {"mode": "allow"},
            },
        ],
        # 保留渠道级 JSON 配置入口，兼容旧配置与更复杂的按模型分组写法。
        "provider_config": {
            "key": "post_body_parameter_filter",
            "type": "json",
            "title": "请求体参数过滤（高级 JSON）",
            "description": (
                "按规则移除上游渠道不支持的字段（deny/allow + dot-path），避免 unknown field / validation error。"
                "支持按 all/* 与模型名分组。与上方可视化参数会自动合并。"
            ),
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


def _split_lines(value: Any) -> List[str]:
    """把 textarea / 列表 / 逗号字符串归一化为字段列表。

    - 声明式 UI 的 textarea 值以换行分隔；
    - 兼容用逗号分隔的写法；
    - 去除空白并保持顺序去重。
    """
    if value is None:
        return []

    if isinstance(value, (list, tuple, set)):
        raw_items: List[str] = []
        for item in value:
            raw_items.extend(_split_lines(item))
        return raw_items

    text = str(value)
    # 先按行拆，再对每行按逗号拆，最大化兼容不同书写习惯。
    items: List[str] = []
    for line in text.splitlines():
        for part in line.split(","):
            token = part.strip()
            if token:
                items.append(token)
    return items


def _parse_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default


def _parse_key_value_options(options: Optional[str]) -> Dict[str, str]:
    """解析 enabled_plugins 中 post_body_parameter_filter 的 key=value 字符串。

    形如：mode=deny,use_defaults=true,deny=thinking\\ntop_k
    注意：deny/allow 的值内部可能包含换行（textarea），只按顶层逗号+key 边界拆分。
    """
    result: Dict[str, str] = {}
    text = str(options or "").strip()
    if not text:
        return result

    known_keys = ("mode", "use_defaults", "deny", "allow")

    # 逐个逗号分段，遇到 "key=" 才视为新字段的开始，否则并入上一个字段的值
    # （这样 deny 里的换行值即使被误拆也能尽量恢复；不过前端序列化不会在值里放逗号）。
    current_key: Optional[str] = None
    for raw_part in text.split(","):
        part = raw_part
        stripped = part.strip()
        matched_key = None
        if "=" in stripped:
            candidate = stripped.split("=", 1)[0].strip()
            if candidate in known_keys:
                matched_key = candidate

        if matched_key is not None:
            key, value = stripped.split("=", 1)
            current_key = matched_key
            result[current_key] = value.strip()
        elif current_key is not None:
            # 属于上一个字段值的续接（值中包含逗号的极端情况）
            result[current_key] = f"{result[current_key]},{stripped}"

    return result


def _build_options_cfg(options: Optional[str]) -> Optional[Dict[str, Any]]:
    """把 enabled_plugins 的 key=value options 转成结构化过滤配置。"""
    kv = _parse_key_value_options(options)
    if not kv:
        return None

    cfg: Dict[str, Any] = {}

    if "mode" in kv and kv["mode"]:
        mode = kv["mode"].strip().lower()
        if mode in ("deny", "allow"):
            cfg["mode"] = mode

    if "use_defaults" in kv and kv["use_defaults"] != "":
        cfg["use_defaults"] = _parse_bool(kv["use_defaults"], default=True)

    deny = _split_lines(kv.get("deny"))
    if deny:
        cfg["deny"] = deny

    allow = _split_lines(kv.get("allow"))
    if allow:
        cfg["allow"] = allow

    return cfg or None


def _merge_into_provider(
    provider: Dict[str, Any],
    options_cfg: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """把 options 解析出的配置合并进 provider.preferences.post_body_parameter_filter。

    - 不修改原 provider，返回浅拷贝（core.payload_filter 只读 provider）。
    - 与已有 JSON 配置合并：deny/allow 取并集；mode/use_defaults 以 options 优先。
    - 若 options 未提供配置，则原样返回 provider（保持旧行为）。
    """
    if not options_cfg:
        return provider

    if not isinstance(provider, dict):
        return provider

    prefs = provider.get("preferences")
    base_prefs: Dict[str, Any] = dict(prefs) if isinstance(prefs, dict) else {}
    existing = base_prefs.get("post_body_parameter_filter")

    # 归一化已有配置：list 视为 deny 列表；dict 直接使用；其他忽略。
    merged: Dict[str, Any] = {}
    if isinstance(existing, list):
        merged = {"deny": list(existing)}
    elif isinstance(existing, dict):
        # 仅当是“扁平结构化配置”时才在这里合并；按模型分组（all/*/model_key）的
        # 复杂结构交给 core.payload_filter 自行处理，此处不注入 options（避免破坏分组语义）。
        if any(k in existing for k in ("deny", "allow", "mode", "enabled", "use_defaults")):
            merged = dict(existing)
        else:
            # 分组结构：保持原样，不与 options 合并（options 作为兜底另行处理）。
            logger.debug(
                "[post_body_parameter_filter] 检测到按模型分组的 JSON 配置，"
                "本次不与 options 合并，直接沿用 JSON。"
            )
            return provider

    # 合并 deny / allow（并集，保持顺序）
    def _union(a: Any, b: Any) -> List[str]:
        out: List[str] = []
        seen = set()
        for item in list(_split_lines(a)) + list(_split_lines(b)):
            if item not in seen:
                seen.add(item)
                out.append(item)
        return out

    deny_union = _union(merged.get("deny"), options_cfg.get("deny"))
    allow_union = _union(merged.get("allow"), options_cfg.get("allow"))
    if deny_union:
        merged["deny"] = deny_union
    if allow_union:
        merged["allow"] = allow_union

    # mode / use_defaults：options 优先，否则沿用已有，否则默认
    if "mode" in options_cfg:
        merged["mode"] = options_cfg["mode"]
    elif "mode" not in merged:
        merged["mode"] = "deny"

    if "use_defaults" in options_cfg:
        merged["use_defaults"] = options_cfg["use_defaults"]
    elif "use_defaults" not in merged:
        merged["use_defaults"] = True

    base_prefs["post_body_parameter_filter"] = merged

    new_provider = dict(provider)
    new_provider["preferences"] = base_prefs
    return new_provider


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

    # 读取声明式 UI（enabled_plugins 的 key=value 参数），合并进 provider 配置
    options_cfg = None
    try:
        options = get_plugin_options(PLUGIN_INFO["name"], provider)
        options_cfg = _build_options_cfg(options)
    except Exception as e:
        logger.debug(
            f"[post_body_parameter_filter] parse options failed: {type(e).__name__}: {e}"
        )
        options_cfg = None

    effective_provider = _merge_into_provider(provider, options_cfg)

    filtered = filter_payload_parameters(
        payload,
        engine=str(engine or "").lower(),
        provider=effective_provider,
        model=str(model) if model else None,
        original_model=original_model,
    )

    logger.debug(
        f"[post_body_parameter_filter] applied. engine={engine}, model={model}, "
        f"original_model={original_model}, options_cfg={options_cfg}"
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
