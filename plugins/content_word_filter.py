"""
屏蔽词过滤插件（content_word_filter）

定位
----
在请求进入上游前，对消息文本中的屏蔽词执行替换 / 移除 / 拒绝。

生效阶段（两个阶段都注册，任一处启用即生效）
-------------------------------------------
- inbound（API Key 级）：由 ``api_keys[].preferences.enabled_plugins`` 控制，
  鉴权后、provider 选择前执行。
- channel_inbound（渠道级）：由 ``providers[].preferences.enabled_plugins`` 控制，
  provider 选定后、渠道适配器转换请求体前执行。

两个阶段下 reject 模式都会直接把错误返回给客户端（渠道级拒绝不会触发换渠道重试）。

配置方式
--------
- 推荐在插件配置面板填写参数（声明式 UI，见 ``PLUGIN_INFO["metadata"]["params_schema"]``）。
- 也支持 YAML 结构化写法::

      enabled_plugins:
        - name: content_word_filter
          params:
            mode: replace
            words: |
              badword1
              badword2
            replacement: "***"

- 以及字符串写法（逗号或换行分隔）::

      - content_word_filter:mode=replace,words=bad1|bad2,replacement=***

处理模式（mode）
----------------
- replace：把匹配到的屏蔽词替换为 replacement
- strip：从文本中移除匹配到的屏蔽词
- reject：直接拒绝请求（默认 400，可通过 reject_status / reject_message 自定义）

匹配范围（scope）
-----------------
- all：所有 role（默认）
- 单个 role：system / user / assistant / tool / developer ...
- 多个 role：手动填写 ``user,system`` 这样的逗号列表
- 命中 system 时，同时会处理 Claude 风格的顶层 ``system`` 字段

匹配类型（match_type）
----------------------
- substring：子串匹配（默认）
- word：独立词匹配。英文按 ASCII 词边界，CJK 词自动退化为子串（``\\w`` 包含汉字，
  强行加词边界会导致中文永远匹配不上）
- pattern：正则匹配。会跳过编译失败、过长、以及能匹配空串的表达式
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Dict, List, Optional, Sequence, Tuple

from fastapi import HTTPException

from core.log_config import logger
from core.plugins.interceptors import (
    parse_plugin_entry,
    register_channel_inbound_interceptor,
    register_inbound_interceptor,
    unregister_channel_inbound_interceptor,
    unregister_inbound_interceptor,
)


PLUGIN_NAME = "content_word_filter"

DEFAULT_REPLACEMENT = "***"
DEFAULT_REJECT_STATUS = 400
DEFAULT_REJECT_MESSAGE = "请求包含屏蔽词，已被 content_word_filter 拒绝。"

VALID_MODES = {"replace", "strip", "reject"}
VALID_MATCH_TYPES = {"word", "substring", "pattern"}

# 正则模式下的安全限制：过长的用户正则容易造成回溯爆炸
MAX_PATTERN_LENGTH = 500
# 可识别为文本块的 content block 类型（兼容 Chat Completions / Responses 两种风格）
TEXT_BLOCK_TYPES = {"text", "input_text", "output_text"}


PARAMS_SCHEMA = [
    {
        "key": "mode",
        "label": "处理模式",
        "type": "select",
        "default": "replace",
        "options": [
            {"value": "replace", "label": "replace（替换）"},
            {"value": "strip", "label": "strip（移除）"},
            {"value": "reject", "label": "reject（拒绝）"},
        ],
        "serialize": "key_value",
    },
    {
        "key": "words",
        "label": "屏蔽词列表（每行一个）",
        "type": "textarea",
        "default": "",
        "placeholder": "badword1\nbadword2\n屏蔽词",
        "serialize": "key_value",
    },
    {
        "key": "replacement",
        "label": "替换文本（replace 模式生效）",
        "type": "text",
        "default": DEFAULT_REPLACEMENT,
        "serialize": "key_value",
    },
    {
        "key": "scope",
        "label": "检查范围（可手填 user,system 多选）",
        "type": "select",
        "default": "all",
        "options": [
            {"value": "all", "label": "all（全部角色）"},
            {"value": "system", "label": "system"},
            {"value": "user", "label": "user"},
            {"value": "assistant", "label": "assistant"},
        ],
        "serialize": "key_value",
    },
    {
        "key": "match_type",
        "label": "匹配类型",
        "type": "select",
        "default": "substring",
        "options": [
            {"value": "substring", "label": "substring（子串）"},
            {"value": "word", "label": "word（独立词，CJK 自动退化为子串）"},
            {"value": "pattern", "label": "pattern（正则）"},
        ],
        "serialize": "key_value",
    },
    {
        "key": "case_sensitive",
        "label": "区分大小写",
        "type": "toggle",
        "default": False,
        "serialize": "key_value",
    },
    {
        "key": "strip_empty_message",
        "label": "strip 模式：移除因此变空的消息",
        "type": "toggle",
        "default": True,
        "serialize": "key_value",
    },
    {
        "key": "reject_status",
        "label": "reject 模式：HTTP 状态码",
        "type": "text",
        "default": str(DEFAULT_REJECT_STATUS),
        "serialize": "key_value",
    },
    {
        "key": "reject_message",
        "label": "reject 模式：拒绝提示文案",
        "type": "text",
        "default": DEFAULT_REJECT_MESSAGE,
        "serialize": "key_value",
    },
]

PLUGIN_INFO = {
    "name": PLUGIN_NAME,
    "version": "2.0.0",
    "description": "屏蔽词过滤插件：对入站请求消息中的屏蔽词执行替换、移除或拒绝。",
    "author": "Zoaholic",
    "dependencies": [],
    "metadata": {
        "category": "interceptors",
        "params_hint": (
            "mode=replace|strip|reject; words=每行一个屏蔽词; "
            "scope=all|system|user|assistant（支持 user,system 多选）; "
            "match_type=substring|word|pattern"
        ),
        "params_schema": PARAMS_SCHEMA,
    },
}

KNOWN_KEYS = tuple(item["key"] for item in PARAMS_SCHEMA)

# 字符串参数形如 "mode=replace,words=a|b,replacement=***"，
# 也允许换行分隔、且 words 的值本身可以跨行。
# 只在“已知参数名 + =”处切分，避免值里的逗号/换行被误当成分隔符。
_KEY_BOUNDARY_RE = re.compile(
    r"(?:^|[,;\n\r])[ \t]*(" + "|".join(re.escape(key) for key in KNOWN_KEYS) + r")[ \t]*=",
    re.IGNORECASE,
)


# ==================== 参数解析 ====================


def _normalize_bool(value: Any, default: bool) -> bool:
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


def _normalize_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _split_words(value: Any) -> List[str]:
    """把 words 参数拆成词列表。

    - list / tuple / set：逐项展开
    - 字符串：优先按换行和 ``|`` 拆；两者都没有时才按逗号拆
      （这样 "hello, world" 这类带逗号的短语可以通过换行写法保留）
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items: List[str] = []
        for item in value:
            items.extend(_split_words(item))
        return items

    text = str(value)
    if "\n" in text or "\r" in text or "|" in text:
        parts = re.split(r"[\r\n|]+", text)
    else:
        parts = text.split(",")
    return [part.strip() for part in parts if part.strip()]


