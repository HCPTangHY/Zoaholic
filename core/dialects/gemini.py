"""
Gemini 方言

支持 Google Gemini 原生格式的入口/出口转换：
- parse_request: Gemini native -> Canonical(RequestModel)
- render_response: Canonical(OpenAI 风格) -> Gemini native
- render_stream: Canonical SSE -> Gemini SSE
- endpoints: 自动注册的端点定义
"""

import json
import asyncio
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from core.json_utils import json_loads, json_dumps_text
from core.models import RequestModel, Message, ContentItem, ImageUrl
from core.usage import extract_cache_usage

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
        reasoning_acc: List[str] = []
        tool_calls: List[Dict[str, Any]] = []
        msg_thought_signature: Optional[str] = None

        for part in parts:
            if not isinstance(part, dict):
                continue
            
            # 提取签名 (优先使用原生驼峰)
            part_signature = part.get("thoughtSignature") or part.get("thought_signature")
            if part_signature:
                msg_thought_signature = part_signature

            # 1. 处理思维链
            if part.get("thought") is True and "text" in part:
                reasoning_acc.append(str(part.get("text", "")))
                continue

            # 2. 处理函数调用 (Gemini native -> Canonical tool_calls)
            if "functionCall" in part:
                fc = part["functionCall"]
                tc_id = f"call_{len(tool_calls)}"
                tc = {
                    "id": tc_id,
                    "type": "function",
                    "function": {
                        "name": fc.get("name"),
                        "arguments": json_dumps_text(fc.get("args") or {}, ensure_ascii=False)
                    }
                }
                if part_signature:
                    tc["extra_content"] = {"google": {"thoughtSignature": part_signature}}
                tool_calls.append(tc)
                continue

            # 3. 处理函数返回 (Gemini native -> Canonical tool role)
            if "functionResponse" in part:
                fr = part["functionResponse"]
                messages.append(Message(
                    role="tool",
                    name=fr.get("name"),
                    content=json_dumps_text(fr.get("response") or {}, ensure_ascii=False),
                    tool_call_id=fr.get("name") # Gemini 通常用 name 匹配
                ))
                continue

            # 4. 处理普通文本和图片
            if "text" in part:
                text = str(part.get("text", ""))
                text_acc.append(text)
                content_items.append(ContentItem(type="text", text=text))
            elif "inlineData" in part and isinstance(part.get("inlineData"), dict):
                inline = part["inlineData"]
                mime_type = inline.get("mimeType", "image/png")
                data = inline.get("data", "")
                if mime_type.startswith("image/"):
                    # 图片 → OAI 标准 image_url 格式（data URI）
                    data_uri = f"data:{mime_type};base64,{data}"
                    content_items.append(
                        ContentItem(
                            type="image_url",
                            image_url=ImageUrl(url=data_uri),
                        )
                    )
                else:
                    # 非图片（音频/PDF等） → file 格式
                    content_items.append(
                        ContentItem(
                            type="file",
                            file={"mime_type": mime_type, "data": data},
                        )
                    )
            elif "fileData" in part and isinstance(part.get("fileData"), dict):
                file_data = part["fileData"]
                mime_type = file_data.get("mimeType", "application/octet-stream")
                file_uri = file_data.get("fileUri", "")
                content_items.append(
                    ContentItem(
                        type="file",
                        file={"mime_type": mime_type, "file_uri": file_uri},
                    )
                )

        if not content_items and not reasoning_acc and not tool_calls:
            continue

        # 封装消息
        msg_kwargs = {}
        if reasoning_acc:
            msg_kwargs["reasoning_content"] = "".join(reasoning_acc)
        if tool_calls:
            msg_kwargs["tool_calls"] = tool_calls
        if msg_thought_signature:
            msg_kwargs["thoughtSignature"] = msg_thought_signature

        if len(content_items) == 1 and content_items[0].type == "text":
            messages.append(Message(role=role, content="".join(text_acc), **msg_kwargs))
        else:
            messages.append(Message(role=role, content=content_items if content_items else None, **msg_kwargs))

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
    parts = []
    if choices:
        msg = choices[0].get("message") or {}
        
        # 1. 思维链 (Reasoning)
        reasoning = msg.get("reasoning_content")
        if reasoning:
            parts.append({"thought": True, "text": reasoning})

        # 2. 文本内容 (Content)
        content = msg.get("content") or ""
        if isinstance(content, list):
            # 结构化 content list → Gemini parts
            for item in content:
                if not isinstance(item, dict):
                    continue
                item_type = item.get("type", "")
                if item_type == "text":
                    text = item.get("text", "")
                    if text:
                        parts.append({"text": text})
                elif item_type == "image_url":
                    image_url = item.get("image_url")
                    url = ""
                    if isinstance(image_url, dict):
                        url = image_url.get("url", "")
                    elif isinstance(image_url, str):
                        url = image_url
                    # 解析 data URI → inlineData
                    if url.startswith("data:"):
                        try:
                            # data:image/png;base64,AAAA...
                            header, b64data = url.split(",", 1)
                            mime = header.split(":", 1)[1].split(";", 1)[0]
                            parts.append({"inlineData": {"mimeType": mime, "data": b64data}})
                        except (ValueError, IndexError):
                            # 解析失败，降级为 markdown 文本
                            parts.append({"text": f"![image]({url})"})
                    else:
                        # 普通 URL，Gemini 用 fileData
                        parts.append({"fileData": {"fileUri": url}})
        elif content:
            parts.append({"text": content})

        # 3. 工具调用 (Tool Calls)
        # 如果存在 tool_calls，Gemini 期望渲染为 functionCall parts
        tool_calls = msg.get("tool_calls") or []
        for i, tc in enumerate(tool_calls):
            fn = tc.get("function") or {}
            try:
                args = json_loads(fn.get("arguments") or "{}")
            except:
                args = {}
            
            part = {
                "functionCall": {
                    "name": fn.get("name"),
                    "args": args
                }
            }
            # 签名逻辑：第一个函数调用必须携带签名（如果是 Gemini 3 模型）
            # 我们优先从 tool_call 的 extra_content 中找，或者使用消息级别的签名
            sig = (tc.get("extra_content") or {}).get("google", {}).get("thoughtSignature")
            if not sig and i == 0:
                sig = msg.get("thoughtSignature")
            
            if sig:
                part["thoughtSignature"] = sig
            
            parts.append(part)

        # 4.兜底签名逻辑：如果没有工具调用，将签名附在最后一个文本块上
        if not tool_calls and msg.get("thoughtSignature") and parts:
            parts[-1]["thoughtSignature"] = msg.get("thoughtSignature")

    usage = canonical_response.get("usage") or {}
    # Gemini 出口需要把 OpenAI prompt_tokens_details.cached_tokens 还原为 cachedContentTokenCount。
    cache_usage = extract_cache_usage(usage)
    usage_metadata = {
        "promptTokenCount": usage.get("prompt_tokens", 0),
        "candidatesTokenCount": usage.get("completion_tokens", 0),
        "totalTokenCount": usage.get("total_tokens", 0),
    }
    if cache_usage["cached_tokens"] > 0:
        usage_metadata["cachedContentTokenCount"] = cache_usage["cached_tokens"]

    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": parts or [{"text": ""}]},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": usage_metadata,
    }


