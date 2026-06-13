"""
GPT/OpenAI 渠道适配器

负责处理 OpenAI 兼容 API 的请求构建和响应流解析
"""

import json
import random
import string
import asyncio
from datetime import datetime

from ..utils import (
    BaseAPI,
    safe_get,
    get_model_dict,
    get_base64_image,
    is_tools_disabled,
    generate_sse_response,
    end_of_line,
    generate_chunked_image_md,
    upload_image_to_0x0st,
)
from ..response import check_response
from ..json_utils import json_loads, json_dumps_text
from ..response_context import mark_adapter_metrics_managed, mark_content_start, merge_usage
from ..stream_utils import aiter_decoded_lines
from ..usage import extract_cache_usage
from ..file_utils import extract_base64_data


# ============================================================
# OpenAI 格式化函数
# ============================================================

def format_text_message(text: str) -> dict:
    """格式化文本消息为 OpenAI 格式"""
    return {"type": "text", "text": text}


async def format_image_message(image_url: str) -> dict:
    """格式化图片消息为 OpenAI 格式"""
    base64_image, _ = await get_base64_image(image_url)
    return {
        "type": "image_url",
        "image_url": {
            "url": base64_image,
        }
    }


async def get_openai_passthrough_meta(request, engine, provider, api_key=None):
    """透传用：仅构建 url/headers，payload 由入口原生请求提供"""
    headers = {
        'Content-Type': 'application/json',
    }
    if api_key:
        headers['Authorization'] = f"Bearer {api_key}"

    base_api = BaseAPI(provider.get('base_url'))
    url = base_api.chat_url
    # 修改原因：OpenRouter 专属请求头已经归属到 OpenRouter 渠道，通用 OpenAI 兼容渠道不应按 URL 猜测。
    # 修改方式：这里只生成 Content-Type 和 Authorization 这类通用请求头。
    # 目的：避免通用渠道隐式修改其他渠道的请求头。
    return url, headers, {}



async def patch_passthrough_openai_payload(
    payload: dict,
    modifications: dict,
    request,
    engine: str,
    provider: dict,
    api_key=None,
) -> dict:
    """透传模式下对 OpenAI(兼容) payload 做渠道级修饰（主要是 system_prompt 注入）。"""
    system_prompt = modifications.get("system_prompt")
    system_prompt_text = str(system_prompt).strip() if system_prompt is not None else ""
    if not system_prompt_text:
        return payload

    # Chat Completions: messages
    messages = payload.get("messages")
    if isinstance(messages, list):
        for msg in messages:
            if isinstance(msg, dict) and msg.get("role") == "system":
                content = msg.get("content")
                if isinstance(content, str):
                    msg["content"] = f"{system_prompt_text}\n\n{content}" if content else system_prompt_text
                elif isinstance(content, list):
                    if content and isinstance(content[0], dict) and "text" in content[0]:
                        old = content[0].get("text") or ""
                        content[0]["text"] = f"{system_prompt_text}\n\n{old}" if old else system_prompt_text
                    else:
                        content.insert(0, {"type": "text", "text": system_prompt_text})
                else:
                    msg["content"] = system_prompt_text
                return payload
        messages.insert(0, {"role": "system", "content": system_prompt_text})
        return payload

    # 修改原因：非 Chat 请求结构由专用渠道负责，OpenAI 兼容渠道不再改写 input/instructions。
    # 修改方式：这里只处理 messages 数组中的 system_prompt 注入，其他 payload 原样返回。
    # 目的：避免通用渠道隐藏改写已经由上游或插件决定的请求格式。
    return payload


