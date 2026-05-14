"""
AWS Bedrock 渠道适配器

负责处理 AWS Bedrock API 的请求构建和响应流解析
"""

import re
import json
import hmac
import base64
import hashlib
import asyncio
import datetime
from datetime import timezone
from datetime import datetime as dt

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
from .claude_channel import gpt2claude_tools_json


# ============================================================
# AWS Bedrock (Claude) 格式化函数
# ============================================================

def format_text_message(text: str) -> dict:
    """格式化文本消息为 AWS Bedrock Claude 格式"""
    return {"type": "text", "text": text}


async def format_image_message(image_url: str) -> dict:
    """格式化图片消息为 AWS Bedrock Claude 格式"""
    base64_image, image_type = await get_base64_image(image_url)
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": image_type,
            "data": extract_base64_data(base64_image),
        }
    }


def sign(key, msg):
    return hmac.new(key, msg.encode('utf-8'), hashlib.sha256).digest()


def get_signature_key(key, date_stamp, region_name, service_name):
    k_date = sign(('AWS4' + key).encode('utf-8'), date_stamp)
    k_region = sign(k_date, region_name)
    k_service = sign(k_region, service_name)
    k_signing = sign(k_service, 'aws4_request')
    return k_signing


def get_signature(request_body, model_id, aws_access_key, aws_secret_key, aws_region, host, content_type, accept_header, endpoint_suffix='invoke-with-response-stream'):
    import urllib.parse
    # 必须用 json_dumps_text 与实际发送保持一致（orjson 无空格，stdlib 有空格）
    request_body = json_dumps_text(request_body)
    SERVICE = "bedrock"
    canonical_querystring = ''
    method = 'POST'
    raw_path = f'/model/{model_id}/{endpoint_suffix}'
    canonical_uri = urllib.parse.quote(raw_path, safe='/-_.~')
    # Create a date for headers and the credential string
    t = datetime.datetime.now(timezone.utc)
    amz_date = t.strftime('%Y%m%dT%H%M%SZ')
    date_stamp = t.strftime('%Y%m%d') # Date YYYYMMDD

    # --- Task 1: Create a Canonical Request ---
    payload_hash = hashlib.sha256(request_body.encode('utf-8')).hexdigest()

    canonical_headers = f'accept:{accept_header}\n' \
                        f'content-type:{content_type}\n' \
                        f'host:{host}\n' \
                        f'x-amz-content-sha256:{payload_hash}\n' \
                        f'x-amz-date:{amz_date}\n'
    # 注意：头名称需要按字母顺序排序

    signed_headers = 'accept;content-type;host;x-amz-content-sha256;x-amz-date' # 按字母顺序排序

    canonical_request = f'{method}\n' \
                        f'{canonical_uri}\n' \
                        f'{canonical_querystring}\n' \
                        f'{canonical_headers}\n' \
                        f'{signed_headers}\n' \
                        f'{payload_hash}'

    # --- Task 2: Create the String to Sign ---
    algorithm = 'AWS4-HMAC-SHA256'
    credential_scope = f'{date_stamp}/{aws_region}/{SERVICE}/aws4_request'
    string_to_sign = f'{algorithm}\n' \
                    f'{amz_date}\n' \
                    f'{credential_scope}\n' \
                    f'{hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()}'

    # --- Task 3: Calculate the Signature ---
    signing_key = get_signature_key(aws_secret_key, date_stamp, aws_region, SERVICE)
    signature = hmac.new(signing_key, string_to_sign.encode('utf-8'), hashlib.sha256).hexdigest()

    # --- Task 4: Add Signing Information to the Request ---
    authorization_header = f'{algorithm} Credential={aws_access_key}/{credential_scope}, SignedHeaders={signed_headers}, Signature={signature}'
    return amz_date, payload_hash, authorization_header


