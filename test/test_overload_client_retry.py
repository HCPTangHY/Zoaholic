"""overload_client_retry 插件单元测试

覆盖：
- dict 错误 chunk（转换路径/非流式）：at capacity 改写 + 400→502、
  slow_down 429 保持、非过载错误不动
- SSE 文本：error 事件、response.failed 嵌套、非过载不动
- 纯 JSON 文本（Gemini 格式、字符串 error）
- 非错误文本直通
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plugins.overload_client_retry import (
    overload_client_retry_response_interceptor as interceptor,
)


def run(chunk, is_stream=False):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(interceptor(chunk, "openai", "gpt-5.6", is_stream))
    finally:
        loop.close()


# ==================== dict 错误 chunk ====================

def test_dict_at_capacity_rewritten_with_502():
    chunk = {
        "error": "upstream response failed: Selected model is at capacity. Please try a different model.",
        "status_code": 400,
        "details": {"error": {"message": "Selected model is at capacity."}},
    }
    out = run(chunk)
    assert out["error"] == "upstream failed. Please retry."
    assert out["details"] == "upstream failed. Please retry."
    assert out["status_code"] == 502


def test_dict_slow_down_keeps_429():
    chunk = {"error": "slow_down: too many requests", "status_code": 429, "details": "..."}
    out = run(chunk)
    assert out["error"] == "upstream failed. Please retry."
    assert out["status_code"] == 429  # 已属可重试档，保持


def test_dict_overloaded_5xx_keeps_status():
    chunk = {"error": "Our servers are currently overloaded. Please try again later.", "status_code": 503}
    out = run(chunk)
    assert out["error"] == "upstream failed. Please retry."
    assert out["status_code"] == 503


def test_dict_non_overload_untouched():
    chunk = {"error": "Invalid request: context length exceeded", "status_code": 400, "details": "..."}
    out = run(chunk)
    assert out == chunk


def test_dict_stream_error_from_adapter():
    # openai_channel 流适配器 yield 的错误形态
    line = {"error": {"message": "Our servers are currently overloaded. Please try again later.", "type": "server_error"}}
    chunk = {"error": "OpenAI Stream Error", "status_code": 400, "details": line}
    out = run(chunk, is_stream=True)
    assert out["error"] == "upstream failed. Please retry."
    assert out["status_code"] == 502


# ==================== SSE 文本（透传路径） ====================

def test_sse_error_event_rewritten():
    frame = json.dumps({
        "type": "error",
        "error": {"type": "service_unavailable_error", "message": "Our servers are currently overloaded. Please try again later."},
    }, ensure_ascii=False)
    chunk = f"event: error\ndata: {frame}\n\n"
    out = run(chunk, is_stream=True)
    assert "event: error" in out
    data_line = [l for l in out.split("\n") if l.startswith("data: ")][0]
    payload = json.loads(data_line[6:])
    assert payload["error"]["message"] == "upstream failed. Please retry."
    assert payload["error"]["type"] == "service_unavailable_error"  # 类型不动


def test_sse_response_failed_nested_rewritten():
    frame = json.dumps({
        "type": "response.failed",
        "response": {"status": "failed", "error": {"code": "server_is_overloaded", "message": "Our servers are currently overloaded."}},
    }, ensure_ascii=False)
    chunk = f"event: response.failed\ndata: {frame}\n\n"
    out = run(chunk, is_stream=True)
    data_line = [l for l in out.split("\n") if l.startswith("data: ")][0]
    payload = json.loads(data_line[6:])
    assert payload["response"]["error"]["message"] == "upstream failed. Please retry."


def test_sse_at_capacity_rewritten():
    frame = json.dumps({
        "error": {"message": "Selected model is at capacity. Please try a different model.", "type": "invalid_request_error"},
    }, ensure_ascii=False)
    chunk = f"data: {frame}\n\n"
    out = run(chunk, is_stream=True)
    payload = json.loads([l for l in out.split("\n") if l.startswith("data: ")][0][6:])
    assert payload["error"]["message"] == "upstream failed. Please retry."


def test_sse_normal_delta_untouched():
    chunk = 'data: {"type":"response.output_text.delta","delta":"hello"}\n\n'
    out = run(chunk, is_stream=True)
    assert out == chunk


def test_sse_non_overload_error_untouched():
    frame = json.dumps({"error": {"message": "Invalid model name", "type": "invalid_request_error"}})
    chunk = f"data: {frame}\n\n"
    out = run(chunk, is_stream=True)
    assert out == chunk


def test_sse_multi_line_chunk_only_error_line_changed():
    ok_frame = json.dumps({"choices": [{"delta": {"content": "hi"}}]})
    err_frame = json.dumps({"error": {"message": "servers are overloaded, please retry"}})
    chunk = f"data: {ok_frame}\ndata: {err_frame}\n\n"
    out = run(chunk, is_stream=True)
    lines = [l for l in out.split("\n") if l.startswith("data: ")]
    assert json.loads(lines[0][6:])["choices"][0]["delta"]["content"] == "hi"
    assert json.loads(lines[1][6:])["error"]["message"] == "upstream failed. Please retry."


# ==================== 纯 JSON 文本 ====================

def test_plain_json_gemini_format_rewritten():
    chunk = json.dumps({
        "error": {"code": 400, "message": "Our servers are currently overloaded. Please try again later.", "status": "UNAVAILABLE"},
    })
    out = run(chunk)
    payload = json.loads(out)
    assert payload["error"]["message"] == "upstream failed. Please retry."


def test_plain_json_string_error_rewritten():
    chunk = json.dumps({"error": "upstream response failed: at capacity"})
    out = run(chunk)
    payload = json.loads(out)
    assert payload["error"]["message"] == "upstream failed. Please retry."


def test_plain_json_no_error_untouched():
    chunk = json.dumps({"id": "resp_123", "status": "completed"})
    out = run(chunk)
    assert out == chunk


# ==================== 防御 ====================

def test_malformed_text_passthrough():
    out = run("data: {not json with error word}", is_stream=True)
    assert out == "data: {not json with error word}"


def test_none_chunk_passthrough():
    assert run(None) is None
