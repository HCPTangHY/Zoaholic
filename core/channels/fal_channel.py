"""
fal.ai 渠道适配器。

修改原因：项目需要把 Chat Completions 请求转换到 fal.ai 原生生成接口，覆盖图片、视频、音频和 3D 场景。
修改方式：新增独立渠道文件，提供 request adapter、非流式 response adapter、流式 stream adapter 和注册函数。
目的：让 fal.ai 可以通过统一渠道注册表使用，同时保持返回给客户端的格式仍为 Chat Completions。
"""

from __future__ import annotations

import asyncio
import random
import string
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from ..json_utils import json_dumps_text, json_loads
from ..response import check_response
from ..response_context import mark_adapter_metrics_managed, mark_content_start
from ..utils import end_of_line, generate_sse_response, get_model_dict, resolve_base_url
from .openai_image_channel import _extract_prompt_and_images as _extract_openai_image_prompt_and_images


# 修改原因：fal 原生接口不接受 Chat Completions 的控制字段。
# 修改方式：集中维护需要从 request extra 字段中剔除的字段名。
# 目的：只把 image_size、duration、output_format 等 fal 原生参数继续传给上游。
_CHAT_COMPLETIONS_FIELDS = {
    "model",
    "messages",
    "stream",
    "tools",
    "tool_choice",
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "max_tokens",
    "max_completion_tokens",
    "presence_penalty",
    "frequency_penalty",
    "n",
    "user",
    "response_format",
    "stream_options",
    "include_usage",
    "logprobs",
    "top_logprobs",
    "thinking",
    "chat_template_kwargs",
}


# 修改原因：部分 fal 端点虽然走同一个 API 域名，但长任务必须使用 queue.fal.run。
# 修改方式：把模型名关键词判断集中到一个函数，并允许 provider.preferences.fal_mode 显式覆盖。
# 目的：避免视频、音频和 3D 请求误发到同步端点，也允许管理员按需强制同步或队列模式。
def _should_use_queue(model_name: str, provider: dict) -> bool:
    """判断当前 fal 请求是否应使用队列模式。"""
    preferences = provider.get("preferences", {}) if isinstance(provider, dict) else {}
    mode = preferences.get("fal_mode") if isinstance(preferences, dict) else None
    if mode == "queue":
        return True
    if mode == "sync":
        return False

    lower = (model_name or "").lower()
    queue_keywords = [
        "video",
        "3d",
        "hunyuan-3d",
        "tts",
        "speech",
        "audio",
        "seedance",
        "kling-video",
        "wan",
    ]
    return any(keyword in lower for keyword in queue_keywords)


# 修改原因：fal 的同步地址允许自定义 base_url，但队列地址按官方域名固定为 queue.fal.run。
# 修改方式：分别提供同步和队列 URL 构造函数。
# 目的：让 request adapter 的 URL 逻辑清晰，同时避免后续流式处理再猜测 endpoint_id。
def _build_sync_url(base_url: str, endpoint_id: str) -> str:
    """构建 fal.run 同步请求 URL。"""
    return resolve_base_url(base_url.rstrip("/"), f"/{endpoint_id.lstrip('/')}")


def _build_queue_url(endpoint_id: str) -> str:
    """构建 queue.fal.run 队列请求 URL。"""
    return f"https://queue.fal.run/{endpoint_id.lstrip('/')}"


# 修改原因：流式适配器拿不到 provider 配置，只能根据 request adapter 产出的 URL 判断模式。
# 修改方式：通过官方队列域名识别 queue 模式。
# 目的：保留 fal_mode=sync 对视频类模型的强制覆盖效果。
def _is_queue_url(url: str) -> bool:
    """根据 URL 判断当前请求是否走 fal 队列端点。"""
    return "queue.fal.run" in (url or "")


# 修改原因：fal 接受大量原生字段，不能只维护少数白名单字段。
# 修改方式：从 RequestModel.model_dump(exclude_unset=True) 中删除 Chat Completions 字段，剩余字段作为 fal 原生参数。
# 目的：让 image_size、aspect_ratio、duration、safety_tolerance 等请求级参数无需改代码即可透传。
def _extract_fal_native_params(request) -> dict[str, Any]:
    """从 request 的 extra 字段中提取 fal 原生参数。"""
    try:
        request_dict = request.model_dump(exclude_unset=True)
    except Exception:
        request_dict = {}

    for field in _CHAT_COMPLETIONS_FIELDS:
        request_dict.pop(field, None)

    # 修改原因：prompt 应统一从最后一条 user message 提取，避免 extra prompt 与 messages 出现冲突。
    # 修改方式：显式删除 request extra 中的 prompt 字段。
    # 目的：保持 Chat Completions → fal 的 prompt 来源稳定。
    request_dict.pop("prompt", None)

    return {key: value for key, value in request_dict.items() if value is not None}


