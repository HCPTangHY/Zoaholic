"""Bounded stream inspection, independent of raw-response logging and plugins."""
from __future__ import annotations

import asyncio
import json
import re
from collections import deque

from fastapi import HTTPException

from core.stream_utils import close_async_iterator

MAX_EVENT_BYTES = 64 * 1024
BOOTSTRAP_CHUNKS = 32
BOOTSTRAP_BYTES = 64 * 1024
BOOTSTRAP_SECONDS = 3.0

# gRPC/Google 风格错误信封用字符串 status 代替 HTTP 状态码（Gemini、Vertex、Firebase）。
_GRPC_STATUS_MAP = {
    "RESOURCE_EXHAUSTED": 429,
    "UNAVAILABLE": 503,
    "OVERLOADED": 503,
    "DEADLINE_EXCEEDED": 504,
    "UNAUTHENTICATED": 401,
    "PERMISSION_DENIED": 403,
    "INVALID_ARGUMENT": 400,
    "NOT_FOUND": 404,
    "ABORTED": 409,
    "INTERNAL": 502,
}

# 各协议不含语义内容的空生命周期事件：Responses、Anthropic、通用 ping。
_PROVISIONAL_EVENT_TYPES = {"response.created", "response.queued", "response.in_progress", "ping"}


def _http_status(value):
    try:
        code = int(value)
        return code if 400 <= code <= 599 else None
    except (TypeError, ValueError):
        return None


def extract_stream_error(data, event_type="", _depth=0):
    """Only inspect protocol error envelopes, never model text or tool arguments."""
    if _depth > 8:
        return None
    if isinstance(data, (str, bytes, bytearray)):
        if len(data) > MAX_EVENT_BYTES:
            return None
        try:
            data = json.loads(data)
        except (ValueError, TypeError):
            return None
    if not isinstance(data, dict):
        return None
    event_type = data.get("type") or event_type
    response = data.get("response")
    failed = event_type in ("error", "response.failed") or data.get("status") == "failed"
    if isinstance(response, dict):
        failed = failed or response.get("status") == "failed"
    error = data.get("error")
    if error is None and isinstance(response, dict):
        error = response.get("error")
    if error is None:
        for choice in data.get("choices") or []:
            if isinstance(choice, dict) and choice.get("error"):
                error = choice["error"]
                break
    # Some compatible providers send an empty error placeholder in successful chunks.
    if isinstance(error, dict) and not any(error.values()) and not failed:
        return None
    if not error and not failed:
        return None

    nested = None
    for child in (data.get("details"), error):
        nested = extract_stream_error(child, _depth=_depth + 1)
        if nested:
            break
    node = error if isinstance(error, dict) else data
    message = node.get("message")
    # Zoaholic check_response 的内部错误信封：{"error": "...标签", "status_code": 400, "details": {"detail": "上游真实原因"}}
    # 上游真实原因比泛化标签更有用，优先提取。
    if not message and isinstance(data.get("details"), dict):
        details = data["details"]
        message = details.get("detail") or details.get("message")
    if not message and isinstance(data.get("details"), str):
        message = data["details"]
    if not message and isinstance(error, str):
        message = error
    kind = node.get("type") or event_type or "upstream_error"
    code = node.get("code")
    # A few gateways JSON-encode a second error envelope in the error message.
    if not nested and isinstance(message, str) and message.lstrip().startswith("{"):
        nested = extract_stream_error(message, _depth=_depth + 1)
    if nested:
        message, kind, code = nested["message"], nested["type"], nested["code"]
    status = _http_status(data.get("status_code")) or _http_status(data.get("status"))
    status = status or _http_status(node.get("status_code")) or _http_status(code)
    if not status:
        for candidate in (data.get("status"), node.get("status"), node.get("state")):
            mapped = _GRPC_STATUS_MAP.get(str(candidate or "").upper())
            if mapped:
                status = mapped
                break
    if not status and nested:
        status = nested["status_code"]
    if not status:
        values = {str(kind).lower(), str(code).lower()}
        if values & {"rate_limit_error", "rate_limit_exceeded", "rate_limit_exhausted", "insufficient_quota", "resource_exhausted"}:
            status = 429
        elif values & {"server_is_overloaded", "overloaded_error", "service_unavailable_error", "unavailable"}:
            status = 503
        else:
            status = 502
    message = str(message or "Upstream stream failed")[:4096]
    kind, code = str(kind)[:128], str(code)[:128] if code is not None else None
    # Keep the whole small error envelope for keyword rules, not the whole stream.
    # iterencode avoids serializing a huge response.failed.output into another huge string.
    parts, remaining = [], 8192
    for part in json.JSONEncoder(ensure_ascii=False, default=str).iterencode(data):
        parts.append(part[:remaining])
        remaining -= len(parts[-1])
        if remaining <= 0:
            break
    rule_text = f"{kind} {code or ''} {message}\n{''.join(parts)}"
    return {"message": message, "type": kind, "code": code, "status_code": status, "rule_text": rule_text}


