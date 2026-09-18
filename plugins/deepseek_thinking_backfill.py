"""DeepSeek thinking 回传补全插件（deepseek_thinking_backfill）

定位：
- 作为"请求拦截器插件"运行（不侵入 core/request.py 与 core/passthrough.py）。
- 仅当某个渠道在 provider.preferences.enabled_plugins 显式启用本插件时生效。
- 转换路径（get_payload）与透传路径（process_request_passthrough）最终都会经过
  apply_request_interceptors，本插件在该位置对最终上游 payload 做补全。

背景：
- DeepSeek V4 系列（deepseek-v4-pro / deepseek-v4-flash 等）默认启用 thinking 模式，
  要求会话历史中的 assistant 消息回传上一轮思维链：
  - OpenAI 兼容端点（api.deepseek.com/v1）检查 messages[].reasoning_content，
    缺失时报 "The `reasoning_content` in the thinking mode must be passed back to the API."
  - Anthropic 兼容端点（api.deepseek.com/anthropic）检查 messages[].content[] 中的
    thinking block，缺失时报 "The `content[].thinking` in the thinking mode must be passed back to the API."
- 多数客户端（Claude Code / Codex / 酒馆等）不回传或丢弃思维链，导致多轮工具调用
  从第二轮起全部 400。

修复方式：
- engine=openai：assistant 消息 content 为字符串/None 时补顶层
  reasoning_content=""；content 为数组时在缺失 thinking block 的数组头部前插
  {"type":"thinking","thinking":""}。
- engine=claude：assistant 消息 content 数组缺失 thinking/redacted_thinking block 时
  前插 {"type":"thinking","thinking":""}；content 为字符串时先转数组再插入。
- 请求显式携带 {"thinking":{"type":"disabled"}} 时跳过（thinking 关闭后上游不校验回传）。
- 非 DeepSeek 渠道（模型名 / base_url / URL 均无 deepseek 字样）不处理。

插件参数（可选）：
- enabled_plugins 写 "deepseek_thinking_backfill:<filler>" 可自定义填充字符串，
  默认空字符串 ""。个别聚合站会把空字符串剥掉或拒绝（如 OpenRouter/Kimi 行为），
  此时可用 "deepseek_thinking_backfill: " 以单个空格填充。

配置位置：
- provider.preferences.enabled_plugins:
    - deepseek_thinking_backfill
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

from core.log_config import logger
from core.plugins import (
    register_request_interceptor,
    unregister_request_interceptor,
)

PLUGIN_INFO = {
    "name": "deepseek_thinking_backfill",
    "version": "1.0.0",
    "description": "DeepSeek thinking 回传补全 - 对缺失思维链的 assistant 历史消息补空 reasoning_content / thinking block，修复 V4 thinking 模式多轮 400",
    "author": "Zoaholic Team",
    "dependencies": [],
    "metadata": {
        "category": "interceptors",
        "tags": ["deepseek", "thinking", "reasoning_content", "compat"],
        "params_hint": "可选：填充字符串（默认空字符串），如 'deepseek_thinking_backfill: ' 用空格填充。",
        "provider_config": {
            "key": "deepseek_thinking_backfill",
            "type": "string",
            "title": "DeepSeek thinking 回传补全",
            "description": "对缺失思维链的 assistant 消息补空 reasoning_content（OpenAI 格式）或 thinking block（Anthropic 格式），修复 DeepSeek V4 thinking 模式多轮 400。",
            "example": "",
        },
    },
}

EXTENSIONS = [
    "interceptors:deepseek_thinking_backfill_request",
]

# V4 系列（默认 thinking）与 V3 时代思考模型的命名特征
_THINKING_MODEL_RE = re.compile(r"deepseek[-_a-z0-9.]*?(v4|reasoner)", re.IGNORECASE)

_THINKING_BLOCK_TYPES = ("thinking", "redacted_thinking")


def _is_deepseek_context(provider: Dict[str, Any], model: str, url: str) -> bool:
    """判断当前请求是否发往 DeepSeek（模型名 / base_url / 最终 URL 任一含 deepseek）。"""
    model_l = (model or "").lower()
    if "deepseek" in model_l:
        return True
    url_l = (url or "").lower()
    if "deepseek" in url_l:
        return True
    if isinstance(provider, dict):
        for key in ("base_url", "provider", "name"):
            value = provider.get(key)
            if isinstance(value, str) and "deepseek" in value.lower():
                return True
    return False


def _thinking_disabled(payload: Dict[str, Any]) -> bool:
    """请求显式关闭 thinking（DeepSeek 原生控制参数 {"thinking":{"type":"disabled"}}）。"""
    thinking = payload.get("thinking")
    return isinstance(thinking, dict) and thinking.get("type") == "disabled"


def _thinking_active(payload: Dict[str, Any], model: str) -> bool:
    """判断 thinking 模式是否生效：显式 enabled 优先，其次按模型名判断（V4 默认启用）。"""
    thinking = payload.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") == "enabled":
        return True
    return bool(_THINKING_MODEL_RE.search(model or ""))


def _has_thinking_block(content: Any) -> bool:
    """content 数组中是否已含 thinking / redacted_thinking block。"""
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict) and block.get("type") in _THINKING_BLOCK_TYPES
        for block in content
    )


def _backfill_openai(payload: Dict[str, Any], filler: str) -> int:
    """OpenAI chat completions 格式补全。

    - content 为字符串 / None / 缺失：顶层补 reasoning_content=filler。
    - content 为数组（DeepSeek V4 原生 content[].thinking 形态）：缺 thinking block 时
      在数组头部前插 {"type":"thinking","thinking":filler}，顶层 reasoning_content 不动。
    """
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return 0
    patched = 0
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, list):
            if not _has_thinking_block(content):
                content.insert(0, {"type": "thinking", "thinking": filler})
                patched += 1
        else:
            if not msg.get("reasoning_content"):
                msg["reasoning_content"] = filler
                patched += 1
    return patched


def _backfill_claude(payload: Dict[str, Any], filler: str) -> int:
    """Anthropic messages 格式补全。

    - content 为数组：缺 thinking / redacted_thinking block 时在数组头部前插
      {"type":"thinking","thinking":filler}（tool_use-only 消息同样适用）。
    - content 为字符串：先转 [{"type":"text","text":...}] 再前插 thinking block。
    - content 为 None / 缺失：直接给 [{"type":"thinking","thinking":filler}]。
    """
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return 0
    patched = 0
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, list):
            if not _has_thinking_block(content):
                content.insert(0, {"type": "thinking", "thinking": filler})
                patched += 1
        elif isinstance(content, str):
            blocks: list = [{"type": "text", "text": content}] if content else []
            blocks.insert(0, {"type": "thinking", "thinking": filler})
            msg["content"] = blocks
            patched += 1
        else:
            msg["content"] = [{"type": "thinking", "thinking": filler}]
            patched += 1
    return patched


async def deepseek_thinking_backfill_request_interceptor(
    request: Any,
    engine: str,
    provider: Dict[str, Any],
    api_key: Optional[str],
    url: str,
    headers: Dict[str, Any],
    payload: Dict[str, Any],
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """请求拦截器入口：对发往 DeepSeek 的最终 payload 补全 assistant 思维链回传。"""
    try:
        if not isinstance(payload, dict):
            return url, headers, payload
        model = ""
        if isinstance(payload.get("model"), str):
            model = payload["model"]
        if not _is_deepseek_context(provider, model, url):
            return url, headers, payload
        if _thinking_disabled(payload):
            return url, headers, payload
        if engine == "claude":
            if not _thinking_active(payload, model):
                return url, headers, payload
            patched = _backfill_claude(payload, "")
        elif engine == "openai":
            patched = _backfill_openai(payload, "")
        else:
            return url, headers, payload
        if patched:
            logger.info(
                "[deepseek_thinking_backfill] engine=%s model=%s 补全 %d 条 assistant 消息的思维链回传",
                engine, model, patched,
            )
        return url, headers, payload
    except Exception as exc:  # 防御：插件异常不阻断请求
        logger.error(f"[deepseek_thinking_backfill] 处理失败，原样放行: {exc}")
        return url, headers, payload


def setup(manager):
    register_request_interceptor(
        "deepseek_thinking_backfill_request",
        deepseek_thinking_backfill_request_interceptor,
        priority=900,
        plugin_name="deepseek_thinking_backfill",
    )


def teardown(manager):
    unregister_request_interceptor("deepseek_thinking_backfill_request")
