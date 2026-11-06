"""Claude payload 兼容性回归测试：parallel_tool_calls、tool_choice、孤立 tool_result。"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.channels.claude_channel import get_claude_payload
from core.models import RequestModel
from plugins.claude_tools import apply_thinking_config

PROVIDER = {"base_url": "https://api.anthropic.com/v1", "model": ["claude-test"]}
TOOLS = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object", "properties": {}}}}]


async def _build(**kwargs):
    request = RequestModel(model="claude-test", messages=kwargs.pop("messages", [{"role": "user", "content": "hi"}]), **kwargs)
    _, _, payload = await get_claude_payload(request, "claude", PROVIDER, "test-key")
    return payload


@pytest.mark.asyncio
async def test_parallel_tool_calls_is_not_forwarded():
    payload = await _build(tools=TOOLS, parallel_tool_calls=False)
    assert "parallel_tool_calls" not in payload


@pytest.mark.asyncio
async def test_tool_choice_none_maps_to_none():
    payload = await _build(tools=TOOLS, tool_choice="none")
    assert payload["tool_choice"] == {"type": "none"}


@pytest.mark.asyncio
async def test_thinking_downgrades_forced_tool_choice():
    payload = await _build(tools=TOOLS, tool_choice="required", thinking={"type": "enabled", "budget_tokens": 2048})
    assert payload["tool_choice"] == {"type": "auto"}

    payload = await _build(tools=TOOLS, tool_choice="required")
    assert payload["tool_choice"] == {"type": "any"}


def test_suffix_thinking_downgrades_forced_tool_choice():
    payload = {"model": "claude-test", "tool_choice": {"type": "tool", "name": "lookup"}}
    apply_thinking_config(payload, 4096, model="claude-test")
    assert payload["tool_choice"] == {"type": "auto"}


@pytest.mark.asyncio
async def test_orphan_tool_result_becomes_text_and_paired_result_is_kept():
    messages = [
        {"role": "user", "content": "start"},
        {"role": "tool", "tool_call_id": "call_orphan", "content": "stale result"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_ok", "type": "function", "function": {"name": "lookup", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_ok", "content": "fresh result"},
    ]
    payload = await _build(tools=TOOLS, messages=messages)

    blocks = [block for message in payload["messages"] if isinstance(message["content"], list) for block in message["content"]]
    tool_results = [block for block in blocks if block.get("type") == "tool_result"]
    assert [block["tool_use_id"] for block in tool_results] == ["call_ok"]
    assert any(block.get("type") == "text" and "stale result" in block.get("text", "") for block in blocks)
