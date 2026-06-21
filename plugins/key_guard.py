"""
Key 入站防护插件：请求头、系统提示词和工具声明的轻量反滥用检查。

兼容旧配置：
  - "key_guard"                            -> 默认剥离 tools/tool_choice，不检查 UA
  - "key_guard:ua:sillytavern,ua:kobold"   -> 允许 UA 包含任意关键词，并剥离 tools
  - "key_guard:ua:sillytavern,no_tools"    -> 允许 UA，且不剥离 tools

推荐结构化配置：
  enabled_plugins:
    - name: key_guard
      params:
        allowed_ua:
          - TauriTavern
          - SillyTavern
        header_allow: |
          user-agent: TauriTavern
          x-client-name: trusted-client
        header_deny: |
          user-agent: curl
        system_allow:
          - trusted system marker
        system_deny:
          - forbidden prompt text
        tools_policy: strip        # allow | strip | deny | require
        tool_allow:
          - search
          - fetch
        tool_deny:
          - execute_shell
        tool_choice_policy: allow  # allow | strip | deny | require
        reject_status: 403
        reject_message: This API key does not allow this request.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

from fastapi import HTTPException

from core.plugins.interceptors import parse_plugin_entry, register_inbound_interceptor


DEFAULT_REJECT_STATUS = 403
DEFAULT_REJECT_MESSAGE = "This API key does not allow this request."
VALID_TOOLS_POLICIES = {"allow", "strip", "deny", "require"}
VALID_TOOL_CHOICE_POLICIES = {"allow", "strip", "deny", "require"}

PARAMS_SCHEMA = [
    {
        "key": "allowed_ua",
        "label": "UA 白名单",
        "type": "textarea",
        "default": "",
        "placeholder": "TauriTavern\nSillyTavern\nnode-fetch",
        "serialize": "key_value",
    },
    {
        "key": "header_allow",
        "label": "请求头必须匹配",
        "type": "textarea",
        "default": "",
        "placeholder": "user-agent: TauriTavern\nx-client-name: trusted-client",
        "serialize": "key_value",
    },
    {
        "key": "header_deny",
        "label": "请求头禁止匹配",
        "type": "textarea",
        "default": "",
        "placeholder": "user-agent: curl\nuser-agent: python-requests",
        "serialize": "key_value",
    },
    {
        "key": "system_allow",
        "label": "系统提示词必须包含",
        "type": "textarea",
        "default": "",
        "placeholder": "trusted system marker",
        "serialize": "key_value",
    },
    {
        "key": "system_deny",
        "label": "系统提示词禁止包含",
        "type": "textarea",
        "default": "",
        "placeholder": "forbidden prompt text",
        "serialize": "key_value",
    },
    {
        "key": "tools_policy",
        "label": "Tools 策略",
        "type": "select",
        "default": "strip",
        "options": [
            {"value": "allow", "label": "允许"},
            {"value": "strip", "label": "剥离"},
            {"value": "deny", "label": "禁止"},
            {"value": "require", "label": "必须存在"},
        ],
        "serialize": "key_value",
    },
    {
        "key": "tool_allow",
        "label": "工具名白名单",
        "type": "textarea",
        "default": "",
        "placeholder": "search\nfetch",
        "serialize": "key_value",
    },
    {
        "key": "tool_deny",
        "label": "工具名黑名单",
        "type": "textarea",
        "default": "",
        "placeholder": "execute_shell\nrun_command",
        "serialize": "key_value",
    },
    {
        "key": "tool_choice_policy",
        "label": "tool_choice 策略",
        "type": "select",
        "default": "allow",
        "options": [
            {"value": "allow", "label": "允许"},
            {"value": "strip", "label": "剥离"},
            {"value": "deny", "label": "禁止"},
            {"value": "require", "label": "必须存在"},
        ],
        "serialize": "key_value",
    },
    {
        "key": "strip_tools",
        "label": "兼容开关：剥离 tools",
        "type": "toggle",
        "default": True,
        "serialize": "key_value",
    },
    {
        "key": "reject_status",
        "label": "拒绝状态码",
        "type": "number",
        "default": DEFAULT_REJECT_STATUS,
        "min": 400,
        "max": 599,
        "serialize": "key_value",
    },
    {
        "key": "reject_message",
        "label": "拒绝提示",
        "type": "text",
        "default": DEFAULT_REJECT_MESSAGE,
        "placeholder": DEFAULT_REJECT_MESSAGE,
        "serialize": "key_value",
    },
]

PLUGIN_INFO = {
    "name": "key_guard",
    "version": "2.0.0",
    "description": "Key 入站防护：请求头、系统提示词和工具声明反滥用检查",
    "author": "Zoaholic",
    "dependencies": [],
    "metadata": {
        "category": "interceptors",
        "params_hint": "支持 UA 白名单、请求头 allow/deny、system prompt allow/deny、tools 策略、工具名 allow/deny。",
        "params_schema": PARAMS_SCHEMA,
    },
}

logger = logging.getLogger("Zoaholic")


def _default_config() -> Dict[str, Any]:
    return {
        "allowed_ua": [],
        "header_allow": {},
        "header_deny": {},
        "system_allow": [],
        "system_deny": [],
        "tools_policy": "strip",
        "tool_allow": [],
        "tool_deny": [],
        "tool_choice_policy": "allow",
        "strip_tools": True,
        "reject_status": DEFAULT_REJECT_STATUS,
        "reject_message": DEFAULT_REJECT_MESSAGE,
    }


def _normalize_text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _dedupe_keep_order(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        text = _normalize_text(value).lower()
        if not text or text in seen:
            continue
        result.append(text)
        seen.add(text)
    return result


def _split_keywords(value: Any) -> List[str]:
    """拆分关键词。结构化配置可传列表；文本框按换行、分号或 | 拆分。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return _dedupe_keep_order(str(item) for item in value)
    normalized = str(value).replace(";", "\n").replace("|", "\n")
    return _dedupe_keep_order(item for item in normalized.splitlines())


