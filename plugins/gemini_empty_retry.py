"""
Gemini 空内容自动重试插件 (v1.2.0)

当 Gemini / Vertex / Firebase 渠道返回空内容（无有效正文且无工具调用）或图像生成失败时，
在适配器内部自动轮换渠道 API Key 就地重试，并与外层 SSE `: keepalive` 保活机制协同工作。

核心特性：
1. 支持全系 Gemini 引擎：`gemini`、`vertex-gemini`、`firebaseVertex`、`firebaseVertexDebugToken`、`gemini-cli`、`antigravity`。
2. 支持普通转换路径（OpenAI/Claude 方言转换）与 `/v1beta/models/...` 原生透传路径（Passthrough）。
3. 流式“前置暂存探测 + 见正文即刻放行真流式”：
   - 普通模型：仅在尚未收到有效正文 `content` 或 `tool_calls` 前暂存思考帧；一旦收到首个非空正文或工具调用，立即 flush 暂存帧并切换为实时直通流式（零额外延迟）。
   - 暂存与内部重试期间不向内层 `guard_stream` 吐出数据，由外层 `iter_sse_with_keepalive` 每隔 `keepalive_interval`（默认 20s）向客户端自动发送 `: keepalive` 保活。
   - 若整条流结束仍无任何有效正文或工具调用（如空回或仅输出思维链后截断），在适配器内部自动调用 Key 池切换下一把 Key 重建 URL/Headers 就地重试（兼容已经发送过 `: keepalive` 导致 `_stream_committed=True` 的长思考场景）。
   - 图像生成模型：完整缓冲校验必须包含生成图片且未被 `MAX_TOKENS` 截断，失败则换 Key 重试。
4. 热重载幂等保护：通过底层函数解包标记 `_is_empty_retry_wrapper` 与重入锁防止热重载套娃递归（`RecursionError`）。
"""

import asyncio
import json
import re
from contextlib import aclosing
from typing import Any, Dict, List, Optional, Tuple

from core.log_config import logger
from core.middleware import request_info
from core.plugins import register_request_interceptor, unregister_request_interceptor

PLUGIN_INFO = {
    "name": "gemini_empty_retry",
    "version": "1.2.0",
    "description": "Gemini 空内容自动重试插件，支持全系 Gemini/Vertex/Firebase 渠道、就地换 Key 重试与流式心跳协同",
    "author": "Zoaholic Team",
    "dependencies": [],
    "metadata": {
        "category": "interceptors",
        "tags": ["gemini", "retry", "empty-response", "image"],
        "params_schema": [
            {
                "key": "retries",
                "label": "最大重试次数",
                "type": "number",
                "default": 3,
                "min": 1,
                "max": 10,
                "description": "检测到空内容时的最大就地重试次数（1-10）",
            }
        ],
        "params_hint": "可直接填数字（如 gemini_empty_retry:5）或 retries=5，默认重试 3 次",
    },
}

EXTENSIONS = [
    "interceptors:gemini_empty_retry_request",
]

DEFAULT_MAX_RETRIES = 3
GEMINI_ENGINES = {
    "gemini",
    "vertex-gemini",
    "firebaseVertex",
    "firebaseVertexDebugToken",
    "gemini-cli",
    "antigravity",
}
IMAGE_MODEL_KEYWORDS = ["image", "imagen", "nano-banana"]

# 保存各渠道被包装前的原始适配器，供 teardown 与幂等还原使用
_saved_channel_adapters: Dict[str, Dict[str, Any]] = {}


def _unwrap_fn(fn: Any) -> Any:
    """循环剥离本插件的包装层，始终返回最底层的原始函数，杜绝热重载套娃递归。"""
    visited = set()
    while fn is not None and getattr(fn, "_is_empty_retry_wrapper", False) and id(fn) not in visited:
        visited.add(id(fn))
        raw = getattr(fn, "_raw_adapter", None)
        if raw is None or raw is fn:
            break
        fn = raw
    return fn


def is_image_generation_model(model: str) -> bool:
    if not model:
        return False
    model_lower = model.lower()
    return any(keyword in model_lower for keyword in IMAGE_MODEL_KEYWORDS)


