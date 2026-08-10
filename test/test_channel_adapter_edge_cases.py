import pytest

from core.models import RequestModel
from core.channels.openai_responses_channel import get_responses_payload
from core.channels.gemini_channel import get_gemini_payload
from core.channels.openai_channel import get_gpt_payload
from core.channels.claude_channel import get_claude_payload
from core.utils import is_tools_disabled


@pytest.mark.asyncio
async def test_openai_responses_uses_output_text_for_assistant_history():
    # 修改原因：Chat Completions 历史消息转发到 Responses API 时，assistant 消息不能使用 input_text。
    # 修改方式：测试同时覆盖字符串内容和结构化文本内容，确保 assistant 历史被转为 output_text。
    # 目的：防止上游 OpenAI Responses API 因 assistant content type 非法而返回校验错误。
    request = RequestModel(
        model="test-model",
        messages=[
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": [{"type": "text", "text": "next"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "answer"}]},
        ],
        stream=False,
    )
    provider = {
        "base_url": "https://api.openai.com/v1",
        "model": ["test-model"],
    }

    _, _, payload = await get_responses_payload(request, "openai-responses", provider)

    assert payload["input"][0]["content"][0]["type"] == "input_text"
    assert payload["input"][1]["role"] == "assistant"
    assert payload["input"][1]["content"][0]["type"] == "output_text"
    assert payload["input"][2]["content"][0]["type"] == "input_text"
    assert payload["input"][3]["role"] == "assistant"
    assert payload["input"][3]["content"][0]["type"] == "output_text"


def _request_with_two_tool_calls():
    # 修改原因：工具调用历史必须完整转发，否则后续 tool_result 会找不到对应 tool_use。
    # 修改方式：构造包含两个 assistant tool_calls 的通用请求，供不同渠道适配器复用。
    # 目的：防止多工具调用被截断后再次引发 tool_use/tool_result 不匹配。
    return RequestModel(
        model="test-model",
        messages=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_first",
                        "type": "function",
                        "function": {"name": "first_tool", "arguments": "{}"},
                    },
                    {
                        "id": "call_second",
                        "type": "function",
                        "function": {"name": "second_tool", "arguments": "{}"},
                    },
                ],
            }
        ],
        stream=False,
    )


def test_is_tools_disabled_only_disables_explicit_false():
    # 修改原因：provider.tools 只有显式为 False 时才表示禁用工具。
    # 修改方式：覆盖 False、缺省、True 和其他配置值，确认判断函数只检查禁用开关。
    # 目的：避免把非 False 配置误判为禁用，影响工具声明和工具历史转发。
    assert is_tools_disabled({"tools": False}) is True
    assert is_tools_disabled({}) is False
    assert is_tools_disabled({"tools": True}) is False
    assert is_tools_disabled({"tools": "legacy"}) is False


@pytest.mark.asyncio
async def test_openai_payload_keeps_all_assistant_tool_calls():
    # 修改原因：OpenAI 兼容请求中 assistant 的多个 tool_calls 需要全部保留。
    # 修改方式：断言 payload 中的 tool_calls id 与输入完全一致。
    # 目的：保证客户端随后发送的每个 tool_result 都能匹配到对应 tool_call。
    provider = {"base_url": "https://api.openai.com/v1", "model": ["test-model"]}

    _, _, payload = await get_gpt_payload(_request_with_two_tool_calls(), "openai", provider)

    assert [call["id"] for call in payload["messages"][0]["tool_calls"]] == ["call_first", "call_second"]


@pytest.mark.asyncio
async def test_claude_payload_keeps_all_assistant_tool_calls():
    # 修改原因：Claude 请求中 assistant 的多个 tool_calls 会转换为多个 tool_use。
    # 修改方式：断言转换后的 tool_use id 与输入 tool_call id 完全一致。
    # 目的：保持 Claude 历史消息中的 tool_use 与后续 tool_result 成对。
    provider = {"base_url": "https://api.anthropic.com/v1", "model": ["test-model"]}

    _, _, payload = await get_claude_payload(_request_with_two_tool_calls(), "claude", provider, "test-key")

    assert [part["id"] for part in payload["messages"][0]["content"]] == ["call_first", "call_second"]
