"""
响应构造和图片处理模块。

修改原因：core.utils.py 文件过大，OpenAI 响应构造和图片编码处理属于独立职责。
修改方式：将流式响应、非流式响应、图片编码和 JSON 容错解析函数移入本文件。
目的：保持函数逻辑不变，并由 core.utils 重新导出旧公开名字。
"""

import ast
import asyncio
import base64
import io
import json
import random
import string

from PIL import Image
from fastapi import HTTPException

from .file_utils import extract_base64_data, fetch_url_content, split_data_uri_prefix_and_data
from .json_utils import json_dumps_text
from .log_config import logger
from .usage import build_openai_usage


async def generate_chunked_image_md(
    image_data: str,
    timestamp: int,
    model: str,
    thought_signature: str = None,
    chunk_size: int = 16384,
    mime_type: str = "image/png",
):
    """
    将较大的图片 data URI 或 base64 转为 Markdown，并分块流式输出 SSE。

    注意：
    - 不先构造完整 markdown 大字符串，避免高并发时额外内存分配和事件循环阻塞。
    - image_data 可以是完整 data URI，也可以是纯 base64 字符串。
    """
    data_uri_prefix, raw_image_data = split_data_uri_prefix_and_data(image_data, mime_type)

    prefix = "\n\n![image](" + data_uri_prefix
    suffix = ")"

    first_chunk_capacity = max(1, chunk_size - len(prefix))
    first_chunk = prefix + raw_image_data[:first_chunk_capacity]
    sse_string = await generate_sse_response(
        timestamp,
        model,
        content=first_chunk,
        thought_signature=thought_signature,
    )
    yield sse_string

    sent = first_chunk_capacity
    if sent < len(raw_image_data):
        await asyncio.sleep(0)

    while sent < len(raw_image_data):
        chunk_content = raw_image_data[sent:sent + chunk_size]
        sse_string = await generate_sse_response(
            timestamp,
            model,
            content=chunk_content,
        )
        yield sse_string
        sent += len(chunk_content)
        if sent < len(raw_image_data):
            await asyncio.sleep(0)

    sse_string = await generate_sse_response(
        timestamp,
        model,
        content=suffix,
    )
    yield sse_string

# end_of_line = "\n\r\n"
# end_of_line = "\r\n"
# end_of_line = "\n\r"
end_of_line = "\n\n"
# end_of_line = "\r"
# end_of_line = "\n"

