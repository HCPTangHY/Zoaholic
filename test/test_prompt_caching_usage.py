"""Prompt caching usage extraction tests.

这些测试先固定各上游 usage 字段的输入与期望输出，目的是防止后续修改
只记录普通 token 而遗漏缓存命中和缓存创建 token。
"""

from core.dialects.claude import parse_claude_usage
from core.dialects.gemini import parse_gemini_usage
from core.dialects.openai import parse_openai_usage
from core.dialects.openai_responses import parse_responses_usage
from core.middleware import request_info
from core.response_context import merge_usage


def test_openai_chat_usage_extracts_cached_tokens():
    # 这里覆盖 Chat Completions 的官方嵌套字段，确保缓存命中 token 会进入统一 usage 字典。
    usage = parse_openai_usage({
        "usage": {
            "prompt_tokens": 2006,
            "completion_tokens": 300,
            "total_tokens": 2306,
            "prompt_tokens_details": {"cached_tokens": 1920},
        }
    })

    assert usage == {
        "prompt_tokens": 2006,
        "completion_tokens": 300,
        "total_tokens": 2306,
        "cached_tokens": 1920,
        "cache_creation_tokens": 0,
    }


def test_openai_usage_extracts_deepseek_prompt_cache_hits():
    # DeepSeek 走 OpenAI 兼容通道，但字段名不同；该测试避免只支持 OpenAI 官方字段。
    usage = parse_openai_usage({
        "usage": {
            "prompt_tokens": 120,
            "completion_tokens": 30,
            "total_tokens": 150,
            "prompt_cache_hit_tokens": 96,
        }
    })

    assert usage["cached_tokens"] == 96
    assert usage["cache_creation_tokens"] == 0


def test_responses_usage_extracts_cached_tokens():
    # Responses API 的缓存字段位于 input_tokens_details，不能依赖 Chat Completions 的字段路径。
    usage = parse_responses_usage({
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 50,
            "total_tokens": 1050,
            "input_tokens_details": {"cached_tokens": 880},
        }
    })

    assert usage == {
        "prompt_tokens": 1000,
        "completion_tokens": 50,
        "total_tokens": 1050,
        "cached_tokens": 880,
        "cache_creation_tokens": 0,
    }


def test_claude_usage_adds_cache_tokens_to_prompt_total():
    # Claude 的 input_tokens 不包含缓存部分；统一 prompt_tokens 需要补上创建和读取缓存的 token。
    usage = parse_claude_usage({
        "usage": {
            "input_tokens": 86,
            "output_tokens": 300,
            "cache_read_input_tokens": 1920,
            "cache_creation_input_tokens": 86,
        }
    })

    assert usage == {
        "prompt_tokens": 2092,
        "completion_tokens": 300,
        "total_tokens": 2392,
        "cached_tokens": 1920,
        "cache_creation_tokens": 86,
    }


def test_gemini_usage_extracts_cached_content_token_count():
    # Gemini 使用 usageMetadata.cachedContentTokenCount；该字段需要映射到统一 cached_tokens。
    usage = parse_gemini_usage({
        "usageMetadata": {
            "promptTokenCount": 600,
            "candidatesTokenCount": 40,
            "totalTokenCount": 640,
            "cachedContentTokenCount": 512,
        }
    })

    assert usage == {
        "prompt_tokens": 600,
        "completion_tokens": 40,
        "total_tokens": 640,
        "cached_tokens": 512,
        "cache_creation_tokens": 0,
    }


def test_merge_usage_persists_cache_fields_in_current_request_info():
    # 适配器直接写 current_info 时也要保留缓存字段，避免只在方言解析路径生效。
    current = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    token = request_info.set(current)
    try:
        merge_usage(
            prompt_tokens=2006,
            completion_tokens=300,
            total_tokens=2306,
            cached_tokens=1920,
            cache_creation_tokens=86,
        )
    finally:
        request_info.reset(token)

    assert current["cached_tokens"] == 1920
    assert current["cache_creation_tokens"] == 86