def parse_retry_options(options: Optional[Any]) -> int:
    """解析插件参数，兼容纯数字 '5'、键值对 'retries=5' 及 dict 格式。"""
    if options is None:
        return DEFAULT_MAX_RETRIES
    if isinstance(options, (int, float)) and not isinstance(options, bool):
        return max(1, min(10, int(options)))
    if isinstance(options, dict):
        val = options.get("retries", options.get("max_retries", DEFAULT_MAX_RETRIES))
        try:
            return max(1, min(10, int(val)))
        except (ValueError, TypeError):
            return DEFAULT_MAX_RETRIES
    if isinstance(options, str):
        s = options.strip()
        if not s:
            return DEFAULT_MAX_RETRIES
        if s.startswith("{"):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, dict):
                    return parse_retry_options(parsed)
            except ValueError:
                pass
        m = re.search(r"(?:retries|max_retries)\s*=\s*(\d+)", s, re.IGNORECASE)
        if m:
            return max(1, min(10, int(m.group(1))))
        try:
            return max(1, min(10, int(s)))
        except ValueError:
            logger.warning(f"[gemini_empty_retry] Invalid retry count '{options}', using default {DEFAULT_MAX_RETRIES}")
            return DEFAULT_MAX_RETRIES
    return DEFAULT_MAX_RETRIES


def check_plugin_enabled(enabled_plugins: Optional[List[str]]) -> Tuple[bool, int]:
    if not enabled_plugins:
        return False, DEFAULT_MAX_RETRIES
    for plugin_entry in enabled_plugins:
        if isinstance(plugin_entry, str):
            entry = plugin_entry.strip()
            if entry == "gemini_empty_retry":
                return True, DEFAULT_MAX_RETRIES
            if entry.startswith("gemini_empty_retry:"):
                options = entry[len("gemini_empty_retry:"):]
                return True, parse_retry_options(options)
        elif isinstance(plugin_entry, dict):
            name = plugin_entry.get("name") or plugin_entry.get("plugin")
            if name == "gemini_empty_retry":
                return True, parse_retry_options(plugin_entry.get("params") or plugin_entry.get("options"))
    return False, DEFAULT_MAX_RETRIES


def _extract_chunk_signals(chunk: Any) -> Tuple[str, str, Optional[str], bool, Optional[str], bool]:
    """
    从单个流式/非流式 chunk 中提取语义信号（同时兼容 OpenAI 格式与 Gemini 原生格式）。

    返回:
        (content_text, reasoning_text, function_call_name, has_image, finish_reason, is_error_dict)
    """
    if isinstance(chunk, dict):
        if "error" in chunk:
            return "", "", None, False, None, True
        return _extract_from_json_obj(chunk)

    if isinstance(chunk, (bytes, bytearray)):
        chunk = chunk.decode("utf-8", errors="replace")

    if not isinstance(chunk, str):
        return "", "", None, False, None, False

    stripped = chunk.strip()
    if not stripped or stripped.startswith(":"):
        return "", "", None, False, None, False

    content_parts: List[str] = []
    reasoning_parts: List[str] = []
    func_name: Optional[str] = None
    has_img = False
    finish_reason: Optional[str] = None
    is_err = False

    lines = stripped.splitlines()
    parsed_any_sse = False
    for line in lines:
        line_s = line.strip()
        if line_s.startswith("data:"):
            parsed_any_sse = True
            data_str = line_s[5:].strip()
            if not data_str or data_str == "[DONE]":
                continue
            try:
                obj = json.loads(data_str)
            except ValueError:
                continue
            c, r, fn, img, fr, err = _extract_from_json_obj(obj)
            if c:
                content_parts.append(c)
            if r:
                reasoning_parts.append(r)
            if fn and not func_name:
                func_name = fn
            if img:
                has_img = True
            if fr:
                finish_reason = fr
            if err:
                is_err = True

    if not parsed_any_sse and (stripped.startswith("{") or stripped.startswith("[")):
        try:
            obj = json.loads(stripped)
            if isinstance(obj, list):
                for item in obj:
                    if isinstance(item, dict):
                        c, r, fn, img, fr, err = _extract_from_json_obj(item)
                        if c:
                            content_parts.append(c)
                        if r:
                            reasoning_parts.append(r)
                        if fn and not func_name:
                            func_name = fn
                        if img:
                            has_img = True
                        if fr:
                            finish_reason = fr
                        if err:
                            is_err = True
            elif isinstance(obj, dict):
                return _extract_from_json_obj(obj)
        except ValueError:
            pass

    return "".join(content_parts), "".join(reasoning_parts), func_name, has_img, finish_reason, is_err