# 修改原因：fal 与 OpenAI Image 渠道需要从同一种 Chat Completions 消息格式提取文本和图片。
# 修改方式：复用 openai_image_channel 中已经验证过的提取函数，而不是复制同一段逻辑。
# 目的：减少两类图片渠道之间的行为差异和后续维护成本。
def _extract_prompt_and_images(request) -> tuple[str, list[str]]:
    """从 Chat Completions messages 提取 prompt 和图片 URL。"""
    prompt, images = _extract_openai_image_prompt_and_images(request)
    return prompt, list(images or [])


# 修改原因：fal 同一个响应可能包含图片、视频、音频、3D 文件、seed 和改写后的 prompt。
# 修改方式：把所有媒体 URL 拼接为 markdown 字符串。
# 目的：满足客户端直接从 message.content 读取结果的要求，不使用结构化 content items。
def _format_fal_result(response_json: dict, payload: dict) -> str:
    """把 fal 响应统一格式化为 markdown string。"""
    if not isinstance(response_json, dict):
        return ""

    # 修改原因：部分队列包装器可能把真实结果放在 result 字段中。
    # 修改方式：检测到 result 为字典时优先格式化 result。
    # 目的：兼容 fal 队列结果和第三方代理的轻微包装差异。
    result = response_json.get("result")
    if isinstance(result, dict):
        response_json = result

    content_parts: list[str] = []

    images = response_json.get("images", [])
    if isinstance(images, dict):
        images = [images]
    if isinstance(images, list):
        for image in images:
            if not isinstance(image, dict):
                continue
            url = image.get("url", "")
            if url:
                content_parts.append(f"![image]({url})")

    video = response_json.get("video")
    if isinstance(video, dict) and video.get("url"):
        content_parts.append(f"[🎬 视频]({video['url']})")

    audio = response_json.get("audio")
    if isinstance(audio, dict) and audio.get("url"):
        content_parts.append(f"[🔊 音频]({audio['url']})")

    model_glb = response_json.get("model_glb")
    if isinstance(model_glb, dict) and model_glb.get("url"):
        content_parts.append(f"[🧊 3D模型]({model_glb['url']})")

    seed = response_json.get("seed")
    if seed is not None:
        content_parts.append(f"\n*seed: {seed}*")

    prompt_text = response_json.get("prompt")
    if prompt_text and prompt_text != payload.get("prompt"):
        content_parts.append(f"\n*Revised prompt: {prompt_text}*")

    return "\n\n".join(content_parts)


# 修改原因：同步和队列的非流式返回都需要包装成同一种 Chat Completions 结构。
# 修改方式：将 id、created、choices 等字段构造集中到公共函数。
# 目的：避免 stream 与 response 两条路径格式不一致。
def _build_chat_completion(content: str, model: str) -> dict[str, Any]:
    """把 markdown 内容包装成 Chat Completions 非流式响应。"""
    timestamp = int(datetime.timestamp(datetime.now()))
    random.seed(timestamp)
    random_str = "".join(random.choices(string.ascii_letters + string.digits, k=29))

    return {
        "id": f"chatcmpl-{random_str}",
        "object": "chat.completion",
        "created": timestamp,
        "model": model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": content,
                "refusal": None,
            },
            "logprobs": None,
            "finish_reason": "stop",
        }],
        # 修改原因：fal 不返回 token usage。
        # 修改方式：保持 usage 为 None，由请求统计上下文只记录 adapter 已管理状态。
        # 目的：避免伪造不存在的 token 统计。
        "usage": None,
        "system_fingerprint": "fp_fal_api",
    }


# 修改原因：fal POST/GET 响应都需要读取完整 JSON，且要复用项目统一的错误处理。
# 修改方式：提供两个小型读取函数，内部调用 check_response 和 json_loads。
# 目的：让同步、非流式队列和流式队列路径的错误处理一致。
async def _read_json_response(response, error_log: str):
    """校验 HTTP 响应并读取 JSON。返回 (error, data)。"""
    error_message = await check_response(response, error_log)
    if error_message:
        return error_message, None

    response_bytes = await response.aread()
    response_json = await asyncio.to_thread(json_loads, response_bytes)
    return None, response_json


