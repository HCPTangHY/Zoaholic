"""
Claude 方言

支持 Anthropic Claude 原生格式的入口/出口转换：
- parse_request: Claude native -> Canonical(RequestModel)
- render_response: Canonical(OpenAI 风格) -> Claude native
- render_stream: Canonical SSE -> Claude SSE（简化实现）
- endpoints: 自动注册的端点定义
"""

import json
import asyncio
from typing import Any, Dict, List, Optional, Union

from core.json_utils import json_loads, json_dumps_text
from core.models import RequestModel, Message, ContentItem
from core.usage import extract_cache_usage

from .registry import DialectDefinition, EndpointDefinition, register_dialect


def _claude_blocks_to_content_items(blocks: List[Dict[str, Any]]) -> List[ContentItem]:
    items: List[ContentItem] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = str(block.get("text", ""))
            items.append(ContentItem(type="text", text=text))
        elif btype == "image" and isinstance(block.get("source"), dict):
            source = block["source"]
            if source.get("type") == "base64":
                media_type = source.get("media_type", "image/png")
                data = source.get("data", "")
                items.append(
                    ContentItem(
                        type="image_url",
                        image_url={"url": f"data:{media_type};base64,{data}"},
                    )
                )
        elif btype == "document" and isinstance(block.get("source"), dict):
            source = block["source"]
            if source.get("type") == "base64":
                media_type = source.get("media_type", "application/octet-stream")
                data = source.get("data", "")
                items.append(
                    ContentItem(
                        type="file",
                        file={"mime_type": media_type, "data": data},
                    )
                )
    return items


