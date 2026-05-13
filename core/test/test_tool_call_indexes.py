"""Tool call index regression tests.

这些测试先固定并行工具调用的输出形态，目的在于防止非 OpenAI
渠道把所有参数片段都写到 index=0，导致多个工具调用的参数被合并。
"""

import asyncio

from core.channels.gemini_channel import gemini_json_process
from core.channels.openai_responses_channel import convert_responses_to_chat_completions
from core.json_utils import json_loads
from core.stream_convert import assemble_stream_to_json
from core.utils import generate_no_stream_response, generate_sse_response


def _parse_sse_payload(sse_text: str) -> dict:
    # 修改原因：本文件需要直接检查 SSE data 里的 JSON，而不是只检查字符串。
    # 修改方式：剥离 data 前缀后使用项目统一 JSON loader 解析。
    # 目的：确保测试断言的是下游实际收到的 tool_calls.index。
    return json_loads(sse_text.removeprefix("data: ").strip())


def test_generate_sse_response_preserves_tool_call_index_for_header_and_arguments():
    # 修改原因：generate_sse_response 是多数非 OAI 流式渠道的统一出口，旧实现把工具调用 index 固定为 0。
    # 修改方式：分别生成工具头和参数片段，并传入同一个非零 tool_call_index。
    # 目的：确保并行工具调用的参数片段不会被下游累积到第一个工具调用中。
    header = asyncio.run(generate_sse_response(
        123,
        "test-model",
        tools_id="call_second",
        function_call_name="second_tool",
        tool_call_index=1,
    ))
    arguments = asyncio.run(generate_sse_response(
        123,
        "test-model",
        function_call_content='{"b":2}',
        tool_call_index=1,
    ))

    header_tool_call = _parse_sse_payload(header)["choices"][0]["delta"]["tool_calls"][0]
    arguments_tool_call = _parse_sse_payload(arguments)["choices"][0]["delta"]["tool_calls"][0]

    assert header_tool_call["index"] == 1
    assert arguments_tool_call["index"] == 1


def test_generate_no_stream_response_accepts_multiple_tool_calls_with_indexes():
    # 修改原因：非流式出口过去只能表达一个工具调用，无法保持并行工具调用的独立索引。
    # 修改方式：传入已经规范化的 tool_calls_list，让出口函数直接构建 message.tool_calls。
    # 目的：确保非流式 Claude、Gemini 等渠道可以一次返回多个互不合并的工具调用。
    response = asyncio.run(generate_no_stream_response(
        123,
        "test-model",
        role="assistant",
        tool_calls_list=[
            {
                "index": 0,
                "id": "call_first",
                "type": "function",
                "function": {"name": "first_tool", "arguments": '{"a":1}'},
            },
            {
                "index": 1,
                "id": "call_second",
                "type": "function",
                "function": {"name": "second_tool", "arguments": '{"b":2}'},
            },
        ],
        return_dict=True,
    ))

    tool_calls = response["choices"][0]["message"]["tool_calls"]
    assert [tc["index"] for tc in tool_calls] == [0, 1]
    assert [tc["id"] for tc in tool_calls] == ["call_first", "call_second"]
    assert response["choices"][0]["finish_reason"] == "tool_calls"


def test_convert_responses_to_chat_completions_keeps_multiple_function_call_indexes():
    # 修改原因：Responses API 非流式输出的 function_call 是顶层 output item，旧转换路径容易漏掉多个调用。
    # 修改方式：构造两个顶层 function_call output item，并检查转换后的 tool_calls 顺序与 index。
    # 目的：确保 Responses 非流式渠道在并行工具调用时不会丢失或合并调用。
    converted = asyncio.run(convert_responses_to_chat_completions({
        "output": [
            {
                "type": "function_call",
                "call_id": "call_first",
                "name": "first_tool",
                "arguments": '{"a":1}',
            },
            {
                "type": "function_call",
                "call_id": "call_second",
                "name": "second_tool",
                "arguments": '{"b":2}',
            },
        ],
        "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
    }, "test-model"))

    tool_calls = converted["choices"][0]["message"]["tool_calls"]
    assert [tc["index"] for tc in tool_calls] == [0, 1]
    assert [tc["function"]["name"] for tc in tool_calls] == ["first_tool", "second_tool"]


def test_gemini_json_process_returns_all_function_calls_in_order():
    # 修改原因：Gemini 的一个 candidates.content.parts 数组中可能包含多个 functionCall，旧解析只取第一个。
    # 修改方式：构造两个 functionCall part，并检查新增的 function_calls_list 返回值。
    # 目的：确保流式和非流式 Gemini 出口都可以逐个分配独立 tool_call index。
    result = gemini_json_process({
        "candidates": [{
            "content": {
                "parts": [
                    {"functionCall": {"name": "first_tool", "args": {"a": 1}}},
                    {"functionCall": {"name": "second_tool", "args": {"b": 2}}},
                ]
            },
            "finishReason": "STOP",
        }],
        "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 2, "totalTokenCount": 3},
    })

    function_calls_list = result[-1]
    assert [fc["name"] for fc in function_calls_list] == ["first_tool", "second_tool"]
    assert [fc["args"] for fc in function_calls_list] == [{"a": 1}, {"b": 2}]


def test_stream_assembler_keeps_arguments_separate_by_tool_call_index():
    # 修改原因：core/stream_convert.py 依赖 tool_calls.index 累积参数；这里验证上游修正 index 后行为正确。
    # 修改方式：模拟两个工具调用交错输出工具头和参数片段。
    # 目的：确认多 index 场景下参数不会被拼接到同一个 tool call。
    async def stream():
        yield await generate_sse_response(123, "test-model", tools_id="call_first", function_call_name="first_tool", tool_call_index=0)
        yield await generate_sse_response(123, "test-model", function_call_content='{"a":', tool_call_index=0)
        yield await generate_sse_response(123, "test-model", tools_id="call_second", function_call_name="second_tool", tool_call_index=1)
        yield await generate_sse_response(123, "test-model", function_call_content='{"b":2}', tool_call_index=1)
        yield await generate_sse_response(123, "test-model", function_call_content='1}', tool_call_index=0)
        yield await generate_sse_response(123, "test-model", stop="tool_calls")

    assembled = asyncio.run(assemble_stream_to_json(stream()))
    tool_calls = assembled["choices"][0]["message"]["tool_calls"]

    assert [tc["id"] for tc in tool_calls] == ["call_first", "call_second"]
    assert tool_calls[0]["function"]["arguments"] == '{"a":1}'
    assert tool_calls[1]["function"]["arguments"] == '{"b":2}'
