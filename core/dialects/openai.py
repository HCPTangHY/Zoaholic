"""
OpenAI 方言

OpenAI 兼容格式本身就是系统 Canonical 形式，因此：
- parse_request: 直接校验并返回 RequestModel
- render_response / render_stream: 直接透传
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


async def render_openai_response(
    canonical_response: Dict[str, Any],
    model: str,
) -> Dict[str, Any]:
    """Canonical(OpenAI 风格) -> OpenAI 原生响应"""
    return canonical_response


async def render_openai_stream(canonical_sse_chunk: str) -> str:
    """Canonical SSE -> OpenAI SSE"""
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


# ============== 自定义端点处理函数 ==============


async def chat_completions_handler(
    request: "Request",
    background_tasks: "BackgroundTasks",
    api_index: int,
    **kwargs,
):
    """
    OpenAI Chat Completions 端点 - POST /v1/chat/completions
    
    OpenAI 是 Canonical 格式，直接解析为 RequestModel 并调用 handler
    """
    from routes.deps import get_model_handler

    native_body: Dict[str, Any] = await request.json()
    request_model = await parse_openai_request(native_body, {}, {})

    model_handler = get_model_handler()
    return await model_handler.request_model(request_model, api_index, background_tasks)


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
            target_engine="openai",
            endpoints=[
                # POST /v1/chat/completions - Chat Completions
                EndpointDefinition(
                    path="/v1/chat/completions",
                    methods=["POST"],
                    handler=chat_completions_handler,
                    tags=["Chat"],
                    summary="Create Chat Completion",
                    description="创建聊天完成请求，兼容 OpenAI Chat Completions API 格式",
                ),
            ],
        )
    )