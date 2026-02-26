"""core.payload_filter

请求体参数过滤工具。

目标：
- 在将请求转发到上游渠道前，按渠道能力/配置移除不被支持的字段，避免 400/422 等参数校验错误。
- 提供默认过滤（对常见 OpenAI 兼容渠道中的非标准字段做兜底移除）。
- 支持通过 provider.preferences.post_body_parameter_filter 自定义 allow/deny。

配置示例（provider.preferences）：

1) 简单 deny 列表（等价于 {mode: "deny", deny: [...] }）

    post_body_parameter_filter:
      - thinking
      - min_p

2) 结构化配置：

    post_body_parameter_filter:
      mode: deny           # deny | allow
      deny: ["thinking"]
      allow: ["temperature", "top_p"]
      use_defaults: true   # 是否叠加内置默认过滤

3) 按模型/全局配置（可选，行为类似 overrides）：

    post_body_parameter_filter:
      all:
        deny: ["thinking"]
      gpt-4o-mini:
        deny: ["response_format"]

说明：
- 当前实现主要过滤 payload 顶层字段；支持 dot-path（如 "stream_options.include_usage"）作为轻量扩展。
- allow 模式会保留必要字段（如 model/messages/input/stream），并仅保留 allow + 必要字段。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional, Set

from core.log_config import logger
from core.utils import safe_get


# 对常见“OpenAI 兼容”渠道做保守兜底：移除明显的非标准字段。
# 说明：不要在这里放太激进的默认过滤（例如 response_format），否则会破坏真实支持该字段的上游。
_COMMON_DEFAULT_DENY: Set[str] = {
    "thinking",  # Claude/Anthropic 风格字段
    "include_usage",  # Zoaholic 内部字段（非 OpenAI 顶层参数）
    "chat_template_kwargs",  # Zoaholic/内部字段
    "min_p",  # 常见非 OpenAI 标准字段，很多兼容网关会直接报错
    "top_k",  # 常见非 OpenAI 标准字段，很多兼容网关会直接报错
}

_DEFAULT_DENY_BY_ENGINE: dict[str, Set[str]] = {
    # OpenAI/第三方 OpenAI-Compatible
    "openai": set(_COMMON_DEFAULT_DENY),
    "openrouter": set(_COMMON_DEFAULT_DENY),
    "azure": set(_COMMON_DEFAULT_DENY),
}


_ALWAYS_KEEP_TOP_LEVEL = {
    # Chat Completions
    "model",
    "messages",
    # Responses API
    "input",
    "instructions",
    # Common
    "stream",
    # Non-chat endpoints
    "prompt",
    "file",
}


def _as_set(value: Any) -> Set[str]:
    if value is None:
        return set()
    if isinstance(value, set):
        return {str(x) for x in value if str(x).strip()}
    if isinstance(value, (list, tuple)):
        out: Set[str] = set()
        for x in value:
            if isinstance(x, str):
                s = x.strip()
                if s:
                    out.add(s)
                continue
            # 兼容 YAML/JSON 的意外类型（例如 deny: [200] 被解析为 int）
            if isinstance(x, (int, float)):
                logger.warning(
                    f"[payload_filter] non-string field name in config list: {x!r} ({type(x).__name__}); treating as string"
                )
                s = str(x).strip()
                if s:
                    out.add(s)
        return out
    if isinstance(value, str):
        s = value.strip()
        return {s} if s else set()
    return set()


def _pop_dot_path(payload: dict, path: str) -> bool:
    """就地删除形如 a.b.c 的字段。返回是否成功删除。"""
    if not path or "." not in path:
        return False

    parts = [p for p in path.split(".") if p]
    if not parts:
        return False

    cur: Any = payload
    for k in parts[:-1]:
        if not isinstance(cur, dict):
            return False
        if k not in cur:
            return False
        cur = cur.get(k)

    last = parts[-1]
    if isinstance(cur, dict) and last in cur:
        cur.pop(last, None)
        return True
    return False



def _pop_dot_path_cow(payload: dict, path: str) -> tuple[dict, bool]:
    """删除形如 a.b.c 的字段（copy-on-write）。

    - 不修改入参 payload（及其路径上的 dict），返回一个新 dict。
    - 仅复制 dot-path 所经过的 dict 节点；其他字段保持引用（避免 deepcopy 的巨大开销）。
    """
    if not path or "." not in path:
        return payload, False

    parts = [p for p in path.split(".") if p]
    if not parts:
        return payload, False

    # 根节点先浅拷贝
    dst: dict = dict(payload)
    cur_src: Any = payload
    cur_dst: Any = dst

    for k in parts[:-1]:
        if not isinstance(cur_src, dict):
            return payload, False
        if k not in cur_src:
            return payload, False

        child_src = cur_src.get(k)
        if not isinstance(child_src, dict):
            return payload, False

        child_dst = dict(child_src)
        cur_dst[k] = child_dst
        cur_src = child_src
        cur_dst = child_dst

    last = parts[-1]
    if isinstance(cur_dst, dict) and last in cur_dst:
        cur_dst.pop(last, None)
        return dst, True
    return dst, False


def _resolve_filter_cfg(
    raw_cfg: Any,
    *,
    model_keys: Iterable[str],
) -> Optional[dict]:
    """把 preferences.post_body_parameter_filter 解析成统一结构。"""
    if raw_cfg is None:
        return None

    # list: 视为 deny
    if isinstance(raw_cfg, list):
        return {"mode": "deny", "deny": raw_cfg, "use_defaults": True}

    if not isinstance(raw_cfg, dict):
        return None

    # 直接结构化配置
    if any(k in raw_cfg for k in ("deny", "allow", "mode", "enabled", "use_defaults")):
        return raw_cfg

    # 按 all/* + model_key 合并
    merged: dict[str, Any] = {"mode": "deny", "deny": [], "allow": [], "use_defaults": True}

    def _merge_one(obj: Any) -> None:
        if not isinstance(obj, dict):
            return
        if obj.get("enabled") is False:
            # 如果明确禁用，直接整块禁用
            merged["enabled"] = False
            return
        if "mode" in obj and isinstance(obj.get("mode"), str):
            merged["mode"] = obj.get("mode")
        if "use_defaults" in obj:
            merged["use_defaults"] = bool(obj.get("use_defaults"))
        if "deny" in obj:
            merged["deny"].extend(obj.get("deny") or [])
        if "allow" in obj:
            merged["allow"].extend(obj.get("allow") or [])

    for global_key in ("all", "*"):
        _merge_one(raw_cfg.get(global_key))

    for mk in model_keys:
        _merge_one(raw_cfg.get(mk))

    return merged


def filter_payload_parameters(
    payload: Dict[str, Any],
    *,
    engine: str,
    provider: Dict[str, Any],
    model: Optional[str] = None,
    original_model: Optional[str] = None,
) -> Dict[str, Any]:
    """按规则过滤 payload（返回新 dict，不修改入参）。"""

    try:
        if not isinstance(payload, dict) or not payload:
            return payload

        # 避免在拦截器链中原地修改同一个 payload 对象导致副作用
        filtered: Dict[str, Any] = dict(payload)

        prefs_cfg = safe_get(provider, "preferences", "post_body_parameter_filter", default=None)
        cfg = _resolve_filter_cfg(prefs_cfg, model_keys=[x for x in (model, original_model) if x])

        # 未配置时也应用默认过滤（仅对特定 engine 生效）
        enabled = True
        if isinstance(cfg, dict) and cfg.get("enabled") is False:
            enabled = False

        if not enabled:
            return filtered

        use_defaults = True
        mode = "deny"
        deny: Set[str] = set()
        allow: Set[str] = set()

        if isinstance(cfg, dict):
            use_defaults = bool(cfg.get("use_defaults", True))
            mode = str(cfg.get("mode", "deny") or "deny").strip().lower()
            deny |= _as_set(cfg.get("deny"))
            allow |= _as_set(cfg.get("allow"))

        if use_defaults:
            deny |= _DEFAULT_DENY_BY_ENGINE.get(engine, set())

        # 顶层键过滤 + dot-path 过滤
        if mode == "allow":
            keep = set(_ALWAYS_KEEP_TOP_LEVEL)
            keep |= allow
            filtered = {k: filtered[k] for k in keep if k in filtered}
        else:
            # deny 模式
            for key in deny:
                if not key:
                    continue
                if "." in key:
                    filtered, _ = _pop_dot_path_cow(filtered, key)
                else:
                    if key in _ALWAYS_KEEP_TOP_LEVEL:
                        # 用户误配置也不允许删除必要字段
                        continue
                    filtered.pop(key, None)

        return filtered

    except Exception as e:
        logger.warning(f"[payload_filter] filter_payload_parameters failed: {type(e).__name__}: {e}")
        return payload