async def render_gemini_stream(canonical_sse_chunk: str) -> str:
    """
    Canonical SSE -> Gemini SSE

    输入: "data: {...}\n\n"
    输出: "data: {...}\n\n" (Gemini candidates 格式)

    支持 delta.content 为结构化 list（图片等）或普通 string。
    """
    if not isinstance(canonical_sse_chunk, str):
        return canonical_sse_chunk

    if not canonical_sse_chunk.startswith("data: "):
        return canonical_sse_chunk

    data_str = canonical_sse_chunk[6:].strip()
    if data_str == "[DONE]":
        return ""

    try:
        canonical = json_loads(data_str)
    except json.JSONDecodeError:
        return canonical_sse_chunk

    usage = canonical.get("usage")
    choices = canonical.get("choices") or []
    if not choices:
        if isinstance(usage, dict):
            # usage-only chunk 没有 choices；这里直接输出 Gemini usageMetadata，避免流式转换时丢失缓存命中字段。
            cache_usage = extract_cache_usage(usage)
            usage_metadata = {
                "promptTokenCount": usage.get("prompt_tokens", 0),
                "candidatesTokenCount": usage.get("completion_tokens", 0),
                "totalTokenCount": usage.get("total_tokens", 0),
            }
            if cache_usage["cached_tokens"] > 0:
                usage_metadata["cachedContentTokenCount"] = cache_usage["cached_tokens"]
            return f"data: {json_dumps_text({'usageMetadata': usage_metadata}, ensure_ascii=False)}\n\n"
        return ""

    delta = choices[0].get("delta") or {}
    content = delta.get("content")
    reasoning = delta.get("reasoning_content") or ""
    thought_signature = delta.get("thoughtSignature")

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

    parts = gemini_chunk["candidates"][0]["content"]["parts"]

    if reasoning:
        parts.append({"thought": True, "text": reasoning})

    if isinstance(content, list):
        # 结构化 content items（图片等）→ Gemini parts
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type", "")
            if item_type == "text":
                text = item.get("text", "")
                if text:
                    parts.append({"text": text})
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
                        mime = header.split(":", 1)[1].split(";", 1)[0]
                        parts.append({"inlineData": {"mimeType": mime, "data": b64data}})
                    except (ValueError, IndexError):
                        parts.append({"text": f"![image]({url})"})
                else:
                    parts.append({"fileData": {"fileUri": url}})
    elif content:
        # 普通 string content
        parts.append({"text": content})

    if thought_signature and parts:
        parts[-1]["thoughtSignature"] = thought_signature

    finish_reason = choices[0].get("finish_reason")
    if finish_reason:
        gemini_chunk["candidates"][0]["finishReason"] = "STOP"

    usage = canonical.get("usage")
    if isinstance(usage, dict):
        cache_usage = extract_cache_usage(usage)
        gemini_chunk["usageMetadata"] = {
            "promptTokenCount": usage.get("prompt_tokens", 0),
            "candidatesTokenCount": usage.get("completion_tokens", 0),
            "totalTokenCount": usage.get("total_tokens", 0),
        }
        if cache_usage["cached_tokens"] > 0:
            # 内容 chunk 和 usage chunk 都走同一个映射规则，目的在于保持流式 Gemini 输出格式一致。
            gemini_chunk["usageMetadata"]["cachedContentTokenCount"] = cache_usage["cached_tokens"]

    json_data = json_dumps_text(gemini_chunk, ensure_ascii=False)
    return f"data: {json_data}\n\n"