class UpstreamStreamError(HTTPException):
    def __init__(self, error):
        self.error = error
        super().__init__(status_code=error["status_code"], detail=error["message"])


def stream_error_from_exception(exc):
    if isinstance(exc, UpstreamStreamError):
        return exc.error
    return {
        "message": str(getattr(exc, "detail", None) or str(exc) or type(exc).__name__)[:4096],
        "type": "upstream_stream_error", "code": None,
        "status_code": _http_status(getattr(exc, "status_code", None)) or 502,
        "rule_text": str(getattr(exc, "detail", None) or exc)[:8192],
    }


def stream_has_semantic_output(data, classifier=None):
    """按结构判断事件是否携带语义内容；渠道注册表可注入自己的分类器。

    classifier(event) 返回 True=可暂存（无语义输出），False=必须提交，None=不表态走通用判断。
    通用兜底覆盖 OpenAI Chat（choices）、Responses（response.*）、Anthropic（message/content_block）、
    Gemini/Vertex（candidates.parts）；未知结构一律提交。
    """
    if not isinstance(data, dict):
        return True
    if classifier is not None:
        try:
            verdict = classifier(data)
        except Exception:
            verdict = None
        if verdict is not None:
            return not verdict
    kind = data.get("type", "")
    if kind in _PROVISIONAL_EVENT_TYPES:
        return bool((data.get("response") or {}).get("output"))
    if "choices" in data:
        for choice in data.get("choices") or []:
            if not isinstance(choice, dict):
                return True
            for field in ("delta", "message"):
                delta = choice.get(field) or {}
                if not isinstance(delta, dict):
                    return True
                if any(value for key, value in delta.items() if key not in {"role"}):
                    return True
        return False
    if "candidates" in data:
        # Gemini/Vertex：finishReason 只是终止标记（在 inspect 中处理），内容以 parts 为准。
        for candidate in data.get("candidates") or []:
            if not isinstance(candidate, dict):
                return True
            parts = (candidate.get("content") or {}).get("parts") or []
            for part in parts:
                if not isinstance(part, dict):
                    return True
                if any(part.get(field) for field in (
                    "text", "functionCall", "function_call", "inlineData", "inline_data",
                    "executableCode", "executable_code", "codeExecutionResult",
                )):
                    return True
        return False
    if kind == "message_start":
        return bool((data.get("message") or {}).get("content"))
    if kind == "content_block_start":
        block = data.get("content_block") or {}
        return block.get("type") not in {"text", "thinking"} or bool(block.get("text") or block.get("thinking"))
    # 工具生命周期事件即使没有文本也算输出。
    if kind == "response.output_item.added":
        item = data.get("item") or {}
        return item.get("type") not in {"message", "reasoning"} or bool(item.get("content") or item.get("summary"))
    if kind in {"response.content_part.added", "response.reasoning_summary_part.added"}:
        return bool((data.get("part") or {}).get("text"))
    if kind in {"response.completed", "response.incomplete"}:
        return bool((data.get("response") or {}).get("output"))
    return True