def _parse_claude_tools(native_body: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """Claude tools -> OpenAI tools"""
    native_tools = native_body.get("tools") or []
    if not isinstance(native_tools, list):
        return None

    tools: List[Dict[str, Any]] = []
    for tool in native_tools:
        if not isinstance(tool, dict):
            continue
        fn = {
            "name": tool.get("name"),
            "description": tool.get("description"),
        }
        if isinstance(tool.get("input_schema"), dict):
            fn["parameters"] = tool["input_schema"]
        if fn.get("name"):
            tools.append({"type": "function", "function": fn})

    return tools or None


def _parse_claude_tool_choice(native_body: Dict[str, Any]) -> Optional[Union[str, Dict[str, Any]]]:
    """Claude tool_choice -> OpenAI tool_choice"""
    tool_choice = native_body.get("tool_choice")
    if tool_choice is None:
        return None

    if isinstance(tool_choice, str):
        return tool_choice

    if isinstance(tool_choice, dict):
        tc_type = tool_choice.get("type")
        if tc_type == "auto":
            return "auto"
        if tc_type == "any":
            return "required"
        if tc_type == "tool" and tool_choice.get("name"):
            return {
                "type": "function",
                "function": {"name": tool_choice["name"]},
            }
        return tool_choice

    return None


async def parse_claude_request(
    native_body: Dict[str, Any],
    path_params: Dict[str, str],
    headers: Dict[str, str],
) -> RequestModel:
    """
    Claude native -> Canonical(RequestModel)

    支持字段：
    - system -> system message
    - messages[].role/content -> messages
    - tools -> tools
    - tool_choice -> tool_choice
    - thinking -> thinking
    """
    messages: List[Message] = []

    # system
    system_field = native_body.get("system")
    if system_field:
        if isinstance(system_field, str):
            sys_text = system_field
        elif isinstance(system_field, list):
            sys_text = "".join(
                str(b.get("text", "")) for b in system_field if isinstance(b, dict)
            )
        else:
            sys_text = str(system_field)
        if sys_text.strip():
            messages.append(Message(role="system", content=sys_text.strip()))

    # messages
    native_messages = native_body.get("messages") or []
    if isinstance(native_messages, list):
        for nm in native_messages:
            if not isinstance(nm, dict):
                continue
            role = nm.get("role") or "user"
            content = nm.get("content")

            # string content
            if isinstance(content, str):
                messages.append(Message(role=role, content=content))
                continue

            # list-of-blocks content
            if isinstance(content, list):
                tool_calls: Optional[List[Dict[str, Any]]] = None
                tool_result_blocks: List[Dict[str, Any]] = []
                text_blocks: List[Dict[str, Any]] = []
                other_blocks: List[Dict[str, Any]] = []

                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "tool_use":
                        name = block.get("name")
                        tool_id = block.get("id") or "call_0"
                        args = block.get("input") or {}
                        if name:
                            tool_calls = tool_calls or []
                            tool_calls.append(
                                {
                                    "id": tool_id,
                                    "type": "function",
                                    "function": {
                                        "name": name,
                                        "arguments": json_dumps_text(args, ensure_ascii=False),
                                    },
                                }
                            )
                    elif btype == "tool_result":
                        tool_result_blocks.append(block)
                    elif btype == "text":
                        text_blocks.append(block)
                    else:
                        other_blocks.append(block)

                # tool_result -> tool role messages
                if tool_result_blocks:
                    for tr in tool_result_blocks:
                        tool_use_id = tr.get("tool_use_id") or tr.get("toolUseId")
                        tr_content = tr.get("content") or ""
                        # 如果是列表（多块内容），提取所有文本内容
                        if isinstance(tr_content, list):
                            text_acc = []
                            for block in tr_content:
                                if isinstance(block, dict):
                                    if block.get("type") == "text":
                                        text_acc.append(block.get("text", ""))
                                    elif "text" in block:
                                        text_acc.append(str(block["text"]))
                                elif isinstance(block, str):
                                    text_acc.append(block)
                            tr_content = "\n".join(text_acc)

                        messages.append(
                            Message(
                                role="tool",
                                content=str(tr_content),
                                tool_call_id=tool_use_id,
                            )
                        )
                    # 若同一条消息里还有文本，则追加一个 user/assistant 文本消息
                    if text_blocks or other_blocks:
                        items = _claude_blocks_to_content_items(text_blocks + other_blocks)
                        if items:
                            if len(items) == 1 and items[0].type == "text":
                                messages.append(Message(role=role, content=items[0].text or ""))
                            else:
                                messages.append(Message(role=role, content=items))
                    continue

                # tool_use -> assistant tool_calls message（content 置空）
                if tool_calls:
                    messages.append(
                        Message(role="assistant", content=None, tool_calls=tool_calls)
                    )
                    continue

                # 普通块
                items = _claude_blocks_to_content_items(content)
                if items:
                    if len(items) == 1 and items[0].type == "text":
                        messages.append(Message(role=role, content=items[0].text or ""))
                    else:
                        messages.append(Message(role=role, content=items))
                continue

    if not messages:
        messages = [Message(role="user", content="")]

    model = native_body.get("model") or path_params.get("model") or ""
    tools = _parse_claude_tools(native_body)
    tool_choice = _parse_claude_tool_choice(native_body)

    request_kwargs: Dict[str, Any] = {}
    for k in ("temperature", "top_p", "top_k", "max_tokens", "stream", "thinking"):
        if k in native_body:
            request_kwargs[k] = native_body.get(k)

    if tools:
        request_kwargs["tools"] = tools
    if tool_choice is not None:
        request_kwargs["tool_choice"] = tool_choice

    return RequestModel(
        model=model,
        messages=messages,
        **request_kwargs,
    )


def _canonical_usage_to_claude_usage(usage: Any, include_input: bool = True) -> Dict[str, int]:
    """将内核 OpenAI usage 转为 Claude usage。

    Claude 原生 input_tokens 不包含缓存读取和缓存创建 token，因此这里从 prompt_tokens 中扣除缓存部分，
    再把缓存字段还原为 cache_read_input_tokens 与 cache_creation_input_tokens。
    """
    usage = usage if isinstance(usage, dict) else {}
    cache_usage = extract_cache_usage(usage)
    cache_read_tokens = cache_usage["cached_tokens"]
    cache_creation_tokens = cache_usage["cache_creation_tokens"]
    prompt_tokens = usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0) or 0
    completion_tokens = usage.get("completion_tokens", 0) or usage.get("output_tokens", 0) or 0
    native_input_tokens = max((prompt_tokens or 0) - cache_read_tokens - cache_creation_tokens, 0)

    claude_usage: Dict[str, int] = {"output_tokens": completion_tokens}
    if include_input:
        claude_usage["input_tokens"] = native_input_tokens
    if cache_read_tokens > 0:
        claude_usage["cache_read_input_tokens"] = cache_read_tokens
    if cache_creation_tokens > 0:
        claude_usage["cache_creation_input_tokens"] = cache_creation_tokens
    return claude_usage