def _extract_from_json_obj(data: Dict[str, Any]) -> Tuple[str, str, Optional[str], bool, Optional[str], bool]:
    if not isinstance(data, dict):
        return "", "", None, False, None, False
    if "error" in data and data.get("error"):
        return "", "", None, False, None, True

    content_parts: List[str] = []
    reasoning_parts: List[str] = []
    func_name: Optional[str] = None
    has_img = False
    finish_reason: Optional[str] = None

    # 1. OpenAI 格式 (choices)
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        c0 = choices[0] if isinstance(choices[0], dict) else {}
        if c0.get("finish_reason"):
            finish_reason = c0.get("finish_reason")
        msg_or_delta = c0.get("delta") or c0.get("message") or {}
        if isinstance(msg_or_delta, dict):
            raw_content = msg_or_delta.get("content")
            if isinstance(raw_content, str):
                if raw_content:
                    content_parts.append(raw_content)
                if "![image](" in raw_content:
                    has_img = True
            elif isinstance(raw_content, list):
                for item in raw_content:
                    if isinstance(item, dict):
                        if item.get("type") in ("image_url", "image", "inline_data"):
                            has_img = True
                        elif item.get("type") == "text" and item.get("text"):
                            content_parts.append(str(item["text"]))
                            if "![image](" in str(item["text"]):
                                has_img = True
            rc = msg_or_delta.get("reasoning_content")
            if isinstance(rc, str) and rc:
                reasoning_parts.append(rc)
            fc = msg_or_delta.get("function_call")
            if isinstance(fc, dict) and fc.get("name"):
                func_name = fc.get("name")
            tcs = msg_or_delta.get("tool_calls")
            if isinstance(tcs, list) and tcs:
                tc0 = tcs[0] if isinstance(tcs[0], dict) else {}
                fn_obj = tc0.get("function") or {}
                func_name = (fn_obj.get("name") if isinstance(fn_obj, dict) else None) or tc0.get("id") or "tool_call"

    # 2. Gemini 原生格式 (candidates)
    candidates = data.get("candidates")
    if isinstance(candidates, list) and candidates:
        for cand in candidates:
            if not isinstance(cand, dict):
                continue
            if cand.get("finishReason"):
                finish_reason = cand.get("finishReason")
            parts = (cand.get("content") or {}).get("parts") or []
            if not isinstance(parts, list):
                continue
            for part in parts:
                if not isinstance(part, dict):
                    continue
                if part.get("thought") is True:
                    t = part.get("text")
                    if isinstance(t, str) and t:
                        reasoning_parts.append(t)
                else:
                    t = part.get("text")
                    if isinstance(t, str) and t:
                        content_parts.append(t)
                        if "![image](" in t:
                            has_img = True
                if part.get("inlineData") or part.get("inline_data"):
                    has_img = True
                fc = part.get("functionCall") or part.get("function_call") or part.get("executableCode") or part.get("executable_code")
                if fc and not func_name:
                    func_name = fc.get("name") if isinstance(fc, dict) and fc.get("name") else "functionCall"

    return "".join(content_parts), "".join(reasoning_parts), func_name, has_img, finish_reason, False


def is_content_empty(
    content: str,
    reasoning_content: str,
    function_call_name: Optional[str],
    has_image: bool,
    is_image_model: bool,
    finish_reason: Optional[str] = None,
) -> bool:
    """
    判断完整响应是否为空（需要触发重试）：
    - 有工具/函数调用 -> 不为空 (False)
    - 图像生成模型 -> 必须有图片且未被 MAX_TOKENS/length 截断才算非空
    - 普通聊天模型 -> 必须有非空正文文本 content（或图片）才算非空；
      若只有思维链 reasoning_content 而没有任何正文/工具调用，视为空回（触发重试）。
    """
    if function_call_name:
        return False

    if is_image_model:
        truncated_reasons = {"max_tokens", "length", "MAX_TOKENS"}
        if finish_reason and finish_reason in truncated_reasons:
            return True
        return not has_image

    if has_image:
        return False

    if content and content.strip():
        return False

    return True


def is_plugin_active_for_request() -> Tuple[bool, int, bool]:
    try:
        current_info = request_info.get()
        if not isinstance(current_info, dict):
            return False, DEFAULT_MAX_RETRIES, False
        enabled = bool(current_info.get("_gemini_empty_retry_enabled", False))
        max_retries = int(current_info.get("_gemini_empty_retry_max", DEFAULT_MAX_RETRIES))
        is_image_model = bool(current_info.get("_gemini_empty_retry_is_image_model", False))
        return enabled, max_retries, is_image_model
    except Exception:
        return False, DEFAULT_MAX_RETRIES, False