async def get_gpt_payload(request, engine, provider, api_key=None):
    """构建 OpenAI 兼容 API 的请求 payload"""
    headers = {
        'Content-Type': 'application/json',
    }
    model_dict = get_model_dict(provider)
    original_model = model_dict[request.model]
    if api_key:
        headers['Authorization'] = f"Bearer {api_key}"
 
    # 这里统一根据 base_url 拼出真正的聊天端点：
    # - 如果传入的是 https://api.openai.com/v1 → 自动补 /chat/completions
    # - 如果传入的是完整聊天端点 → 原样使用
    base_api = BaseAPI(provider['base_url'])
    url = base_api.chat_url
    # 修改原因：OpenRouter 专属请求头已迁移到 OpenRouter 渠道，通用 OpenAI 渠道不再按 URL 注入。
    # 修改方式：这里只保留通用 headers，避免根据 base_url 做渠道猜测。
    # 目的：让渠道专属行为集中在对应渠道中维护。

    messages = []
    for msg in request.messages:
        # 透传 Message model 上的额外字段（reasoning_content 等）
        # Message 使用 extra='allow'，客户端传入的非标准字段保存在 model_extra 中
        extra_fields = {k: v for k, v in (msg.model_extra or {}).items() if v is not None}

        tool_calls = None
        tool_call_id = None
        if isinstance(msg.content, list):
            content = []
            # 修改原因：Responses API 的 input_* 转换和模型名判断已经移出通用 OpenAI 兼容渠道。
            # 修改方式：这里只构造标准 Chat Completions content item，并继续保留已有的图片和文本文件处理。
            # 目的：让模型能力和特殊格式由上游配置或专用渠道控制，而不是在 core 中硬编码模型名。
            for item in msg.content:
                if item.type == "text":
                    content.append(format_text_message(item.text))
                elif item.type == "image_url" and provider.get("image", True):
                    content.append(await format_image_message(item.image_url.url))
                elif item.type == "file":
                    item_file = item.file
                    if item_file is None:
                        continue

                    mime = getattr(item_file, "mime_type", "") or ""
                    is_image = False
                    if mime.startswith("image/"):
                        is_image = True
                    elif getattr(item_file, "url", None) and item_file.url.startswith("data:image/"):
                        is_image = True

                    if is_image and provider.get("image", True):
                        if getattr(item_file, "data", None):
                            b64 = f"data:{mime};base64,{item_file.data}"
                            content.append(await format_image_message(b64))
                        elif getattr(item_file, "url", None):
                            content.append(await format_image_message(item_file.url))
                    else:
                        # 非图片文件：尝试解码文本类文件为内联文本
                        is_text = (
                            mime.startswith("text/")
                            or mime in (
                                "application/json", "application/xml", "application/yaml",
                                "application/x-yaml", "application/javascript",
                                "application/typescript", "application/sql",
                                "application/x-python", "application/toml",
                                "application/csv", "application/ld+json",
                            )
                        )
                        if is_text and getattr(item_file, "data", None):
                            import base64 as _b64
                            try:
                                decoded = _b64.b64decode(item_file.data).decode("utf-8")
                                fname = getattr(item_file, "filename", "") or ""
                                if fname:
                                    decoded = f"📄 {fname}\n```\n{decoded}\n```"
                                content.append({"type": "text", "text": decoded})
                            except Exception:
                                pass  # 解码失败静默跳过
                        # 其他类型静默跳过
        else:
            # 修改原因：system 消息内容不应按具体模型名自动追加提示词。
            # 修改方式：非列表 content 保持请求原文，仅继续转发工具调用元数据。
            # 目的：避免 core 对模型行为做隐藏改写。
            content = msg.content
            tool_calls = msg.tool_calls
            tool_call_id = msg.tool_call_id

        if tool_calls:
            if not is_tools_disabled(provider):
                tool_calls_list = []
                # 修改原因：OpenAI 兼容历史中的工具调用必须完整保留。
                # 修改方式：工具未禁用时直接遍历全部 tool_calls。
                # 目的：保证后续每个 tool_result 都能匹配到对应 tool_call。
                for tool_call in tool_calls:
                    tool_calls_list.append({
                        "id": tool_call.id,
                        "type": tool_call.type,
                        "function": {
                            "name": tool_call.function.name,
                            "arguments": tool_call.function.arguments
                        }
                    })
                messages.append({"role": msg.role, "tool_calls": tool_calls_list, **extra_fields})
        elif tool_call_id:
            # 修改原因：禁用工具时不应继续转发 tool_result 历史。
            # 修改方式：沿用 provider.tools=False 作为唯一禁用判断。
            # 目的：保留禁用工具逻辑，同时删除工具模式变量。
            if not is_tools_disabled(provider):
                messages.append({"role": msg.role, "tool_call_id": tool_call_id, "content": content, **extra_fields})
        else:
            messages.append({"role": msg.role, "content": content, **extra_fields})

    # 修改原因：Responses API 请求结构已由 openai_responses_channel 负责，通用 OpenAI 渠道只生成 Chat payload。
    # 修改方式：删除按 URL 切换 input/store 的分支，始终使用 messages 字段。
    # 目的：避免同一渠道同时维护两套协议转换逻辑。
    payload = {
        "model": original_model,
        "messages": messages,
    }

    miss_fields = [
        'model',
        'messages',
    ]

    for field, value in request.model_dump(exclude_unset=True).items():
        if field not in miss_fields and value is not None:
            # 修改原因：max_tokens 兼容转换应是字段级通用行为，不应依赖具体模型名。
            # 修改方式：遇到 max_tokens 时统一写入 max_completion_tokens，其余字段保持原样。
            # 目的：保留兼容能力，同时删除模型名硬编码。
            if field == "max_tokens":
                payload["max_completion_tokens"] = value
            else:
                payload[field] = value

    # 修改原因：工具字段清理只应服从 provider.tools=False，不再按模型名猜测能力。
    # 修改方式：删除模型名条件，只保留统一的工具禁用判断。
    # 目的：让模型能力差异由上游或插件控制。
    if is_tools_disabled(provider):
        payload.pop("tools", None)
        payload.pop("tool_choice", None)

    if "api.x.ai" in url:
        payload.pop("stream_options", None)
        payload.pop("presence_penalty", None)
        payload.pop("frequency_penalty", None)

    # 修改原因：temperature 删除、默认 temperature 和 Responses 专属字段都不应由通用渠道按模型名或 URL 决定。
    # 修改方式：删除这些硬编码分支，保留请求、上游和插件已经确定的 payload。
    # 目的：让 core 不再隐藏改写模型参数。
    return url, headers, payload