def _parse_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    text = _normalize_text(value).lower()
    if text in {"1", "true", "yes", "on", "strip_tools", "strip"}:
        return True
    if text in {"0", "false", "no", "off", "no_tools", "allow"}:
        return False
    return default


def _parse_status(value: Any, default: int = DEFAULT_REJECT_STATUS) -> int:
    try:
        status = int(str(value).strip())
    except Exception:
        return default
    if 400 <= status <= 599:
        return status
    return default


def _normalize_policy(value: Any, valid: set, default: str) -> str:
    text = _normalize_text(value).lower()
    return text if text in valid else default


def _parse_header_rules(value: Any) -> Dict[str, List[str]]:
    """解析 header 规则。

    支持：
    - dict: {"user-agent": ["TauriTavern"]}
    - 文本：每行 "header-name: keyword" 或 "header-name=keyword"
    同一 header 可写多行。
    """
    result: Dict[str, List[str]] = {}
    if not value:
        return result

    if isinstance(value, dict):
        for raw_name, raw_keywords in value.items():
            name = _normalize_text(raw_name).lower()
            if not name:
                continue
            result.setdefault(name, [])
            result[name].extend(_split_keywords(raw_keywords))
        return {name: _dedupe_keep_order(keywords) for name, keywords in result.items()}

    if isinstance(value, (list, tuple, set)):
        lines: List[str] = []
        for item in value:
            if isinstance(item, dict):
                nested = _parse_header_rules(item)
                for name, keywords in nested.items():
                    result.setdefault(name, []).extend(keywords)
            else:
                lines.extend(str(item).splitlines())
    else:
        lines = str(value).splitlines()

    for raw_line in lines:
        line = _normalize_text(raw_line)
        if not line:
            continue
        if ":" in line:
            raw_name, raw_keyword = line.split(":", 1)
        elif "=" in line:
            raw_name, raw_keyword = line.split("=", 1)
        else:
            continue
        name = _normalize_text(raw_name).lower()
        if not name:
            continue
        result.setdefault(name, [])
        result[name].extend(_split_keywords(raw_keyword))

    return {name: _dedupe_keep_order(keywords) for name, keywords in result.items()}