async def _prepare_retry_credentials(
    current_url: str,
    current_headers: Dict[str, Any],
    model: str,
    is_stream: bool,
    is_passthrough: bool = False,
) -> Tuple[str, Dict[str, Any]]:
    """
    在就地重试时从当前渠道的 Key 池轮换下一把 Key，并通过渠道适配器重新生成匹配的 URL 与 Headers。
    兼容 gemini (AI Studio)、firebaseVertex (project_id:api_key)、firebaseVertexDebugToken 及 vertex-gemini。
    """
    try:
        current_info = request_info.get()
        if not isinstance(current_info, dict):
            return current_url, current_headers

        for k in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "cache_creation_tokens"):
            current_info[k] = 0

        ctx = current_info.get("_gemini_empty_retry_ctx") or {}
        provider = ctx.get("provider") or current_info.get("_provider_cfg")
        engine = ctx.get("engine") or (provider.get("engine") if isinstance(provider, dict) else None) or "gemini"
        req_obj = ctx.get("request")

        if not isinstance(provider, dict):
            return current_url, current_headers

        from core.utils import provider_api_circular_list
        from core.channels import get_channel

        provider_name = provider.get("provider")
        pool = provider_api_circular_list.get(provider_name) if provider_name else None
        if not pool or not getattr(pool, "items", None):
            return current_url, current_headers

        # 修复 sticky_ip 渠道下就地重试反复拿到同一把粘滞 Key 的问题：
        # 在调用 pool.next(model) 前，先对上一把失败/空回的 Key 施加规则冷却或短暂冷却，
        # 使 sticky_ip 的 is_rate_limited(pref_item) 返回 True 并自动顺移借用下一把可用 Key。
        prev_key = current_info.get("_used_api_key")
        if prev_key and prev_key != "*" and len(pool.items) > 1 and not current_info.get("_is_byok_request"):
            try:
                last_err = current_info.pop("_gemini_empty_retry_last_error", None)
                cooled = False
                if isinstance(last_err, dict) and last_err.get("status_code"):
                    from core.key_rules import resolve_key_rules, match_key_rules, apply_key_rule
                    rules = resolve_key_rules(provider.get("preferences") or {})
                    rule = match_key_rules(rules, last_err["status_code"], last_err.get("rule_text") or last_err.get("message") or "")
                    if rule and rule.get("duration"):
                        await apply_key_rule(pool, prev_key, rule)
                        cooled = True
                if not cooled:
                    cd = max(int(getattr(pool, "cooldown_period", 3) or 3), 10)
                    await pool.set_auto_disabled(prev_key, duration=cd, reason="gemini_empty_retry")
            except Exception as _cd_err:
                logger.debug(f"[gemini_empty_retry] Cooling previous key before rotation failed: {_cd_err}")

        next_key = await pool.next(model)
        if not next_key:
            return current_url, current_headers

        current_info["_used_api_key"] = next_key
        if next_key in pool.items:
            current_info["provider_key_index"] = pool.items.index(next_key)

        channel = get_channel(engine)
        adapter = None
        if channel:
            if is_passthrough:
                adapter = getattr(channel, "passthrough_adapter", None) or getattr(channel, "request_adapter", None)
            else:
                adapter = getattr(channel, "request_adapter", None)

        if adapter and req_obj is not None:
            new_url, new_headers, _ = await adapter(req_obj, engine, provider, api_key=next_key)
            if is_stream and "generateContent" in new_url and "streamGenerateContent" not in new_url:
                new_url = new_url.replace("generateContent", "streamGenerateContent")
            elif not is_stream and "streamGenerateContent" in new_url:
                new_url = new_url.replace("streamGenerateContent", "generateContent")
            if is_stream and "streamGenerateContent" in new_url and "alt=sse" in current_url and "alt=sse" not in new_url:
                new_url += "&alt=sse" if "?" in new_url else "?alt=sse"
            return new_url, new_headers

        updated_headers = dict(current_headers or {})
        if ":" in next_key and engine in ("firebaseVertex", "firebaseVertexDebugToken"):
            proj_id, actual_key = next_key.split(":", 1)
            updated_headers["x-goog-api-key"] = actual_key
            new_url = re.sub(r"/projects/[^/]+/", f"/projects/{proj_id}/", current_url)
            return new_url, updated_headers
        updated_headers["x-goog-api-key"] = next_key
        return current_url, updated_headers
    except Exception as e:
        logger.warning(f"[gemini_empty_retry] Key rotation on retry failed, reusing previous credentials: {e}")
        return current_url, current_headers


# ==================== 包装后的响应适配器 ====================