async def fetch_openai_response(client, url, headers, payload, model, timeout):
    """处理 OpenAI 兼容 API 的非流式响应"""
    json_payload = await asyncio.to_thread(json_dumps_text, payload)
    response = await client.post(url, headers=headers, content=json_payload, timeout=timeout)
    
    error_message = await check_response(response, "fetch_openai_response")
    if error_message:
        yield error_message
        return

    response_bytes = await response.aread()
    response_json = await asyncio.to_thread(json_loads, response_bytes)
    mark_adapter_metrics_managed()
    usage = safe_get(response_json, "usage", default={}) or {}
    # 非流式 OpenAI 兼容响应在同一个 usage 对象中携带缓存字段，随普通 token 一起写入 current_info。
    merge_usage(
        prompt_tokens=safe_get(usage, "prompt_tokens", default=0),
        completion_tokens=safe_get(usage, "completion_tokens", default=0),
        total_tokens=safe_get(usage, "total_tokens", default=0),
        **extract_cache_usage(usage),
    )

    # 兼容原 core/response.py 中的特殊逻辑
    if "dashscope.aliyuncs.com" in url and "multimodal-generation" in url:
        content = safe_get(response_json, "output", "choices", 0, "message", "content", 0, default=None)
        if content:
            mark_content_start()
        yield content
    elif "embedContent" in url:
        content = safe_get(response_json, "embedding", "values", default=[])
        response_embedContent = {
            "object": "list",
            "data": [
                {
                    "object": "embedding",
                    "embedding": content,
                    "index": 0
                }
            ],
            "model": model,
            "usage": {
                "prompt_tokens": 0,
                "total_tokens": 0
            }
        }
        yield response_embedContent
    else:
        if safe_get(response_json, "choices", 0, "message", "content", default=None) or safe_get(response_json, "data", 0, "b64_json", default=None):
            mark_content_start()
        yield response_json