def _parse_ak_sk(provider, api_key=None):
    """解析 AWS AK/SK，兼容 provider 字段和旧的 "AK:SK" api_key 格式。"""
    # 修改原因：普通转换路径、模型列表和 Claude 透传路径都需要相同的凭据解析逻辑。
    # 修改方式：把重复的 provider/aws_access_key 与 api_key 拆分逻辑集中到一个函数。
    # 目的：避免后续只修一处导致 AWS 签名来源不一致。
    aws_ak = provider.get("aws_access_key") or ""
    aws_sk = provider.get("aws_secret_key") or ""
    if not aws_ak and api_key and ":" in str(api_key):
        parts = str(api_key).split(":", 1)
        aws_ak = parts[0].strip()
        aws_sk = parts[1].strip()
    return aws_ak, aws_sk


def _extract_region(base_url, provider):
    """从 Bedrock Runtime URL 提取 region，提取失败时回退到 provider.aws_region。"""
    # 修改原因：直连和反代 base_url 都可能包含 bedrock-runtime.{region}.amazonaws.com。
    # 修改方式：复用同一个正则提取 region，并在失败时使用配置默认值。
    # 目的：让普通请求、透传请求和签名上下文始终使用同一 region 推断规则。
    _region_match = re.search(r'bedrock-runtime\.([a-z0-9-]+)\.amazonaws\.com', base_url or "")
    return _region_match.group(1) if _region_match else provider.get('aws_region', 'us-east-1')


def _build_aws_sigv4_headers(payload_dict, original_model, aws_ak, aws_sk, aws_region, host, endpoint_suffix='invoke-with-response-stream'):
    """构建 AWS SigV4 签名 headers。payload_dict 会用 json_dumps_text 序列化后计算 hash。"""
    # 修改原因：透传签名和普通转换签名都必须用与实际发送一致的 JSON 文本计算 body hash。
    # 修改方式：把 get_signature 的调用封装成公共 headers 构造函数，并显式传入 endpoint_suffix。
    # 目的：确保 invoke 与 invoke-with-response-stream 两种端点都能得到匹配的 SigV4 签名。
    content_type = "application/json"
    accept_header = "application/json"
    amz_date, payload_hash, authorization_header = get_signature(
        payload_dict,
        original_model,
        aws_ak,
        aws_sk,
        aws_region,
        host,
        content_type,
        accept_header,
        endpoint_suffix,
    )
    return {
        'Accept': accept_header,
        'Content-Type': content_type,
        'X-Amz-Date': amz_date,
        'X-Amz-Content-Sha256': payload_hash,
        'Authorization': authorization_header,
    }


