"""AWS Bedrock Claude passthrough tests.

这些测试先固定本次改动的外部行为：AWS 渠道需要声明为 Claude 类型，
透传请求需要在最终 payload 确定后签名，并且 Bedrock 事件流需要转换为
Claude 客户端可读的标准 SSE 文本。
"""

import base64
import asyncio

from core.channels import get_channel
from core.channels.registry import register_channel, unregister_channel
from core.channels.aws_channel import (
    _aws_passthrough_signing_interceptor,
    fetch_aws_passthrough_stream,
    get_aws_passthrough_meta,
)


class _Request:
    def __init__(self, model="alias", stream=True):
        self.model = model
        self.stream = stream


class _StreamResponse:
    status_code = 200
    headers = {}

    def __init__(self, chunks):
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aread(self):
        return b""

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    async def aiter_text(self):
        # 修改原因：check_response 会包装 httpx.Response.aiter_text，测试替身也要提供该接口。
        # 修改方式：把字节块按 UTF-8 转成文本异步迭代。
        # 目的：让测试覆盖真实响应对象在成功状态下会经历的包装流程。
        for chunk in self._chunks:
            yield chunk.decode("utf-8")


class _Client:
    def __init__(self, chunks):
        self.chunks = chunks
        self.calls = []

    def stream(self, method, url, headers=None, content=None, timeout=None):
        self.calls.append({
            "method": method,
            "url": url,
            "headers": headers,
            "content": content,
            "timeout": timeout,
        })
        return _StreamResponse(self.chunks)


def test_registry_accepts_custom_passthrough_response_adapters():
    # 修改原因：handler 需要通过渠道注册表发现自定义透传响应处理器。
    # 修改方式：注册一个临时渠道并断言两个新字段会保留在 ChannelDefinition 上。
    # 目的：防止 register_channel 接口接受参数后丢弃自定义透传适配器。
    async def stream_adapter(*args, **kwargs):
        if False:
            yield None

    async def response_adapter(*args, **kwargs):
        if False:
            yield None

    register_channel(
        id="aws-passthrough-test",
        type_name="claude",
        passthrough_stream_adapter=stream_adapter,
        passthrough_response_adapter=response_adapter,
        overwrite=True,
    )
    try:
        channel = get_channel("aws-passthrough-test")
        assert channel.passthrough_stream_adapter is stream_adapter
        assert channel.passthrough_response_adapter is response_adapter
    finally:
        unregister_channel("aws-passthrough-test")


import pytest

@pytest.mark.parametrize(
    ("stream", "expected_suffix"),
    [(True, "/model/anthropic.claude-3-sonnet/invoke-with-response-stream"), (False, "/model/anthropic.claude-3-sonnet/invoke")],
)
def test_get_aws_passthrough_meta_builds_bedrock_url_and_context(stream, expected_suffix):
    # 修改原因：Claude 方言透传不能提前签名，因为签名必须包含最终 body hash。
    # 修改方式：meta 阶段只构建 URL、基础 headers 和 provider 临时签名上下文。
    # 目的：确保流式与非流式请求使用正确的 Bedrock InvokeModel 端点。
    provider = {
        "base_url": "https://bedrock-runtime.eu-west-3.amazonaws.com",
        "model": [{"anthropic.claude-3-sonnet": "alias"}],
    }

    url, headers, payload = asyncio.run(get_aws_passthrough_meta(_Request(stream=stream), "aws", provider, "AKID:SECRET"))

    assert url == "https://bedrock-runtime.eu-west-3.amazonaws.com" + expected_suffix
    assert headers == {"Content-Type": "application/json", "Accept": "application/json"}
    assert payload == {}
    assert provider["_aws_passthrough_ctx"] == {
        "ak": "AKID",
        "sk": "SECRET",
        "region": "eu-west-3",
        "host": "bedrock-runtime.eu-west-3.amazonaws.com",
        "model": "anthropic.claude-3-sonnet",
    }


def test_aws_passthrough_signing_interceptor_signs_final_payload_and_removes_context():
    # 修改原因：SigV4 的 payload hash 必须按最终透传 body 计算。
    # 修改方式：请求拦截器在 payload 确定后补齐 AWS 签名头。
    # 目的：避免把临时签名上下文或错误 body hash 发送到 Bedrock。
    provider = {
        "_aws_passthrough_ctx": {
            "ak": "AKID",
            "sk": "SECRET",
            "region": "us-east-1",
            "host": "bedrock-runtime.us-east-1.amazonaws.com",
            "model": "anthropic.claude-3-sonnet",
        }
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    payload = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 8}

    url, signed_headers, signed_payload = asyncio.run(_aws_passthrough_signing_interceptor(
        _Request(), "aws", provider, "AKID:SECRET",
        "https://bedrock-runtime.us-east-1.amazonaws.com/model/anthropic.claude-3-sonnet/invoke",
        headers,
        payload,
    ))

    assert signed_payload is payload
    assert "_aws_passthrough_ctx" not in provider
    assert "Authorization" in signed_headers
    assert "X-Amz-Date" in signed_headers
    assert "X-Amz-Content-Sha256" in signed_headers
    assert "AKID" in signed_headers["Authorization"]
    assert "/invoke" not in url or url.endswith("/invoke")


def test_fetch_aws_passthrough_stream_decodes_bedrock_event_to_claude_sse():
    # 修改原因：Bedrock 流式响应不是普通 SSE，而是带 base64 bytes 字段的事件流。
    # 修改方式：AWS 透传流处理器解出 bytes 后重新包装为 Claude SSE data 行。
    # 目的：让 Claude Code 这类原生 Anthropic 客户端能读取 AWS 流式响应。
    encoded = base64.b64encode(b'{"type":"content_block_delta","delta":{"type":"text_delta","text":"hi"}}').decode()
    chunks = [f':event-type\r\nevent{{"bytes":"{encoded}"}}\r'.encode()]
    client = _Client(chunks)

    async def collect():
        output = []
        async for chunk in fetch_aws_passthrough_stream(
            client,
            "https://bedrock-runtime.us-east-1.amazonaws.com/model/anthropic.claude-3-sonnet/invoke-with-response-stream",
            {"Content-Type": "application/json"},
            {"messages": [], "stream": True},
            "alias",
            60,
        ):
            output.append(chunk)
        return output

    output = asyncio.run(collect())

    assert output == ['data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hi"}}\n\n']
    assert client.calls[0]["content"] == '{"messages":[],"stream":true}'


def test_registered_aws_channel_declares_claude_passthrough_adapters():
    # 修改原因：detect_passthrough 会用渠道 type_name 匹配 Claude 方言的目标引擎。
    # 修改方式：AWS 渠道注册为 claude 类型，并暴露专用透传响应处理器。
    # 目的：确保 /v1/messages 入口可以路由到 aws 引擎的 Claude 透传路径。
    channel = get_channel("aws")

    assert channel.type_name == "claude"
    assert channel.passthrough_adapter is get_aws_passthrough_meta
    assert channel.passthrough_stream_adapter is fetch_aws_passthrough_stream
    assert channel.passthrough_response_adapter is not None
