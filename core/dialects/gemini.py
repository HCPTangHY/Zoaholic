"""
Gemini 方言

支持 Google Gemini 原生格式的入口/出口转换：
- parse_request: Gemini native -> Canonical(RequestModel)
- render_response: Canonical(OpenAI 风格) -> Gemini native
- render_stream: Canonical SSE -> Gemini SSE
- endpoints: 自动注册的端点定义
"""

import json
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from core.models import RequestModel, Message, ContentItem

from .registry import DialectDefinition, EndpointDefinition, register_dialect

if TYPE_CHECKING:
    from fastapi import Request


async def extract_gemini_token(request: "Request") -> Optional[str]:
    """
    从 Gemini 风格请求中提取 API token
    
    支持两种方式：
    1. x-goog-api-key 头部
    2. ?key=xxx 查询参数
    """
    # x-goog-api-key 头
    if request.headers.get("x-goog-api-key"):
        return request.headers.get("x-goog-api-key")
    
    # ?key=xxx 查询参数
    if request.query_params.get("key"):
        return request.query_params.get("key")
    
    return None


def _parse_gemini_tools(native_body: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """提取 Gemini tools.function_declarations 并转换为 OpenAI tools 结构"""
    native_tools = native_body.get("tools") or []
    if not isinstance(native_tools, list):
        return None

    tools: List[Dict[str, Any]] = []
    for tool_group in native_tools:
        if not isinstance(tool_group, dict):
            continue
        declarations = (
            tool_group.get("function_declarations")
            or tool_group.get("functionDeclarations")
            or tool_group.get("function_declaration")
            or tool_group.get("functionDeclaration")
        )
        if not declarations or not isinstance(declarations, list):
            continue
        for decl in declarations:
            if not isinstance(decl, dict):
                continue
            fn = {
                "name": decl.get("name"),
                "description": decl.get("description"),
            }
            if isinstance(decl.get("parameters"), dict):
                fn["parameters"] = decl.get("parameters")
            if fn.get("name"):
                tools.append({"type": "function", "function": fn})

    return tools or None


async def parse_gemini_request(
    native_body: Dict[str, Any],
    path_params: Dict[str, str],
    headers: Dict[str, str],
) -> RequestModel:
    """
    Gemini native -> Canonical(RequestModel)

    支持字段：
    - contents[].role/parts -> messages
    - systemInstruction -> system message
    - generationConfig -> temperature/max_tokens/top_p/top_k
    - tools.function_declarations -> tools
    """
    messages: List[Message] = []

    # systemInstruction
    system_instruction = native_body.get("systemInstruction")
    if isinstance(system_instruction, dict):
        sys_parts = system_instruction.get("parts") or []
        if isinstance(sys_parts, list):
            sys_text = "".join(
                str(p.get("text", "")) for p in sys_parts if isinstance(p, dict)
            ).strip()
            if sys_text:
                messages.append(Message(role="system", content=sys_text))

    # contents
    for content in native_body.get("contents", []) or []:
        if not isinstance(content, dict):
            continue
        role = content.get("role") or "user"
        if role == "model":
            role = "assistant"

        parts = content.get("parts") or []
        if not isinstance(parts, list):
            continue

        content_items: List[ContentItem] = []
        text_acc: List[str] = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            if "text" in part:
                text = str(part.get("text", ""))
                text_acc.append(text)
                content_items.append(ContentItem(type="text", text=text))
            elif "inlineData" in part and isinstance(part.get("inlineData"), dict):
                inline = part["inlineData"]
                mime_type = inline.get("mimeType", "image/png")
                data = inline.get("data", "")
                content_items.append(
                    ContentItem(
                        type="image_url",
                        image_url={"url": f"data:{mime_type};base64,{data}"},
                    )
                )

        if not content_items:
            continue

        if len(content_items) == 1 and content_items[0].type == "text":
            messages.append(Message(role=role, content="".join(text_acc)))
        else:
            messages.append(Message(role=role, content=content_items))

    model = path_params.get("model") or native_body.get("model") or ""
    action = path_params.get("action") or ""
    stream_flag = "streamGenerateContent" in action or bool(native_body.get("stream"))

    gen_config = native_body.get("generationConfig") or {}
    if not isinstance(gen_config, dict):
        gen_config = {}

    tools = _parse_gemini_tools(native_body)

    if not messages:
        messages = [Message(role="user", content="")]

    # 提取所有未显式处理的字段到 extra_body.google
    # 这样可以确保像 thinkingConfig 这样的字段能透传到上游
    google_extra = {
        k: v for k, v in native_body.items()
        if k not in ("contents", "systemInstruction", "generationConfig", "tools", "model", "stream")
    }
    # 合并 generationConfig 中的额外字段
    for k, v in gen_config.items():
        if k not in ("temperature", "maxOutputTokens", "topP", "topK"):
            google_extra[k] = v

    return RequestModel(
        model=model,
        messages=messages,
        temperature=gen_config.get("temperature"),
        max_tokens=gen_config.get("maxOutputTokens"),
        top_p=gen_config.get("topP"),
        top_k=gen_config.get("topK"),
        tools=tools,
        stream=stream_flag,
        extra_body={"google": google_extra} if google_extra else None,
    )


async def render_gemini_response(
    canonical_response: Dict[str, Any],
    model: str,
) -> Dict[str, Any]:
    """
    Canonical(OpenAI 风格) -> Gemini native response
    """
    choices = canonical_response.get("choices") or []
    content_text = ""
    if choices:
        msg = choices[0].get("message") or {}
        content_text = msg.get("content") or ""
        if isinstance(content_text, list):
            content_text = "".join(
                str(i.get("text", "")) for i in content_text if isinstance(i, dict)
            )

    usage = canonical_response.get("usage") or {}

    return {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [{"text": content_text}],
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": usage.get("prompt_tokens", 0),
            "candidatesTokenCount": usage.get("completion_tokens", 0),
            "totalTokenCount": usage.get("total_tokens", 0),
        },
    }