async def _run_non_stream_with_retry(
    raw_adapter,
    client,
    url,
    headers,
    payload,
    model,
    timeout,
    *,
    is_passthrough: bool = False,
):
    enabled, max_retries, is_image_model = is_plugin_active_for_request()
    try:
        current_info = request_info.get()
    except Exception:
        current_info = None

    if not enabled or (isinstance(current_info, dict) and current_info.get("_gemini_empty_retry_in_non_stream")):
        async with aclosing(raw_adapter(client, url, headers, payload, model, timeout)) as src:
            async for chunk in src:
                yield chunk
        return

    if isinstance(current_info, dict):
        current_info["_gemini_empty_retry_in_non_stream"] = True

    try:
        cur_url, cur_headers = url, headers
        last_chunks: List[Any] = []

        for attempt in range(max_retries + 1):
            if attempt > 0:
                cur_url, cur_headers = await _prepare_retry_credentials(
                    cur_url, cur_headers, model, is_stream=False, is_passthrough=is_passthrough
                )

            collected_chunks: List[Any] = []
            total_content = ""
            total_reasoning = ""
            func_call_name: Optional[str] = None
            has_image = False
            finish_reason: Optional[str] = None
            has_error = False

            async with aclosing(raw_adapter(client, cur_url, cur_headers, payload, model, timeout)) as src:
                async for chunk in src:
                    collected_chunks.append(chunk)
                    c, r, fn, img, fr, err = _extract_chunk_signals(chunk)
                    if err:
                        has_error = True
                        break
                    if c:
                        total_content += c
                    if r:
                        total_reasoning += r
                    if fn and not func_call_name:
                        func_call_name = fn
                    if img:
                        has_image = True
                    if fr:
                        finish_reason = fr

            last_chunks = collected_chunks

            if has_error:
                if attempt == 0:
                    for chunk in collected_chunks:
                        yield chunk
                    return
                if attempt < max_retries:
                    logger.warning(
                        f"[gemini_empty_retry] Non-stream upstream error on retry {attempt}/{max_retries} "
                        f"for model={model}, trying next key..."
                    )
                    continue
                for chunk in collected_chunks:
                    yield chunk
                return

            if is_content_empty(total_content, total_reasoning, func_call_name, has_image, is_image_model, finish_reason):
                if attempt < max_retries:
                    reason = (
                        f"truncated (finish_reason={finish_reason})"
                        if (is_image_model and finish_reason in {"max_tokens", "length", "MAX_TOKENS"})
                        else ("no image generated" if is_image_model else "empty content (thought-only or blank)")
                    )
                    logger.warning(
                        f"[gemini_empty_retry] Non-stream {reason} for model={model}, "
                        f"retrying in-place ({attempt + 1}/{max_retries})..."
                    )
                    continue
                logger.error(
                    f"[gemini_empty_retry] Non-stream still empty after {max_retries} retries "
                    f"for model={model}, returning last response"
                )

            for chunk in collected_chunks:
                yield chunk
            return

        for chunk in last_chunks:
            yield chunk
    finally:
        if isinstance(current_info, dict):
            current_info.pop("_gemini_empty_retry_in_non_stream", None)