def _dedupe(words: Sequence[str]) -> List[str]:
    seen = set()
    result: List[str] = []
    for word in words:
        if word and word not in seen:
            seen.add(word)
            result.append(word)
    return result


def _parse_options_string(options: str) -> Dict[str, str]:
    """解析 "k=v,k=v" / 多行 "k=v" 形式的参数字符串。"""
    raw: Dict[str, str] = {}
    if not options:
        return raw

    matches = list(_KEY_BOUNDARY_RE.finditer(options))
    if not matches:
        return raw

    for index, match in enumerate(matches):
        key = match.group(1).strip().lower()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(options)
        raw[key] = options[start:end].strip()
    return raw


def _collect_raw_options(enabled_plugins: Optional[Sequence[Any]]) -> Optional[Dict[str, Any]]:
    """从 enabled_plugins 中取出本插件的原始参数；未启用时返回 None。"""
    if not enabled_plugins:
        return None

    found = False
    raw: Dict[str, Any] = {}
    for entry in enabled_plugins:
        plugin_name, options = parse_plugin_entry(entry)
        if plugin_name != PLUGIN_NAME:
            continue
        found = True
        if isinstance(options, dict):
            for key, value in options.items():
                raw[str(key).strip().lower()] = value
        elif isinstance(options, str) and options.strip():
            raw.update(_parse_options_string(options))

    return raw if found else None


