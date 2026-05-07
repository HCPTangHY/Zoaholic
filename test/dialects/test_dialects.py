import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
from fastapi import BackgroundTasks, FastAPI
from fastapi.testclient import TestClient

from core.dialects.gemini import parse_gemini_request, render_gemini_response
from core.dialects.claude import parse_claude_request
from core.dialects.passthrough import detect_passthrough, apply_passthrough_modifications
from core.models import RequestModel, Message


def run(coro):
    return asyncio.run(coro)


def _compact_test_app():
    from core.dialects import dialect_router

    app = FastAPI()
    app.state.global_rate_limit = [(9999, 60)]
    app.state.api_list = ["test-key"]
    app.state.api_keys_db = [{"role": "user"}]
    app.include_router(dialect_router)
    return app


async def _read_response_json(response):
    chunks = []
    async for chunk in response.body_iterator:
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        chunks.append(chunk)
    return json.loads(b"".join(chunks).decode("utf-8"))


def _handler_app_state(client_manager):
    return SimpleNamespace(
        config={"preferences": {"max_retry_count": 1}},
        provider_timeouts={"global": {}, "provider1": {}, "provider2": {}},
        keepalive_interval={"global": {}, "provider1": {}, "provider2": {}},
        client_manager=client_manager,
        channel_manager=SimpleNamespace(cooldown_period=0),
        error_triggers={},
    )


class RaisingClientManager:
    @asynccontextmanager
    async def get_client(self, base_url, proxy=None):
        raise AssertionError(f"unexpected upstream request to {base_url}")
        yield


class MockClientManager:
    def __init__(self, handler):
        self.handler = handler

    @asynccontextmanager
    async def get_client(self, base_url, proxy=None):
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.handler)) as client:
            yield client


async def _noop_update_stats(*args, **kwargs):
    return None


def test_gemini_parse_simple_text():
    native = {
        "contents": [
            {"role": "user", "parts": [{"text": "Hello"}]}
        ]
    }
    canonical = run(parse_gemini_request(native, {"model": "gemini-pro", "action": "generateContent"}, {}))
    assert canonical.model == "gemini-pro"
    assert canonical.messages[0].role == "user"
    assert canonical.messages[0].content == "Hello"
    assert canonical.stream is False or canonical.stream is None


def test_gemini_parse_system_instruction():
    native = {
        "systemInstruction": {"parts": [{"text": "SYS"}]},
        "contents": [{"role": "user", "parts": [{"text": "Hi"}]}],
        "generationConfig": {"temperature": 0.7},
    }
    canonical = run(parse_gemini_request(native, {"model": "gemini-pro", "action": "generateContent"}, {}))
    assert canonical.messages[0].role == "system"
    assert canonical.messages[0].content == "SYS"
    assert canonical.messages[1].role == "user"
    assert canonical.temperature == 0.7


