"""
Claude/Anthropic 渠道适配器

负责处理 Anthropic Claude API 的请求构建和响应流解析
"""

import json
import copy
import asyncio
from datetime import datetime

from ..utils import (
    safe_get,
    get_model_dict,
    get_base64_image,
    is_tools_disabled,
    generate_sse_response,
    generate_no_stream_response,
    end_of_line,
)
from ..response import check_response
from ..json_utils import json_loads, json_dumps_text
from ..response_context import mark_adapter_metrics_managed, mark_content_start, merge_usage
from ..stream_utils import aiter_decoded_lines
from ..usage import extract_cache_usage
from ..file_utils import extract_base64_data


# ============================================================
# 工具函数
# ============================================================

def _normalize_claude_base_url(base_url: str) -> str:
    """归一化 Claude base_url，去除末尾的 /messages 端点路径，确保只保留到 /v1 层级。
    兼容旧配置 https://api.anthropic.com/v1/messages 和新配置 https://api.anthropic.com/v1。"""
    if base_url.endswith('#'):
        return base_url  # 保留 '#'，由 resolve_base_url 处理
    url = base_url.rstrip('/')
    if url.endswith('/v1/messages'):
        url = url[:-len('/messages')]
    return url

# ============================================================
# Claude 格式化函数
# ============================================================

def format_text_message(text: str) -> dict:
    """格式化文本消息为 Claude 格式"""
    return {"type": "text", "text": text}


async def format_image_message(image_url: str) -> dict:
    """格式化图片消息为 Claude 格式"""
    base64_image, image_type = await get_base64_image(image_url)
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": image_type,
            "data": extract_base64_data(base64_image),
        }
    }


async def gpt2claude_tools_json(json_dict):
    """将 GPT 格式的工具定义转换为 Claude 格式"""
    json_dict = copy.deepcopy(json_dict)

    # 处理 $ref 引用
    def resolve_refs(obj, defs):
        if isinstance(obj, dict):
            # 如果有 $ref 引用，替换为实际定义
            if "$ref" in obj and obj["$ref"].startswith("#/$defs/"):
                ref_name = obj["$ref"].split("/")[-1]
                if ref_name in defs:
                    # 完全替换为引用的对象
                    ref_obj = copy.deepcopy(defs[ref_name])
                    # 保留原始对象中的其他属性
                    for k, v in obj.items():
                        if k != "$ref":
                            ref_obj[k] = v
                    return ref_obj

            # 递归处理所有属性
            for key, value in list(obj.items()):
                obj[key] = resolve_refs(value, defs)

        elif isinstance(obj, list):
            # 递归处理列表中的每个元素
            for i, item in enumerate(obj):
                obj[i] = resolve_refs(item, defs)

        return obj

    # 提取 $defs 定义
    defs = {}
    if "parameters" in json_dict and isinstance(json_dict["parameters"], dict) and "defs" in json_dict["parameters"]:
        defs = json_dict["parameters"]["defs"]
        # 从参数中删除 $defs，因为 Claude 不需要它
        del json_dict["parameters"]["defs"]

    # 解析所有引用
    json_dict = resolve_refs(json_dict, defs)

    # 继续原有的键名转换逻辑
    keys_to_change = {
        "parameters": "input_schema",
    }
    for old_key, new_key in keys_to_change.items():
        if old_key in json_dict:
            if new_key:
                if json_dict[old_key] is None:
                    json_dict[old_key] = {
                        "type": "object",
                        "properties": {}
                    }
                json_dict[new_key] = json_dict.pop(old_key)
            else:
                json_dict.pop(old_key)
    return json_dict