async def get_aws_payload(request, engine, provider, api_key=None):
    """构建 AWS Bedrock API 的请求 payload"""
    CONTENT_TYPE = "application/json"
    model_dict = get_model_dict(provider)
    original_model = model_dict[request.model]
    base_url = provider.get('base_url')
    is_fixed_url = base_url.endswith('#')

    # 修改原因：Claude 透传路径和普通 OAI 转换路径必须用同一套 region/host 规则签名。
    # 修改方式：复用 _extract_region，并固定 SigV4 canonical host 为 AWS 原始域名。
    # 目的：支持反代 base_url 时仍按 Bedrock 真实 host 生成签名。
    AWS_REGION = _extract_region(base_url, provider)
    HOST = f"bedrock-runtime.{AWS_REGION}.amazonaws.com"

    if is_fixed_url:
        url = base_url[:-1].rstrip('/')
    else:
        url = f"{base_url}/model/{original_model}/invoke-with-response-stream"

    messages = []
    tool_id = None
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
        else:
            content = msg.content
            tool_calls = msg.tool_calls
            tool_id = tool_calls[0].id if tool_calls else None or tool_id
            tool_call_id = msg.tool_call_id

        if tool_calls:
            tool_calls_list = []
            # 修改原因：AWS Claude 历史中的工具调用必须完整保留。
            # 修改方式：直接遍历全部 tool_calls，不再截断。
            # 目的：避免 tool_use 与后续 tool_result 数量不一致。
            for tool_call in tool_calls:
                tool_calls_list.append({
                    "type": "tool_use",
                    "id": tool_call.id,
                    "name": tool_call.function.name,
                    "input": json_loads(tool_call.function.arguments),
                })
            messages.append({"role": msg.role, "content": tool_calls_list})
        elif tool_call_id:
            messages.append({"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": tool_id,
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
            messages.append({"role": msg.role, "content": content})

    conversation_len = len(messages) - 1
    message_index = 0
    while message_index < conversation_len:
        if messages[message_index]["role"] == messages[message_index + 1]["role"]:
            if messages[message_index].get("content"):
                if isinstance(messages[message_index]["content"], list):
                    messages[message_index]["content"].extend(messages[message_index + 1]["content"])
                elif isinstance(messages[message_index]["content"], str) and isinstance(messages[message_index + 1]["content"], list):
                    content_list = [{"type": "text", "text": messages[message_index]["content"]}]
                    content_list.extend(messages[message_index + 1]["content"])
                    messages[message_index]["content"] = content_list
                else:
                    messages[message_index]["content"] += messages[message_index + 1]["content"]
            messages.pop(message_index + 1)
            conversation_len = conversation_len - 1
        else:
            message_index = message_index + 1

    max_tokens = 4096

    payload = {
        "messages": messages,
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
    }

    if request.max_tokens:
        payload["max_tokens"] = int(request.max_tokens)

    miss_fields = [
        'model',
        'messages',
        'presence_penalty',
        'frequency_penalty',
        'n',
        'user',
        'include_usage',
        'stream_options',
        'stream',
    ]

    for field, value in request.model_dump(exclude_unset=True).items():
        if field not in miss_fields and value is not None:
            payload[field] = value

    # 修改原因：provider.tools=False 仍需要禁用工具声明。
    # 修改方式：仅在工具未禁用时转换 request.tools。
    # 目的：保留禁用工具能力，同时移除无意义的工具模式变量。
    if request.tools and not is_tools_disabled(provider):
        tools = []
        for tool in request.tools:
            json_tool = await gpt2claude_tools_json(tool.dict()["function"])
            tools.append(json_tool)
        payload["tools"] = tools
        if "tool_choice" in payload:
            if isinstance(payload["tool_choice"], dict):
                if payload["tool_choice"]["type"] == "function":
                    payload["tool_choice"] = {
                        "type": "tool",
                        "name": payload["tool_choice"]["function"]["name"]
                    }
            if isinstance(payload["tool_choice"], str):
                if payload["tool_choice"] == "auto":
                    payload["tool_choice"] = {
                        "type": "auto"
                    }
                if payload["tool_choice"] == "none":
                    payload["tool_choice"] = {
                        "type": "any"
                    }

    # 修改原因：禁用工具时，payload 中不能残留工具字段。
    # 修改方式：用统一的禁用判断清理 tools 和 tool_choice。
    # 目的：延续 provider.tools=False 的禁用语义。
    if is_tools_disabled(provider):
        payload.pop("tools", None)
        payload.pop("tool_choice", None)

    headers = {}
    # 修改原因：凭据解析需要与透传签名拦截器共享，避免两条路径行为不同。
    # 修改方式：使用 _parse_ak_sk 兼容 provider 字段和 "AK:SK" api_key。
    # 目的：保持现有配置方式在新增 Claude 透传后继续可用。
    aws_ak, aws_sk = _parse_ak_sk(provider, api_key)
    if aws_ak and aws_sk:
        headers = await asyncio.to_thread(
            _build_aws_sigv4_headers, payload, original_model, aws_ak, aws_sk, AWS_REGION, HOST
        )
        # 存储签名参数供非流式路径重新签名（塞 payload 临时字段，不污染 headers）
        payload['_aws_signing'] = {
            'ak': aws_ak, 'sk': aws_sk, 'region': AWS_REGION,
            'host': HOST, 'ct': CONTENT_TYPE, 'accept': headers.get('Accept', 'application/json'),
            'model': original_model,
        }

    return url, headers, payload

async def get_aws_passthrough_meta(request, engine, provider, api_key=None):
    """Claude 方言透传用：只构建 Bedrock URL 和基础 headers，SigV4 签名延迟到拦截器完成。"""
    # 修改原因：透传 payload 会在 handler 中由原始 Claude body 生成，meta 阶段还不知道最终 body。
    # 修改方式：这里只选择 invoke/invoke-with-response-stream URL，并把签名上下文暂存在 provider。
    # 目的：让后续请求拦截器拿到最终 payload 后再计算准确的 SigV4 body hash。
    model_dict = get_model_dict(provider)
    original_model = model_dict[request.model]
    base_url = provider.get('base_url') or "https://bedrock-runtime.us-east-1.amazonaws.com"
    aws_region = _extract_region(base_url, provider)
    host = f"bedrock-runtime.{aws_region}.amazonaws.com"

    is_fixed_url = base_url.endswith('#')
    if is_fixed_url:
        url = base_url[:-1].rstrip('/')
    else:
        endpoint_suffix = 'invoke-with-response-stream' if getattr(request, 'stream', False) else 'invoke'
        url = f"{base_url.rstrip('/')}/model/{original_model}/{endpoint_suffix}"

    aws_ak, aws_sk = _parse_ak_sk(provider, api_key)
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}

    if aws_ak and aws_sk:
        provider['_aws_passthrough_ctx'] = {
            'ak': aws_ak,
            'sk': aws_sk,
            'region': aws_region,
            'host': host,
            'model': original_model,
        }

    return url, headers, {}


async def _aws_passthrough_signing_interceptor(request, engine, provider, api_key, url, headers, payload):
    """Claude 透传路径的 SigV4 签名拦截器：在最终 payload 确定后完成签名。"""
    # 修改原因：AWS SigV4 签名必须包含最终 JSON body 的 SHA256，不能在 passthrough_meta 阶段提前计算。
    # 修改方式：从 provider 取出一次性透传上下文，根据实际 URL 判断 endpoint_suffix 后补齐签名头。
    # 目的：避免 body hash 不匹配，同时在发送前删除临时上下文，防止污染后续请求。
    if engine != "aws":
        return url, headers, payload

    ctx = provider.pop('_aws_passthrough_ctx', None)
    if not ctx:
        return url, headers, payload

    has_authorization = any(str(key).lower() == 'authorization' for key in headers.keys())
    if has_authorization:
        return url, headers, payload

    if '/invoke-with-response-stream' in url:
        endpoint_suffix = 'invoke-with-response-stream'
    elif '/invoke' in url:
        endpoint_suffix = 'invoke'
    else:
        endpoint_suffix = 'invoke-with-response-stream'

    sig_headers = await asyncio.to_thread(
        _build_aws_sigv4_headers,
        payload,
        ctx['model'],
        ctx['ak'],
        ctx['sk'],
        ctx['region'],
        ctx['host'],
        endpoint_suffix,
    )
    headers.update(sig_headers)

    return url, headers, payload


async def fetch_aws_passthrough_stream(client, url, headers, payload, model, timeout):
    """解析 Bedrock 二进制事件流，并把其中的 Claude 事件原样包装为标准 SSE data 行。"""
    # 修改原因：Bedrock invoke-with-response-stream 返回 AWS 事件流，不是 Anthropic 标准 SSE。
    # 修改方式：沿用现有 AWS 流式解析中的 base64 bytes 解码逻辑，但不转换为 OpenAI chunk。
    # 目的：Claude Code 等原生 Anthropic 客户端可以通过 AWS 渠道读取 Claude SSE 事件。
    from ..log_config import logger
    from ..response import _log_upstream_request
    import httpx

    _log_upstream_request(url, payload)

    stream_timeout = httpx.Timeout(
        connect=15.0,
        read=None,
        write=300.0,
        pool=10.0,
    )

    json_payload = await asyncio.to_thread(json_dumps_text, payload)
    async with client.stream('POST', url, headers=headers, content=json_payload, timeout=stream_timeout) as response:
        error_message = await check_response(response, "fetch_aws_passthrough_stream")
        if error_message:
            yield error_message
            return

        async for line in aiter_decoded_lines(response.aiter_bytes(), delimiter=b"\r"):
            if not line or line.strip() == "" or line.strip().startswith(':content-type') or line.strip().startswith(':event-type'):
                continue

            json_match = re.search(r'event{.*?}', line)
            if not json_match:
                continue

            try:
                chunk_data = json_loads(json_match.group(0).lstrip('event'))
            except json.JSONDecodeError:
                logger.error(f"AWS passthrough stream JSON parse failed: {json_match.group(0).lstrip('event')!r}")
                continue

            if "bytes" not in chunk_data:
                continue

            try:
                decoded_bytes = base64.b64decode(chunk_data["bytes"])
                decoded_text = decoded_bytes.decode("utf-8")
            except Exception as exc:
                logger.error(f"AWS passthrough stream base64 decode failed: {exc}")
                continue

            yield f"data: {decoded_text}" + end_of_line


async def fetch_aws_passthrough_response(client, url, headers, payload, model, timeout):
    """透传 Bedrock invoke 的非流式 Claude Messages JSON 响应。"""
    # 修改原因：非流式 Bedrock invoke 已返回 Claude Messages API JSON，不需要 OpenAI 格式转换。
    # 修改方式：使用与通用透传类似的 POST/JSON 发送逻辑，只保留 AWS channel 内部日志与错误检查。
    # 目的：让 Claude 方言非流式请求通过 AWS 渠道得到原生 Claude JSON 响应。
    from ..response import _log_upstream_request
    import httpx

    _log_upstream_request(url, payload)

    request_timeout = httpx.Timeout(
        connect=15.0,
        read=timeout,
        write=300.0,
        pool=10.0,
    )
    json_payload = await asyncio.to_thread(json_dumps_text, payload)
    response = await client.post(url, headers=headers, content=json_payload, timeout=request_timeout)
    error_message = await check_response(response, "fetch_aws_passthrough_response")
    if error_message:
        yield error_message
        return

    response_bytes = await response.aread()
    yield response_bytes.decode("utf-8")



async def fetch_aws_response(client, url, headers, payload, model, timeout):
    """处理 AWS Bedrock 非流式响应"""
    # 切换到非流式端点并重新签名
    url = url.replace("invoke-with-response-stream", "invoke")
    
    timestamp = int(dt.timestamp(dt.now()))
    
    # 从 payload 中取出签名上下文，重新计算非流式端点的签名
    signing_ctx = payload.pop('_aws_signing', None)
    if signing_ctx:
        # 修改原因：非流式请求会把 URL 从 stream 端点切到 invoke，原 stream 签名不能复用。
        # 修改方式：用公共 SigV4 headers 构造函数按 invoke 端点重新计算签名。
        # 目的：确保 canonical URI 与实际 Bedrock 非流式请求路径一致。
        sig_headers = await asyncio.to_thread(
            _build_aws_sigv4_headers,
            payload,
            signing_ctx['model'],
            signing_ctx['ak'],
            signing_ctx['sk'],
            signing_ctx['region'],
            signing_ctx['host'],
            'invoke',
        )
        headers.update(sig_headers)
    
    json_payload = await asyncio.to_thread(json_dumps_text, payload)
    
    response = await client.post(url, headers=headers, content=json_payload, timeout=timeout)
    error_message = await check_response(response, "fetch_aws_response")
    if error_message:
        yield error_message
        return

    response_bytes = await response.aread()
    response_json = await asyncio.to_thread(json_loads, response_bytes)
    mark_adapter_metrics_managed()
    
    # 解析 AWS Bedrock Claude 格式；Claude 的缓存字段需要计入统一 prompt_tokens 口径。
    content = safe_get(response_json, "content", 0, "text", default="")
    usage = safe_get(response_json, "usage", default={}) or {}
    cache_usage = extract_cache_usage(usage)
    prompt_tokens = (
        (usage.get("input_tokens") or 0)
        + cache_usage["cached_tokens"]
        + cache_usage["cache_creation_tokens"]
    )
    output_tokens = usage.get("output_tokens", 0)
    merge_usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=output_tokens,
        total_tokens=prompt_tokens + output_tokens,
        **cache_usage,
    )
    if content:
        mark_content_start()
    
    yield await generate_no_stream_response(
        timestamp, model, content=content, role="assistant",
        total_tokens=prompt_tokens + output_tokens,
        prompt_tokens=prompt_tokens, completion_tokens=output_tokens,
        # Bedrock 非流式返回 Claude usage 时，需要把缓存字段继续传给下游输出函数。
        cached_tokens=cache_usage["cached_tokens"],
        cache_creation_tokens=cache_usage["cache_creation_tokens"],
        return_dict=True
    )