async def fetch_gpt_response_stream(client, url, headers, payload, model, timeout):
    """处理 GPT/OpenAI 流式响应"""
    from ..log_config import logger
    
    timestamp = int(datetime.timestamp(datetime.now()))
    random.seed(timestamp)
    random_str = ''.join(random.choices(string.ascii_letters + string.digits, k=29))
    # 修改原因：core 不解析供应商私有思维链文本，避免把上游内容拆成 reasoning_content。
    # 修改方式：删除思维链解析状态变量，后续流式内容按原始 chunk 透传或按已有结构字段处理。
    # 目的：让思维链处理完全交给上游或插件。
    json_payload = await asyncio.to_thread(json_dumps_text, payload)
    
    async with client.stream('POST', url, headers=headers, content=json_payload, timeout=timeout) as response:
        error_message = await check_response(response, "fetch_gpt_response_stream")
        if error_message:
            yield error_message
            return

        mark_adapter_metrics_managed()
        enter_buffer = ""

        input_tokens = 0
        output_tokens = 0
        # 流式 usage 的缓存字段可能只在最后一个 chunk 出现，需要跨 chunk 暂存后统一 merge。
        cached_tokens = 0
        cache_creation_tokens = 0
        done_received = False

        async for line in aiter_decoded_lines(response.aiter_bytes()):
            # logger.info("line: %s", repr(line))
            if line.startswith(": keepalive"):
                yield line + end_of_line
                continue
            if line and not line.startswith(":") and (result:=line.lstrip("data: ").strip()) and not line.startswith("event: "):
                if result.strip() == "[DONE]":
                    done_received = True
                    break

                line = json_loads(result)

                # 提取 usage（OpenAI chat/completions 流式最后一个 chunk 中包含）
                chunk_usage = line.get("usage") if isinstance(line, dict) else None
                if chunk_usage and isinstance(chunk_usage, dict):
                    _in = chunk_usage.get("prompt_tokens") if "prompt_tokens" in chunk_usage else chunk_usage.get("input_tokens", 0)
                    _out = chunk_usage.get("completion_tokens") if "completion_tokens" in chunk_usage else chunk_usage.get("output_tokens", 0)
                    # OpenAI、Responses API 和 DeepSeek 的缓存字段都在 usage 内，这里统一提取并跨 chunk 保留。
                    _cache_usage = extract_cache_usage(chunk_usage)
                    if _in:
                        input_tokens = _in
                    if _out:
                        output_tokens = _out
                    if _cache_usage["cached_tokens"]:
                        cached_tokens = _cache_usage["cached_tokens"]
                    if _cache_usage["cache_creation_tokens"]:
                        cache_creation_tokens = _cache_usage["cache_creation_tokens"]

                # 检查返回的 JSON 是否包含错误信息
                if 'error' in line:
                    yield {"error": "OpenAI Stream Error", "status_code": 400, "details": line}
                    return

                line['id'] = f"chatcmpl-{random_str}"

                # 修改原因：Responses API 流式事件由 openai_responses_channel 处理，通用 OpenAI 渠道不再转换 response.* 事件。
                # 修改方式：删除 response.* 专用分支以及供应商私有思维链文本解析，继续处理标准 Chat chunk。
                # 目的：保证通用渠道不隐藏解析或改写上游原始内容。
                no_stream_content = safe_get(line, "choices", 0, "message", "content", default=None)
                openrouter_reasoning = safe_get(line, "choices", 0, "delta", "reasoning", default="")
                # reasoning_details 数组格式回退：部分模型只返回 reasoning_details 而不带 reasoning
                if not openrouter_reasoning:
                    _reasoning_details = safe_get(line, "choices", 0, "delta", "reasoning_details", default=None)
                    if _reasoning_details and isinstance(_reasoning_details, list):
                        _parts = []
                        for _rd_item in _reasoning_details:
                            if isinstance(_rd_item, dict) and _rd_item.get("text"):
                                _parts.append(_rd_item["text"])
                        if _parts:
                            openrouter_reasoning = "".join(_parts)
                openrouter_base64_image = safe_get(line, "choices", 0, "delta", "images", 0, "image_url", "url", default="")
                if openrouter_base64_image:
                    b64_pure = extract_base64_data(openrouter_base64_image if openrouter_base64_image.startswith("data:image/") else f"data:image/png;base64,{openrouter_base64_image}")
                    # 发结构化 image content item，方言出口各自转换
                    mark_content_start()
                    image_content_item = [{
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64_pure}"}
                    }]
                    sse_string = await generate_sse_response(timestamp, payload["model"], content=image_content_item)
                    yield sse_string
                    continue

                azure_databricks_claude_summary_content = safe_get(line, "choices", 0, "delta", "content", 0, "summary", 0, "text", default="")
                azure_databricks_claude_signature_content = safe_get(line, "choices", 0, "delta", "content", 0, "summary", 0, "signature", default="")
                if azure_databricks_claude_signature_content:
                    pass
                elif azure_databricks_claude_summary_content:
                    sse_string = await generate_sse_response(timestamp, payload["model"], reasoning_content=azure_databricks_claude_summary_content)
                    yield sse_string
                elif openrouter_reasoning:
                    if openrouter_reasoning.endswith("\\"):
                        enter_buffer += openrouter_reasoning
                        continue
                    elif enter_buffer.endswith("\\") and openrouter_reasoning == 'n':
                        enter_buffer += "n"
                        continue
                    elif enter_buffer.endswith("\\n") and openrouter_reasoning == '\\n':
                        enter_buffer += "\\n"
                        continue
                    elif enter_buffer.endswith("\\n\\n"):
                        openrouter_reasoning = '\n\n' + openrouter_reasoning
                        enter_buffer = ""
                    elif enter_buffer:
                        openrouter_reasoning = enter_buffer + openrouter_reasoning
                        enter_buffer = ''
                    openrouter_reasoning = openrouter_reasoning.replace("\\n", "\n")

                    mark_content_start()
                    sse_string = await generate_sse_response(timestamp, payload["model"], reasoning_content=openrouter_reasoning)
                    yield sse_string
                elif no_stream_content:
                    mark_content_start()
                    sse_string = await generate_sse_response(safe_get(line, "created", default=None), safe_get(line, "model", default=None), content=no_stream_content)
                    yield sse_string
                else:
                    if no_stream_content:
                        del line["choices"][0]["message"]
                    json_line = json_dumps_text(line, ensure_ascii=False)
                    yield "data: " + json_line.strip() + end_of_line

            if done_received:
                break

    if input_tokens or output_tokens:
        # 结束时再 merge 一次，确保只在末尾出现的缓存字段不会被遗漏。
        merge_usage(
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            cached_tokens=cached_tokens,
            cache_creation_tokens=cache_creation_tokens,
        )
        sse_string = await generate_sse_response(
            timestamp, payload["model"], None, None, None, None, None,
            total_tokens=input_tokens + output_tokens,
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            # 结束 usage chunk 需要带上前面跨 chunk 暂存的缓存字段，避免只写入统计而不返回给下游。
            cached_tokens=cached_tokens,
            cache_creation_tokens=cache_creation_tokens,
        )
        yield sse_string

    yield "data: [DONE]" + end_of_line