def parse_gemini_usage(data: Any) -> Optional[Dict[str, int]]:
    """从 Gemini 格式中提取 usage"""
    if not isinstance(data, dict):
        return None
    usage = data.get("usageMetadata")
    if usage:
        prompt = usage.get("promptTokenCount", 0)
        completion = usage.get("candidatesTokenCount", 0)
        total = usage.get("totalTokenCount", prompt + completion)
        # Gemini 的缓存命中字段在 usageMetadata.cachedContentTokenCount，需映射为统一 cached_tokens。
        cache_usage = extract_cache_usage(usage)
        if prompt or completion or cache_usage["cached_tokens"] or cache_usage["cache_creation_tokens"]:
            return {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": total,
                **cache_usage,
            }
    return None


# ============== 自定义端点处理函数 ==============


async def _get_gemini_models(api_index: int, app):
    """获取格式化后的 Gemini 模型列表"""
    from utils import post_all_models

    models = post_all_models(api_index, app.state.config, app.state.api_list, app.state.models_list)
    
    gemini_models = []
    for m in models:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        
        model_id = m["id"]
        # 确保 name 以 models/ 开头且不重复
        name = model_id if model_id.startswith("models/") else f"models/{model_id}"
        
        gemini_models.append({
            "name": name,
            "version": "v1beta",
            "displayName": model_id,
            "description": f"Zoaholic provided model: {model_id}",
            "supportedGenerationMethods": ["generateContent", "countTokens"],
            "inputTokenLimit": 30720,
            "outputTokenLimit": 2048,
        })
    return gemini_models


