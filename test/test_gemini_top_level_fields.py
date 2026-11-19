"""Gemini 请求根节点字段：toolConfig 转成 tool_choice，extra_body.google 中的根节点字段不进 generationConfig。"""

import pytest

from core.channels.claude_channel import get_claude_payload
from core.channels.gemini_channel import get_gemini_payload
from core.dialects.gemini import parse_gemini_request
from core.models import RequestModel

PATH = {"model": "gemini-x", "action": "generateContent"}
CONTENTS = [{"role": "user", "parts": [{"text": "上海天气"}]}]
TOOLS = [{"functionDeclarations": [
    {"name": "get_weather", "parameters": {"type": "object", "properties": {}}},
    {"name": "get_time", "parameters": {"type": "object", "properties": {}}},
]}]


def _tool_choice_dump(tool_choice):
    return tool_choice.model_dump(exclude_none=True) if hasattr(tool_choice, "model_dump") else tool_choice


@pytest.mark.asyncio
@pytest.mark.parametrize("fcc, expected", [
    ({"mode": "ANY", "allowedFunctionNames": ["get_weather"]}, {"type": "function", "function": {"name": "get_weather"}}),
    ({"mode": "ANY", "allowedFunctionNames": ["get_weather", "get_time"]}, "required"),
    ({"mode": "ANY"}, "required"),
    ({"mode": "NONE"}, "none"),
    ({"mode": "AUTO"}, "auto"),
])
async def test_tool_config_maps_to_tool_choice(fcc, expected):
    native = {"contents": CONTENTS, "tools": TOOLS, "toolConfig": {"functionCallingConfig": fcc}}
    request = await parse_gemini_request(native, PATH, {})
    assert _tool_choice_dump(request.tool_choice) == expected
    assert request.extra_body is None


@pytest.mark.asyncio
async def test_snake_case_tool_config():
    native = {"contents": CONTENTS, "tools": TOOLS,
              "tool_config": {"function_calling_config": {"mode": "ANY", "allowed_function_names": ["get_time"]}}}
    request = await parse_gemini_request(native, PATH, {})
    assert _tool_choice_dump(request.tool_choice) == {"type": "function", "function": {"name": "get_time"}}
    assert request.extra_body is None


@pytest.mark.asyncio
async def test_tool_config_without_tools_is_dropped():
    native = {"contents": CONTENTS, "toolConfig": {"functionCallingConfig": {"mode": "ANY"}}}
    request = await parse_gemini_request(native, PATH, {})
    assert request.tool_choice is None
    assert request.extra_body is None


@pytest.mark.asyncio
async def test_forced_call_reaches_claude_channel():
    native = {"contents": CONTENTS, "tools": TOOLS,
              "toolConfig": {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["get_weather"]}}}
    request = await parse_gemini_request(native, PATH, {})
    provider = {"base_url": "https://api.anthropic.com/v1", "model": ["gemini-x"]}
    _, _, payload = await get_claude_payload(request, "claude", provider, "test-key")
    assert payload["tool_choice"] == {"type": "tool", "name": "get_weather"}
    assert "extra_body" not in payload


@pytest.mark.asyncio
async def test_extra_body_google_top_level_fields_go_to_payload_root():
    client_safety = [{"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_ONLY_HIGH"}]
    request = RequestModel(
        model="gemini-x",
        messages=[{"role": "user", "content": "hi"}],
        extra_body={"google": {
            "cached_content": "cachedContents/abc",
            "safety_settings": client_safety,
            "tool_config": {"function_calling_config": {"mode": "NONE"}},
            "thinking_config": {"include_thoughts": True},
        }},
    )
    provider = {"provider": "g", "engine": "gemini",
                "base_url": "https://generativelanguage.googleapis.com/v1beta", "model": ["gemini-x"]}
    _, _, payload = await get_gemini_payload(request, "gemini", provider, api_key="k")

    assert payload["safetySettings"] == client_safety
    assert payload["cachedContent"] == "cachedContents/abc"
    assert payload["tool_config"] == {"functionCallingConfig": {"mode": "NONE"}}
    assert payload["generationConfig"]["thinkingConfig"] == {"includeThoughts": True}
    for key in ("safetySettings", "cachedContent", "toolConfig"):
        assert key not in payload["generationConfig"]