async def patch_passthrough_claude_payload(
    payload: dict,
    modifications: dict,
    request,
    engine: str,
    provider: dict,
    api_key=None,
) -> dict:
    """透传模式下对 Claude native payload 做渠道级修饰（system_prompt 注入）。"""
    system_prompt = modifications.get("system_prompt")
    system_prompt_text = str(system_prompt).strip() if system_prompt is not None else ""
    if not system_prompt_text:
        return payload

    old_system = payload.get("system")
    if isinstance(old_system, str):
        payload["system"] = f"{system_prompt_text}\n\n{old_system}" if old_system else system_prompt_text
    elif isinstance(old_system, list):
        # Claude system 也可能是 blocks
        if old_system and isinstance(old_system[0], dict) and "text" in old_system[0]:
            old = old_system[0].get("text") or ""
            old_system[0]["text"] = f"{system_prompt_text}\n\n{old}" if old else system_prompt_text
        else:
            old_system.insert(0, {"type": "text", "text": system_prompt_text})
        payload["system"] = old_system
    else:
        payload["system"] = system_prompt_text

    return payload


async def get_claude_passthrough_meta(request, engine, provider, api_key=None):
    """透传用：仅构建 url/headers，不执行 payload 转换。

    透传模式下 payload 取自入口原始请求体，此函数只负责提供
    上游 URL 和认证/版本头信息，避免执行完整的 get_claude_payload()
    中 messages/tools 转换逻辑。

    注意：anthropic-beta 不在此设置。透传模式下客户端原始请求头中
    的 anthropic-beta 会由 process_request_passthrough 自动合并，
    无需此处预设默认值。
    """
    headers = {
        "content-type": "application/json",
        "x-api-key": f"{api_key}",
        "anthropic-version": "2023-06-01",
    }
    from ..utils import resolve_base_url
    url = resolve_base_url(_normalize_claude_base_url(provider['base_url']), '/messages')

    return url, headers, {}