async def generate_sse_response(
    timestamp,
    model,
    content=None,
    tools_id=None,
    function_call_name=None,
    function_call_content=None,
    role=None,
    total_tokens=0,
    prompt_tokens=0,
    completion_tokens=0,
    reasoning_content=None,
    stop=None,
    thought_signature=None,
    cached_tokens=0,
    cache_creation_tokens=0,
    tool_call_index=0,
    refusal=None
):
    """
    生成 OpenAI Chat Completions 格式的 SSE 响应
    
    Args:
        timestamp: 时间戳
        model: 模型名称
        content: 文本内容
        tools_id: 工具调用 ID
        function_call_name: 函数名称
        function_call_content: 函数参数内容
        role: 角色（首个 chunk 发送）
        total_tokens: 总 token 数（用于 usage chunk）
        prompt_tokens: 输入 token 数
        completion_tokens: 输出 token 数
        reasoning_content: 推理内容
        stop: 停止原因（如 "stop", "tool_calls"）
        thought_signature: Gemini 思考签名
        cached_tokens: 上游缓存命中 token 数，用于输出 prompt_tokens_details.cached_tokens
        cache_creation_tokens: 上游缓存创建 token 数，作为内核补充字段供 Claude 方言还原
        tool_call_index: 当前工具调用在并行 tool_calls 数组中的索引
    """
    random.seed(timestamp)
    random_str = ''.join(random.choices(string.ascii_letters + string.digits, k=29))

    # 构建 delta 内容（按优先级处理，互斥情况）
    delta_content = {}
    finish_reason = None
    
    # 优先级 1：显式的停止信号
    if stop:
        delta_content = {}
        finish_reason = stop
    # 优先级 2：usage chunk（无 choices）
    elif total_tokens:
        # usage chunk 会清空 choices，不需要设置 delta
        pass
    # 优先级 3：角色声明（首个 chunk）
    elif role and not content and not function_call_content and not function_call_name:
        delta_content = {"role": role, "content": ""}
    # 优先级 4：工具调用开始（有 tools_id 和 function_call_name）
    elif tools_id and function_call_name:
        # 修改原因：非 OAI 渠道的并行工具调用会共享本出口，硬编码 index=0 会把多个工具的参数合并到一起。
        # 修改方式：由各渠道传入当前工具调用的 tool_call_index，单工具场景继续使用默认值 0。
        # 目的：让下游按 OpenAI Chat Completions 规范用 index 区分并行工具调用。
        tc = {
            "index": tool_call_index,
            "id": tools_id,
            "type": "function",
            "function": {"name": function_call_name, "arguments": ""}
        }
        if thought_signature:
            tc["extra_content"] = {"google": {"thoughtSignature": thought_signature}}
        delta_content = {"tool_calls": [tc]}
    # 优先级 5：工具调用参数流式输出
    elif function_call_content is not None:
        # 确保 arguments 是字符串（OpenAI 格式要求）
        if isinstance(function_call_content, dict):
            args_str = json.dumps(function_call_content, ensure_ascii=False)
        else:
            args_str = str(function_call_content) if function_call_content else ""
        # 修改原因：工具参数片段必须沿用对应工具头的 index，否则并行调用会在流式累积时串到同一项。
        # 修改方式：参数 chunk 使用调用方传入的 tool_call_index，而不是固定写 0。
        # 目的：保证 stream_convert 和客户端都能把参数累积到正确的 tool call。
        delta_content = {"tool_calls": [{"index": tool_call_index, "function": {"arguments": args_str}}]}
    # 优先级 6：拒绝回答 (Refusal)
    elif refusal:
        delta_content = {"role": "assistant", "refusal": refusal}
    # 优先级 7：推理内容
    elif reasoning_content:
        delta_content = {"role": "assistant", "content": "", "reasoning_content": reasoning_content}
        if thought_signature:
            delta_content["thought_signature"] = thought_signature
    # 优先级 8：普通文本内容（支持 string 或结构化 list）
    elif content is not None and content != "":
        delta_content = {"role": "assistant", "content": content}
        if thought_signature:
            delta_content["thought_signature"] = thought_signature
    # 优先级 9：空 chunk（无内容）→ 结束信号
    else:
        delta_content = {}
        finish_reason = "stop"

    sample_data = {
        "id": f"chatcmpl-{random_str}",
        "object": "chat.completion.chunk",
        "created": timestamp,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta_content,
                "logprobs": None,
                "finish_reason": finish_reason
            }
        ],
        "usage": None,
        "system_fingerprint": "fp_d576307f90",
    }
    
    # usage chunk 特殊处理：清空 choices，设置 usage
    if total_tokens:
        # usage chunk 是内核到下游的统一出口；这里集中补充缓存字段，避免各通道重复拼装且出现遗漏。
        sample_data["usage"] = build_openai_usage(
            prompt_tokens,
            completion_tokens,
            total_tokens,
            cached_tokens=cached_tokens,
            cache_creation_tokens=cache_creation_tokens,
        )
        sample_data["choices"] = []

    json_data = json_dumps_text(sample_data, ensure_ascii=False)
    sse_response = f"data: {json_data}" + end_of_line

    return sse_response

