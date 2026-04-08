"""
压力测试：大规模模型名称归一化 + 价格匹配。

覆盖各种命名格式：dated 版本号、插件后缀叠加、大小写混合、
OpenAI reasoning 后缀、Gemini 思考预算、边界情况等。
"""

import pytest
from core.model_name_utils import normalize_model_name
from core.default_prices import match_default_price


# ═══════════════════════════════════════
# 归一化压力测试
# ═══════════════════════════════════════

class TestNormalizeStress:
    """大规模后缀归一化压力测试。"""

    # ── OpenAI 模型名变体 ──

    @pytest.mark.parametrize("input_name, expected", [
        # GPT-4o 系列
        ("gpt-4o", "gpt-4o"),
        ("gpt-4o-high", "gpt-4o"),
        ("gpt-4o-low", "gpt-4o"),
        ("gpt-4o-medium", "gpt-4o"),
        ("gpt-4o-2024-08-06", "gpt-4o-2024-08-06"),
        ("gpt-4o-2024-08-06-high", "gpt-4o-2024-08-06"),
        ("gpt-4o-2024-11-20-high", "gpt-4o-2024-11-20"),
        ("GPT-4o", "GPT-4o"),  # 保留原始大小写（归一化不改变大小写）
        ("GPT-4O-HIGH", "GPT-4O"),
        ("gpt-4o-search", "gpt-4o"),
        ("gpt-4o-code", "gpt-4o"),

        # GPT-4o-mini 系列
        ("gpt-4o-mini", "gpt-4o-mini"),
        ("gpt-4o-mini-high", "gpt-4o-mini"),
        ("gpt-4o-mini-low", "gpt-4o-mini"),
        ("gpt-4o-mini-search", "gpt-4o-mini"),
        ("gpt-4o-mini-2024-07-18", "gpt-4o-mini-2024-07-18"),

        # GPT-4.1 系列
        ("gpt-4.1", "gpt-4.1"),
        ("gpt-4.1-high", "gpt-4.1"),
        ("gpt-4.1-2025-04-14", "gpt-4.1-2025-04-14"),
        ("gpt-4.1-mini", "gpt-4.1-mini"),
        ("gpt-4.1-mini-high", "gpt-4.1-mini"),
        ("gpt-4.1-nano", "gpt-4.1-nano"),
        ("gpt-4.1-nano-low", "gpt-4.1-nano"),

        # GPT-5 系列
        ("gpt-5", "gpt-5"),
        ("gpt-5-high", "gpt-5"),
        ("gpt-5-mini", "gpt-5-mini"),
        ("gpt-5-mini-high", "gpt-5-mini"),
        ("gpt-5-nano", "gpt-5-nano"),
        ("gpt-5.1", "gpt-5.1"),
        ("gpt-5.2", "gpt-5.2"),
        ("gpt-5.2-pro", "gpt-5.2-pro"),

        # O 系列
        ("o3", "o3"),
        ("o3-high", "o3"),
        ("o3-medium", "o3"),
        ("o3-low", "o3"),
        ("o3-minimal", "o3"),
        ("o3-none", "o3"),
        ("o3-xhigh", "o3"),
        ("o3-mini", "o3-mini"),
        ("o3-mini-high", "o3-mini"),
        ("o3-mini-low", "o3-mini"),
        ("o3-pro", "o3-pro"),
        ("o3-pro-high", "o3-pro"),
        ("o1", "o1"),
        ("o1-high", "o1"),
        ("o1-mini", "o1-mini"),
        ("o1-mini-high", "o1-mini"),
        ("o1-pro", "o1-pro"),
        ("o4-mini", "o4-mini"),
        ("o4-mini-high", "o4-mini"),

        # 旧 GPT 模型
        ("gpt-4-turbo", "gpt-4-turbo"),
        ("gpt-4-turbo-2024-04-09", "gpt-4-turbo-2024-04-09"),
        ("gpt-4", "gpt-4"),
        ("gpt-3.5-turbo", "gpt-3.5-turbo"),
        ("gpt-3.5-turbo-0125", "gpt-3.5-turbo-0125"),
        ("chatgpt-4o-latest", "chatgpt-4o-latest"),
    ])
    def test_openai_variants(self, input_name, expected):
        assert normalize_model_name(input_name) == expected

    # ── Claude 模型名变体 ──

    @pytest.mark.parametrize("input_name, expected", [
        # Claude 4.x
        ("claude-sonnet-4", "claude-sonnet-4"),
        ("claude-sonnet-4-thinking", "claude-sonnet-4"),
        ("claude-sonnet-4-thinking-32768", "claude-sonnet-4"),
        ("claude-sonnet-4-thinking-0", "claude-sonnet-4"),
        ("claude-sonnet-4-thinking-100000", "claude-sonnet-4"),
        ("claude-sonnet-4-search", "claude-sonnet-4"),
        ("claude-sonnet-4-code", "claude-sonnet-4"),
        ("claude-sonnet-4-computer", "claude-sonnet-4"),
        ("claude-sonnet-4-artifacts", "claude-sonnet-4"),
        ("claude-sonnet-4-thinking-search", "claude-sonnet-4"),
        ("claude-sonnet-4-thinking-32768-search", "claude-sonnet-4"),
        ("claude-sonnet-4-thinking-search-code", "claude-sonnet-4"),
        ("claude-sonnet-4-thinking-32768-search-code-computer", "claude-sonnet-4"),
        ("claude-sonnet-4-image", "claude-sonnet-4"),

        # Claude 4 各版本
        ("claude-opus-4", "claude-opus-4"),
        ("claude-opus-4-thinking", "claude-opus-4"),
        ("claude-opus-4-thinking-65536", "claude-opus-4"),
        ("claude-opus-4-0", "claude-opus-4-0"),
        ("claude-opus-4-0-thinking", "claude-opus-4-0"),
        ("claude-opus-4-6", "claude-opus-4-6"),
        ("claude-opus-4-6-thinking", "claude-opus-4-6"),
        ("claude-sonnet-4-0", "claude-sonnet-4-0"),
        ("claude-sonnet-4-0-thinking", "claude-sonnet-4-0"),
        ("claude-sonnet-4-6", "claude-sonnet-4-6"),
        ("claude-sonnet-4-6-thinking", "claude-sonnet-4-6"),
        ("claude-haiku-4", "claude-haiku-4"),

        # Claude 3.x dated 版本
        ("claude-3-5-sonnet-20241022", "claude-3-5-sonnet-20241022"),
        ("claude-3-5-sonnet-20241022-thinking", "claude-3-5-sonnet-20241022"),
        ("claude-3-5-sonnet-20241022-thinking-32768", "claude-3-5-sonnet-20241022"),
        ("claude-3-5-sonnet-20241022-thinking-search", "claude-3-5-sonnet-20241022"),
        ("claude-3-5-sonnet-20240620", "claude-3-5-sonnet-20240620"),
        ("claude-3-5-haiku-20241022", "claude-3-5-haiku-20241022"),
        ("claude-3-7-sonnet-20250219", "claude-3-7-sonnet-20250219"),
        ("claude-3-7-sonnet-20250219-thinking", "claude-3-7-sonnet-20250219"),
        ("claude-3-7-sonnet-20250219-thinking-65536", "claude-3-7-sonnet-20250219"),

        # 大小写混合
        ("Claude-Sonnet-4-THINKING", "Claude-Sonnet-4"),
        ("CLAUDE-OPUS-4-THINKING-32768", "CLAUDE-OPUS-4"),
        ("claude-SONNET-4-Search-Code", "claude-SONNET-4"),
    ])
    def test_claude_variants(self, input_name, expected):
        assert normalize_model_name(input_name) == expected

    # ── Gemini 模型名变体 ──

    @pytest.mark.parametrize("input_name, expected", [
        # Gemini 2.5
        ("gemini-2.5-pro", "gemini-2.5-pro"),
        ("gemini-2.5-flash", "gemini-2.5-flash"),
        ("gemini-2.5-flash-think-8192", "gemini-2.5-flash"),
        ("gemini-2.5-flash-think-32768", "gemini-2.5-flash"),
        ("gemini-2.5-flash-think--1", "gemini-2.5-flash"),
        ("gemini-2.5-flash-think-0", "gemini-2.5-flash"),
        ("gemini-2.5-flash-think-8192-search", "gemini-2.5-flash"),
        ("gemini-2.5-flash-image", "gemini-2.5-flash"),
        ("gemini-2.5-flash-lite", "gemini-2.5-flash-lite"),
        ("gemini-2.5-pro-think-8192", "gemini-2.5-pro"),
        ("gemini-2.5-pro-search", "gemini-2.5-pro"),

        # Gemini 2.0
        ("gemini-2.0-flash", "gemini-2.0-flash"),
        ("gemini-2.0-flash-exp", "gemini-2.0-flash-exp"),
        ("gemini-2.0-flash-exp-image-generation", "gemini-2.0-flash-exp"),
        ("gemini-2.0-flash-lite", "gemini-2.0-flash-lite"),
        ("gemini-2.0-flash-think-8192", "gemini-2.0-flash"),

        # Gemini 1.5
        ("gemini-1.5-pro", "gemini-1.5-pro"),
        ("gemini-1.5-flash", "gemini-1.5-flash"),

        # Gemini 3.x
        ("gemini-3-pro", "gemini-3-pro"),
        ("gemini-3-flash", "gemini-3-flash"),
        ("gemini-3.1-pro", "gemini-3.1-pro"),

        # 大小写
        ("GEMINI-2.5-FLASH-THINK-8192", "GEMINI-2.5-FLASH"),
        ("Gemini-2.5-Pro-Search", "Gemini-2.5-Pro"),

        # 不应被修改
        ("gemini-2.5-flash-think", "gemini-2.5-flash-think"),  # 裸 think 无数字
        ("gemini-2.5-flash-image-preview", "gemini-2.5-flash-image-preview"),  # 不以 -image 结尾
    ])
    def test_gemini_variants(self, input_name, expected):
        assert normalize_model_name(input_name) == expected

    # ── 其他模型 + 安全测试 ──

    @pytest.mark.parametrize("input_name, expected", [
        # DeepSeek
        ("deepseek-chat", "deepseek-chat"),
        ("deepseek-reasoner", "deepseek-reasoner"),
        ("deepseek-r1", "deepseek-r1"),
        ("deepseek-v3", "deepseek-v3"),

        # Grok
        ("grok-3", "grok-3"),
        ("grok-3-mini", "grok-3-mini"),
        ("grok-2", "grok-2"),

        # Mistral
        ("mistral-large", "mistral-large"),
        ("mistral-small", "mistral-small"),
        ("codestral", "codestral"),
        ("pixtral-large", "pixtral-large"),

        # Llama
        ("llama-4-maverick", "llama-4-maverick"),
        ("llama-4-scout", "llama-4-scout"),
        ("llama-3.3", "llama-3.3"),

        # Qwen
        ("qwen-max", "qwen-max"),
        ("qwen-plus", "qwen-plus"),
        ("qwen-turbo", "qwen-turbo"),

        # 不应被误匹配的名称
        ("some-model-flow", "some-model-flow"),  # flow ≠ -low
        ("deepseek-acode", "deepseek-acode"),     # acode ≠ -code
        ("model-highres", "model-highres"),        # highres ≠ -high
        ("model-mediumpriority", "model-mediumpriority"),  # mediumpriority ≠ -medium
        ("my-custom-model", "my-custom-model"),
        ("local-llama-7b", "local-llama-7b"),
        ("together-llama-3.1-8b", "together-llama-3.1-8b"),

        # 边界情况
        ("", ""),
        ("a", "a"),
        ("-high", ""),  # 退化情况：仅有后缀
        ("-thinking", ""),
        ("model-thinking-", "model-thinking-"),  # 尾部有 dash 不匹配
    ])
    def test_other_and_safety(self, input_name, expected):
        assert normalize_model_name(input_name) == expected