async def _post_json(client, url: str, headers: dict, payload: dict, timeout: int, error_log: str):
    """以项目统一 JSON 序列化方式发起 POST 请求并读取响应。"""
    json_payload = await asyncio.to_thread(json_dumps_text, payload)
    response = await client.post(url, headers=headers, content=json_payload, timeout=timeout)
    return await _read_json_response(response, error_log)


# 修改原因：非流式客户端请求视频、音频或 3D 时也可能进入 queue.fal.run。
# 修改方式：抽取队列提交、轮询和取结果逻辑，供非流式 response adapter 复用。
# 目的：让客户端即使不请求 stream=true，也能拿到最终 fal 生成结果。
async def _fetch_fal_queue_result(client, queue_url: str, headers: dict, payload: dict, timeout: int):
    """提交 fal 队列任务并等待最终结果。返回 (error, result_json)。"""
    error_message, submit_data = await _post_json(
        client,
        queue_url,
        headers,
        payload,
        30,
        "fetch_fal_queue_submit",
    )
    if error_message:
        return error_message, None

    request_id = submit_data.get("request_id") if isinstance(submit_data, dict) else None
    if not request_id:
        return {"error": "fal queue submit missing request_id", "status_code": 502, "details": submit_data}, None

    status_base = f"{queue_url}/requests/{request_id}"
    deadline = time.time() + max(int(timeout or 0), 30)

    while True:
        response = await client.get(f"{status_base}/status?logs=1", headers=headers, timeout=15)
        error_message, status_data = await _read_json_response(response, "fetch_fal_queue_status")
        if error_message:
            return error_message, None

        status = status_data.get("status") if isinstance(status_data, dict) else None
        if status == "COMPLETED":
            break
        if status in {"FAILED", "CANCELED", "CANCELLED"}:
            return {"error": "fal queue request failed", "status_code": 502, "details": status_data}, None
        if time.time() >= deadline:
            return {"error": "fal queue request timeout", "status_code": 504, "details": status_data}, None

        await asyncio.sleep(3)

    result_response = await client.get(status_base, headers=headers, timeout=30)
    return await _read_json_response(result_response, "fetch_fal_queue_result")


# ============================================================
# 请求构建
# ============================================================


async def get_fal_payload(request, engine, provider, api_key=None):
    """
    Chat Completions → fal.ai 原生格式。

    1. 从 messages 提取 prompt（最后一条 user message 的文本内容）
    2. 从 messages 提取图片 URL（user message 中的 image_url content items）
    3. 从 request extra fields 合并 fal 原生参数；provider overrides 由 core.request 统一合并
    4. 构建 fal 请求 URL、headers 和 payload
    """
    headers = {
        "Content-Type": "application/json",
    }
    if api_key is not None:
        # 修改原因：fal.ai 认证头使用 Key 前缀，不兼容 OpenAI 的 Bearer 前缀。
        # 修改方式：Authorization 固定写为 Key {api_key}。
        # 目的：避免上游因鉴权格式错误拒绝请求。
        headers["Authorization"] = f"Key {api_key}"

    model_dict = get_model_dict(provider)
    endpoint_id = model_dict.get(request.model, request.model)

    base_url = provider.get("base_url") or "https://fal.run"
    if _should_use_queue(endpoint_id, provider):
        url = _build_queue_url(endpoint_id)
    else:
        url = _build_sync_url(base_url, endpoint_id)

    prompt, images = _extract_prompt_and_images(request)
    payload: dict[str, Any] = {"prompt": prompt}

    # 修改原因：fal 的 Kontext/Edit 类模型对单图和多图字段名不同。
    # 修改方式：一张图写 image_url，多张图写 image_urls。
    # 目的：让同一个 Chat Completions 图片附件格式可覆盖编辑类模型。
    if images:
        if len(images) == 1:
            payload["image_url"] = images[0]
        else:
            payload["image_urls"] = images

    payload.update(_extract_fal_native_params(request))

    # 修改原因：即使 extra 字段中带入了 Chat Completions 字段，也不能把它们发给 fal。
    # 修改方式：在返回前再次删除通用聊天字段。
    # 目的：对齐 openai_image_channel 的清理行为，降低上游 400 风险。
    for field in _CHAT_COMPLETIONS_FIELDS:
        payload.pop(field, None)

    return url, headers, payload