async def fetch_aws_response_stream(client, url, headers, payload, model, timeout):
    """处理 AWS Bedrock 流式响应"""
    from ..log_config import logger
    
    # 流式路径不需要重签，但要清理临时字段避免发到上游
    payload.pop('_aws_signing', None)
    
    timestamp = int(dt.timestamp(dt.now()))
    json_payload = await asyncio.to_thread(json_dumps_text, payload)
    async with client.stream('POST', url, headers=headers, content=json_payload, timeout=timeout) as response:
        error_message = await check_response(response, "fetch_aws_response_stream")
        if error_message:
            yield error_message
            return

        mark_adapter_metrics_managed()
        # Bedrock 流式响应可能先返回 Claude usage，再在 invocationMetrics 中返回总量；缓存字段需要跨事件暂存。
        input_tokens = 0
        output_tokens = 0
        cached_tokens = 0
        cache_creation_tokens = 0
        async for line in aiter_decoded_lines(response.aiter_bytes(), delimiter=b"\r"):
                if not line or \
                line.strip() == "" or \
                line.strip().startswith(':content-type') or \
                line.strip().startswith(':event-type'):
                    continue

                json_match = re.search(r'event{.*?}', line)
                if not json_match:
                    continue
                try:
                    chunk_data = json_loads(json_match.group(0).lstrip('event'))
                except json.JSONDecodeError:
                    logger.error(f"DEBUG json.JSONDecodeError: {json_match.group(0).lstrip('event')!r}")
                    continue

                if "bytes" in chunk_data:
                    decoded_bytes = base64.b64decode(chunk_data["bytes"])
                    payload_chunk = json_loads(decoded_bytes)

                    claude_usage = safe_get(payload_chunk, "message", "usage", default={}) or safe_get(payload_chunk, "usage", default={}) or {}
                    if claude_usage:
                        # Claude 流式 usage 中的 input_tokens 不含缓存部分，这里先还原为统一 prompt_tokens。
                        _cache_usage = extract_cache_usage(claude_usage)
                        cached_tokens = _cache_usage["cached_tokens"] or cached_tokens
                        cache_creation_tokens = _cache_usage["cache_creation_tokens"] or cache_creation_tokens
                        if claude_usage.get("input_tokens") is not None:
                            input_tokens = (
                                (claude_usage.get("input_tokens") or 0)
                                + cached_tokens
                                + cache_creation_tokens
                            )
                        if claude_usage.get("output_tokens"):
                            output_tokens = claude_usage.get("output_tokens", 0)

                    text = safe_get(payload_chunk, "delta", "text", default="")
                    if text:
                        mark_content_start()
                        sse_string = await generate_sse_response(timestamp, model, text, None, None)
                        yield sse_string

                    usage = safe_get(payload_chunk, "amazon-bedrock-invocationMetrics", default="")
                    if usage:
                        raw_input_tokens = usage.get("inputTokenCount", 0)
                        output_tokens = usage.get("outputTokenCount", output_tokens)
                        input_tokens = input_tokens or raw_input_tokens
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
                            # Bedrock invocationMetrics 触发最终 usage chunk，需要带上先前 Claude usage 中的缓存字段。
                            cached_tokens=cached_tokens,
                            cache_creation_tokens=cache_creation_tokens,
                        )
                        yield sse_string

    yield "data: [DONE]" + end_of_line


