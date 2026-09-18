"""Offline regressions for the /v1/responses cross-protocol path."""
import asyncio
import importlib
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from core.dialects.openai_responses import (
    convert_responses_input_to_messages, convert_responses_tools,
    parse_responses_request, render_responses_response,
)
from core.dialects.responses_stream import ResponsesStreamRenderer, render_responses_iterator


def chunk(delta=None, finish=None, usage=None):
    return {"object": "chat.completion.chunk", "choices": [] if delta is None and finish is None else [
        {"index": 0, "delta": delta or {}, "finish_reason": finish}], "usage": usage}


def wire(data):
    return "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"


def events(frames):
    return [json.loads(line[5:]) for frame in frames for line in frame.splitlines()
            if line.startswith("data:") and line[5:].strip() != "[DONE]"]


def run_stream(parts):
    async def run():
        async def source():
            for part in parts:
                yield part
        return events([x async for x in render_responses_iterator(source(), "test-model", stream=True)])
    return asyncio.run(run())


def test_mixed_text_parallel_tools_and_usage():
    result = run_stream([
        wire(chunk({"content": "你好"})),
        wire(chunk({"tool_calls": [
            {"index": 0, "id": "call_a", "function": {"name": "edit", "arguments": '{"path":'}},
            {"index": 1, "id": "call_b", "function": {"name": "read", "arguments": '{"path":"b"}'}},
        ]})),
        wire(chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"a"}'}}]}, "tool_calls")),
        wire(chunk(usage={"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14,
                          "prompt_tokens_details": {"cached_tokens": 5},
                          "completion_tokens_details": {"reasoning_tokens": 2}})),
        "data: [DONE]\n\n",
    ])
    assert [e["sequence_number"] for e in result] == list(range(len(result)))
    assert result[0]["type"] == "response.created"
    assert result[-1]["type"] == "response.completed"
    output = result[-1]["response"]["output"]
    assert [o["type"] for o in output] == ["message", "function_call", "function_call"]
    assert output[0]["content"][0]["text"] == "你好"
    assert output[1]["call_id"] == "call_a" and output[1]["arguments"] == '{"path":"a"}'
    assert output[2]["call_id"] == "call_b"
    usage = result[-1]["response"]["usage"]
    assert usage["input_tokens_details"]["cached_tokens"] == 5
    assert usage["output_tokens_details"]["reasoning_tokens"] == 2
    for index, item in enumerate(output):
        added = next(e for e in result if e["type"] == "response.output_item.added" and e["output_index"] == index)
        done = next(e for e in result if e["type"] == "response.output_item.done" and e["output_index"] == index)
        assert added["item"]["id"] == done["item"]["id"] == item["id"]
    assert result[0]["response"]["output"] == []


def test_arbitrary_utf8_and_crlf_boundaries():
    raw = (": keepalive\r\n\r\n" + wire(chunk({"content": "你好🙂"}, "stop")).replace("\n", "\r\n")
           + "data: [DONE]\r\n\r\n").encode()
    result = run_stream([raw[i:i + 1] for i in range(len(raw))])
    assert result[-1]["type"] == "response.completed"
    assert result[-1]["response"]["output"][0]["content"][0]["text"] == "你好🙂"


@pytest.mark.parametrize("reason,status", [("stop", "completed"), ("length", "incomplete"),
                                           ("content_filter", "incomplete")])
def test_terminal_status_and_no_duplicate_done(reason, status):
    result = run_stream([wire(chunk({"content": "x"}, reason)), "data: [DONE]\n\n", "data: [DONE]\n\n"])
    assert sum(e["type"] == f"response.{status}" for e in result) == 1
    assert result[-1]["response"]["status"] == status


@pytest.mark.parametrize("parts", [[wire(chunk({"content": "partial"}))], ["data: not-json\n\n"],
                                   [{"error": {"message": "upstream failed", "code": "bad_gateway"}}], []])
def test_broken_stream_fails_not_empty_success(parts):
    result = run_stream(parts)
    assert result[-1]["type"] == "response.failed"
    assert not any(e["type"] == "response.completed" for e in result)


def test_source_exception_and_cleanup():
    closed = []
    async def source():
        try:
            yield wire(chunk({"content": "partial"}))
            raise OSError("upstream connection lost")
        finally:
            closed.append(True)
    async def run():
        return events([x async for x in render_responses_iterator(source(), "m", stream=True)])
    result = asyncio.run(run())
    assert closed == [True] and result[-1]["type"] == "response.failed"


def test_reasoning_and_refusal():
    result = run_stream([wire(chunk({"reasoning_content": "thinking"})),
                         wire(chunk({"refusal": "cannot comply"}, "stop"))])
    output = result[-1]["response"]["output"]
    assert output[0]["summary"][0]["text"] == "thinking"
    assert output[1]["content"][0]["refusal"] == "cannot comply"