async def generate_no_stream_response(timestamp, model, content=None, role=None, total_tokens=0, prompt_tokens=0, completion_tokens=0, reasoning_content=None, image_base64=None, thought_signature=None, cached_tokens=0, cache_creation_tokens=0, return_dict: bool = True, tool_calls_list: list[dict] | None = None):

    random.seed(timestamp)
    random_str = ''.join(random.choices(string.ascii_letters + string.digits, k=29))
    message = {
        "role": role,
        "content": content,
        "refusal": None
    }
    if reasoning_content:
        message["reasoning_content"] = reasoning_content
    
    if thought_signature:
        message["thought_signature"] = thought_signature

    sample_data = {
        "id": f"chatcmpl-{random_str}",
        "object": "chat.completion",
        "created": timestamp,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "logprobs": None,
                "finish_reason": "stop"
            }
        ],
        "usage": None,
        "system_fingerprint": "fp_a7d06e42a7"
    }

    if tool_calls_list:
        # 修改原因：Claude、Gemini、Responses 等非流式渠道可能一次返回多个工具调用，旧出口只能表达单个调用。
        # 修改方式：接收调用方已收集好的 tool_calls_list，并为缺失的 index/type/arguments 做最小规范化。
        # 目的：保持单工具旧参数兼容，同时让并行工具调用在非流式响应中保留独立 index。
        normalized_tool_calls = []
        for idx, tool_call in enumerate(tool_calls_list):
            normalized_tool_call = dict(tool_call)
            normalized_tool_call.setdefault("index", idx)
            normalized_tool_call.setdefault("type", "function")
            function_payload = dict(normalized_tool_call.get("function") or {})
            if "arguments" in function_payload and not isinstance(function_payload["arguments"], str):
                function_payload["arguments"] = json_dumps_text(function_payload["arguments"], ensure_ascii=False)
            function_payload.setdefault("arguments", "")
            normalized_tool_call["function"] = function_payload
            normalized_tool_calls.append(normalized_tool_call)

        sample_data = {
            "id": f"chatcmpl-{random_str}",
            "object": "chat.completion",
            "created": timestamp,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": role or "assistant",
                        "content": None,
                        "tool_calls": normalized_tool_calls,
                        "refusal": None
                    },
                    "logprobs": None,
                    "finish_reason": "tool_calls"
                }
            ],
            "usage": None,
            "service_tier": "default",
            "system_fingerprint": "fp_4691090a87"
        }

    if image_base64:
        sample_data = {
            "created": timestamp,
            "data": [{
                "b64_json": image_base64
            }],
        }
        
        # Images responses don't have usage, so we just clear it
        total_tokens = None

    if total_tokens:
        # 非流式响应与 SSE usage chunk 使用同一构造函数，目的在于让缓存字段在两种输出模式中保持一致。
        sample_data["usage"] = build_openai_usage(
            prompt_tokens,
            completion_tokens,
            total_tokens,
            cached_tokens=cached_tokens,
            cache_creation_tokens=cache_creation_tokens,
        )

    if return_dict:
        return sample_data

    json_data = json_dumps_text(sample_data, ensure_ascii=False)
    # print("json_data", json.dumps(sample_data, indent=4, ensure_ascii=False))

    return json_data

def get_image_format(file_content: bytes):
    try:
        with Image.open(io.BytesIO(file_content)) as img:
            img_format = (img.format or "").lower()
        return img_format or None
    except Exception:
        return None

def encode_image(file_content: bytes):
    img_format = get_image_format(file_content)
    if not img_format:
        raise ValueError("无法识别的图片格式")
    base64_encoded = base64.b64encode(file_content).decode('utf-8')

    if img_format == 'png':
        return f"data:image/png;base64,{base64_encoded}"
    elif img_format in ['jpg', 'jpeg']:
        return f"data:image/jpeg;base64,{base64_encoded}"
    else:
        raise ValueError(f"不支持的图片格式: {img_format}")

async def get_image_from_url(url):
    try:
        content, _ = await fetch_url_content(url)
        return content
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取 URL 时发生错误: {url}: {e}")
        raise HTTPException(status_code=400, detail=f"无法从 URL 获取内容: {url}")

async def get_encode_image(image_url):
    file_content = await get_image_from_url(image_url)
    base64_image = await asyncio.to_thread(encode_image, file_content)
    return base64_image