def test_gemini_render_response():
    canonical_resp = {
        "choices": [{"message": {"role": "assistant", "content": "OK"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    }
    gemini_resp = run(render_gemini_response(canonical_resp, "gemini-pro"))
    assert gemini_resp["candidates"][0]["content"]["parts"][0]["text"] == "OK"
    assert gemini_resp["usageMetadata"]["promptTokenCount"] == 1
    assert gemini_resp["usageMetadata"]["candidatesTokenCount"] == 2
    assert gemini_resp["usageMetadata"]["totalTokenCount"] == 3


def test_claude_parse_basic_blocks_and_system():
    native = {
        "model": "claude-3-5-sonnet",
        "system": "SYS",
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Hello"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "Hi"}]},
        ],
        "max_tokens": 123,
    }
    canonical = run(parse_claude_request(native, {}, {}))
    assert canonical.model == "claude-3-5-sonnet"
    assert canonical.messages[0].role == "system"
    assert canonical.messages[0].content == "SYS"
    assert canonical.messages[1].role == "user"
    assert canonical.messages[1].content == "Hello"
    assert canonical.messages[2].role == "assistant"
    assert canonical.messages[2].content == "Hi"
    assert canonical.max_tokens == 123


def test_detect_passthrough_registry_and_fallback():
    assert detect_passthrough("gemini", "gemini") is True
    assert detect_passthrough("gemini", "openai") is False
    # 未注册 dialect 的回退规则：dialect_id == engine
    assert detect_passthrough("foo", "foo") is True
    assert detect_passthrough("foo", "bar") is False


def test_apply_passthrough_system_prompt_openai():
    """测试透传模式下 OpenAI 渠道的 system_prompt 注入。

    system_prompt 注入由渠道级 passthrough_payload_adapter 负责，
    不是 apply_passthrough_modifications 的职责。
    """
    import asyncio
    from core.channels.openai_channel import patch_passthrough_openai_payload

    payload = {"model": "gpt-4o", "messages": [{"role": "user", "content": "Hi"}]}
    mods = {"system_prompt": "SYS", "model_rename": None, "overrides": None}
    new_payload = asyncio.run(patch_passthrough_openai_payload(payload, mods, None, "openai", {}, None))
    assert new_payload["messages"][0]["role"] == "system"
    assert "SYS" in new_payload["messages"][0]["content"]


def test_apply_passthrough_overrides_deep_merge():
    payload = {"generationConfig": {"temperature": 0.1}, "foo": 1}
    mods = {
        "overrides": {
            "all": {"generationConfig": {"topP": 0.9}},
            "gpt-4o": {"foo": 2},
            "bar": 3,
        }
    }
    new_payload = apply_passthrough_modifications(
        payload, mods, "gemini", request_model="gpt-4o", original_model="gpt-4o"
    )
    assert new_payload["generationConfig"]["temperature"] == 0.1
    assert new_payload["generationConfig"]["topP"] == 0.9
    assert new_payload["foo"] == 2
    assert new_payload["bar"] == 3


def test_gemini_parse_file_data():
    native = {
        "contents": [
            {"role": "user", "parts": [
                {"text": "Describe this pdf"},
                {"fileData": {"mimeType": "application/pdf", "fileUri": "https://example.com/a.pdf"}}
            ]}
        ]
    }
    canonical = run(parse_gemini_request(native, {"model": "gemini-1.5-flash"}, {}))
    assert canonical.messages[0].role == "user"
    assert isinstance(canonical.messages[0].content, list)
    assert canonical.messages[0].content[0].text == "Describe this pdf"
    assert canonical.messages[0].content[1].type == "file"
    assert canonical.messages[0].content[1].file.mime_type == "application/pdf"
    assert canonical.messages[0].content[1].file.file_uri == "https://example.com/a.pdf"


def test_openai_responses_parse_input_file():
    """测试 Responses API 的 input_file 类型转换为 Canonical 的 file 类型"""
    from core.dialects.openai_responses import parse_responses_request
    native = {
        "model": "gpt-4o-realtime",
        "input": [
            {
                "type": "message",
                "role": "system",
                "content": [{"type": "input_text", "text": "SYS"}]
            },
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "analysis"},
                    {"type": "input_file", "file_id": "file-123", "filename": "data.csv"},
                ]
            },
        ]
    }
    canonical = run(parse_responses_request(native, {}, {}))
    assert canonical.messages[0].role == "system"
    assert canonical.messages[1].role == "user"
    content = canonical.messages[1].content
    assert content[0].type == "text"
    assert content[0].text == "analysis"
    assert content[1].type == "file"
    assert content[1].file.file_id == "file-123"
    assert content[1].file.filename == "data.csv"


def test_openai_responses_parse_instructions():
    """测试 Responses API 的顶层 instructions 被映射为首条 system message"""
    from core.dialects.openai_responses import parse_responses_request
    native = {
        "model": "gpt-4o",
        "instructions": "You are a helpful assistant.",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Hello"}]
            }
        ]
    }
    canonical = run(parse_responses_request(native, {}, {}))
    assert canonical.messages[0].role == "system"
    assert canonical.messages[0].content == "You are a helpful assistant."
    assert canonical.messages[1].role == "user"


def test_openai_responses_compact_no_auth_enters_auth_layer():
    app = _compact_test_app()

    with TestClient(app) as client:
        response = client.post("/v1/responses/compact", json={"model": "gpt-5"})

    assert response.status_code == 403
    assert response.status_code not in (404, 405)


def test_openai_responses_compact_requires_model():
    app = _compact_test_app()

    with TestClient(app) as client:
        response = client.post(
            "/v1/responses/compact",
            json={"input": []},
            headers={"Authorization": "Bearer test-key"},
        )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert "model" in response.json()["error"]["message"]