async def list_gemini_models_handler(
    request: "Request",
    api_index: int,
    **kwargs,
):
    """
    Gemini 模型列表端点 - GET /v1/models & /v1beta/models
    """
    from fastapi.responses import JSONResponse
    gemini_models = await _get_gemini_models(api_index, request.app)
    return JSONResponse(content={"models": gemini_models})


async def get_gemini_model_handler(
    request: "Request",
    api_index: int,
    **kwargs,
):
    """
    Gemini 获取单个模型详情 - GET /v1/models/{model} & /v1beta/models/{model}
    """
    from fastapi.responses import JSONResponse
    from fastapi import HTTPException
    
    model_id = request.path_params.get("model")
    if not model_id:
        raise HTTPException(status_code=400, detail="Model ID is required")
    
    # 支持带 models/ 前缀的查询
    target_name = model_id if model_id.startswith("models/") else f"models/{model_id}"
    
    models = await _get_gemini_models(api_index, request.app)
    for m in models:
        if m["name"] == target_name:
            return JSONResponse(content=m)
            
    raise HTTPException(status_code=404, detail=f"Model not found: {model_id}")


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
            structured_stream=True,  # Gemini 原生支持 inlineData，保留结构化 content
            extract_token=extract_gemini_token,
            endpoints=[
                # GET /v1beta/models - 列出模型
                EndpointDefinition(
                    prefix="/v1beta",
                    path="/models",
                    methods=["GET"],
                    handler=list_gemini_models_handler,
                    tags=["Gemini Dialect"],
                    summary="List Gemini Models",
                ),
                # GET /v1/models - 列出模型
                EndpointDefinition(
                    prefix="/v1",
                    path="/models",
                    methods=["GET"],
                    handler=list_gemini_models_handler,
                    tags=["Gemini Dialect"],
                    summary="List Gemini Models (v1)",
                ),
                # GET /v1beta/models/{model} - 获取模型详情
                EndpointDefinition(
                    prefix="/v1beta",
                    path="/models/{model}",
                    methods=["GET"],
                    handler=get_gemini_model_handler,
                    tags=["Gemini Dialect"],
                    summary="Get Gemini Model",
                ),
                # GET /v1/models/{model} - 获取模型详情
                EndpointDefinition(
                    prefix="/v1",
                    path="/models/{model}",
                    methods=["GET"],
                    handler=get_gemini_model_handler,
                    tags=["Gemini Dialect"],
                    summary="Get Gemini Model (v1)",
                ),
                # POST /v1beta/models/{model}:generateContent - 非流式
                EndpointDefinition(
                    prefix="/v1beta",
                    path="/models/{model}:generateContent",
                    methods=["POST"],
                    tags=["Gemini Dialect"],
                    summary="Generate Content",
                ),
                # POST /v1/models/{model}:generateContent - 非流式
                EndpointDefinition(
                    prefix="/v1",
                    path="/models/{model}:generateContent",
                    methods=["POST"],
                    tags=["Gemini Dialect"],
                    summary="Generate Content (v1)",
                ),
                # POST /v1beta/models/{model}:streamGenerateContent - 流式
                EndpointDefinition(
                    prefix="/v1beta",
                    path="/models/{model}:streamGenerateContent",
                    methods=["POST"],
                    tags=["Gemini Dialect"],
                    summary="Stream Generate Content",
                ),
                # POST /v1/models/{model}:streamGenerateContent - 流式
                EndpointDefinition(
                    prefix="/v1",
                    path="/models/{model}:streamGenerateContent",
                    methods=["POST"],
                    tags=["Gemini Dialect"],
                    summary="Stream Generate Content (v1)",
                ),
            ],
        )
    )