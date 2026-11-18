"""Regression suite for runtime request controls, stream keepalive, proxy/pool config, and multi-dialect tool compatibility."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import BackgroundTasks, HTTPException

from core.auth import authenticate_admin_api_key_token, list_admin_api_keys
from core.channels.gemini_channel import extract_tool_call_signature
from core.channels.openai_channel import (
    fetch_gpt_response_stream,
    flatten_reasoning_content,
    get_gpt_payload,
)
from core.channels.openai_responses_channel import get_responses_payload
from core.client_manager import ClientManager
from core.dialects.gemini import parse_gemini_request, render_gemini_stream
from core.dialects.openai_responses import convert_responses_input_to_messages
from core.handler import ModelRequestHandler
from core.ip_blacklist import extract_client_ip, parse_ip_blacklist
from core.models import ContentItem, FunctionCall, Message, RequestModel, ToolCall
from core.rate_limit import InMemoryRateLimiter
from core.stream_pipeline import prepare_stream
from core.utils import get_proxy, is_valid_model_name, scrub_sensitive_error_text


def test_extract_client_ip_respects_trusted_proxies(monkeypatch):
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
    headers = {
        "x-forwarded-for": "1.1.1.1, 203.0.113.9",
        "x-real-ip": "198.51.100.2",
    }
    # Untrusted peer cannot override client IP via forwarded headers
    assert extract_client_ip(headers, "8.8.8.8") == "8.8.8.8"
    # Trusted private/loopback proxy uses rightmost X-Forwarded-For hop
    assert extract_client_ip(headers, "172.28.0.1") == "203.0.113.9"
    # CF-Connecting-IP takes precedence when arriving from trusted proxy
    cf_headers = {**headers, "cf-connecting-ip": "198.51.100.77"}
    assert extract_client_ip(cf_headers, "127.0.0.1") == "198.51.100.77"


@pytest.mark.asyncio
async def test_rate_limiter_bounds_key_dictionary():
    limiter = InMemoryRateLimiter()
    limiter.MAX_KEYS = 8
    for idx in range(25):
        limited = await limiter.is_rate_limited(f"k-{idx}", [(2, 60)])
        assert limited is False
    assert len(limiter.requests) <= 8


def test_admin_key_requires_explicit_admin_role():
    app = SimpleNamespace(
        state=SimpleNamespace(
            api_list=["zk-single-user-key"],
            api_keys_db=[{"api": "zk-single-user-key", "role": "user"}],
            global_ip_blacklist=parse_ip_blacklist([]),
        )
    )
    assert list_admin_api_keys(app.state.api_keys_db) == []
    with pytest.raises(HTTPException) as exc_info:
        authenticate_admin_api_key_token(app, "zk-single-user-key", "127.0.0.1")
    assert exc_info.value.status_code == 403


def test_model_name_validation_and_error_text_scrubbing():
    assert is_valid_model_name("gemini-2.5-pro") is True
    assert is_valid_model_name("publishers/google/models/gemini-2.5-pro") is True
    assert is_valid_model_name("../v1beta/models") is False
    assert is_valid_model_name("gpt-4o?key=abc") is False
    assert is_valid_model_name("gpt-4o#frag") is False
    assert is_valid_model_name("models%2f..%2ftest", check_encoded=True) is False

    raw_err = (
        "ConnectError to https://generativelanguage.googleapis.com/v1beta?key=AIzaSyDummySecretKey1234567890 "
        "with Authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz1234 and custom my-custom-secret-token"
    )
    scrubbed = scrub_sensitive_error_text(raw_err, ["my-custom-secret-token"])
    assert "AIzaSyDummySecretKey" not in scrubbed
    assert "sk-abcdefghijklmnopqrstuvwxyz1234" not in scrubbed
    assert "my-custom-secret-token" not in scrubbed
    assert "key=***" in scrubbed


@pytest.mark.asyncio
async def test_pre_commit_keepalive_emits_before_first_upstream_chunk(monkeypatch):
    import core.stream_pipeline as sp

    monkeypatch.setattr(sp, "STREAM_PRE_COMMIT_KEEPALIVE_SECONDS", 0.05)

    async def slow_thinking_gen():
        await asyncio.sleep(0.16)
        yield 'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'

    info = {}
    stream_iter, _ = await prepare_stream(
        slow_thinking_gen(),
        current_info=info,
        keepalive_interval=0.04,
        stream_header_timeout=1.0,
        engine="openai",
    )
    chunks = [chunk async for chunk in stream_iter]
    joined = b"".join(c if isinstance(c, bytes) else c.encode() for c in chunks).decode()
    assert ": keepalive\n\n" in joined
    assert '"content":"ok"' in joined


@pytest.mark.asyncio
async def test_handler_aborts_retry_when_client_disconnected(monkeypatch):
    from core import handler as handler_mod

    attempts_made = 0

    async def fake_process_request(*args, **kwargs):
        nonlocal attempts_made
        attempts_made += 1
        raise HTTPException(status_code=503, detail="upstream unavailable")

    monkeypatch.setattr(handler_mod, "process_request", fake_process_request)

    app = SimpleNamespace(
        state=SimpleNamespace(
            config={"providers": [], "api_keys": [{"api": "zk-u", "model": ["all"]}]},
            provider_timeouts={"global": {"default": 30}},
            keepalive_interval={"global": {"default": 15}},
            stream_header_timeouts={"global": {"default": 30}},
            channel_manager=SimpleNamespace(cooldown_period=0, is_Model_Cooling=lambda *a, **k: False),
        )
    )
    h = ModelRequestHandler(app, lambda: {}, lambda *a, **k: None)

    async def is_disconnected():
        return True

    fake_raw_request = SimpleNamespace(is_disconnected=is_disconnected, _body=b"abc")
    providers = [
        {"provider": "p1", "engine": "openai", "api": "k1", "model": ["gpt-4o"]},
        {"provider": "p2", "engine": "openai", "api": "k2", "model": ["gpt-4o"]},
    ]
    req = RequestModel(model="gpt-4o", messages=[Message(role="user", content="hi")], stream=False)

    with pytest.raises(asyncio.CancelledError):
        await h.request_model(
            req,
            api_index=0,
            background_tasks=BackgroundTasks(),
            override_providers=providers,
            override_auto_retry=True,
            raw_request=fake_raw_request,
        )
    # First attempt failed -> before retry #1, is_disconnected() aborted p2
    assert attempts_made == 1
    assert fake_raw_request._body == b""


@pytest.mark.asyncio
async def test_socks5h_proxy_and_http2_host_allowlist(monkeypatch):
    captured = {}

    class FakeProxyTransport:
        @classmethod
        def from_url(cls, url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return "sentinel-transport"

    monkeypatch.setattr("core.utils.AsyncProxyTransport", FakeProxyTransport)
    limits = httpx.Limits(max_connections=50, max_keepalive_connections=20, keepalive_expiry=120.0)
    cfg = get_proxy("socks5h://127.0.0.1:1080", {"verify": True, "limits": limits, "http2": True})
    assert captured["url"] == "socks5://127.0.0.1:1080"
    assert captured["kwargs"]["rdns"] is True
    assert captured["kwargs"]["limits"] is limits
    assert captured["kwargs"]["http2"] is True
    assert cfg["transport"] == "sentinel-transport"

    monkeypatch.delenv("HTTP2_ENABLED", raising=False)
    cm = ClientManager()
    await cm.init({"follow_redirects": True, "verify": True})
    try:
        async with cm.get_client("https://api.openai.com/v1/chat/completions") as oai_client:
            assert oai_client._transport._pool._http2 is True
            assert oai_client.follow_redirects is True
        async with cm.get_client("https://custom-relay.example.org/v1/chat/completions") as relay_client:
            assert relay_client._transport._pool._http2 is False
            assert relay_client.follow_redirects is True
    finally:
        await cm.close()


@pytest.mark.asyncio
async def test_gemini_signature_roundtrip_and_stream_tool_calls():
    parsed = await parse_gemini_request(
        {
            "contents": [
                {"role": "user", "parts": [{"text": "check weather"}]},
                {
                    "role": "model",
                    "parts": [
                        {
                            "functionCall": {"name": "get_weather", "args": {"city": "Tokyo"}},
                            "thoughtSignature": "sig_xyz_123",
                        }
                    ],
                },
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": "get_weather",
                                "response": {"temp": 22, "media_ref_1": "drop_me"},
                            }
                        }
                    ],
                },
            ]
        },
        {"model": "publishers/google/models/gemini-2.5-pro", "action": "streamGenerateContent"},
        {},
    )
    assistant_msg = parsed.messages[1]
    tc = assistant_msg.tool_calls[0]
    assert "__thought__sig_xyz_123" in tc.id
    assert extract_tool_call_signature(tc) == "sig_xyz_123"
    tool_msg = parsed.messages[2]
    assert tool_msg.tool_call_id == tc.id
    assert "media_ref_1" not in tool_msg.content

    sse_chunk = (
        'data: {"choices":[{"delta":{"tool_calls":[{"id":"call_0__thought__sig_stream","type":"function",'
        '"function":{"name":"lookup","arguments":"{\\"q\\":\\"ok\\"}"}}]},"finish_reason":"tool_calls"}]}\n\n'
    )
    rendered = await render_gemini_stream(sse_chunk)
    payload = json.loads(rendered[len("data: "):].strip())
    fc_part = payload["candidates"][0]["content"]["parts"][0]
    assert fc_part["functionCall"] == {"name": "lookup", "args": {"q": "ok"}}
    assert fc_part["thoughtSignature"] == "sig_stream"


@pytest.mark.asyncio
async def test_openai_and_responses_tool_and_mixed_chunk_compatibility():
    # 1) Responses input conversion with reasoning + function_call + function_call_output
    converted = convert_responses_input_to_messages(
        [
            {"role": "user", "content": [{"type": "input_text", "text": "run calc"}]},
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "need calculator"}]},
            {"type": "function_call", "call_id": "call_9", "name": "calc", "arguments": {"expr": "2+2"}},
            {"type": "function_call_output", "call_id": "call_9", "output": {"answer": 4}},
        ]
    )
    assert len(converted) == 3
    assert converted[1]["role"] == "assistant"
    assert converted[1]["reasoning_content"] == "need calculator"
    assert converted[1]["tool_calls"][0]["id"] == "call_9"
    assert converted[2] == {"role": "tool", "tool_call_id": "call_9", "content": '{"answer":4}'}

    # 2) OpenAI & Responses outbound payload keep assistant content + tool_calls + structured tool output
    req = RequestModel(
        model="gpt-4o",
        stream=True,
        messages=[
            Message(
                role="assistant",
                content=[ContentItem(type="text", text="Let me check.")],
                tool_calls=[
                    ToolCall(
                        id="call_9",
                        type="function",
                        function=FunctionCall(name="calc", arguments='{"expr":"2+2"}'),
                    )
                ],
            ),
            Message(
                role="tool",
                tool_call_id="call_9",
                content=[ContentItem(type="text", text="4")],
            ),
        ],
    )
    prov_cfg = {"base_url": "https://api.openai.com/v1", "model": ["gpt-4o"]}
    _, _, oai_payload = await get_gpt_payload(req, "openai", prov_cfg, "sk-test")
    assert oai_payload["stream_options"] == {"include_usage": True}
    assert oai_payload["messages"][0]["content"] == [{"type": "text", "text": "Let me check."}]
    assert oai_payload["messages"][0]["tool_calls"][0]["id"] == "call_9"
    assert oai_payload["messages"][1] == {"role": "tool", "content": "4", "tool_call_id": "call_9"}

    _, _, resp_payload = await get_responses_payload(
        req, "openai-responses", prov_cfg, "sk-test"
    )
    item_types = [it["type"] for it in resp_payload["input"]]
    assert item_types == ["message", "function_call", "function_call_output"]

    # 3) Mixed single-chunk SSE splitting + list reasoning_content flattening + payload=None safety
    assert flatten_reasoning_content([{"text": "step1 "}, {"summary": "step2"}]) == "step1 step2"

    mixed_sse = (
        b'data: {"id":"1","choices":[{"index":0,"delta":{"role":"assistant","reasoning_content":[{"text":"think"}],'
        b'"content":"hello","tool_calls":[{"index":0,"id":"c1","type":"function","function":{"name":"fn","arguments":"{}"}}]},'
        b'"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}\n\n'
        b"data: [DONE]\n\n"
    )
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=mixed_sse))
    async with httpx.AsyncClient(transport=transport) as client:
        frames = [
            frame
            async for frame in fetch_gpt_response_stream(
                client,
                "https://api.openai.com/v1/chat/completions",
                {},
                {"model": "gpt-4o", "messages": [], "stream": True},
                "gpt-4o",
                30,
            )
        ]
    data_objs = [
        json.loads(f[len("data: "):].strip())
        for f in frames
        if isinstance(f, str) and f.startswith("data: {")
    ]
    # Split into reasoning frame, content frame, tool_calls frame, finish_reason frame, plus trailing usage frame
    assert data_objs[0]["choices"][0]["delta"]["reasoning_content"] == "think"
    assert data_objs[1]["choices"][0]["delta"]["content"] == "hello"
    assert data_objs[2]["choices"][0]["delta"]["tool_calls"][0]["id"] == "c1"
    assert data_objs[3]["choices"][0]["finish_reason"] == "tool_calls"


@pytest.mark.asyncio
async def test_persist_config_succeeds_with_attached_client_manager(monkeypatch, tmp_path):
    """_persist_config must cleanly publish runtime state when app.state.client_manager is attached."""
    import routes.admin as admin_mod

    yaml_file = tmp_path / "api.yaml"
    yaml_file.write_text("providers: []\napi_keys: []\npreferences: {}\n", encoding="utf-8")
    monkeypatch.setattr(admin_mod, "API_YAML_PATH", str(yaml_file))
    monkeypatch.setenv("CONFIG_STORAGE", "file")

    import core.config.file_store as file_store_mod
    monkeypatch.setattr(file_store_mod, "API_YAML_PATH", str(yaml_file))

    candidate = {
        "providers": [
            {
                "provider": "demo",
                "base_url": "https://api.openai.com/v1",
                "api": "sk-test",
                "model": ["gpt-4o"],
            }
        ],
        "api_keys": [{"api": "zk-admin", "role": "admin"}],
        "preferences": {"rate_limit": "999999/min"},
    }
    cm = ClientManager(pool_size=4)
    await cm.init({"headers": {"User-Agent": "curl/7.68.0"}, "http2": False, "verify": True})
    app = SimpleNamespace(
        state=SimpleNamespace(
            config={"providers": [], "api_keys": [], "preferences": {}},
            client_manager=cm,
        )
    )
    res = await admin_mod._persist_config(app, sections_to_verify=["providers", "preferences"], candidate_config=candidate)
    assert res["save_to_file"] is True
    assert app.state.config["providers"][0]["provider"] == "demo"