async def get_claude_payload(request, engine, provider, api_key=None):
    """构建 Claude API 的请求 payload"""
    model_dict = get_model_dict(provider)
    original_model = model_dict[request.model]

    if "claude-3-7-sonnet" in original_model:
        anthropic_beta = "output-128k-2025-02-19"
    elif "claude-3-5-sonnet" in original_model:
        anthropic_beta = "max-tokens-3-5-sonnet-2024-07-15"
    else:
        anthropic_beta = "tools-2024-05-16"

    headers = {
        "content-type": "application/json",
        "x-api-key": f"{api_key}",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": anthropic_beta,
    }
    from ..utils import resolve_base_url
    url = resolve_base_url(_normalize_claude_base_url(provider['base_url']), '/messages')

    messages = []
    system_prompt = None
    tool_id = None
    is_deepseek = "deepseek" in original_model.lower()
    for msg in request.messages:
        tool_call_id = None
        tool_calls = None
        if isinstance(msg.content, list):
            content = []
            for item in msg.content:
                if item.type == "text":
                    text_message = format_text_message(item.text)
                    content.append(text_message)
                elif item.type == "image_url" and provider.get("image", True):
                    image_message = await format_image_message(item.image_url.url)
                    content.append(image_message)
                elif item.type == "file":
                    b64_data = ""
                    mime_type = item.file.mime_type or "application/octet-stream"
                    if getattr(item.file, "data", None):
                        b64_data = item.file.data
                    elif getattr(item.file, "url", None):
                        from ..file_utils import get_base64_file, parse_data_uri
                        data_uri, mime_type = await get_base64_file(item.file.url)
                        if data_uri.startswith("data:"):
                            _, b64_data = parse_data_uri(data_uri)
                        else:
                            b64_data = data_uri
                    if b64_data:
                        if mime_type.startswith("image/"):
                            content.append({
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": mime_type,
                                    "data": b64_data,
                                }
                            })
                        elif mime_type == "application/pdf":
                            content.append({
                                "type": "document",
                                "source": {
                                    "type": "base64",
                                    "media_type": mime_type,
                                    "data": b64_data,
                                }
                            })
                        else:
                            # 文本类文件：解码 base64 转为内联文本
                            import base64 as _b64
                            try:
                                decoded_text = _b64.b64decode(b64_data).decode("utf-8")
                                fname = getattr(item.file, "filename", "") or ""
                                if fname:
                                    decoded_text = f"📄 {fname}\n```\n{decoded_text}\n```"
                                content.append({"type": "text", "text": decoded_text})
                            except Exception:
                                # 解码失败，尝试原样发 document（可能报错）
                                content.append({
                                    "type": "document",
                                    "source": {
                                        "type": "base64",
                                        "media_type": mime_type,
                                        "data": b64_data,
                                    }
                                })
        else:
            content = msg.content
            tool_calls = msg.tool_calls
            tool_id = tool_calls[0].id if tool_calls else None or tool_id
            tool_call_id = msg.tool_call_id

        # DeepSeek Claude 端点豁免：回传 reasoning_content 为 thinking block（不带 signature）
        thinking_blocks = []
        if is_deepseek and msg.role == "assistant":
            reasoning = getattr(msg, "reasoning_content", None) or (msg.model_extra or {}).get("reasoning_content")
            if reasoning and isinstance(reasoning, str) and reasoning.strip():
                thinking_blocks.append({"type": "thinking", "thinking": reasoning})

        if tool_calls:
            tool_calls_list = []
            for tool_call in tool_calls:
                tool_calls_list.append({
                    "type": "tool_use",
                    "id": tool_call.id,
                    "name": tool_call.function.name,
                    "input": json_loads(tool_call.function.arguments),
                })
            messages.append({"role": msg.role, "content": thinking_blocks + tool_calls_list})
        elif tool_call_id:
            messages.append({"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": tool_call_id,
                "content": [{"type": "text", "text": content}] if isinstance(content, str) else content
            }]})
        elif msg.role == "function":
            messages.append({"role": "assistant", "content": [{
                "type": "tool_use",
                "id": "toolu_017r5miPMV6PGSNKmhvHPic4",
                "name": msg.name,
                "input": {"prompt": "..."}
            }]})
            messages.append({"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": "toolu_017r5miPMV6PGSNKmhvHPic4",
                "content": [{"type": "text", "text": msg.content}] if isinstance(msg.content, str) else msg.content
            }]})
        elif msg.role != "system":
            # content 是 list 时，清洗裸字符串元素为 dict
            if isinstance(content, list):
                content = [
                    item if isinstance(item, dict) else {"type": "text", "text": str(item)}
                    for item in content
                ]
            if thinking_blocks:
                if isinstance(content, str):
                    content = [{"type": "text", "text": content}] if content else []
                elif not isinstance(content, list):
                    content = []
                content = thinking_blocks + content
            messages.append({"role": msg.role, "content": content})
        elif msg.role == "system":
            if system_prompt is None:
                system_prompt = content
            elif isinstance(system_prompt, str) and isinstance(content, str):
                system_prompt = system_prompt + "\n\n" + content
            elif isinstance(system_prompt, list) and isinstance(content, list):
                system_prompt = system_prompt + content
            else:
                # 类型不一致时（str vs list），以最新的为准并拼接旧内容为文本
                system_prompt = (str(system_prompt) + "\n\n" + str(content)) if content else system_prompt

    conversation_len = len(messages) - 1
    message_index = 0
    while message_index < conversation_len:
        if messages[message_index]["role"] == messages[message_index + 1]["role"]:
            if messages[message_index].get("content"):
                if isinstance(messages[message_index]["content"], list):
                    next_content = messages[message_index + 1]["content"]
                    if isinstance(next_content, list):
                        messages[message_index]["content"].extend(next_content)
                    elif isinstance(next_content, str):
                        messages[message_index]["content"].append({"type": "text", "text": next_content})
                    else:
                        messages[message_index]["content"].append({"type": "text", "text": str(next_content)})
                elif isinstance(messages[message_index]["content"], str) and isinstance(messages[message_index + 1]["content"], list):
                    content_list = [{"type": "text", "text": messages[message_index]["content"]}]
                    content_list.extend(messages[message_index + 1]["content"])
                    messages[message_index]["content"] = content_list
                else:
                    next_content = messages[message_index + 1]["content"]
                    if isinstance(messages[message_index]["content"], str) and isinstance(next_content, str):
                        messages[message_index]["content"] += "\n" + next_content
                    else:
                        messages[message_index]["content"] += next_content
            messages.pop(message_index + 1)
            conversation_len = conversation_len - 1
        else:
            message_index = message_index + 1

    max_tokens = 32768

    payload = {
        "model": original_model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if system_prompt:
        payload["system"] = system_prompt

    if request.max_tokens:
        payload["max_tokens"] = int(request.max_tokens)

    miss_fields = [
        'model',
       'messages',
        'tools',
        'tool_choice',
        'presence_penalty',
        'frequency_penalty',
        'n',
        'user',
        'include_usage',
        'stream_options',
    ]

    for field, value in request.model_dump(exclude_unset=True).items():
        if field not in miss_fields and value is not None:
            payload[field] = value

    # 修改原因：provider.tools=False 仍需要禁用工具声明。
    # 修改方式：仅在工具未禁用时转换 request.tools。
    # 目的：保留禁用工具能力，同时移除无意义的工具模式变量。
    if request.tools and not is_tools_disabled(provider):
        tools = payload.get("tools", [])  # 保留已有的工具（如插件添加的）
        for tool in request.tools:
            # 检查是否已经是 Claude 服务器端工具格式（如 web_search_20250305）
            if hasattr(tool, 'dict'):
                tool_dict = tool.dict()
            else:
                tool_dict = tool if isinstance(tool, dict) else {}

            # 服务器端工具的 type 包含日期后缀，直接保留
            tool_type = tool_dict.get("type", "")
            if tool_type and ("_20" in tool_type or tool_type.startswith("web_search") or tool_type.startswith("code_execution") or tool_type.startswith("computer_") or tool_type.startswith("text_editor")):
                # 服务器端工具，直接使用
                tools.append(tool_dict)
            elif "function" in tool_dict:
                # 客户端函数工具，需要转换格式
                json_tool = await gpt2claude_tools_json(tool_dict["function"])
                tools.append(json_tool)
            else:
                # 其他格式，尝试转换
                json_tool = await gpt2claude_tools_json(tool_dict)
                tools.append(json_tool)
        payload["tools"] = tools

        # tool_choice 转换：从 request 对象读取，而非从 payload 读取
        # （tool_choice 在 miss_fields 中被排除，不会自动进入 payload）
        raw_tool_choice = request.tool_choice
        if raw_tool_choice is not None:
            if isinstance(raw_tool_choice, dict) or (hasattr(raw_tool_choice, 'type') and hasattr(raw_tool_choice, 'function')):
                tc_dict = raw_tool_choice if isinstance(raw_tool_choice, dict) else raw_tool_choice.model_dump(exclude_none=True)
                if tc_dict.get("type") == "function" and tc_dict.get("function", {}).get("name"):
                    payload["tool_choice"] = {
                        "type": "tool",
                        "name": tc_dict["function"]["name"]
                    }
                else:
                    payload["tool_choice"] = tc_dict
            elif isinstance(raw_tool_choice, str):
                if raw_tool_choice == "auto":
                    payload["tool_choice"] = {"type": "auto"}
                elif raw_tool_choice == "none":
                    payload["tool_choice"] = {"type": "any"}
                elif raw_tool_choice == "required":
                    payload["tool_choice"] = {"type": "any"}

    # 修改原因：禁用工具时，payload 中不能残留任何工具声明或工具选择字段。
    # 修改方式：用统一的禁用判断清理字段。
    # 目的：延续 provider.tools=False 的禁用语义。
    if is_tools_disabled(provider):
        payload.pop("tools", None)
        payload.pop("tool_choice", None)

    if request.thinking:
        thinking_config = {}
        if request.thinking.budget_tokens is not None:
            thinking_config["budget_tokens"] = request.thinking.budget_tokens
        if request.thinking.type is not None:
            thinking_config["type"] = request.thinking.type
        payload["thinking"] = thinking_config
        payload["temperature"] = 1
        payload.pop("top_p", None)
        payload.pop("top_k", None)

    return url, headers, payload


async def fetch_claude_response(client, url, headers, payload, model, timeout):
    """处理 Claude 非流式响应"""
    timestamp = int(datetime.timestamp(datetime.now()))
    json_payload = await asyncio.to_thread(json_dumps_text, payload)
    response = await client.post(url, headers=headers, content=json_payload, timeout=timeout)

    error_message = await check_response(response, "fetch_claude_response")
    if error_message:
        yield error_message
        return

    response_bytes = await response.aread()
    response_json = await asyncio.to_thread(json_loads, response_bytes)
    mark_adapter_metrics_managed()

    # 遍历 content 数组，提取文本内容和客户端工具调用
    # 跳过服务器端工具（server_tool_use, web_search_tool_result）
    content_list = response_json.get("content", [])
    text_parts = []
    # 修改原因：Claude 非流式 content 可能包含多个 tool_use，旧实现只保留最后一个工具调用。
    # 修改方式：按 content 顺序收集 tool_calls_list，并为每个工具调用写入独立 index。
    # 目的：让并行工具调用在转换成 Chat Completions 非流式响应时不会被丢失或合并。
    tool_calls_list = []

    thinking_parts = []

    for item in content_list:
        item_type = item.get("type", "")

        # 服务器端工具 - 跳过（Claude 已自动处理）
        if item_type in ("server_tool_use", "web_search_tool_result"):
            continue

        # thinking 内容
        if item_type == "thinking":
            thinking_parts.append(item.get("thinking", ""))

        # 文本内容
        if item_type == "text":
            text_parts.append(item.get("text", ""))

        # 客户端工具调用
        if item_type == "tool_use":
            tool_calls_list.append({
                "index": len(tool_calls_list),
                "id": item.get("id"),
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    "arguments": json_dumps_text(item.get("input"), ensure_ascii=False),
                },
            })

    content = "".join(text_parts) if text_parts else None
    reasoning_content = "".join(thinking_parts) if thinking_parts else None

    usage = safe_get(response_json, "usage", default={}) or {}
    cache_usage = extract_cache_usage(usage)
    # Claude 的 input_tokens 不包含缓存创建和读取 token；prompt_tokens 需要按计量口径补齐。
    prompt_tokens = (
        (usage.get("input_tokens") or 0)
        + cache_usage["cache_creation_tokens"]
        + cache_usage["cached_tokens"]
    ) or None
    output_tokens = usage.get("output_tokens")
    total_tokens = (prompt_tokens or 0) + (output_tokens or 0)

    merge_usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=output_tokens,
        total_tokens=total_tokens,
        **cache_usage,
    )
    if content or reasoning_content or tool_calls_list:
        mark_content_start()

    # 如果非流式直接触发拒绝，因为没有半路断联问题，直接 400 报错拦截最为安全，100% 杜绝 Agent 误读
    if response_json.get("stop_reason") == "refusal":
        stop_details = response_json.get("stop_details") or {}
        refusal_reason = (
            stop_details.get("explanation")
            or stop_details.get("type")
            or stop_details.get("category")
            or "This request triggered Claude safeguards and was refused."
        )
        yield {
            "error": f"This request triggered Claude safeguards and was refused: {refusal_reason}",
            "status_code": 400,
            "details": f"Claude Refusal ({stop_details.get('category', 'unknown')})"
        }
        return

    role = safe_get(response_json, "role")

    yield await generate_no_stream_response(
        timestamp, model, content=content,
        role=role, total_tokens=total_tokens, prompt_tokens=prompt_tokens,
        completion_tokens=output_tokens, reasoning_content=reasoning_content,
        # Claude 非流式缓存字段已统一提取；传给出口函数后才能继续给下游方言使用。
        cached_tokens=cache_usage["cached_tokens"],
        cache_creation_tokens=cache_usage["cache_creation_tokens"],
        return_dict=True,
        tool_calls_list=tool_calls_list or None,
    )