# ═══════════════════════════════════════
# 价格匹配压力测试
# ═══════════════════════════════════════

class TestPriceMatchStress:
    """大规模价格匹配压力测试。"""

    # ── OpenAI 价格匹配 ──

    @pytest.mark.parametrize("model, expected", [
        # GPT-4o 系列
        ("gpt-4o", (2.5, 10.0)),
        ("gpt-4o-high", (2.5, 10.0)),
        ("gpt-4o-low", (2.5, 10.0)),
        ("gpt-4o-medium", (2.5, 10.0)),
        ("gpt-4o-2024-08-06", (2.5, 10.0)),
        ("gpt-4o-2024-08-06-high", (2.5, 10.0)),
        ("gpt-4o-2024-11-20", (2.5, 10.0)),
        ("GPT-4O", (2.5, 10.0)),
        ("GPT-4o-HIGH", (2.5, 10.0)),

        # GPT-4o-mini
        ("gpt-4o-mini", (0.15, 0.6)),
        ("gpt-4o-mini-high", (0.15, 0.6)),
        ("gpt-4o-mini-low", (0.15, 0.6)),
        ("gpt-4o-mini-2024-07-18", (0.15, 0.6)),

        # GPT-4o-audio
        ("gpt-4o-audio-preview", (2.5, 10.0)),
        ("gpt-4o-audio-preview-2024-12-17", (2.5, 10.0)),

        # GPT-4.1 系列
        ("gpt-4.1", (2.0, 8.0)),
        ("gpt-4.1-high", (2.0, 8.0)),
        ("gpt-4.1-2025-04-14", (2.0, 8.0)),
        ("gpt-4.1-mini", (0.4, 1.6)),
        ("gpt-4.1-mini-high", (0.4, 1.6)),
        ("gpt-4.1-nano", (0.1, 0.4)),
        ("gpt-4.1-nano-low", (0.1, 0.4)),

        # GPT-5 系列
        ("gpt-5", (1.25, 10.0)),
        ("gpt-5-high", (1.25, 10.0)),
        ("gpt-5-mini", (0.25, 2.0)),
        ("gpt-5.1", (1.25, 10.0)),
        ("gpt-5.2", (1.75, 14.0)),
        ("gpt-5.2-pro", (21.0, 168.0)),
        ("gpt-5-nano", (0.05, 0.4)),

        # OpenAI 图像生成模型（token-based）
        ("gpt-image-1.5", (5.0, 32.0)),
        ("gpt-image-1", (10.0, 40.0)),
        ("gpt-image-1-mini", (2.0, 8.0)),
        ("gpt-image-1.5-2026-03-01", (5.0, 32.0)),  # dated 版本前缀匹配

        # O 系列
        ("o3", (2.0, 8.0)),
        ("o3-high", (2.0, 8.0)),
        ("o3-medium", (2.0, 8.0)),
        ("o3-low", (2.0, 8.0)),
        ("o3-minimal", (2.0, 8.0)),
        ("o3-none", (2.0, 8.0)),
        ("o3-xhigh", (2.0, 8.0)),
        ("o3-mini", (1.1, 4.4)),
        ("o3-mini-high", (1.1, 4.4)),
        ("o3-mini-low", (1.1, 4.4)),
        ("o3-pro", (20.0, 80.0)),
        ("o3-pro-high", (20.0, 80.0)),
        ("o1", (15.0, 60.0)),
        ("o1-high", (15.0, 60.0)),
        ("o1-mini", (1.1, 4.4)),
        ("o1-pro", (150.0, 600.0)),
        ("o1-pro-high", (150.0, 600.0)),
        ("o4-mini", (1.1, 4.4)),
        ("o4-mini-high", (1.1, 4.4)),

        # 旧 GPT 模型
        ("gpt-4-turbo", (10.0, 30.0)),
        ("gpt-4-turbo-2024-04-09", (10.0, 30.0)),
        ("gpt-4", (30.0, 60.0)),
        ("gpt-3.5-turbo", (0.5, 1.5)),
        ("gpt-3.5-turbo-0125", (0.5, 1.5)),
        ("chatgpt-4o-latest", (5.0, 15.0)),
    ])
    def test_openai_prices(self, model, expected):
        result = match_default_price(model)
        assert result == expected, f"{model}: got {result}, expected {expected}"

    # ── Claude 价格匹配 ──

    @pytest.mark.parametrize("model, expected", [
        # Claude 4.x
        ("claude-sonnet-4", (3.0, 15.0)),
        ("claude-sonnet-4-thinking", (3.0, 15.0)),
        ("claude-sonnet-4-thinking-32768", (3.0, 15.0)),
        ("claude-sonnet-4-thinking-32768-search", (3.0, 15.0)),
        ("claude-sonnet-4-thinking-search-code", (3.0, 15.0)),
        ("claude-sonnet-4-search", (3.0, 15.0)),
        ("claude-sonnet-4-code", (3.0, 15.0)),
        ("claude-sonnet-4-computer", (3.0, 15.0)),
        ("claude-sonnet-4-artifacts", (3.0, 15.0)),

        # Claude 各版本
        ("claude-opus-4", (5.0, 25.0)),
        ("claude-opus-4-thinking", (5.0, 25.0)),
        ("claude-opus-4-thinking-65536", (5.0, 25.0)),
        ("claude-opus-4-0", (15.0, 75.0)),
        ("claude-opus-4-0-thinking", (15.0, 75.0)),
        ("claude-opus-4-0-20250514", (15.0, 75.0)),
        ("claude-opus-4-6", (5.0, 25.0)),
        ("claude-opus-4-6-thinking", (5.0, 25.0)),
        ("claude-opus-4-6-20260101", (5.0, 25.0)),
        ("claude-sonnet-4-0", (3.0, 15.0)),
        ("claude-sonnet-4-0-thinking", (3.0, 15.0)),
        ("claude-sonnet-4-6", (3.0, 15.0)),
        ("claude-sonnet-4-6-thinking", (3.0, 15.0)),
        ("claude-haiku-4", (1.0, 5.0)),

        # Claude 3.x
        ("claude-3-5-sonnet-20241022", (3.0, 15.0)),
        ("claude-3-5-sonnet-20241022-thinking", (3.0, 15.0)),
        ("claude-3-5-sonnet-20241022-thinking-32768-search", (3.0, 15.0)),
        ("claude-3-5-sonnet-20240620", (3.0, 15.0)),
        ("claude-3-5-haiku-20241022", (0.8, 4.0)),
        ("claude-3-7-sonnet-20250219", (3.0, 15.0)),
        ("claude-3-7-sonnet-20250219-thinking", (3.0, 15.0)),
        ("claude-3-7-sonnet-20250219-thinking-65536", (3.0, 15.0)),
        ("claude-3.5-sonnet", (3.0, 15.0)),
        ("claude-3.7-sonnet", (3.0, 15.0)),
        ("claude-3-opus", (15.0, 75.0)),
        ("claude-3-sonnet", (3.0, 15.0)),
        ("claude-3-haiku", (0.25, 1.25)),

        # 大小写
        ("Claude-Sonnet-4-THINKING", (3.0, 15.0)),
        ("CLAUDE-OPUS-4", (5.0, 25.0)),
    ])
    def test_claude_prices(self, model, expected):
        result = match_default_price(model)
        assert result == expected, f"{model}: got {result}, expected {expected}"

    # ── Gemini 价格匹配 ──

    @pytest.mark.parametrize("model, expected", [
        ("gemini-2.5-pro", (1.25, 10.0)),
        ("gemini-2.5-pro-think-8192", (1.25, 10.0)),
        ("gemini-2.5-pro-search", (1.25, 10.0)),
        ("gemini-2.5-flash", (0.3, 2.5)),
        ("gemini-2.5-flash-think-8192", (0.3, 2.5)),
        ("gemini-2.5-flash-think-32768", (0.3, 2.5)),
        ("gemini-2.5-flash-think--1", (0.3, 2.5)),
        ("gemini-2.5-flash-think-8192-search", (0.3, 2.5)),
        ("gemini-2.5-flash-image", (0.3, 2.5)),
        ("gemini-2.5-flash-lite", (0.1, 0.4)),
        ("gemini-2.0-flash", (0.1, 0.4)),
        ("gemini-2.0-flash-exp-image-generation", (0.1, 0.4)),
        ("gemini-2.0-flash-lite", (0.075, 0.3)),
        ("gemini-1.5-pro", (1.25, 5.0)),
        ("gemini-1.5-flash", (0.075, 0.3)),
        ("gemini-3-pro", (2.0, 12.0)),
        ("gemini-3-flash", (0.5, 3.0)),
        ("gemini-3.1-pro", (2.0, 12.0)),
        ("GEMINI-2.5-FLASH-THINK-8192", (0.3, 2.5)),
    ])
    def test_gemini_prices(self, model, expected):
        result = match_default_price(model)
        assert result == expected, f"{model}: got {result}, expected {expected}"

    # ── 其他模型价格匹配 ──

    @pytest.mark.parametrize("model, expected", [
        # DeepSeek
        ("deepseek-chat", (0.28, 0.42)),
        ("deepseek-reasoner", (0.28, 0.42)),
        ("deepseek-r1", (0.28, 0.42)),
        ("deepseek-v3", (0.28, 0.42)),

        # xAI
        ("grok-3", (3.0, 15.0)),
        ("grok-3-mini", (0.3, 0.5)),
        ("grok-2", (2.0, 10.0)),

        # Mistral
        ("mistral-large", (2.0, 6.0)),
        ("mistral-small", (0.15, 0.6)),
        ("codestral", (0.3, 0.9)),
        ("pixtral-large", (2.0, 6.0)),
        ("ministral-8b", (0.1, 0.1)),
        ("ministral-3b", (0.04, 0.04)),

        # Meta Llama
        ("llama-4-maverick", (0.15, 0.6)),
        ("llama-4-scout", (0.10, 0.3)),
        ("llama-3.3", (0.1, 0.3)),
        ("llama-3.1-405b", (0.9, 0.9)),
        ("llama-3.1-70b", (0.6, 0.6)),
        ("llama-3.1-8b", (0.05, 0.05)),

        # Other
        ("command-r-plus", (2.5, 10.0)),
        ("command-r", (0.15, 0.6)),
        ("qwen-max", (1.6, 6.4)),
        ("qwen-plus", (0.4, 1.2)),
        ("qwen-turbo", (0.05, 0.2)),
    ])
    def test_other_prices(self, model, expected):
        result = match_default_price(model)
        assert result == expected, f"{model}: got {result}, expected {expected}"

    # ── 不应匹配的模型 ──

    @pytest.mark.parametrize("model", [
        "some-unknown-model",
        "my-custom-llm",
        "claude-3",          # 太短，不应匹配任何 key
        "gpt-6",             # 不存在
        "o5",                # 不存在
        "gemini-4.0-ultra",  # 不存在
        "mistral-7b",        # 不在库中
        "phi-3-mini",
        "",
        None,
    ])
    def test_no_match(self, model):
        assert match_default_price(model) is None