class StreamInspector:
    """SSE lines/events may span HTTP chunks. Inspection buffers never exceed 64 KiB.

    ponytail: oversized/unknown events commit without buffering or rejecting them;
    add protocol-specific classification here if a new provisional event is needed.
    """
    def __init__(self, classifier=None):
        self.classifier = classifier
        self.line = bytearray()
        self.data = bytearray()
        self.event = ""
        self.discard_line = False
        self.discard_event = False
        self.after_cr = False
        self.has_output = False
        self.commit = False
        self.terminal = False
        self.error = None

    def inspect(self, data):
        error = extract_stream_error(data, self.event)
        if error:
            self.error = self.error or error
            self.commit = True
            return
        if data == "[DONE]":
            self.terminal = self.commit = True
            return
        if isinstance(data, dict):
            self.terminal |= data.get("type") in {"response.completed", "response.incomplete", "message_stop"}
            self.terminal |= any(isinstance(c, dict) and c.get("finish_reason") for c in data.get("choices") or [])
            self.terminal |= any(isinstance(c, dict) and c.get("finishReason") for c in data.get("candidates") or [])
        output = stream_has_semantic_output(data, self.classifier)
        self.has_output |= output
        self.commit |= output or self.terminal

    def _dispatch(self):
        if self.data and not self.discard_event:
            try:
                self.inspect(json.loads(self.data))
            except ValueError:
                self.inspect({"type": self.event, "message": self.data.decode("utf-8", "replace")}) if self.event in {"error", "response.failed"} else self.inspect(None)
        self.data.clear()
        self.event = ""
        self.discard_event = False

    def _line(self, line):
        if not line:
            self._dispatch()
        elif line.startswith(b"event:"):
            self.event = line[6:].strip().decode("utf-8", "replace")[:128]
        elif line.startswith(b"data:"):
            part = line[5:].lstrip(b" ")
            if part == b"[DONE]":
                self.inspect("[DONE]")
            elif not self.discard_event:
                if len(self.data) + len(part) + 1 > MAX_EVENT_BYTES:
                    self.data.clear()
                    self.discard_event = self.has_output = self.commit = True
                else:
                    self.data.extend(part + b"\n")
        elif line.startswith((b":", b"id:", b"retry:")):
            return
        else:
            try:
                self.inspect(json.loads(line))
            except ValueError:
                self.inspect(None)

    def feed(self, item):
        if isinstance(item, dict):
            self.inspect(item)
            return
        if isinstance(item, str):
            item = item.encode("utf-8")
        if not isinstance(item, (bytes, bytearray)):
            self.inspect(None)
            return
        # Work on bounded slices; an image-sized HTTP chunk must not be retained.
        start = 0
        for match in re.finditer(br"[\r\n]", item):
            end = match.start()
            if not self.discard_line:
                if len(self.line) + end - start > MAX_EVENT_BYTES:
                    self.line.clear()
                    self.discard_event = self.has_output = self.commit = True
                else:
                    self.line.extend(item[start:end])
                    if not (self.after_cr and item[end] == 10 and not self.line):
                        self._line(bytes(self.line))
                    self.line.clear()
            self.discard_line = False
            self.after_cr = item[end] == 13
            start = end + 1
        if start < len(item):
            self.after_cr = False
            if len(self.line) + len(item) - start > MAX_EVENT_BYTES:
                self.line.clear()
                self.discard_line = self.discard_event = self.has_output = self.commit = True
            elif not self.discard_line:
                self.line.extend(item[start:])

    def finish(self):
        if self.line and not self.discard_line:
            self._line(bytes(self.line))
        self.line.clear()
        self._dispatch()


def reset_stream_state(info):
    for key in ("_stream_error", "_stream_error_handled", "_stream_committed", "_stream_has_output", "_stream_raw_inspector", "_channel_stats_call", "error_message", "adapter_metrics_managed", "content_start_time"):
        info.pop(key, None)
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "cache_creation_tokens"):
        info[key] = 0
    info["_used_api_key"] = None