async def fetch_claude_response_stream(client, url, headers, payload, model, timeout):
    """处理 Claude 流式响应"""
    timestamp = int(datetime.timestamp(datetime.now()))
    json_payload = await asyncio.to_thread(json_dumps_text, payload)
    async with client.stream('POST', url, headers=headers, content=json_payload, timeout=timeout) as response:
        error_message = await check_response(response, "fetch_claude_response_stream")
        if error_message:
            yield error_message
            return
        mark_adapter_metrics_managed()
        input_tokens = 0
        # Claude 流式缓存字段出现在 message_start 的 usage 中，先暂存，等 output_tokens 到达时一起写入。
        cached_tokens = 0
        cache_creation_tokens = 0
        # 跟踪当前 content_block 类型，用于区分服务器端工具和客户端工具
        current_block_type = None
        current_block_id = None
        # 修改原因：Claude 流式 tool_use 按顺序出现，但旧转换把所有工具调用 chunk 都写成 index=0。
        # 修改方式：用 tc_index 分配新工具调用索引，用 current_tc_index 绑定后续 input_json_delta 参数片段。
        # 目的：确保并行工具调用的参数在 OpenAI 兼容流中保持独立。
        tc_index = 0
        current_tc_index = None
        async for line in aiter_decoded_lines(response.aiter_bytes()):

                if line.startswith("data:") and (line := line.lstrip("data: ")):
                    resp: dict = json_loads(line)

                    # 流内错误事件检测（overloaded、429、赛博安全等）
                    event_type_early = resp.get("type", "")
                    if event_type_early == "error" or (event_type_early not in (
                        "message_start", "message_delta", "message_stop",
                        "content_block_start", "content_block_delta", "content_block_stop",
                        "ping",
                    ) and "error" in resp):
                        err_obj = resp.get("error", {})
                        if isinstance(err_obj, dict):
                            err_msg = err_obj.get("message", str(resp))
                            err_type = err_obj.get("type", "upstream_error")
                        else:
                            err_msg = str(err_obj or resp)
                            err_type = "upstream_error"
                        oai_err = json_dumps_text({
                            "error": {
                                "message": err_msg,
                                "type": err_type,
                                "param": None,
                                "code": err_type,
                            }
                        }, ensure_ascii=False)
                        yield f"data: {oai_err}" + end_of_line
                        logger.warning(f"[claude_stream] Upstream error event: {err_type}: {err_msg[:200]}")
                        break

                    if not input_tokens and not cached_tokens and not cache_creation_tokens:
                        msg_usage = safe_get(resp, "message", "usage", default={})
                        if msg_usage:
                            _cache_usage = extract_cache_usage(msg_usage)
                            cached_tokens = _cache_usage["cached_tokens"]
                            cache_creation_tokens = _cache_usage["cache_creation_tokens"]
                            input_tokens = (
                                (msg_usage.get("input_tokens") or 0)
                                + cache_creation_tokens
                                + cached_tokens
                            )
                    # 处理 message_delta 中的拒绝回答（Refusal）信号
                    # 必须在 usage 处理和 break 之前检查，否则 refusal 会被跳过
                    delta_for_refusal = safe_get(resp, "delta", default={})
                    if delta_for_refusal.get("stop_reason") == "refusal":
                        stop_details = delta_for_refusal.get("stop_details", {})
                        refusal_reason = (
                            stop_details.get("explanation")
                            or stop_details.get("type")
                            or stop_details.get("category")
                            or "This request triggered Claude safeguards and was refused."
                        )
                        mark_content_start()
                        sse_content_filter = await generate_sse_response(
                            timestamp, model, content=f"\n[Safeguards Refusal] {refusal_reason}\n", stop="content_filter"
                        )
                        yield sse_content_filter
                        err_payload = {
                            "error": {
                                "message": f"This request triggered Claude safeguards and was refused: {refusal_reason}",
                                "type": "invalid_request_error",
                                "param": None,
                                "code": "content_filter"
                            }
                        }
                        yield f"data: {json_dumps_text(err_payload, ensure_ascii=False)}\n\n"
                        # refusal 后仍然需要写 usage 再 break

                    output_tokens = safe_get(resp, "usage", "output_tokens", default=0)
                    if output_tokens:
                        total_tokens = input_tokens + output_tokens
                        merge_usage(
                            prompt_tokens=input_tokens,
                            completion_tokens=output_tokens,
                            total_tokens=total_tokens,
                            cached_tokens=cached_tokens,
                            cache_creation_tokens=cache_creation_tokens,
                        )
                        sse_string = await generate_sse_response(
                            timestamp, model, None, None, None, None, None,
                            total_tokens, input_tokens, output_tokens,
                            # 流式 Claude 的缓存字段在 message_start 中采集，最终 usage chunk 需要一起输出。
                            cached_tokens=cached_tokens,
                            cache_creation_tokens=cache_creation_tokens,
                        )
                        yield sse_string
                        break

                    # 处理 content_block_start 事件，记录当前块类型
                    event_type = resp.get("type", "")
                    if event_type == "content_block_start":
                        content_block = resp.get("content_block", {})
                        current_block_type = content_block.get("type", "")
                        current_block_id = content_block.get("id", "")

                        # 服务器端工具（server_tool_use）- 不转换为 tool_calls
                        # 让 Claude 自动执行，等待结果
                        if current_block_type == "server_tool_use":
                            # 可选：输出搜索中的提示
                            # sse_string = await generate_sse_response(timestamp, model, content=f"[正在搜索: {content_block.get('name', '')}]\n")
                            # yield sse_string
                            continue

                        # 客户端工具（tool_use）- 转换为 tool_calls
                        if current_block_type == "tool_use":
                            function_call_name = content_block.get("name", "")
                            tools_id = content_block.get("id", "")
                            if tools_id and function_call_name:
                                current_tc_index = tc_index
                                tc_index += 1
                                mark_content_start()
                                sse_string = await generate_sse_response(
                                    timestamp, model, None, tools_id, function_call_name, None,
                                    tool_call_index=current_tc_index,
                                )
                                yield sse_string
                            continue

                    # 处理 content_block_delta 事件
                    if event_type == "content_block_delta":
                        delta = resp.get("delta", {})
                        delta_type = delta.get("type", "")

                        # 服务器端工具的 input_json_delta - 跳过（内部处理）
                        if current_block_type == "server_tool_use" and delta_type == "input_json_delta":
                            continue

                        # 客户端工具的 input_json_delta - 输出参数
                        if current_block_type == "tool_use" and delta_type == "input_json_delta":
                            partial_json = delta.get("partial_json", "")
                            if partial_json:
                                mark_content_start()
                                sse_string = await generate_sse_response(
                                    timestamp, model, None, None, None, partial_json,
                                    tool_call_index=current_tc_index or 0,
                                )
                                yield sse_string
                            continue

                    # 处理 web_search_tool_result - 服务器端搜索结果（跳过，Claude 会自动使用）
                    if event_type == "content_block_start":
                        content_block = resp.get("content_block", {})
                        if content_block.get("type") == "web_search_tool_result":
                            current_block_type = "web_search_tool_result"
                            continue

                    if current_block_type == "web_search_tool_result":
                        continue

                    if event_type == "content_block_stop":
                        # 修改原因：Claude 后续 content_block_start 可能开启新的工具调用，旧状态不能继续套用。
                        # 修改方式：在块结束事件里清空当前块类型和当前工具 index。
                        # 目的：避免服务器端工具或文本块之后的参数片段误用前一个工具调用 index。
                        current_block_type = None
                        current_block_id = None
                        current_tc_index = None
                        continue

                    # 正常文本输出
                    text = safe_get(resp, "delta", "text", default="")
                    if text:
                        mark_content_start()
                        sse_string = await generate_sse_response(timestamp, model, text)
                        yield sse_string
                        continue

                    # 兼容旧逻辑：直接从 content_block 获取工具信息
                    function_call_name = safe_get(resp, "content_block", "name", default=None)
                    tools_id = safe_get(resp, "content_block", "id", default=None)
                    block_type = safe_get(resp, "content_block", "type", default="")
                    # 只处理客户端工具（tool_use），跳过服务器端工具（server_tool_use）
                    if tools_id and function_call_name and block_type == "tool_use":
                        current_tc_index = tc_index
                        tc_index += 1
                        mark_content_start()
                        sse_string = await generate_sse_response(
                            timestamp, model, None, tools_id, function_call_name, None,
                            tool_call_index=current_tc_index,
                        )
                        yield sse_string

                    # thinking 内容
                    thinking_content = safe_get(resp, "delta", "thinking", default="")
                    if thinking_content:
                        mark_content_start()
                        sse_string = await generate_sse_response(timestamp, model, reasoning_content=thinking_content)
                        yield sse_string

                    # 客户端工具参数（兼容旧逻辑）
                    function_call_content = safe_get(resp, "delta", "partial_json", default="")
                    if function_call_content and current_block_type != "server_tool_use":
                        mark_content_start()
                        sse_string = await generate_sse_response(
                            timestamp, model, None, None, None, function_call_content,
                            tool_call_index=current_tc_index or 0,
                        )
                        yield sse_string

    yield "data: [DONE]" + end_of_line


