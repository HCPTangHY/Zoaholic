"""
OpenAI 方言

OpenAI 兼容格式本身就是系统 Canonical 形式，因此：
- parse_request: 直接校验并返回 RequestModel
- render_response: 将结构化 content list 拍扁为 markdown 字符串
- render_stream: 直接透传（流式 delta 已是字符串）
- endpoints: Chat Completions 端点
"""

from typing import Any, Dict, Optional, TYPE_CHECKING

from core.models import RequestModel

from .registry import DialectDefinition, EndpointDefinition, register_dialect

if TYPE_CHECKING:
    from fastapi import Request, BackgroundTasks


async def parse_openai_request(
    native_body: Dict[str, Any],
    path_params: Dict[str, str],
    headers: Dict[str, str],
) -> RequestModel:
    """native(OpenAI) -> Canonical(RequestModel)"""
    if isinstance(native_body, RequestModel):
        return native_body
    return RequestModel(**native_body)


def _flatten_content_list(content) -> str:
    """将结构化 content list 拍扁为 markdown 字符串。

    Chat Completions API 的响应 content 必须是 string，
    但内核可能返回结构化 list（含 text/image_url 等 items）。
    """
    if not isinstance(content, list):
        return content

    parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type", "")
        if item_type == "text":
            text = item.get("text", "")
            if text:
                parts.append(text)
        elif item_type == "image_url":
            image_url = item.get("image_url")
            url = ""
            if isinstance(image_url, dict):
                url = image_url.get("url", "")
            elif isinstance(image_url, str):
                url = image_url
            if url:
                parts.append(f"![image]({url})")

    return "\n\n".join(parts) if parts else ""


async def render_openai_response(
    canonical_response: Dict[str, Any],
    model: str,
) -> Dict[str, Any]:
    """Canonical -> OpenAI 原生响应

    将结构化 content list 拍扁为 markdown 字符串，
    因为 Chat Completions API 的 content 字段必须是 string。
    """
    choices = canonical_response.get("choices") or []
    for choice in choices:
        msg = choice.get("message")
        if not msg:
            continue
        content = msg.get("content")
        if isinstance(content, list):
            msg["content"] = _flatten_content_list(content)

    return canonical_response


async def render_openai_stream(canonical_sse_chunk: str) -> str:
    """Canonical SSE -> OpenAI SSE（直接透传）"""
    return canonical_sse_chunk


def parse_openai_usage(data: Any) -> Optional[Dict[str, int]]:
    """从 OpenAI 格式中提取 usage"""
    if not isinstance(data, dict):
        return None
    usage = data.get("usage")
    if not usage:
        usage = data.get("message", {}).get("usage")

    if usage:
        prompt = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
        completion = usage.get("completion_tokens") or usage.get("output_tokens") or 0
        total = usage.get("total_tokens") or (prompt + completion)
        if prompt or completion:
            return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}
    return None


# ============== 注册 ==============


def register() -> None:
    """注册 OpenAI 方言"""
    register_dialect(
        DialectDefinition(
            id="openai",
            name="OpenAI Compatible",
            description="OpenAI 兼容格式（默认 Canonical）",
            parse_request=parse_openai_request,
            render_response=render_openai_response,
            render_stream=render_openai_stream,
            parse_usage=parse_openai_usage,
            target_engine=["openai", "openrouter"],
            endpoints=[
                # POST /v1/chat/completions - Chat Completions
                EndpointDefinition(
                    path="/v1/chat/completions",
                    methods=["POST"],
                    tags=["Chat"],
                    summary="Create Chat Completion",
                    description="创建聊天完成请求，兼容 OpenAI Chat Completions API 格式",
                ),
            ],
        )
    )