async def render_claude_response(
    canonical_response: Dict[str, Any],
    model: str,
) -> Dict[str, Any]:
    """
    Canonical(OpenAI 风格) -> Claude native response
    """
    choices = canonical_response.get("choices") or []
    content = []
    stop_reason = "end_turn"
    
    if choices:
        msg = choices[0].get("message") or {}
        
        # 1. 思维链 (Thinking)
        reasoning = msg.get("reasoning_content")
        if reasoning:
            content.append({"type": "thinking", "thinking": reasoning})

        # 2. 文本内容（支持结构化 content list）
        msg_content = msg.get("content")
        if isinstance(msg_content, list):
            # 结构化 content list → Claude content blocks
            for item in msg_content:
                if not isinstance(item, dict):
                    continue
                item_type = item.get("type", "")
                if item_type == "text":
                    text_val = item.get("text", "")
                    if text_val:
                        content.append({"type": "text", "text": text_val})
                elif item_type == "image_url":
                    image_url = item.get("image_url")
                    url = ""
                    if isinstance(image_url, dict):
                        url = image_url.get("url", "")
                    elif isinstance(image_url, str):
                        url = image_url
                    if url.startswith("data:"):
                        try:
                            header, b64data = url.split(",", 1)
                            media_type = header.split(":", 1)[1].split(";", 1)[0]
                            content.append({
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": b64data,
                                }
                            })
                        except (ValueError, IndexError):
                            content.append({"type": "text", "text": f"![image]({url})"})
                    elif url:
                        content.append({
                            "type": "image",
                            "source": {"type": "url", "url": url}
                        })
        elif msg_content:
            content.append({"type": "text", "text": msg_content})
            
        # 2. 工具调用
        tool_calls = msg.get("tool_calls") or []
        if tool_calls:
            stop_reason = "tool_use"
            for tc in tool_calls:
                fn = tc.get("function") or {}
                try:
                    args = json_loads(fn.get("arguments") or "{}")
                except:
                    args = {}
                content.append({
                    "type": "tool_use",
                    "id": tc.get("id"),
                    "name": fn.get("name"),
                    "input": args
                })

        finish_reason = choices[0].get("finish_reason")
        if finish_reason == "tool_calls":
            stop_reason = "tool_use"
        elif finish_reason == "stop":
            stop_reason = "end_turn"

    usage = canonical_response.get("usage") or {}
    # 非流式 Claude 出口需要把内核缓存字段还原为 Claude 原生字段，避免下游只看到合并后的 prompt_tokens。
    claude_usage = _canonical_usage_to_claude_usage(usage, include_input=True)

    return {
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "usage": claude_usage,
    }