async def _run_stream_with_retry(
    raw_adapter,
    client,
    url,
    headers,
    payload,
    model,
    timeout,
    *,
    is_passthrough: bool = False,
):
    enabled, max_retries, is_image_model = is_plugin_active_for_request()
    try:
        current_info = request_info.get()
    except Exception:
        current_info = None

    if not enabled or (isinstance(current_info, dict) and current_info.get("_gemini_empty_retry_in_stream")):
        async with aclosing(raw_adapter(client, url, headers, payload, model, timeout)) as src:
            async for chunk in src:
                yield chunk
        return

    if isinstance(current_info, dict):
        current_info["_gemini_empty_retry_in_stream"] = True

    try:
        cur_url, cur_headers = url, headers
        last_buffered: List[Any] = []

        for attempt in range(max_retries + 1):
            if attempt > 0:
                cur_url, cur_headers = await _prepare_retry_credentials(
                    cur_url, cur_headers, model, is_stream=True, is_passthrough=is_passthrough
                )

            buffered_chunks: List[Any] = []
            flushed_to_downstream = False
            total_content = ""
            total_reasoning = ""
            func_call_name: Optional[str] = None
            has_image = False
            finish_reason: Optional[str] = None
            has_error = False

            import time as _time_mod
            import httpx as _httpx_mod
            from core.stream_errors import extract_stream_error as _extract_se
            if isinstance(current_info, dict):
                current_info["_upstream_first_byte_ok"] = False
                current_info["_last_stream_activity_at"] = _time_mod.monotonic()
            attempt_header_timeout = (
                current_info.get("_stream_header_timeout")
                if isinstance(current_info, dict)
                else None
            )
            try:
                attempt_header_timeout = float(attempt_header_timeout) if attempt_header_timeout is not None else None
                if attempt_header_timeout is not None and attempt_header_timeout <= 0:
                    attempt_header_timeout = None
            except (TypeError, ValueError):
                attempt_header_timeout = None

            seen_first_upstream_chunk = False
            header_timed_out = False
            async with aclosing(raw_adapter(client, cur_url, cur_headers, payload, model, timeout)) as src:
                src_iter = src.__aiter__()
                while True:
                    try:
                        if not seen_first_upstream_chunk and attempt_header_timeout:
                            chunk = await asyncio.wait_for(src_iter.__anext__(), timeout=attempt_header_timeout)
                        else:
                            chunk = await src_iter.__anext__()
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        header_timed_out = True
                        break

                    if flushed_to_downstream:
                        yield chunk
                        continue

                    if isinstance(chunk, str) and chunk.lstrip().startswith(":"):
                        continue

                    c, r, fn, img, fr, err = _extract_chunk_signals(chunk)
                    if err:
                        has_error = True
                        if isinstance(current_info, dict):
                            se = _extract_se(chunk) if isinstance(chunk, dict) else None
                            if se:
                                current_info["_gemini_empty_retry_last_error"] = se
                        buffered_chunks.append(chunk)
                        break

                    if not seen_first_upstream_chunk:
                        seen_first_upstream_chunk = True
                        if isinstance(current_info, dict):
                            current_info["_upstream_first_byte_ok"] = True
                    if isinstance(current_info, dict):
                        current_info["_last_stream_activity_at"] = _time_mod.monotonic()

                    buffered_chunks.append(chunk)
                    if c:
                        total_content += c
                    if r:
                        total_reasoning += r
                    if fn and not func_call_name:
                        func_call_name = fn
                    if img:
                        has_image = True
                    if fr:
                        finish_reason = fr

                    if not is_image_model and (
                        (total_content and total_content.strip()) or func_call_name or has_image
                    ):
                        flushed_to_downstream = True
                        for b_chunk in buffered_chunks:
                            yield b_chunk
                        buffered_chunks.clear()

            if flushed_to_downstream:
                return

            if header_timed_out:
                stream_committed = bool(isinstance(current_info, dict) and current_info.get("_stream_committed"))
                if attempt == 0 and not stream_committed:
                    raise _httpx_mod.ReadTimeout(f"Stream header/first-byte timeout after {attempt_header_timeout}s")
                if attempt < max_retries:
                    logger.warning(
                        f"[gemini_empty_retry] Stream header timeout ({attempt_header_timeout}s) on attempt "
                        f"{attempt + 1}/{max_retries + 1} for model={model}, rotating key and retrying in-place..."
                    )
                    continue
                raise _httpx_mod.ReadTimeout(f"Stream header/first-byte timeout after {attempt_header_timeout}s")

            last_buffered = buffered_chunks

            if has_error:
                stream_committed = bool(isinstance(current_info, dict) and current_info.get("_stream_committed"))
                if attempt == 0 and not stream_committed:
                    for b_chunk in buffered_chunks:
                        yield b_chunk
                    return
                if attempt < max_retries:
                    logger.warning(
                        f"[gemini_empty_retry] Stream upstream error on attempt {attempt + 1}/{max_retries + 1} "
                        f"for model={model}, rotating key and retrying in-place..."
                    )
                    continue
                for b_chunk in buffered_chunks:
                    yield b_chunk
                return

            if is_content_empty(total_content, total_reasoning, func_call_name, has_image, is_image_model, finish_reason):
                if attempt < max_retries:
                    reason = (
                        f"truncated (finish_reason={finish_reason})"
                        if (is_image_model and finish_reason in {"max_tokens", "length", "MAX_TOKENS"})
                        else ("no image generated" if is_image_model else "empty content (thought-only or blank)")
                    )
                    logger.warning(
                        f"[gemini_empty_retry] Stream {reason} for model={model}, "
                        f"retrying in-place ({attempt + 1}/{max_retries})..."
                    )
                    continue
                logger.error(
                    f"[gemini_empty_retry] Stream still empty after {max_retries} retries "
                    f"for model={model}, replaying last buffered chunks"
                )

            for b_chunk in buffered_chunks:
                yield b_chunk
            return

        for b_chunk in last_buffered:
            yield b_chunk
    finally:
        if isinstance(current_info, dict):
            current_info.pop("_gemini_empty_retry_in_stream", None)


async def wrapped_fetch_gemini_response(client, url, headers, payload, model, timeout):
    from core.channels import gemini_channel as gc_mod
    raw = getattr(gc_mod, "_unwrapped_fetch_gemini_response", None) or _unwrap_fn(gc_mod.fetch_gemini_response)
    async for chunk in _run_non_stream_with_retry(raw, client, url, headers, payload, model, timeout, is_passthrough=False):
        yield chunk


async def wrapped_fetch_gemini_response_stream(client, url, headers, payload, model, timeout):
    from core.channels import gemini_channel as gc_mod
    raw = getattr(gc_mod, "_unwrapped_fetch_gemini_response_stream", None) or _unwrap_fn(gc_mod.fetch_gemini_response_stream)
    async for chunk in _run_stream_with_retry(raw, client, url, headers, payload, model, timeout, is_passthrough=False):
        yield chunk