@dataclass
class FilterConfig:
    mode: str = "replace"
    words: Tuple[str, ...] = ()
    replacement: str = DEFAULT_REPLACEMENT
    scopes: Tuple[str, ...] = ("all",)
    match_type: str = "substring"
    case_sensitive: bool = False
    strip_empty_message: bool = True
    reject_status: int = DEFAULT_REJECT_STATUS
    reject_message: str = DEFAULT_REJECT_MESSAGE

    def covers(self, role: Optional[str]) -> bool:
        if "all" in self.scopes:
            return True
        return bool(role) and role in self.scopes


def _build_config(raw: Dict[str, Any]) -> FilterConfig:
    mode = str(raw.get("mode", "replace")).strip().lower() or "replace"
    if mode not in VALID_MODES:
        logger.warning(f"[{PLUGIN_NAME}] 未知 mode={mode!r}，已回退为 replace。")
        mode = "replace"

    match_type = str(raw.get("match_type", "substring")).strip().lower() or "substring"
    if match_type not in VALID_MATCH_TYPES:
        logger.warning(f"[{PLUGIN_NAME}] 未知 match_type={match_type!r}，已回退为 substring。")
        match_type = "substring"

    scope_raw = raw.get("scope", "all")
    scopes = [token.strip().lower() for token in re.split(r"[,\s|]+", str(scope_raw)) if token.strip()]
    if not scopes:
        scopes = ["all"]
    if "all" in scopes:
        scopes = ["all"]

    replacement = raw.get("replacement", DEFAULT_REPLACEMENT)
    replacement = DEFAULT_REPLACEMENT if replacement is None else str(replacement)

    reject_message = raw.get("reject_message") or DEFAULT_REJECT_MESSAGE

    return FilterConfig(
        mode=mode,
        words=tuple(_dedupe(_split_words(raw.get("words")))),
        replacement=replacement,
        scopes=tuple(scopes),
        match_type=match_type,
        case_sensitive=_normalize_bool(raw.get("case_sensitive"), default=False),
        strip_empty_message=_normalize_bool(raw.get("strip_empty_message"), default=True),
        reject_status=_normalize_int(raw.get("reject_status"), DEFAULT_REJECT_STATUS),
        reject_message=str(reject_message),
    )


# ==================== 匹配器 ====================


def _is_ascii_word_char(char: str) -> bool:
    return char.isascii() and (char.isalnum() or char == "_")


def _word_boundary_pattern(word: str) -> str:
    """为独立词匹配加边界。

    Python 的 ``\\w`` 在 Unicode 模式下包含汉字，所以对 CJK 词加词边界会导致
    "这是屏蔽词测试" 匹配不到 "屏蔽词"。这里只在词首/词尾是 ASCII 单词字符时加边界。
    """
    escaped = re.escape(word)
    prefix = r"(?<![0-9A-Za-z_])" if _is_ascii_word_char(word[0]) else ""
    suffix = r"(?![0-9A-Za-z_])" if _is_ascii_word_char(word[-1]) else ""
    return f"{prefix}{escaped}{suffix}"