def _parse_key_value_options(options: str) -> Dict[str, str]:
    """解析 key=value 形式的旧字符串参数。

    这里保持简单兼容。复杂值建议使用结构化 params。
    """
    result: Dict[str, str] = {}
    for raw_part in str(options or "").split(","):
        part = raw_part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        if key:
            result[key] = value.strip()
    return result


def _apply_options_dict(config: Dict[str, Any], options: Dict[str, Any]) -> None:
    if not options:
        return

    config["allowed_ua"].extend(_split_keywords(options.get("allowed_ua") or options.get("ua")))
    config["header_allow"].update(_parse_header_rules(options.get("header_allow")))
    config["header_deny"].update(_parse_header_rules(options.get("header_deny")))
    config["system_allow"].extend(_split_keywords(options.get("system_allow")))
    config["system_deny"].extend(_split_keywords(options.get("system_deny")))
    config["tool_allow"].extend(_split_keywords(options.get("tool_allow") or options.get("allowed_tools")))
    config["tool_deny"].extend(_split_keywords(options.get("tool_deny") or options.get("denied_tools")))

    if "strip_tools" in options:
        config["strip_tools"] = _parse_bool(options.get("strip_tools"), default=config["strip_tools"])
    elif "tools" in options:
        config["strip_tools"] = _parse_bool(options.get("tools"), default=config["strip_tools"])

    if options.get("tools_policy") is not None:
        config["tools_policy"] = _normalize_policy(options.get("tools_policy"), VALID_TOOLS_POLICIES, config["tools_policy"])
    else:
        config["tools_policy"] = "strip" if config["strip_tools"] else "allow"

    if options.get("tool_choice_policy") is not None:
        config["tool_choice_policy"] = _normalize_policy(
            options.get("tool_choice_policy"), VALID_TOOL_CHOICE_POLICIES, config["tool_choice_policy"]
        )

    if options.get("reject_status") is not None:
        config["reject_status"] = _parse_status(options.get("reject_status"), default=config["reject_status"])
    if options.get("reject_message") is not None:
        message = _normalize_text(options.get("reject_message"))
        if message:
            config["reject_message"] = message


def _parse_opts(enabled_plugins: list) -> Dict[str, Any]:
    config = _default_config()
    if not enabled_plugins:
        return config

    for entry in enabled_plugins:
        plugin_name, options = parse_plugin_entry(entry)
        if plugin_name != "key_guard":
            continue

        if isinstance(options, dict):
            _apply_options_dict(config, options)
            continue

        if not isinstance(options, str) or not options:
            continue

        key_values = _parse_key_value_options(options)
        if key_values:
            _apply_options_dict(config, key_values)

        for raw_part in options.split(","):
            part = raw_part.strip()
            if not part:
                continue
            if part.startswith("ua:"):
                config["allowed_ua"].extend(_split_keywords(part[3:]))
            elif part == "no_tools":
                config["strip_tools"] = False
                config["tools_policy"] = "allow"
            elif part == "strip_tools":
                config["strip_tools"] = True
                config["tools_policy"] = "strip"
            elif part in VALID_TOOLS_POLICIES:
                config["tools_policy"] = part

    config["allowed_ua"] = _dedupe_keep_order(config["allowed_ua"])
    config["system_allow"] = _dedupe_keep_order(config["system_allow"])
    config["system_deny"] = _dedupe_keep_order(config["system_deny"])
    config["tool_allow"] = _dedupe_keep_order(config["tool_allow"])
    config["tool_deny"] = _dedupe_keep_order(config["tool_deny"])
    return config


def _header_map(request, api_key_info=None) -> Dict[str, str]:
    header_sources = []
    if request is not None:
        try:
            header_sources.append(request.headers)
        except Exception:
            pass
    if api_key_info:
        header_sources.append(api_key_info.get("headers") or {})
        header_sources.append(api_key_info.get("original_headers") or {})

    headers: Dict[str, str] = {}
    for source in header_sources:
        try:
            for key, value in dict(source).items():
                name = str(key).lower()
                if name and name not in headers:
                    headers[name] = str(value or "")
        except Exception:
            continue
    return headers