# ============================================================
# 响应处理
# ============================================================


async def fetch_fal_response(client, url, headers, payload, model, timeout):
    """
    非流式响应处理。

    同步模式：POST fal.run → 拿到结果 → 包装成 chat completions 格式。
    队列模式：POST queue.fal.run → 轮询完成 → 拿到结果 → 包装成 chat completions 格式。
    """
    payload.pop("stream", None)

    if _is_queue_url(url):
        error_message, response_json = await _fetch_fal_queue_result(client, url, headers, payload, timeout)
    else:
        error_message, response_json = await _post_json(
            client,
            url,
            headers,
            payload,
            timeout,
            "fetch_fal_response",
        )

    if error_message:
        yield error_message
        return

    content = _format_fal_result(response_json, payload)

    mark_adapter_metrics_managed()
    if content:
        mark_content_start()

    yield _build_chat_completion(content, model)


async def _fetch_fal_sync_stream(client, url, headers, payload, model, timeout):
    """把 fal 同步结果转换为 Chat Completions SSE。"""
    error_message, response_json = await _post_json(
        client,
        url,
        headers,
        payload,
        timeout,
        "fetch_fal_stream_sync",
    )
    if error_message:
        yield error_message
        return

    timestamp = int(datetime.timestamp(datetime.now()))
    mark_adapter_metrics_managed()

    yield await generate_sse_response(timestamp, model, role="assistant")

    content = _format_fal_result(response_json, payload)
    if content:
        mark_content_start()
        yield await generate_sse_response(timestamp, model, content=content)

    yield await generate_sse_response(timestamp, model, stop="stop")
    yield "data: [DONE]" + end_of_line


async def _fetch_fal_queue_stream(client, queue_url, headers, payload, model, timeout):
    """提交 fal 队列任务、推送进度 SSE，并在完成后推送最终 markdown 内容。"""
    timestamp = int(datetime.timestamp(datetime.now()))

    error_message, submit_data = await _post_json(
        client,
        queue_url,
        headers,
        payload,
        30,
        "fetch_fal_stream_submit",
    )
    if error_message:
        yield error_message
        return

    request_id = submit_data.get("request_id") if isinstance(submit_data, dict) else None
    if not request_id:
        yield {"error": "fal queue submit missing request_id", "status_code": 502, "details": submit_data}
        return

    mark_adapter_metrics_managed()

    yield await generate_sse_response(timestamp, model, role="assistant")
    yield await generate_sse_response(timestamp, model, content="⏳ 任务已提交，等待处理...\n")
    mark_content_start()

    status_base = f"{queue_url}/requests/{request_id}"
    last_heartbeat = time.time()
    seen_logs: set[str] = set()
    deadline = time.time() + max(int(timeout or 0), 30)

    while True:
        status_response = await client.get(f"{status_base}/status?logs=1", headers=headers, timeout=15)
        error_message, status_data = await _read_json_response(status_response, "fetch_fal_stream_status")
        if error_message:
            yield error_message
            return

        status = status_data.get("status") if isinstance(status_data, dict) else None
        if status == "COMPLETED":
            break
        if status in {"FAILED", "CANCELED", "CANCELLED"}:
            yield {"error": "fal queue request failed", "status_code": 502, "details": status_data}
            return
        if time.time() >= deadline:
            yield {"error": "fal queue request timeout", "status_code": 504, "details": status_data}
            return

        if status == "IN_QUEUE":
            position = status_data.get("queue_position", "?") if isinstance(status_data, dict) else "?"
            yield await generate_sse_response(timestamp, model, content=f"\r⏳ 排队中 (位置 {position})...")
        elif status == "IN_PROGRESS":
            logs = status_data.get("logs", []) if isinstance(status_data, dict) else []
            if isinstance(logs, list) and logs:
                for log in logs:
                    message = log.get("message", "") if isinstance(log, dict) else str(log)
                    if not message or message in seen_logs:
                        continue
                    seen_logs.add(message)
                    yield await generate_sse_response(timestamp, model, content=f"\n📋 {message}")
            else:
                yield await generate_sse_response(timestamp, model, content="\n⚙️ 生成中...")

        # 修改原因：fal 长任务可能超过反向代理空闲超时。
        # 修改方式：每 15 秒输出一个 SSE comment 心跳，不作为 data 事件。
        # 目的：降低 nginx proxy_read_timeout 或客户端空闲断连概率。
        if time.time() - last_heartbeat > 15:
            yield ": heartbeat" + end_of_line
            last_heartbeat = time.time()

        await asyncio.sleep(3)

    result_response = await client.get(status_base, headers=headers, timeout=30)
    error_message, result_data = await _read_json_response(result_response, "fetch_fal_stream_result")
    if error_message:
        yield error_message
        return

    content = _format_fal_result(result_data, payload)
    if content:
        yield await generate_sse_response(timestamp, model, content=f"\n\n{content}")

    yield await generate_sse_response(timestamp, model, stop="stop")
    yield "data: [DONE]" + end_of_line