class ClaudeStreamRenderer:
    """
    有状态的 Claude SSE 流渲染器。

    维护流的完整生命周期，按照 Claude Messages API 标准协议
    生成完整的 SSE 事件序列：
    message_start → content_block_start → content_block_delta(s) →
    content_block_stop → message_delta → message_stop
    """

    def __init__(self):
        self._message_started = False
        self._current_block_type = None  # "thinking" | "text" | "tool_use" | None
        self._block_index = -1  # 每个新块 +1
        self._model = ""
        self._msg_id = ""
        self._tool_block_indices = {}  # OpenAI tool_call index -> Claude block index

    def _make_message_start(self, canonical: dict) -> str:
        """生成 message_start 事件"""
        import uuid
        self._model = canonical.get("model", "") or self._model
        raw_id = canonical.get("id", "") or uuid.uuid4().hex[:24]
        self._msg_id = f"msg_{raw_id}"

        event = {
            "type": "message_start",
            "message": {
                "id": self._msg_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": self._model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                },
            },
        }
        return f"event: message_start\ndata: {json_dumps_text(event, ensure_ascii=False)}\n\n"

    def _make_block_start(self, block_type: str, **kwargs) -> str:
        """生成 content_block_start 事件"""
        self._block_index += 1
        self._current_block_type = block_type

        if block_type == "thinking":
            content_block = {"type": "thinking", "thinking": ""}
        elif block_type == "text":
            content_block = {"type": "text", "text": ""}
        elif block_type == "tool_use":
            content_block = {
                "type": "tool_use",
                "id": kwargs.get("id", ""),
                "name": kwargs.get("name", ""),
                "input": {},
            }
        else:
            content_block = {"type": block_type}

        event = {
            "type": "content_block_start",
            "index": self._block_index,
            "content_block": content_block,
        }
        return f"event: content_block_start\ndata: {json_dumps_text(event, ensure_ascii=False)}\n\n"

    def _make_block_stop(self) -> str:
        """生成 content_block_stop 事件"""
        if self._current_block_type is None:
            return ""
        event = {
            "type": "content_block_stop",
            "index": self._block_index,
        }
        self._current_block_type = None
        return f"event: content_block_stop\ndata: {json_dumps_text(event, ensure_ascii=False)}\n\n"

    def _ensure_message_start(self, canonical: dict) -> str:
        """确保 message_start 已发送"""
        if not self._message_started:
            self._message_started = True
            return self._make_message_start(canonical)
        return ""

    def _transition_to_block(self, block_type: str, **kwargs) -> str:
        """切换到新的内容块，必要时关闭旧块并打开新块"""
        result = ""
        # tool_use 每次调用都是新块；其他类型仅在类型变化时切换
        if self._current_block_type != block_type or block_type == "tool_use":
            if self._current_block_type is not None:
                result += self._make_block_stop()
            result += self._make_block_start(block_type, **kwargs)
        return result

    async def __call__(self, canonical_sse_chunk: str) -> str:
        if not isinstance(canonical_sse_chunk, str):
            return canonical_sse_chunk

        if not canonical_sse_chunk.startswith("data: "):
            return canonical_sse_chunk

        data_str = canonical_sse_chunk[6:].strip()
        if data_str == "[DONE]":
            result = ""
            if self._current_block_type is not None:
                result += self._make_block_stop()
            result += 'event: message_stop\ndata: {"type":"message_stop"}\n\n'
            return result

        try:
            canonical = json_loads(data_str)
        except json.JSONDecodeError:
            return canonical_sse_chunk

        usage = canonical.get("usage")
        choices = canonical.get("choices") or []
        if not choices:
            if isinstance(usage, dict):
                # 经过内核重组的 usage-only chunk 没有 choices；这里转成 Claude message_delta，保留缓存字段。
                event = {
                    "type": "message_delta",
                    "delta": {},
                    "usage": _canonical_usage_to_claude_usage(usage, include_input=True),
                }
                return f"event: message_delta\ndata: {json_dumps_text(event, ensure_ascii=False)}\n\n"
            return ""

        delta = choices[0].get("delta") or {}
        result = ""

        # 确保 message_start 已发送
        result += self._ensure_message_start(canonical)

        # 1. 思维链 (Thinking)
        reasoning = delta.get("reasoning_content") or ""
        if reasoning:
            result += self._transition_to_block("thinking")
            event = {
                "type": "content_block_delta",
                "index": self._block_index,
                "delta": {"type": "thinking_delta", "thinking": reasoning},
            }
            result += f"event: content_block_delta\ndata: {json_dumps_text(event, ensure_ascii=False)}\n\n"
            return result

        # 2. 文本
        content = delta.get("content") or ""
        if content:
            result += self._transition_to_block("text")
            event = {
                "type": "content_block_delta",
                "index": self._block_index,
                "delta": {"type": "text_delta", "text": content},
            }
            result += f"event: content_block_delta\ndata: {json_dumps_text(event, ensure_ascii=False)}\n\n"
            return result

        # 3. 工具调用
        tool_calls = delta.get("tool_calls") or []
        if tool_calls:
            tc = tool_calls[0]
            tc_index = tc.get("index", 0)

            if tc.get("function", {}).get("name"):
                # 新工具调用 → 新的 content_block
                result += self._transition_to_block(
                    "tool_use",
                    id=tc.get("id", ""),
                    name=tc["function"]["name"],
                )
                self._tool_block_indices[tc_index] = self._block_index

                if tc["function"].get("arguments"):
                    event = {
                        "type": "content_block_delta",
                        "index": self._block_index,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": tc["function"]["arguments"],
                        },
                    }
                    result += f"event: content_block_delta\ndata: {json_dumps_text(event, ensure_ascii=False)}\n\n"
            elif tc.get("function", {}).get("arguments"):
                # arguments 续传
                idx = self._tool_block_indices.get(tc_index, self._block_index)
                event = {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": tc["function"]["arguments"],
                    },
                }
                result += f"event: content_block_delta\ndata: {json_dumps_text(event, ensure_ascii=False)}\n\n"
            return result

        # 4. 完成
        if choices[0].get("finish_reason"):
            if self._current_block_type is not None:
                result += self._make_block_stop()

            finish_reason = choices[0]["finish_reason"]
            stop_reason = "tool_use" if finish_reason == "tool_calls" else "end_turn"

            event = {
                "type": "message_delta",
                "delta": {
                    "stop_reason": stop_reason,
                    "stop_sequence": None,
                },
                # 完成事件通常不携带完整 usage；若后续 usage-only chunk 到达，会再发送带缓存字段的 message_delta。
                "usage": _canonical_usage_to_claude_usage(canonical.get("usage", {}), include_input=False),
            }
            result += f"event: message_delta\ndata: {json_dumps_text(event, ensure_ascii=False)}\n\n"
            return result

        return result or ""