def _get_ua(headers: Dict[str, str]) -> str:
    return str(headers.get("user-agent", "")).lower()


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("text") is not None:
                    parts.append(str(item.get("text") or ""))
                elif item.get("type") == "text" and item.get("content") is not None:
                    parts.append(str(item.get("content") or ""))
            else:
                text = getattr(item, "text", None)
                if text is not None:
                    parts.append(str(text or ""))
        return "\n".join(part for part in parts if part)
    return str(content)


def _extract_system_text(request_data: Any) -> str:
    texts: List[str] = []

    for attr in ("system", "system_instruction", "systemInstruction"):
        value = getattr(request_data, attr, None)
        if value:
            texts.append(_content_to_text(value))
        if isinstance(request_data, dict) and request_data.get(attr):
            texts.append(_content_to_text(request_data.get(attr)))

    messages = getattr(request_data, "messages", None)
    if isinstance(request_data, dict):
        messages = request_data.get("messages", messages)

    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict):
                role = str(message.get("role") or "").lower()
                content = message.get("content")
            else:
                role = str(getattr(message, "role", "") or "").lower()
                content = getattr(message, "content", None)
            if role == "system":
                text = _content_to_text(content)
                if text:
                    texts.append(text)

    return "\n".join(text for text in texts if text).lower()


def _get_tools(request_data: Any) -> List[Any]:
    tools = getattr(request_data, "tools", None)
    if isinstance(request_data, dict):
        tools = request_data.get("tools", tools)
    if isinstance(tools, list):
        return tools
    return []


def _tool_choice_present(request_data: Any) -> bool:
    if isinstance(request_data, dict):
        return request_data.get("tool_choice") is not None
    return getattr(request_data, "tool_choice", None) is not None


def _extract_tool_names_from_item(tool: Any) -> List[str]:
    names: List[str] = []
    if tool is None:
        return names

    if isinstance(tool, dict):
        if tool.get("name"):
            names.append(str(tool.get("name")))
        function = tool.get("function")
        if isinstance(function, dict) and function.get("name"):
            names.append(str(function.get("name")))
        for key in ("function_declarations", "functionDeclarations"):
            declarations = tool.get(key)
            if isinstance(declarations, list):
                for declaration in declarations:
                    if isinstance(declaration, dict) and declaration.get("name"):
                        names.append(str(declaration.get("name")))
        return names

    name = getattr(tool, "name", None)
    if name:
        names.append(str(name))
    function = getattr(tool, "function", None)
    function_name = getattr(function, "name", None)
    if function_name:
        names.append(str(function_name))
    return names


def _extract_tool_names(request_data: Any) -> List[str]:
    names: List[str] = []
    for tool in _get_tools(request_data):
        names.extend(_extract_tool_names_from_item(tool))
    return _dedupe_keep_order(names)


def _strip_tools(request_data: Any) -> None:
    if hasattr(request_data, "tools"):
        request_data.tools = None
    if hasattr(request_data, "tool_choice"):
        request_data.tool_choice = None
    if isinstance(request_data, dict):
        request_data.pop("tools", None)
        request_data.pop("tool_choice", None)


def _strip_tool_choice(request_data: Any) -> None:
    if hasattr(request_data, "tool_choice"):
        request_data.tool_choice = None
    if isinstance(request_data, dict):
        request_data.pop("tool_choice", None)


def _reject(config: Dict[str, Any], api_key: str, reason: str) -> None:
    logger.warning(f"[key_guard] rejected key {api_key}... reason={reason}")
    raise HTTPException(
        status_code=config.get("reject_status", DEFAULT_REJECT_STATUS),
        detail=config.get("reject_message", DEFAULT_REJECT_MESSAGE),
    )


def _check_allowed_ua(config: Dict[str, Any], headers: Dict[str, str], api_key: str) -> None:
    allowed_ua = config.get("allowed_ua") or []
    if not allowed_ua:
        return
    ua = _get_ua(headers)
    if not any(keyword in ua for keyword in allowed_ua):
        logger.warning(f"[key_guard] UA not in whitelist for key {api_key}... (UA: {ua[:80]}), allowed: {allowed_ua}")
        _reject(config, api_key, "ua_not_allowed")