def test_nonstream_tools_round_trip_and_unique_ids():
    canonical = {"choices": [{"message": {"role": "assistant", "content": "run tools",
        "tool_calls": [{"id": "call_one", "function": {"name": "read", "arguments": '{"path":"a"}'}},
                       {"id": "call_two", "function": {"name": "read", "arguments": '{"path":"b"}'}}]},
        "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}}
    response = asyncio.run(render_responses_response(canonical, "m"))
    assert response["id"] != asyncio.run(render_responses_response(canonical, "m"))["id"]
    assert [i["type"] for i in response["output"]] == ["message", "function_call", "function_call"]
    messages = convert_responses_input_to_messages(response["output"] + [
        {"type": "function_call_output", "call_id": "call_one", "output": "a-content"},
        {"type": "function_call_output", "call_id": "call_two", "output": "b-content"}])
    assert messages[0]["content"][0]["text"] == "run tools"
    assert [t["id"] for t in messages[0]["tool_calls"]] == ["call_one", "call_two"]
    assert messages[1] == {"role": "tool", "tool_call_id": "call_one", "content": "a-content"}


def test_request_parameters_and_strict_false():
    tools = [{"type": "function", "name": "edit", "strict": False,
              "parameters": {"type": "object", "properties": {}}}]
    assert convert_responses_tools(tools)[0]["function"]["strict"] is False
    request = asyncio.run(parse_responses_request({"model": "m", "input": "hi", "tools": tools,
        "max_output_tokens": 200, "tool_choice": {"type": "function", "name": "edit"}}, {}, {}))
    data = request.model_dump(exclude_none=True)
    assert data["max_tokens"] == 200
    assert data["tool_choice"] == {"type": "function", "function": {"name": "edit"}}


@pytest.mark.parametrize("stream,mode", [(True, "auto"), (False, "auto"),
                                         (True, "force_non_stream"), (False, "force_stream")])
@pytest.mark.parametrize("dialect", ["openai-responses", "openai", None])
def test_process_request_output_integration(monkeypatch, stream, mode, dialect):
    """Exercise the real process_request output path, never contact an upstream."""
    module = importlib.import_module("core.process_request")
    handler = importlib.import_module("core.handler")
    from core.models import RequestModel
    from fastapi import FastAPI, BackgroundTasks
    canonical = {"id": "chatcmpl_test", "object": "chat.completion", "model": "m",
                 "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello"},
                              "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}}
    async def payload(*args):
        return "https://offline.invalid/v1/chat/completions", {}, {"stream": stream}
    async def fetch(*args, **kwargs):
        yield json.dumps(canonical)
    async def fetch_stream(*args, **kwargs):
        yield wire(chunk({"content": "hello"}, "stop"))
        yield wire(chunk(usage=canonical["usage"]))
        yield "data: [DONE]\n\n"
    async def wrapper(generator, *args, **kwargs):
        # Real wrapper yields parsed dicts for non-stream responses.
        if args[2] is False:
            async def parsed():
                async for v in generator:
                    yield json.loads(v)
            return parsed(), 0.01
        return generator, 0.01
    async def resolve(app, key, **kwargs):
        return key
    @asynccontextmanager
    async def client(*args):
        yield object()
    monkeypatch.setattr(module, "get_payload", payload)
    monkeypatch.setattr(module, "get_engine", lambda *args: ("openai", None, mode))
    monkeypatch.setattr(module, "fetch_response", fetch)
    monkeypatch.setattr(module, "fetch_response_stream", fetch_stream)
    monkeypatch.setattr(module, "error_handling_wrapper", wrapper)
    monkeypatch.setattr(handler, "_resolve_oauth_api_key", resolve)
    monkeypatch.setattr(handler, "_fire_and_forget_channel_stats", lambda *a, **k: None)
    app = FastAPI()
    app.state.config = {}
    app.state.error_triggers = []
    app.state.client_manager = SimpleNamespace(get_client=client)
    info = {"request_id": "test", "api_key": "test"}
    async def run():
        response = await module.process_request(RequestModel(model="m", messages=[{"role": "user", "content": "hi"}], stream=stream),
            {"provider": "offline-test", "_model_dict_cache": {"m": "m"}}, BackgroundTasks(), app,
            lambda: info, lambda *a, **k: None, dialect_id=dialect)
        raw = [v async for v in response.body_iterator]
        return raw
    raw = asyncio.run(run())
    if stream:
        decoded = events(raw)
        if dialect == "openai-responses":
            assert decoded[-1]["type"] == "response.completed"
            assert decoded[-1]["response"]["output"][0]["content"][0]["text"] == "hello"
        else:
            assert decoded[0]["object"] == "chat.completion.chunk"
    else:
        value = raw[0] if isinstance(raw[0], dict) else json.loads(raw[0])
        assert value["object"] == ("response" if dialect == "openai-responses" else "chat.completion")


def test_cancel_closes_upstream():
    closed = []
    async def source():
        try:
            yield wire(chunk({"content": "x"}))
            await asyncio.sleep(3600)
        finally:
            closed.append(True)
    async def run():
        stream = render_responses_iterator(source(), "m", stream=True)
        await anext(stream)
        await stream.aclose()
    asyncio.run(run())
    assert closed == [True]


def test_ws_forwards_responses_events_without_reconversion():
    from core.dialects.openai_responses_ws import _handle_sse_line
    sent = []
    class Socket:
        async def send_text(self, value):
            sent.append(json.loads(value))
    async def run():
        for event in run_events:
            await _handle_sse_line(Socket(), ("data: " + json.dumps(event)).encode())
    run_events = run_stream([wire(chunk({"content": "ws-ok"}, "stop"))])
    asyncio.run(run())
    assert sent == run_events


def test_usage_logging_and_image_fallback():
    from core.streaming import LoggingStreamingResponse
    from core.dialects.openai_responses import register
    from core.dialects.registry import get_dialect
    if get_dialect("openai-responses") is None:
        register()
    result = run_stream([wire(chunk({"content": [{"type": "image_url", "image_url": {"url": "https://example.invalid/a.png"}}]}, "stop")),
                         wire(chunk(usage={"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}))])
    response = result[-1]["response"]
    assert response["output"][0]["content"][0]["text"] == "![image](https://example.invalid/a.png)"
    info = {"test": True}
    wrapper = LoggingStreamingResponse(None, current_info=info, dialect_id="openai-responses")
    wrapper._try_extract_usage(result[-1])
    assert info["prompt_tokens"] == 2 and info["completion_tokens"] == 3