def _convert_webp_base64_to_png(base64_image: str) -> tuple[str, str]:
    image_data = base64.b64decode(extract_base64_data(base64_image))
    with Image.open(io.BytesIO(image_data)) as image:
        png_buffer = io.BytesIO()
        image.save(png_buffer, format="PNG")
    png_base64 = base64.b64encode(png_buffer.getvalue()).decode('utf-8')
    return f"data:image/png;base64,{png_base64}", "image/png"


def _prepare_image_for_upload(base64_image: str, max_size_mb: float) -> dict:
    base64_data = extract_base64_data(base64_image)
    image_size_bytes = len(base64_data) * 3 // 4
    image_size_mb = image_size_bytes / (1024 * 1024)
    result = {
        "base64_data": base64_data,
        "original_size_mb": image_size_mb,
        "compressed": False,
        "compressed_size_mb": image_size_mb,
        "size": None,
    }

    if image_size_mb <= max_size_mb:
        return result

    image_bytes = base64.b64decode(base64_data)
    with Image.open(io.BytesIO(image_bytes)) as img:
        scale = (max_size_mb / image_size_mb) ** 0.5
        new_width = max(1, int(img.width * scale))
        new_height = max(1, int(img.height * scale))
        resized = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        if resized.mode in ('RGBA', 'LA', 'P'):
            resized = resized.convert('RGB')
        resized.save(output, format='JPEG', quality=85, optimize=True)
    compressed_base64 = base64.b64encode(output.getvalue()).decode('utf-8')
    compressed_size_mb = len(compressed_base64) * 3 // 4 / (1024 * 1024)
    result.update({"base64_data": compressed_base64, "compressed": True, "compressed_size_mb": compressed_size_mb, "size": (new_width, new_height)})
    return result

# from PIL import Image
# import io
# def validate_image(image_data, image_type):
#     try:
#         decoded_image = base64.b64decode(image_data)
#         image = Image.open(io.BytesIO(decoded_image))

#         # 检查图片格式是否与声明的类型匹配
#         # print("image.format", image.format)
#         if image_type == "image/png" and image.format != "PNG":
#             raise ValueError("Image is not a valid PNG")
#         elif image_type == "image/jpeg" and image.format not in ["JPEG", "JPG"]:
#             raise ValueError("Image is not a valid JPEG")

#         # 如果没有异常,则图片有效
#         return True
#     except Exception as e:
#         print(f"Image validation failed: {str(e)}")
#         return False

async def get_base64_image(image_url: str) -> tuple[str, str]:
    """
    获取 base64 编码的图片数据和 MIME 类型
    
    Args:
        image_url: 图片 URL 或已编码的 base64 字符串
        
    Returns:
        tuple: (base64_image_with_prefix, mime_type)
               例如: ("data:image/png;base64,xxx", "image/png")
    """
    from .file_utils import get_base64_file
    base64_image, image_type = await get_base64_file(image_url)

    if not image_type.startswith("image/"):
        raise ValueError(f"Expected an image MIME type, but got: {image_type}")

    # 将 webp 转换为 png（某些 API 不支持 webp）
    if image_type == "image/webp":
        base64_image, image_type = await asyncio.to_thread(_convert_webp_base64_to_png, base64_image)

    return base64_image, image_type

def parse_json_safely(json_str):
    """
    尝试解析JSON字符串，先使用ast.literal_eval，失败则使用json.loads

    Args:
        json_str: 要解析的JSON字符串

    Returns:
        解析后的Python对象

    Raises:
        Exception: 当两种方法都失败时抛出异常
    """
    try:
        # 首先尝试使用ast.literal_eval解析
        return ast.literal_eval(json_str)
    except (SyntaxError, ValueError):
        try:
            # 如果失败，尝试使用json.loads解析
            return json.loads(json_str, strict=False)
        except json.JSONDecodeError as e:
            # 两种方法都失败，抛出异常
            raise Exception(f"无法解析JSON字符串: {e}, {json_str}")