async def wrapped_passthrough_gemini_response(client, url, headers, payload, model, timeout):
    from core.passthrough import _fetch_passthrough_response
    try:
        current_info = request_info.get() or {}
    except Exception:
        current_info = {}
    ctx = current_info.get("_gemini_empty_retry_ctx") or {}
    provider = ctx.get("provider") or current_info.get("_provider_cfg") or {}
    engine = ctx.get("engine") or provider.get("engine") or "gemini"

    async def _raw_pt_non_stream(c, u, h, p, m, t):
        # 修改原因：这里调用 _fetch_passthrough_response 时不带 enabled_plugins，会走快路径按网络分片多次 yield 响应体。
        #   一方面 core 的 passthrough_response_adapter 分支会对每个分片单独执行非流式响应拦截器，渠道插件拿到的是半截 JSON；
        #   另一方面下方空回检测也按分片解析，解析失败被当成空回，启用本插件时正常响应也会换 Key 重试到上限。
        # 修改方式：把文本分片拼成完整响应体后一次性 yield；错误 dict 等非文本项原样透出。
        # 目的：非流式透传与非透传路径一致，拦截器和空回检测都基于完整响应体工作。
        text_parts: List[str] = []
        async for item in _fetch_passthrough_response(
            c, u, h, p, t, engine=engine, model=m, provider=provider
        ):
            if isinstance(item, (bytes, bytearray)):
                item = item.decode("utf-8", errors="replace")
            if isinstance(item, str):
                text_parts.append(item)
                continue
            if text_parts:
                yield "".join(text_parts)
                text_parts = []
            yield item
        if text_parts:
            yield "".join(text_parts)

    async for chunk in _run_non_stream_with_retry(
        _raw_pt_non_stream, client, url, headers, payload, model, timeout, is_passthrough=True
    ):
        yield chunk


async def wrapped_passthrough_gemini_stream(client, url, headers, payload, model, timeout):
    from core.passthrough import _fetch_passthrough_stream
    try:
        current_info = request_info.get() or {}
    except Exception:
        current_info = {}
    ctx = current_info.get("_gemini_empty_retry_ctx") or {}
    provider = ctx.get("provider") or current_info.get("_provider_cfg") or {}
    engine = ctx.get("engine") or provider.get("engine") or "gemini"

    async def _raw_pt_stream(c, u, h, p, m, t):
        async for item in _fetch_passthrough_stream(
            c, u, h, p, t, engine=engine, model=m, provider=provider
        ):
            yield item

    async for chunk in _run_stream_with_retry(
        _raw_pt_stream, client, url, headers, payload, model, timeout, is_passthrough=True
    ):
        yield chunk


for _wrapper_fn in (
    wrapped_fetch_gemini_response,
    wrapped_fetch_gemini_response_stream,
    wrapped_passthrough_gemini_response,
    wrapped_passthrough_gemini_stream,
):
    _wrapper_fn._is_empty_retry_wrapper = True


# ==================== 请求拦截器 ====================