async def render_gemini_stream(canonical_sse_chunk: str) -> str:
    """
    Canonical SSE -> Gemini SSE

    输入: "data: {...}\n\n"
    输出: "data: {...}\n\n" (Gemini candidates 格式)
    """
    if not isinstance(canonical_sse_chunk, str):
        return canonical_sse_chunk

    if not canonical_sse_chunk.startswith("data: "):
        return canonical_sse_chunk

    data_str = canonical_sse_chunk[6:].strip()
    if data_str == "[DONE]":
        return ""

    try:
        canonical = json.loads(data_str)
    except json.JSONDecodeError:
        return canonical_sse_chunk

    choices = canonical.get("choices") or []
    if not choices:
        return ""

    delta = choices[0].get("delta") or {}
    content = delta.get("content") or ""
    reasoning = delta.get("reasoning_content") or ""

    gemini_chunk: Dict[str, Any] = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [],
                }
            }
        ]
    }

    if reasoning:
        gemini_chunk["candidates"][0]["content"]["parts"].append(
            {"thought": True, "text": reasoning}
        )
    if content:
        gemini_chunk["candidates"][0]["content"]["parts"].append({"text": content})

    finish_reason = choices[0].get("finish_reason")
    if finish_reason:
        gemini_chunk["candidates"][0]["finishReason"] = "STOP"

    usage = canonical.get("usage")
    if isinstance(usage, dict):
        gemini_chunk["usageMetadata"] = {
            "promptTokenCount": usage.get("prompt_tokens", 0),
            "candidatesTokenCount": usage.get("completion_tokens", 0),
            "totalTokenCount": usage.get("total_tokens", 0),
        }

    return f"data: {json.dumps(gemini_chunk, ensure_ascii=False)}\n\n"


def parse_gemini_usage(data: Any) -> Optional[Dict[str, int]]:
    """从 Gemini 格式中提取 usage"""
    if not isinstance(data, dict):
        return None
    usage = data.get("usageMetadata")
    if usage:
        prompt = usage.get("promptTokenCount", 0)
        completion = usage.get("candidatesTokenCount", 0)
        total = usage.get("totalTokenCount", prompt + completion)
        if prompt or completion:
            return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}
    return None


# ============== 自定义端点处理函数 ==============


async def list_gemini_models_handler(
    request: "Request",
    api_index: int,
    **kwargs,
):
    """
    Gemini 模型列表端点 - GET /v1beta/models
    
    返回格式与 Google Gemini API 一致：
    {"models": [{"name": "models/{id}"}, ...]}
    """
    from fastapi.responses import JSONResponse
    from routes.deps import get_app
    from utils import post_all_models

    app = get_app()
    models = post_all_models(api_index, app.state.config, app.state.api_list, app.state.models_list)
    gemini_models = [
        {"name": f"models/{m['id']}"} for m in models
        if isinstance(m, dict) and m.get("id")
    ]
    return JSONResponse(content={"models": gemini_models})


# ============== 注册 ==============


def register() -> None:
    """注册 Gemini 方言"""
    register_dialect(
        DialectDefinition(
            id="gemini",
            name="Google Gemini",
            description="Google Gemini API 原生格式",
            parse_request=parse_gemini_request,
            render_response=render_gemini_response,
            render_stream=render_gemini_stream,
            parse_usage=parse_gemini_usage,
            target_engine="gemini",
            extract_token=extract_gemini_token,
            endpoints=[
                # GET /v1beta/models - 列出模型（自定义处理函数）
                EndpointDefinition(
                    prefix="/v1beta",
                    path="/models",
                    methods=["GET"],
                    handler=list_gemini_models_handler,
                    tags=["Gemini Dialect"],
                    summary="List Gemini Models",
                    description="返回 Gemini 格式的模型列表",
                ),
                # POST /v1beta/models/{model}:generateContent - 非流式
                EndpointDefinition(
                    prefix="/v1beta",
                    path="/models/{model}:generateContent",
                    methods=["POST"],
                    tags=["Gemini Dialect"],
                    summary="Generate Content",
                    description="Gemini 原生格式非流式生成",
                ),
                # POST /v1beta/models/{model}:streamGenerateContent - 流式
                EndpointDefinition(
                    prefix="/v1beta",
                    path="/models/{model}:streamGenerateContent",
                    methods=["POST"],
                    tags=["Gemini Dialect"],
                    summary="Stream Generate Content",
                    description="Gemini 原生格式流式生成",
                ),
            ],
        )
    )