@lru_cache(maxsize=256)
def _build_matchers(
    words: Tuple[str, ...],
    case_sensitive: bool,
    match_type: str,
) -> Tuple[re.Pattern, ...]:
    """编译匹配器（按参数缓存，避免每个请求都重新编译）。"""
    if not words:
        return ()

    flags = 0 if case_sensitive else re.IGNORECASE

    if match_type == "pattern":
        compiled: List[re.Pattern] = []
        for word in words:
            if len(word) > MAX_PATTERN_LENGTH:
                logger.warning(f"[{PLUGIN_NAME}] 正则过长（>{MAX_PATTERN_LENGTH}），已跳过: {word[:50]!r}...")
                continue
            try:
                pattern = re.compile(word, flags)
            except re.error as exc:
                logger.warning(f"[{PLUGIN_NAME}] 正则编译失败，已跳过: {word!r} ({exc})")
                continue
            if pattern.match(""):
                logger.warning(f"[{PLUGIN_NAME}] 正则可匹配空串，会污染全文，已跳过: {word!r}")
                continue
            compiled.append(pattern)
        return tuple(compiled)

    # substring / word：可以安全地合并成一条 alternation，减少扫描次数
    if match_type == "word":
        parts = [_word_boundary_pattern(word) for word in words]
    else:
        parts = [re.escape(word) for word in words]

    try:
        return (re.compile("|".join(parts), flags),)
    except re.error as exc:  # 理论上不会发生（已转义）
        logger.warning(f"[{PLUGIN_NAME}] 合并匹配器失败，回退为逐词编译: {exc}")
        return tuple(re.compile(part, flags) for part in parts)


def _find_first_hit(text: str, matchers: Sequence[re.Pattern]) -> Optional[str]:
    for pattern in matchers:
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None


def _apply_replacement(text: str, matchers: Sequence[re.Pattern], replacement: str) -> str:
    # 用 lambda 而不是字符串，避免 replacement 里的 \n、\1、\g<x> 被当成正则替换模板
    for pattern in matchers:
        text = pattern.sub(lambda _match: replacement, text)
    return text


# ==================== 内容处理 ====================


@dataclass
class _Context:
    config: FilterConfig
    matchers: Tuple[re.Pattern, ...]
    hits: List[str] = field(default_factory=list)
    changed: int = 0

    def record(self, hit: str) -> None:
        if len(self.hits) < 10:
            self.hits.append(hit)


def _reject(ctx: _Context, hit: str) -> None:
    logger.warning(f"[{PLUGIN_NAME}] 命中屏蔽词，拒绝请求: {hit[:32]!r}")
    raise HTTPException(
        status_code=ctx.config.reject_status,
        detail={
            "error": {
                "message": ctx.config.reject_message,
                "type": "content_filter",
                "code": PLUGIN_NAME,
            }
        },
    )


def _filter_text(text: str, ctx: _Context) -> Tuple[str, bool]:
    """过滤单段文本，返回 (新文本, 是否改变)。reject 模式下直接抛出 HTTPException。"""
    if not text or not ctx.matchers:
        return text, False

    hit = _find_first_hit(text, ctx.matchers)
    if hit is None:
        return text, False

    ctx.record(hit)

    if ctx.config.mode == "reject":
        _reject(ctx, hit)

    if ctx.config.mode == "strip":
        new_text = _apply_replacement(text, ctx.matchers, "")
    else:
        new_text = _apply_replacement(text, ctx.matchers, ctx.config.replacement)

    changed = new_text != text
    if changed:
        ctx.changed += 1
    return new_text, changed


def _block_text(block: Any) -> Optional[str]:
    """取出 content block 的文本；不是文本块时返回 None。"""
    if isinstance(block, dict):
        if str(block.get("type") or "") in TEXT_BLOCK_TYPES:
            text = block.get("text")
            return text if isinstance(text, str) else None
        return None
    block_type = getattr(block, "type", None)
    if isinstance(block_type, str) and block_type in TEXT_BLOCK_TYPES:
        text = getattr(block, "text", None)
        return text if isinstance(text, str) else None
    return None