async def record_stream_failure(info, error):
    """Disable the actual request key before forwarding the error to a retrying client."""
    error = info.setdefault("_stream_error", error)
    info["success"] = False
    # HTTP 状态已发送时，只能修正统计结果；规则仍按原始上游状态匹配。
    info["status_code"] = 502 if info.get("_stream_committed") else error["status_code"]
    info["error_message"] = error["message"]
    if not info.get("_stream_committed") or info.get("_stream_error_handled"):
        return
    from core.key_rules import resolve_key_rules, match_key_rules, apply_key_rule
    from core.utils import provider_api_circular_list
    from core.log_config import logger

    # 多层包装器可能观察到同一错误，只处理一次。持久化故障不能遮蔽上游错误。
    info["_stream_error_handled"] = True
    provider = info.get("_provider_cfg") or {}
    channel_id = provider.get("provider") or info.get("provider_id") or info.get("provider")
    key = info.get("_used_api_key")
    pool = provider_api_circular_list.get(channel_id)
    if key and key != "*" and pool and not info.get("_is_byok_request"):
        try:
            rule = match_key_rules(resolve_key_rules(provider.get("preferences") or {}), error["status_code"], error["rule_text"])
            await apply_key_rule(pool, key, rule)
        except Exception as exc:
            logger.warning("[stream_guard] Key rule processing failed: provider=%s exception=%s",
                           channel_id, type(exc).__name__)
        finally:
            # Do not erase a concurrent request's newer successful sticky assignment.
            async with pool.lock:
                ip = info.get("client_ip")
                session = pool._sticky_sessions.get(ip)
                if session and 0 <= session[0] < len(pool.items) and pool.items[session[0]] == key:
                    pool._sticky_sessions.pop(ip, None)


async def observe_stream_chunk(info, chunk, inspector=None):
    if inspector is None:
        inspector = info.get("_stream_raw_inspector")
        if inspector is None:
            inspector = info["_stream_raw_inspector"] = StreamInspector()
    inspector.feed(chunk)
    if inspector.has_output:
        info["_stream_has_output"] = True
    if inspector.error:
        await record_stream_failure(info, inspector.error)


async def guard_stream(source, *, info=None, classifier=None, max_chunks=BOOTSTRAP_CHUNKS, max_bytes=BOOTSTRAP_BYTES, error_triggers=()):
    """Yield the original chunks only after a bounded bootstrap; close the owned source."""
    if info is None:
        try:
            from core.middleware import request_info
            info = request_info.get()
        except Exception:
            info = None
        info = info if isinstance(info, dict) else {}
    inspector = StreamInspector(classifier=classifier)
    buffered = deque()
    size = 0
    released = False
    try:
        async for item in source:
            inspector.feed(item)
            if inspector.has_output:
                info["_stream_has_output"] = True
            error = info.get("_stream_error") or inspector.error
            if not error and not released and error_triggers and any(t in str(item) for t in error_triggers):
                error = stream_error_from_exception(HTTPException(502, "Upstream error trigger matched"))
            if error:
                await record_stream_failure(info, error)
                # 消费生成器不等于发送 HTTP。非流式组装仍须把错误抛回 handler。
                # 内部 dict 错误也交给外层按客户端方言渲染，不能原样当正文发送。
                if not info.get("_stream_committed") or isinstance(item, dict):
                    raise UpstreamStreamError(error)
                while buffered:
                    yield buffered.popleft()
                yield item
                return
            item_size = len(item.encode("utf-8") if isinstance(item, str) else item) if isinstance(item, (str, bytes, bytearray)) else max_bytes
            if not released and not inspector.commit and not info.get("_stream_committed") and len(buffered) + 1 < max_chunks and size + item_size < max_bytes:
                buffered.append(item)
                size += item_size
                continue
            released = True
            while buffered:
                yield buffered.popleft()
            yield item
        inspector.finish()
        error = info.get("_stream_error") or inspector.error
        if error:
            await record_stream_failure(info, error)
            if not info.get("_stream_committed"):
                raise UpstreamStreamError(error)
        if not inspector.has_output and not inspector.terminal and not error:
            raise UpstreamStreamError(stream_error_from_exception(HTTPException(502, "Upstream stream ended before output")))
        while buffered:
            yield buffered.popleft()
    except (GeneratorExit, asyncio.CancelledError):
        raise
    except Exception as exc:
        if info.get("_stream_committed"):
            await record_stream_failure(info, stream_error_from_exception(exc))
        raise
    finally:
        buffered.clear()
        await close_async_iterator(source)