def test_openai_responses_compact_rejects_non_responses_provider_before_upstream():
    from core.handler import ModelRequestHandler

    request_info = {"request_id": "req-test", "api_key": "test-key"}
    app = SimpleNamespace(state=_handler_app_state(RaisingClientManager()))
    handler = ModelRequestHandler(app, lambda: request_info, _noop_update_stats, default_timeout=30)
    provider = {
        "provider": "provider1",
        "engine": "openai",
        "_model_dict_cache": {"gpt-5": "gpt-5"},
        "preferences": {},
    }

    response = run(handler.request_model(
        RequestModel(model="gpt-5", messages=[Message(role="user", content="")], stream=False),
        api_index=0,
        background_tasks=BackgroundTasks(),
        endpoint="/v1/responses/compact",
        dialect_id="openai-responses",
        original_payload={"model": "gpt-5"},
        original_headers={},
        passthrough_only=True,
        override_providers=[provider],
    ))

    assert response.status_code == 501
    assert "requires passthrough mode" in response.body.decode("utf-8")


def test_openai_responses_compact_passthrough_uses_custom_base_url_and_raw_payload():
    from core.handler import ModelRequestHandler

    captured = {}

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={"object": "response.compaction", "id": "cmp_123", "status": "completed"},
        )

    request_info = {"request_id": "req-test", "api_key": "test-key"}
    app = SimpleNamespace(state=_handler_app_state(MockClientManager(upstream_handler)))
    handler = ModelRequestHandler(app, lambda: request_info, _noop_update_stats, default_timeout=30)
    provider = {
        "provider": "provider1",
        "engine": "openai-responses",
        "base_url": "https://proxy.example/custom/v1",
        "_model_dict_cache": {"alias-model": "gpt-5-real"},
        "preferences": {
            "system_prompt": "SHOULD_NOT_BE_INJECTED",
            "post_body_parameter_overrides": {
                "all": {"metadata": {"tenant": "test"}}
            },
        },
    }
    original_payload = {
        "model": "alias-model",
        "input": [{"role": "system", "content": "keep-as-input"}],
        "max_tokens": 123,
    }

    response = run(handler.request_model(
        RequestModel(model="alias-model", messages=[Message(role="user", content="")], stream=False),
        api_index=0,
        background_tasks=BackgroundTasks(),
        endpoint="/v1/responses/compact",
        dialect_id="openai-responses",
        original_payload=original_payload,
        original_headers={"authorization": "Bearer client-key"},
        passthrough_only=True,
        override_providers=[provider],
    ))
    body = run(_read_response_json(response))

    assert response.status_code == 200
    assert body == {"object": "response.compaction", "id": "cmp_123", "status": "completed"}
    assert captured["url"] == "https://proxy.example/custom/v1/responses/compact"
    assert captured["payload"]["model"] == "gpt-5-real"
    assert captured["payload"]["input"] == original_payload["input"]
    assert captured["payload"]["max_tokens"] == 123
    assert captured["payload"]["metadata"] == {"tenant": "test"}
    assert "instructions" not in captured["payload"]
    assert "store" not in captured["payload"]


def test_openai_responses_compact_passthrough_preserves_upstream_error_status():
    from core.handler import ModelRequestHandler

    captured = {}

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        captured["called"] = True
        return httpx.Response(
            401,
            json={"error": {"message": "upstream auth failed", "type": "invalid_request_error"}},
        )

    request_info = {"request_id": "req-test", "api_key": "test-key"}
    app = SimpleNamespace(state=_handler_app_state(MockClientManager(upstream_handler)))
    handler = ModelRequestHandler(app, lambda: request_info, _noop_update_stats, default_timeout=30)
    provider = {
        "provider": "provider1",
        "engine": "openai-responses",
        "base_url": "https://proxy.example/v1",
        "_model_dict_cache": {"gpt-5": "gpt-5"},
        "preferences": {},
    }

    response = run(handler.request_model(
        RequestModel(model="gpt-5", messages=[Message(role="user", content="")], stream=False),
        api_index=0,
        background_tasks=BackgroundTasks(),
        endpoint="/v1/responses/compact",
        dialect_id="openai-responses",
        original_payload={"model": "gpt-5"},
        original_headers={},
        passthrough_only=True,
        override_providers=[provider],
    ))

    assert captured["called"] is True
    assert response.status_code == 401
    assert "upstream auth failed" in response.body.decode("utf-8")
