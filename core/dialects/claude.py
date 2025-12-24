"""
Claude 方言

支持 Anthropic Claude 原生格式的入口/出口转换：
- parse_request: Claude native -> Canonical(RequestModel)
- render_response: Canonical(OpenAI 风格) -> Claude native
- render_stream: Canonical SSE -> Claude SSE（简化实现）
- endpoints: 自动注册的端点定义
"""

import json
from typing import Any, Dict, List, Optional, Union

from core.models import RequestModel, Message, ContentItem

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
                                        "arguments": json.dumps(args, ensure_ascii=False),
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

        # 2. 文本内容
        text = msg.get("content")
        if text:
            content.append({"type": "text", "text": text})
            
        # 2. 工具调用
        tool_calls = msg.get("tool_calls") or []
        if tool_calls:
            stop_reason = "tool_use"
            for tc in tool_calls:
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
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
    prompt_tokens = usage.get("prompt_tokens", 0) or 0
    completion_tokens = usage.get("completion_tokens", 0) or 0

    return {
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "usage": {
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
        },
    }


async def render_claude_stream(canonical_sse_chunk: str) -> str:
    """
    Canonical SSE -> Claude SSE
    """
    if not isinstance(canonical_sse_chunk, str):
        return canonical_sse_chunk

    if not canonical_sse_chunk.startswith("data: "):
        return canonical_sse_chunk

    data_str = canonical_sse_chunk[6:].strip()
    if data_str == "[DONE]":
        return "event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n"

    try:
        canonical = json.loads(data_str)
    except json.JSONDecodeError:
        return canonical_sse_chunk

    choices = canonical.get("choices") or []
    if not choices:
        return ""

    delta = choices[0].get("delta") or {}
    
    # 1. 处理思维链 (Thinking)
    reasoning = delta.get("reasoning_content") or ""
    if reasoning:
        claude_event = {
            "type": "content_block_delta",
            "index": 0,
            "delta": {
                "type": "thinking_delta",
                "thinking": reasoning,
            },
        }
        return f"event: content_block_delta\ndata: {json.dumps(claude_event, ensure_ascii=False)}\n\n"

    # 2. 处理文本
    content = delta.get("content") or ""
    if content:
        claude_event = {
            "type": "content_block_delta",
            "index": 0,
            "delta": {
                "type": "text_delta",
                "text": content,
            },
        }
        return f"event: content_block_delta\ndata: {json.dumps(claude_event, ensure_ascii=False)}\n\n"

    # 2. 处理工具调用开始
    tool_calls = delta.get("tool_calls") or []
    if tool_calls:
        tc = tool_calls[0]
        # 如果有 name，说明是新的 block 开始
        if tc.get("function", {}).get("name"):
            event_start = {
                "type": "content_block_start",
                "index": 1,
                "content_block": {
                    "type": "tool_use",
                    "id": tc.get("id"),
                    "name": tc["function"]["name"],
                    "input": {}
                }
            }
            # 如果同时有 arguments，追加一个 delta
            if tc["function"].get("arguments"):
                event_delta = {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": tc["function"]["arguments"]
                    }
                }
                return f"event: content_block_start\ndata: {json.dumps(event_start, ensure_ascii=False)}\n\n" + \
                       f"event: content_block_delta\ndata: {json.dumps(event_delta, ensure_ascii=False)}\n\n"
            return f"event: content_block_start\ndata: {json.dumps(event_start, ensure_ascii=False)}\n\n"
        
        # 只有 arguments，则是 delta
        elif tc.get("function", {}).get("arguments"):
            event_delta = {
                "type": "content_block_delta",
                "index": 1,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": tc["function"]["arguments"]
                }
            }
            return f"event: content_block_delta\ndata: {json.dumps(event_delta, ensure_ascii=False)}\n\n"

    # 3. 处理完成
    if choices[0].get("finish_reason"):
        event_msg_delta = {
            "type": "message_delta",
            "delta": {
                "stop_reason": "tool_use" if choices[0].get("finish_reason") == "tool_calls" else "end_turn",
                "stop_sequence": None
            },
            "usage": {
               "output_tokens": canonical.get("usage", {}).get("completion_tokens", 0)
            }
        }
        return f"event: message_delta\ndata: {json.dumps(event_msg_delta, ensure_ascii=False)}\n\n"

    return ""


def parse_claude_usage(data: Any) -> Optional[Dict[str, int]]:
    """从 Claude 格式中提取 usage"""
    if not isinstance(data, dict):
        return None
    usage = data.get("usage")
    if usage:
        prompt = usage.get("input_tokens", 0)
        completion = usage.get("output_tokens", 0)
        total = prompt + completion
        if prompt or completion:
            return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}
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
            ],
        )
    )