def _sign_get_request(path, aws_access_key, aws_secret_key, aws_region, host, service="bedrock"):
    """SigV4 签名 GET 请求（无 body）"""
    import urllib.parse
    t = datetime.datetime.now(timezone.utc)
    amz_date = t.strftime('%Y%m%dT%H%M%SZ')
    date_stamp = t.strftime('%Y%m%d')

    canonical_uri = urllib.parse.quote(path, safe='/-_.~')
    payload_hash = hashlib.sha256(b'').hexdigest()  # GET 无 body

    canonical_headers = (
        f'host:{host}\n'
        f'x-amz-content-sha256:{payload_hash}\n'
        f'x-amz-date:{amz_date}\n'
    )
    signed_headers = 'host;x-amz-content-sha256;x-amz-date'

    canonical_request = (
        f'GET\n{canonical_uri}\n\n'
        f'{canonical_headers}\n'
        f'{signed_headers}\n'
        f'{payload_hash}'
    )

    algorithm = 'AWS4-HMAC-SHA256'
    credential_scope = f'{date_stamp}/{aws_region}/{service}/aws4_request'
    string_to_sign = (
        f'{algorithm}\n{amz_date}\n{credential_scope}\n'
        f'{hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()}'
    )

    signing_key = get_signature_key(aws_secret_key, date_stamp, aws_region, service)
    signature = hmac.new(signing_key, string_to_sign.encode('utf-8'), hashlib.sha256).hexdigest()
    authorization = f'{algorithm} Credential={aws_access_key}/{credential_scope}, SignedHeaders={signed_headers}, Signature={signature}'

    return {
        'X-Amz-Date': amz_date,
        'X-Amz-Content-Sha256': payload_hash,
        'Authorization': authorization,
    }