def create_claude_stream_renderer():
    """工厂函数：为每次流请求创建独立的有状态 Claude SSE 渲染器"""
    return ClaudeStreamRenderer()


async def render_claude_stream(canonical_sse_chunk: str) -> str:
    """
    无状态兼容接口（保留供直接调用场景使用）。

    注意：此函数不生成 message_start / content_block_start / content_block_stop
    等生命周期事件。完整协议兼容请使用 create_claude_stream_renderer() 工厂。
    """
    renderer = ClaudeStreamRenderer()
    renderer._message_started = True  # 跳过 message_start，保持旧行为
    return await renderer(canonical_sse_chunk)



def parse_claude_usage(data: Any) -> Optional[Dict[str, int]]:
    """从 Claude 格式中提取 usage"""
    if not isinstance(data, dict):
        return None

    # message_start 事件: usage 嵌套在 message.usage 中
    msg = data.get("message")
    if isinstance(msg, dict) and msg.get("usage"):
        usage = msg["usage"]
    else:
        # message_delta 事件及非流式响应: 顶层 usage
        usage = data.get("usage")

    if usage:
        # Claude 的 input_tokens 不含缓存创建和读取部分；统一 prompt_tokens 需要补齐这两类 token。
        cache_usage = extract_cache_usage(usage)
        prompt = (
            (usage.get("input_tokens", 0) or 0)
            + cache_usage["cached_tokens"]
            + cache_usage["cache_creation_tokens"]
        )
        completion = usage.get("output_tokens", 0)
        total = prompt + completion
        if prompt or completion or cache_usage["cached_tokens"] or cache_usage["cache_creation_tokens"]:
            return {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": total,
                **cache_usage,
            }
    return None


def register() -> None:
    """注册 Claude 方言"""
    register_dialect(
        DialectDefinition(
            id="claude",
            name="Anthropic Claude",
            description="Anthropic Claude API 原生格式",
            parse_request=parse_claude_request,
            render_response=render_claude_response,
            render_stream=render_claude_stream,
            render_stream_factory=create_claude_stream_renderer,
            parse_usage=parse_claude_usage,
            target_engine="claude",
            endpoints=[
                # POST /v1/messages - Claude 消息接口
                EndpointDefinition(
                    path="/v1/messages",
                    methods=["POST"],
                    tags=["Claude Dialect"],
                    summary="Create Message",
                    description="Claude 原生格式消息生成接口",
                ),
                # POST /v1/messages/* - Claude 同协议子端点透传入口
                EndpointDefinition(
                    path="/v1/messages/{subpath:path}",
                    passthrough_root="/v1/messages",
                    methods=["POST"],
                    tags=["Claude Dialect"],
                    summary="Claude Messages Passthrough Subpaths",
                    description="Claude /v1/messages 下的子端点透传入口（仅在上游为 Claude 时可用）",
                    passthrough_only=True,
                ),
            ],
        )
    )