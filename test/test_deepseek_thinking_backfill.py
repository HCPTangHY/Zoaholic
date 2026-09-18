"""deepseek_thinking_backfill 插件单元测试

覆盖：
- OpenAI 格式（engine=openai）：字符串 content 补顶层 reasoning_content、
  tool_calls 消息补全、content 数组补 thinking block、已有值不动、disabled 跳过
- Anthropic 格式（engine=claude）：content 数组前插 thinking block、
  字符串 content 转数组、None content、tool_use-only、redacted_thinking 视为已有、
  非 thinking 模型跳过
- 通用：非 DeepSeek 渠道不处理、异常 payload 原样放行
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plugins.deepseek_thinking_backfill import (
    deepseek_thinking_backfill_request_interceptor as interceptor,
)

PROVIDER_DS_CLAUDE = {
    "provider": "ds打野",
    "base_url": "https://api.deepseek.com/anthropic",
}
PROVIDER_DS_OPENAI = {
    "provider": "ds自用",
    "base_url": "https://api.deepseek.com",
}


def run(engine, provider, payload, url="https://api.deepseek.com/v1/chat/completions"):
    return asyncio.new_event_loop().run_until_complete(
        interceptor(None, engine, provider, "sk-test", url, {}, payload)
    )


# ==================== OpenAI 格式 ====================

def test_openai_backfills_reasoning_content_on_string_content():
    payload = {
        "model": "deepseek-v4-flash",
        "messages": [
            {"role": "user", "content": "帮我查文件"},
            {"role": "assistant", "content": "好的，我来查。"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "read", "arguments": "{\"path\":\"a.md\"}"}}
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "文件内容"},
        ],
    }
    _, _, out = run("openai", PROVIDER_DS_OPENAI, payload)
    msgs = out["messages"]
    assert msgs[1]["reasoning_content"] == ""
    assert msgs[2]["reasoning_content"] == ""
    # user / tool 消息不动
    assert "reasoning_content" not in msgs[0]
    assert "reasoning_content" not in msgs[3]


def test_openai_keeps_existing_reasoning_content():
    payload = {
        "model": "deepseek-v4-flash",
        "messages": [
            {"role": "assistant", "content": "ok",
             "reasoning_content": "用户需要查文件"},
        ],
    }
    _, _, out = run("openai", PROVIDER_DS_OPENAI, payload)
    assert out["messages"][0]["reasoning_content"] == "用户需要查文件"


def test_openai_content_array_gets_thinking_block():
    payload = {
        "model": "deepseek-v4-pro",
        "messages": [
            {"role": "assistant", "content": [
                {"type": "output", "output": "结果"},
            ]},
        ],
    }
    _, _, out = run("openai", PROVIDER_DS_OPENAI, payload)
    blocks = out["messages"][0]["content"]
    assert blocks[0] == {"type": "thinking", "thinking": ""}
    assert blocks[1]["type"] == "output"


def test_openai_content_array_with_thinking_untouched():
    payload = {
        "model": "deepseek-v4-pro",
        "messages": [
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "原始思维链"},
                {"type": "output", "output": "结果"},
            ]},
        ],
    }
    _, _, out = run("openai", PROVIDER_DS_OPENAI, payload)
    assert out["messages"][0]["content"][0]["thinking"] == "原始思维链"


def test_openai_thinking_disabled_skipped():
    payload = {
        "model": "deepseek-v4-flash",
        "thinking": {"type": "disabled"},
        "messages": [{"role": "assistant", "content": "ok"}],
    }
    _, _, out = run("openai", PROVIDER_DS_OPENAI, payload)
    assert "reasoning_content" not in out["messages"][0]


# ==================== Anthropic messages 格式 ====================

def test_claude_backfills_thinking_block_in_array():
    payload = {
        "model": "deepseek-v4-flash",
        "messages": [
            {"role": "user", "content": "查文件"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "好的"},
            ]},
        ],
    }
    _, _, out = run("claude", PROVIDER_DS_CLAUDE, payload,
                    url="https://api.deepseek.com/anthropic/v1/messages")
    blocks = out["messages"][1]["content"]
    assert blocks[0] == {"type": "thinking", "thinking": ""}
    assert blocks[1] == {"type": "text", "text": "好的"}
    # user 消息不动
    assert out["messages"][0]["content"] == "查文件"


def test_claude_tool_use_only_message_backfilled():
    payload = {
        "model": "deepseek-v4-flash",
        "messages": [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "read", "input": {"path": "a"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
            ]},
        ],
    }
    _, _, out = run("claude", PROVIDER_DS_CLAUDE, payload,
                    url="https://api.deepseek.com/anthropic/v1/messages")
    blocks = out["messages"][0]["content"]
    assert blocks[0] == {"type": "thinking", "thinking": ""}
    assert blocks[1]["type"] == "tool_use"


def test_claude_string_content_converted_to_array():
    payload = {
        "model": "deepseek-v4-flash",
        "messages": [
            {"role": "assistant", "content": "好的"},
        ],
    }
    _, _, out = run("claude", PROVIDER_DS_CLAUDE, payload,
                    url="https://api.deepseek.com/anthropic/v1/messages")
    assert out["messages"][0]["content"] == [
        {"type": "thinking", "thinking": ""},
        {"type": "text", "text": "好的"},
    ]


def test_claude_none_content_gets_thinking_only():
    payload = {
        "model": "deepseek-v4-flash",
        "messages": [
            {"role": "assistant", "content": None},
        ],
    }
    _, _, out = run("claude", PROVIDER_DS_CLAUDE, payload,
                    url="https://api.deepseek.com/anthropic/v1/messages")
    assert out["messages"][0]["content"] == [{"type": "thinking", "thinking": ""}]


def test_claude_existing_thinking_untouched():
    payload = {
        "model": "deepseek-v4-flash",
        "messages": [
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "原始思维链"},
                {"type": "text", "text": "好的"},
            ]},
        ],
    }
    _, _, out = run("claude", PROVIDER_DS_CLAUDE, payload,
                    url="https://api.deepseek.com/anthropic/v1/messages")
    assert out["messages"][0]["content"][0]["thinking"] == "原始思维链"


def test_claude_redacted_thinking_counts_as_present():
    payload = {
        "model": "deepseek-v4-flash",
        "messages": [
            {"role": "assistant", "content": [
                {"type": "redacted_thinking", "data": "xxx"},
                {"type": "text", "text": "好的"},
            ]},
        ],
    }
    _, _, out = run("claude", PROVIDER_DS_CLAUDE, payload,
                    url="https://api.deepseek.com/anthropic/v1/messages")
    assert len(out["messages"][0]["content"]) == 2  # 未插入新 block


def test_claude_non_thinking_model_skipped():
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "assistant", "content": [{"type": "text", "text": "好的"}]},
        ],
    }
    _, _, out = run("claude", PROVIDER_DS_CLAUDE, payload,
                    url="https://api.deepseek.com/anthropic/v1/messages")
    assert out["messages"][0]["content"] == [{"type": "text", "text": "好的"}]


def test_claude_explicit_thinking_enabled_backfills():
    payload = {
        "model": "some-alias-model",
        "thinking": {"type": "enabled", "budget_tokens": 1024},
        "messages": [
            {"role": "assistant", "content": [{"type": "text", "text": "好的"}]},
        ],
    }
    _, _, out = run("claude", PROVIDER_DS_CLAUDE, payload,
                    url="https://api.deepseek.com/anthropic/v1/messages")
    assert out["messages"][0]["content"][0]["type"] == "thinking"


# ==================== 通用防御 ====================

def test_non_deepseek_provider_untouched():
    payload = {
        "model": "gpt-5.5",
        "messages": [{"role": "assistant", "content": "ok"}],
    }
    provider = {"provider": "oai官", "base_url": "https://api.openai.com/v1"}
    _, _, out = run("openai", provider, payload, url="https://api.openai.com/v1/chat/completions")
    assert "reasoning_content" not in out["messages"][0]


def test_model_name_with_channel_prefix_matched():
    payload = {
        "model": "[v]deepseek-v4-flash",
        "messages": [{"role": "assistant", "content": "ok"}],
    }
    _, _, out = run("openai", {"provider": "中转站", "base_url": "https://relay.example.com/v1"},
                    payload, url="https://relay.example.com/v1/chat/completions")
    assert out["messages"][0]["reasoning_content"] == ""


def test_malformed_payload_passthrough():
    _, headers, out = run("openai", PROVIDER_DS_OPENAI, {"model": "deepseek-v4-flash"})
    assert out == {"model": "deepseek-v4-flash"}
    # messages 为 None / 非 dict payload 均不抛异常
    _, _, out2 = run("openai", PROVIDER_DS_OPENAI, {"model": "deepseek-v4-flash", "messages": None})
    assert out2["messages"] is None