async def fetch_aws_models(client, provider):
    """获取 AWS Bedrock 可用模型列表（ListFoundationModels API）"""
    from ..log_config import logger

    base_url = provider.get('base_url', '')
    api_key = provider.get('api') or ''
    if isinstance(api_key, list):
        api_key = api_key[0] if api_key else ''

    # 修改原因：模型列表请求也应复用统一的 AWS 凭据解析逻辑。
    # 修改方式：调用 _parse_ak_sk 兼容 aws_access_key/aws_secret_key 与 "AK:SK"。
    # 目的：避免新增透传签名后出现多套凭据解析规则。
    aws_ak, aws_sk = _parse_ak_sk(provider, api_key)

    if not aws_ak or not aws_sk:
        raise ValueError('AWS credentials not configured (api key should be AK:SK format)')

    # 修改原因：ListFoundationModels 与 Runtime 调用同样需要从 base_url 推断 region。
    # 修改方式：复用 _extract_region，保留直连和反代 URL 的兼容性。
    # 目的：减少 AWS region 推断逻辑的重复实现。
    aws_region = _extract_region(base_url, provider)

    # ListFoundationModels 用 bedrock 而不是 bedrock-runtime
    host = f'bedrock.{aws_region}.amazonaws.com'
    path = '/foundation-models'

    headers = _sign_get_request(path, aws_ak, aws_sk, aws_region, host, service='bedrock')
    url = f'https://{host}{path}'

    logger.debug(f'[aws] ListFoundationModels: {url}')
    response = await client.get(url, headers=headers)
    response.raise_for_status()

    data = response.json()
    models = []
    for m in data.get('modelSummaries', []):
        model_id = m.get('modelId', '')
        if model_id:
            models.append(model_id)
    return sorted(models)