# ═══════════════════════════════════════
# 最长前缀消歧测试
# ═══════════════════════════════════════

class TestLongestPrefixDisambiguation:
    """验证最长前缀匹配不会误选更短的前缀。"""

    @pytest.mark.parametrize("model, expected_price, should_not_match", [
        # gpt-4o-mini 不应匹配 gpt-4o
        ("gpt-4o-mini", (0.15, 0.6), (2.5, 10.0)),
        # gpt-4o-audio 不应匹配 gpt-4o
        ("gpt-4o-audio-preview", (2.5, 10.0), None),
        # o3-mini 不应匹配 o3
        ("o3-mini", (1.1, 4.4), (2.0, 8.0)),
        # o3-pro 不应匹配 o3
        ("o3-pro", (20.0, 80.0), (2.0, 8.0)),
        # gpt-4.1-mini 不应匹配 gpt-4.1
        ("gpt-4.1-mini", (0.4, 1.6), (2.0, 8.0)),
        # gpt-4.1-nano 不应匹配 gpt-4.1
        ("gpt-4.1-nano", (0.1, 0.4), (2.0, 8.0)),
        # claude-opus-4-6 不应匹配 claude-opus-4
        ("claude-opus-4-6", (5.0, 25.0), (5.0, 25.0)),
        # claude-opus-4-0 不应匹配 claude-opus-4
        ("claude-opus-4-0", (15.0, 75.0), (5.0, 25.0)),
        # gemini-2.5-flash-lite 不应匹配 gemini-2.5-flash
        ("gemini-2.5-flash-lite", (0.1, 0.4), (0.3, 2.5)),
        # gemini-2.0-flash-lite 不应匹配 gemini-2.0-flash
        ("gemini-2.0-flash-lite", (0.075, 0.3), (0.1, 0.4)),
        # gpt-5-mini 不应匹配 gpt-5
        ("gpt-5-mini", (0.25, 2.0), (1.25, 10.0)),
    ])
    def test_longest_prefix_wins(self, model, expected_price, should_not_match):
        result = match_default_price(model)
        assert result == expected_price, f"{model}: got {result}, expected {expected_price}"
        if should_not_match and should_not_match != expected_price:
            assert result != should_not_match, f"{model}: incorrectly matched shorter prefix"