def _set_block_text(block: Any, text: str) -> None:
    if isinstance(block, dict):
        block["text"] = text
    else:
        setattr(block, "text", text)


def _filter_content(content: Any, ctx: _Context) -> Tuple[Any, bool, bool]:
    """过滤消息 content。

    返回 (新 content, 是否改变, 是否还有有效载荷)。
    "有效载荷" 指还剩下非空文本或者图片/文件等非文本块。
    """
    if content is None:
        return content, False, False

    if isinstance(content, str):
        new_text, changed = _filter_text(content, ctx)
        return new_text, changed, bool(new_text.strip())

    if isinstance(content, list):
        changed = False
        has_payload = False
        kept: List[Any] = []
        for block in content:
            text = _block_text(block)
            if text is None:
                # 图片 / 文件 / 未知块：原样保留，不做任何字符串化处理
                kept.append(block)
                has_payload = True
                continue

            new_text, block_changed = _filter_text(text, ctx)
            changed = changed or block_changed
            if block_changed:
                _set_block_text(block, new_text)

            if not new_text.strip():
                # strip 模式下清空的文本块直接丢弃，避免留下空块
                if ctx.config.mode == "strip" and ctx.config.strip_empty_message and block_changed:
                    continue
            else:
                has_payload = True
            kept.append(block)

        return kept, changed, has_payload

    # 其它类型（结构化对象等）：不认识就不动，避免把它 str() 成 repr 污染请求体
    return content, False, True


def _message_role(message: Any) -> Optional[str]:
    if isinstance(message, dict):
        role = message.get("role")
    else:
        role = getattr(message, "role", None)
    return role if isinstance(role, str) else None


def _message_content(message: Any) -> Any:
    if isinstance(message, dict):
        return message.get("content")
    return getattr(message, "content", None)


def _set_message_content(message: Any, content: Any) -> None:
    if isinstance(message, dict):
        message["content"] = content
    else:
        setattr(message, "content", content)


def _has_tool_payload(message: Any) -> bool:
    """带 tool_calls / tool_call_id 的消息不能被删除，否则会破坏对话结构。"""
    if isinstance(message, dict):
        return bool(message.get("tool_calls") or message.get("tool_call_id"))
    return bool(getattr(message, "tool_calls", None) or getattr(message, "tool_call_id", None))


def _filter_messages(messages: Sequence[Any], ctx: _Context) -> Tuple[List[Any], bool]:
    kept: List[Any] = []
    changed = False
    dropped_last: Optional[Any] = None

    for message in messages:
        role = _message_role(message)
        if not ctx.config.covers(role):
            kept.append(message)
            continue

        content = _message_content(message)
        new_content, content_changed, has_payload = _filter_content(content, ctx)
        if content_changed:
            changed = True
            _set_message_content(message, new_content)

        should_drop = (
            ctx.config.mode == "strip"
            and ctx.config.strip_empty_message
            and content_changed
            and not has_payload
            and not _has_tool_payload(message)
        )
        if should_drop:
            dropped_last = message
            continue

        kept.append(message)

    # 兜底：不能把 messages 清空，否则上游会直接 400
    if not kept and dropped_last is not None:
        logger.warning(f"[{PLUGIN_NAME}] strip 后所有消息均为空，保留最后一条以避免上游报错。")
        kept.append(dropped_last)

    return kept, changed


def _filter_system_field(request_data: Any, ctx: _Context) -> bool:
    """处理 Claude 风格的顶层 system 字段。"""
    if not ctx.config.covers("system"):
        return False

    if isinstance(request_data, dict):
        system = request_data.get("system")
    else:
        system = getattr(request_data, "system", None)
    if system is None:
        return False

    new_system, changed, _payload = _filter_content(system, ctx)
    if not changed:
        return False

    if isinstance(request_data, dict):
        request_data["system"] = new_system
    else:
        setattr(request_data, "system", new_system)
    return True