def register():
    """注册 AWS 渠道到注册中心"""
    from .registry import register_channel
    from core.plugins import register_request_interceptor

    # 修改原因：Claude 方言透传需要 detect_passthrough 通过渠道 type_name 命中 target_engine="claude"。
    # 修改方式：AWS 渠道仍保留 id="aws"，但类型声明改为 claude，并注册 Bedrock 专用透传处理器。
    # 目的：让 Claude Code 等原生 Anthropic 客户端可以经 aws 渠道调用 Bedrock。
    register_channel(
        id="aws",
        type_name="claude",
        default_base_url="https://bedrock-runtime.us-east-1.amazonaws.com",
        auth_header="AWS Signature V4",
        description="AWS Bedrock (Claude, Llama, etc.)",
        request_adapter=get_aws_payload,
        passthrough_adapter=get_aws_passthrough_meta,
        passthrough_stream_adapter=fetch_aws_passthrough_stream,
        passthrough_response_adapter=fetch_aws_passthrough_response,
        response_adapter=fetch_aws_response,
        stream_adapter=fetch_aws_response_stream,
        models_adapter=fetch_aws_models,
        source="builtin",
    )

    # 修改原因：透传签名必须在 handler 合成最终 payload 后执行。
    # 修改方式：注册一个内置全局请求拦截器；不设置 plugin_name，避免被 enabled_plugins 过滤跳过。
    # 目的：确保所有 AWS Claude 透传请求都会在发送前补齐正确的 SigV4 headers。
    register_request_interceptor(
        interceptor_id="aws_passthrough_signing",
        callback=_aws_passthrough_signing_interceptor,
        priority=5,
        overwrite=True,
        metadata={"description": "AWS Bedrock 透传 SigV4 签名"},
    )