def _check_header_allow(config: Dict[str, Any], headers: Dict[str, str], api_key: str) -> None:
    for name, keywords in (config.get("header_allow") or {}).items():
        actual = str(headers.get(name, "")).lower()
        if keywords:
            if not any(keyword in actual for keyword in keywords):
                _reject(config, api_key, f"header_allow_failed:{name}")
        elif not actual:
            _reject(config, api_key, f"header_required:{name}")


def _check_header_deny(config: Dict[str, Any], headers: Dict[str, str], api_key: str) -> None:
    for name, keywords in (config.get("header_deny") or {}).items():
        actual = str(headers.get(name, "")).lower()
        if keywords:
            if any(keyword in actual for keyword in keywords):
                _reject(config, api_key, f"header_deny_matched:{name}")
        elif actual:
            _reject(config, api_key, f"header_denied:{name}")


def _check_system_prompt(config: Dict[str, Any], request_data: Any, api_key: str) -> None:
    system_text = _extract_system_text(request_data)
    system_allow = config.get("system_allow") or []
    system_deny = config.get("system_deny") or []

    if system_allow and not any(keyword in system_text for keyword in system_allow):
        _reject(config, api_key, "system_allow_failed")
    if system_deny and any(keyword in system_text for keyword in system_deny):
        _reject(config, api_key, "system_deny_matched")


def _check_tools(config: Dict[str, Any], request_data: Any, api_key: str) -> None:
    tools = _get_tools(request_data)
    has_tools = bool(tools)
    has_tool_choice = _tool_choice_present(request_data)
    tool_names = _extract_tool_names(request_data)
    tool_name_set = set(tool_names)

    tool_deny = set(config.get("tool_deny") or [])
    denied = sorted(tool_name_set & tool_deny)
    if denied:
        _reject(config, api_key, f"tool_deny_matched:{','.join(denied)}")

    tool_allow = set(config.get("tool_allow") or [])
    if tool_allow and has_tools:
        unknown = sorted(name for name in tool_names if name not in tool_allow)
        if unknown:
            _reject(config, api_key, f"tool_allow_failed:{','.join(unknown)}")

    tool_choice_policy = config.get("tool_choice_policy") or "allow"
    if tool_choice_policy == "deny" and has_tool_choice:
        _reject(config, api_key, "tool_choice_denied")
    if tool_choice_policy == "require" and not has_tool_choice:
        _reject(config, api_key, "tool_choice_required")

    tools_policy = config.get("tools_policy") or "allow"
    if tools_policy == "deny" and has_tools:
        _reject(config, api_key, "tools_denied")
    if tools_policy == "require" and not has_tools:
        _reject(config, api_key, "tools_required")
    if tools_policy == "strip" and has_tools:
        _strip_tools(request_data)
        logger.info(f"[key_guard] Stripped tools for key {api_key}...")
    elif tool_choice_policy == "strip" and has_tool_choice:
        _strip_tool_choice(request_data)
        logger.info(f"[key_guard] Stripped tool_choice for key {api_key}...")


async def key_guard_interceptor(request_data, request, api_key_info, enabled_plugins):
    if request_data is None:
        return request_data

    config = _parse_opts(enabled_plugins)
    api_key = (api_key_info.get("api_key", "???") if api_key_info else "???")[:15]
    headers = _header_map(request, api_key_info)

    _check_allowed_ua(config, headers, api_key)
    _check_header_allow(config, headers, api_key)
    _check_header_deny(config, headers, api_key)
    _check_system_prompt(config, request_data, api_key)
    _check_tools(config, request_data, api_key)

    return request_data


register_inbound_interceptor(
    "key_guard",
    key_guard_interceptor,
    priority=30,
    plugin_name="key_guard",
    metadata={
        "description": PLUGIN_INFO["description"],
        "stage": "inbound_interceptors",
        "params_hint": PLUGIN_INFO["metadata"]["params_hint"],
        "params_schema": PLUGIN_INFO["metadata"]["params_schema"],
    },
)