async def fetch_fal_stream(client, url, headers, payload, model, timeout):
    """
    流式响应处理。

    队列模式：提交到 queue.fal.run，轮询状态，推送进度 SSE，完成后推送最终内容。
    同步模式：POST fal.run 拿完整结果，再转换成 SSE 返回。
    """
    payload.pop("stream", None)

    if _is_queue_url(url):
        async for chunk in _fetch_fal_queue_stream(client, url, headers, payload, model, timeout):
            yield chunk
        return

    async for chunk in _fetch_fal_sync_stream(client, url, headers, payload, model, timeout):
        yield chunk


# ============================================================
# 模型列表
# ============================================================


async def fetch_fal_models(client, provider):
    """全量获取 fal.ai 模型列表，并转换为 OpenAI 兼容模型对象。"""
    # 修改原因：fal.ai 模型列表接口实测为 https://fal.ai/api/models，返回 items/page/pages/total，模型端点字段为 id。
    # 修改方式：固定从 fal.ai 官网 API 按 page 参数分页拉取，不复用 fal.run 推理 base_url。
    # 目的：全量返回所有 fal 模型端点，避免只读取第一页或把推理端点误当模型列表端点。
    api_key = provider.get("api") if isinstance(provider, dict) else None
    if isinstance(api_key, list):
        api_key = api_key[0] if api_key else None

    headers = {"Content-Type": "application/json"}
    if api_key:
        # 修改原因：fal.ai 使用 Key 认证格式；实测模型列表接口带无效 Key 也会返回公开模型。
        # 修改方式：当配置中存在 api 时仍按渠道认证格式传入 Authorization。
        # 目的：保持 models_adapter 与请求适配器的认证格式一致，也兼容未来需要认证的列表接口。
        headers["Authorization"] = f"Key {api_key}"

    limit = 1000
    page = 1
    total_pages = 1
    models: list[dict[str, Any]] = []

    while page <= total_pages:
        query = urlencode({"limit": limit, "page": page})
        response = await client.get(f"https://fal.ai/api/models?{query}", headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()

        items = data.get("items", []) if isinstance(data, dict) else []
        if not isinstance(items, list):
            items = []

        for item in items:
            # 修改原因：用户要求不加任何过滤，但仍需要跳过没有 endpoint id 的异常条目，否则无法形成有效模型对象。
            # 修改方式：仅从 dict 条目读取 id 字段并做空值保护，不检查 status/deprecated/removed/category 等字段。
            # 目的：把 fal 返回的每个可识别 endpoint_id 都原样暴露给模型列表。
            endpoint_id = item.get("id") if isinstance(item, dict) else None
            if not endpoint_id:
                continue
            models.append(endpoint_id)

        # 修改原因：fal.ai API 返回 pages 表示总页数；全量拉取必须持续到最后一页。
        # 修改方式：每次响应后更新 total_pages，并在缺失 pages 时按 items 数量判断是否继续。
        # 目的：兼容当前实测字段，同时避免接口轻微变化导致无限循环或漏页。
        if isinstance(data, dict) and isinstance(data.get("pages"), int):
            total_pages = max(data["pages"], page)
        elif len(items) >= limit:
            total_pages = page + 1
        else:
            total_pages = page

        page += 1

    return models


# ============================================================
# 注册
# ============================================================


def register():
    """注册 fal.ai 渠道到注册中心。"""
    from .registry import register_channel

    register_channel(
        id="fal",
        type_name="fal",
        default_base_url="https://fal.run",
        auth_header="Authorization: Key {api_key}",
        description="Fal.ai 图片 / 视频 / 音频 / 3D 生成",
        request_adapter=get_fal_payload,
        response_adapter=fetch_fal_response,
        stream_adapter=fetch_fal_stream,
        models_adapter=fetch_fal_models,
        source="builtin",
    )