async def fetch_openai_models(client, provider):
    """获取 OpenAI 兼容 API 的模型列表"""
    raw_base_url = provider.get('base_url', 'https://api.openai.com/v1')
    api_key = provider.get('api')
    if isinstance(api_key, list):
        api_key = api_key[0] if api_key else None
    
    headers = {'Content-Type': 'application/json'}
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'
    
    from ..utils import resolve_base_url
    url = resolve_base_url(raw_base_url, '/models')
    response = await client.get(url, headers=headers)
    response.raise_for_status()
    
    data = response.json()
    models = []
    if isinstance(data, dict) and 'data' in data:
        models = [m.get('id') for m in data['data'] if m.get('id')]
    elif isinstance(data, list):
        models = [m.get('id') if isinstance(m, dict) else m for m in data]
    
    return models


def register():
    """注册 GPT 渠道到注册中心"""
    from .registry import register_channel
    
    register_channel(
        id="openai",
        type_name="openai",
        default_base_url="https://api.openai.com/v1",
        auth_header="Authorization: Bearer {api_key}",
        description="OpenAI 兼容 API",
        request_adapter=get_gpt_payload,
        passthrough_adapter=get_openai_passthrough_meta,
        passthrough_payload_adapter=patch_passthrough_openai_payload,
        response_adapter=fetch_openai_response,
        stream_adapter=fetch_gpt_response_stream,
        models_adapter=fetch_openai_models,
        source="builtin",
    )