def _get_messages(request_data: Any) -> Optional[List[Any]]:
    if isinstance(request_data, dict):
        messages = request_data.get("messages")
    else:
        messages = getattr(request_data, "messages", None)
    return messages if isinstance(messages, list) else None


def _set_messages(request_data: Any, messages: List[Any]) -> None:
    if isinstance(request_data, dict):
        request_data["messages"] = messages
    else:
        setattr(request_data, "messages", messages)


def apply_word_filter(request_data: Any, enabled_plugins: Optional[Sequence[Any]]) -> Any:
    """插件主逻辑：就地过滤 request_data 并返回它（两个阶段共用）。"""
    if request_data is None:
        return request_data

    raw = _collect_raw_options(enabled_plugins)
    if raw is None:
        return request_data

    config = _build_config(raw)
    if not config.words:
        return request_data

    matchers = _build_matchers(config.words, config.case_sensitive, config.match_type)
    if not matchers:
        return request_data

    ctx = _Context(config=config, matchers=matchers)

    system_changed = _filter_system_field(request_data, ctx)

    messages = _get_messages(request_data)
    if messages:
        kept, changed = _filter_messages(messages, ctx)
        if changed or len(kept) != len(messages):
            _set_messages(request_data, kept)
    elif not system_changed:
        return request_data

    if ctx.hits:
        logger.debug(
            f"[{PLUGIN_NAME}] mode={config.mode}, match_type={config.match_type}, "
            f"scope={','.join(config.scopes)}, words={len(config.words)}, "
            f"hits={len(ctx.hits)}, changed_segments={ctx.changed}"
        )
    return request_data


# ==================== 拦截器入口 ====================


async def content_word_filter_inbound(
    request_data: Any,
    request: Any,
    api_key_info: Optional[Dict[str, Any]],
    enabled_plugins: Optional[List[Any]],
) -> Any:
    """API Key 级入站拦截器。reject 模式下抛出的 HTTPException 会被框架原样上抛。"""
    return apply_word_filter(request_data, enabled_plugins)


async def content_word_filter_channel_inbound(
    request_data: Any,
    request: Any,
    provider: Optional[Dict[str, Any]],
    api_key_info: Optional[Dict[str, Any]],
    enabled_plugins: Optional[List[Any]],
) -> Any:
    """渠道级入站拦截器。reject 模式下抛出的 HTTPException 会被框架原样上抛。"""
    return apply_word_filter(request_data, enabled_plugins)


def setup(manager: Any) -> None:
    logger.info(f"[{PLUGIN_NAME}] 正在初始化...")
    metadata = {
        "description": PLUGIN_INFO["description"],
        "params_hint": PLUGIN_INFO["metadata"]["params_hint"],
        "params_schema": PLUGIN_INFO["metadata"]["params_schema"],
    }
    register_inbound_interceptor(
        interceptor_id=f"{PLUGIN_NAME}_inbound",
        callback=content_word_filter_inbound,
        priority=100,
        plugin_name=PLUGIN_NAME,
        metadata={**metadata, "stage": "inbound_interceptors"},
    )
    register_channel_inbound_interceptor(
        interceptor_id=f"{PLUGIN_NAME}_channel_inbound",
        callback=content_word_filter_channel_inbound,
        priority=100,
        plugin_name=PLUGIN_NAME,
        metadata={**metadata, "stage": "channel_inbound_interceptors"},
    )
    logger.info(f"[{PLUGIN_NAME}] 已注册入站 / 渠道入站拦截器")


def teardown(manager: Any) -> None:
    logger.info(f"[{PLUGIN_NAME}] 正在清理...")
    unregister_inbound_interceptor(f"{PLUGIN_NAME}_inbound")
    unregister_channel_inbound_interceptor(f"{PLUGIN_NAME}_channel_inbound")
    _build_matchers.cache_clear()
    logger.info(f"[{PLUGIN_NAME}] 已清理完成")


def unload() -> None:
    logger.info(f"[{PLUGIN_NAME}] Plugin unloading...")