async def gemini_empty_retry_request_interceptor(
    request: Any,
    engine: str,
    provider: Dict[str, Any],
    api_key: Optional[str],
    url: str,
    headers: Dict[str, Any],
    payload: Dict[str, Any],
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    if engine not in GEMINI_ENGINES:
        return url, headers, payload

    enabled_plugins = provider.get("preferences", {}).get("enabled_plugins", [])
    enabled, max_retries = check_plugin_enabled(enabled_plugins)
    if not enabled:
        return url, headers, payload

    try:
        current_info = request_info.get()
        if isinstance(current_info, dict) and "_gemini_empty_retry_enabled" not in current_info:
            model = ""
            if isinstance(request, dict):
                model = request.get("model", "")
            elif hasattr(request, "model"):
                model = request.model or ""

            is_img_model = is_image_generation_model(model)
            current_info["_gemini_empty_retry_enabled"] = True
            current_info["_gemini_empty_retry_max"] = max_retries
            current_info["_gemini_empty_retry_is_image_model"] = is_img_model
            current_info["_gemini_empty_retry_ctx"] = {
                "request": request,
                "engine": engine,
                "provider": provider,
            }
            logger.debug(
                f"[gemini_empty_retry] Enabled for provider={provider.get('provider')}, "
                f"engine={engine}, model={model}, max_retries={max_retries}, is_image_model={is_img_model}"
            )
    except Exception as e:
        logger.error(f"[gemini_empty_retry] Failed to initialize state: {e}")

    return url, headers, payload


# ==================== 渠道包装管理 ====================

def wrap_gemini_channel():
    """幂等包装所有 Gemini 同族渠道适配器，不丢失原字段且支持多次热重载。"""
    try:
        from core.channels import get_channel
        from core.channels import gemini_channel as gc_mod

        raw_non_stream = getattr(gc_mod, "_unwrapped_fetch_gemini_response", None) or _unwrap_fn(gc_mod.fetch_gemini_response)
        raw_stream = getattr(gc_mod, "_unwrapped_fetch_gemini_response_stream", None) or _unwrap_fn(gc_mod.fetch_gemini_response_stream)

        gc_mod._unwrapped_fetch_gemini_response = raw_non_stream
        gc_mod._unwrapped_fetch_gemini_response_stream = raw_stream

        wrapped_fetch_gemini_response._raw_adapter = raw_non_stream
        wrapped_fetch_gemini_response_stream._raw_adapter = raw_stream

        gc_mod.fetch_gemini_response = wrapped_fetch_gemini_response
        gc_mod.fetch_gemini_response_stream = wrapped_fetch_gemini_response_stream

        for ch_id in GEMINI_ENGINES:
            ch = get_channel(ch_id)
            if ch is None:
                continue
            if ch_id not in _saved_channel_adapters:
                _saved_channel_adapters[ch_id] = {
                    "response_adapter": _unwrap_fn(ch.response_adapter),
                    "stream_adapter": _unwrap_fn(ch.stream_adapter),
                    "passthrough_response_adapter": _unwrap_fn(getattr(ch, "passthrough_response_adapter", None)),
                    "passthrough_stream_adapter": _unwrap_fn(getattr(ch, "passthrough_stream_adapter", None)),
                }
            if ch_id in ("gemini", "vertex-gemini"):
                ch.response_adapter = wrapped_fetch_gemini_response
                ch.stream_adapter = wrapped_fetch_gemini_response_stream
            if getattr(ch, "passthrough_response_adapter", None) is None or getattr(
                ch.passthrough_response_adapter, "_is_empty_retry_wrapper", False
            ):
                ch.passthrough_response_adapter = wrapped_passthrough_gemini_response
            if getattr(ch, "passthrough_stream_adapter", None) is None or getattr(
                ch.passthrough_stream_adapter, "_is_empty_retry_wrapper", False
            ):
                ch.passthrough_stream_adapter = wrapped_passthrough_gemini_stream

        logger.info("[gemini_empty_retry] Wrapped Gemini family channels (v1.2.0)")
    except Exception as e:
        logger.error(f"[gemini_empty_retry] Failed to wrap Gemini channels: {e}")


def unwrap_gemini_channel():
    """恢复原始 Gemini 同族渠道适配器。"""
    try:
        from core.channels import get_channel
        from core.channels import gemini_channel as gc_mod

        raw_non_stream = getattr(gc_mod, "_unwrapped_fetch_gemini_response", None)
        raw_stream = getattr(gc_mod, "_unwrapped_fetch_gemini_response_stream", None)
        if raw_non_stream is not None:
            gc_mod.fetch_gemini_response = raw_non_stream
        if raw_stream is not None:
            gc_mod.fetch_gemini_response_stream = raw_stream

        for ch_id in GEMINI_ENGINES:
            ch = get_channel(ch_id)
            if ch is None:
                continue
            saved = _saved_channel_adapters.get(ch_id) or {}
            if ch_id in ("gemini", "vertex-gemini"):
                if saved.get("response_adapter") or raw_non_stream:
                    ch.response_adapter = saved.get("response_adapter") or raw_non_stream
                if saved.get("stream_adapter") or raw_stream:
                    ch.stream_adapter = saved.get("stream_adapter") or raw_stream
            if getattr(getattr(ch, "passthrough_response_adapter", None), "_is_empty_retry_wrapper", False):
                ch.passthrough_response_adapter = saved.get("passthrough_response_adapter")
            if getattr(getattr(ch, "passthrough_stream_adapter", None), "_is_empty_retry_wrapper", False):
                ch.passthrough_stream_adapter = saved.get("passthrough_stream_adapter")

        _saved_channel_adapters.clear()
        logger.info("[gemini_empty_retry] Unwrapped Gemini family channels")
    except Exception as e:
        logger.error(f"[gemini_empty_retry] Failed to unwrap Gemini channels: {e}")


# ==================== 插件生命周期 ====================


def setup(manager=None):
    register_request_interceptor(
        interceptor_id="gemini_empty_retry_request",
        callback=gemini_empty_retry_request_interceptor,
        priority=50,
        plugin_name="gemini_empty_retry",
        metadata={"description": "Gemini 空内容自动重试请求拦截器"},
        overwrite=True,
    )
    wrap_gemini_channel()


def teardown(manager=None):
    unregister_request_interceptor("gemini_empty_retry_request")
    unwrap_gemini_channel()