async def fetch_claude_models(client, provider):
    """获取 Anthropic Claude API 的模型列表"""
    from ..utils import resolve_base_url
    raw_base_url = provider.get('base_url', 'https://api.anthropic.com/v1')
    is_fixed = raw_base_url.endswith('#')
    base_url = resolve_base_url(_normalize_claude_base_url(raw_base_url), '')
    api_key = provider.get('api')
    if isinstance(api_key, list):
        api_key = api_key[0] if api_key else None

    headers = {
        'Content-Type': 'application/json',
        'anthropic-version': '2023-06-01',
    }
    if api_key:
        headers['x-api-key'] = api_key

    models = []
    after_id = None
    while True:
        url = base_url if is_fixed else f"{base_url}/models?limit=1000"
        if after_id and not is_fixed:
            url += f"&after_id={after_id}"
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and 'data' in data:
            models.extend(m.get('id') for m in data['data'] if m.get('id'))
            if data.get('has_more') and data.get('last_id'):
                after_id = data['last_id']
            else:
                break
        else:
            break

    return models


def register():
    """注册 Claude 渠道到注册中心"""
    from .registry import register_channel
    
    register_channel(
        id="claude",
        type_name="anthropic",
        default_base_url="https://api.anthropic.com/v1",
        auth_header="x-api-key: {api_key}",
        description="Anthropic Claude API",
        request_adapter=get_claude_payload,
        passthrough_payload_adapter=patch_passthrough_claude_payload,
        passthrough_adapter=get_claude_passthrough_meta,
        response_adapter=fetch_claude_response,
        stream_adapter=fetch_claude_response_stream,
        models_adapter=fetch_claude_models,
        source="builtin",
    )
