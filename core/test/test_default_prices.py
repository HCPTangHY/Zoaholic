"""
测试默认价格库和后缀归一化。
"""

import pytest
from core.model_name_utils import normalize_model_name
from core.default_prices import match_default_price, DEFAULT_MODEL_PRICES


# ═══════════════════════════════════════
# normalize_model_name 测试
# ═══════════════════════════════════════

class TestNormalizeModelName:
    """后缀归一化测试。"""

    # ── Claude 工具后缀 ──

    def test_claude_thinking(self):
        assert normalize_model_name("claude-sonnet-4-thinking") == "claude-sonnet-4"

    def test_claude_thinking_with_budget(self):
        assert normalize_model_name("claude-sonnet-4-thinking-32768") == "claude-sonnet-4"

    def test_claude_search(self):
        assert normalize_model_name("claude-sonnet-4-search") == "claude-sonnet-4"

    def test_claude_code(self):
        assert normalize_model_name("claude-sonnet-4-code") == "claude-sonnet-4"

    def test_claude_computer(self):
        assert normalize_model_name("claude-sonnet-4-computer") == "claude-sonnet-4"

    def test_claude_artifacts(self):
        assert normalize_model_name("claude-sonnet-4-artifacts") == "claude-sonnet-4"

    def test_claude_stacked_suffixes(self):
        assert normalize_model_name("claude-sonnet-4-thinking-32768-search") == "claude-sonnet-4"

    def test_claude_triple_stack(self):
        assert normalize_model_name("claude-sonnet-4-thinking-search-code") == "claude-sonnet-4"

    def test_claude_dated_with_thinking(self):
        assert normalize_model_name("claude-3-5-sonnet-20241022-thinking") == "claude-3-5-sonnet-20241022"

    # ── OpenAI reasoning 后缀 ──

    def test_oai_high(self):
        assert normalize_model_name("gpt-4o-high") == "gpt-4o"

    def test_oai_medium(self):
        assert normalize_model_name("o3-medium") == "o3"

    def test_oai_low(self):
        assert normalize_model_name("gpt-4o-mini-low") == "gpt-4o-mini"

    def test_oai_minimal(self):
        assert normalize_model_name("o3-minimal") == "o3"

    def test_oai_none(self):
        assert normalize_model_name("o3-none") == "o3"

    def test_oai_xhigh(self):
        assert normalize_model_name("o3-xhigh") == "o3"

    # ── Gemini 后缀 ──

    def test_gemini_think_budget(self):
        assert normalize_model_name("gemini-2.5-flash-think-8192") == "gemini-2.5-flash"

    def test_gemini_think_negative(self):
        assert normalize_model_name("gemini-2.5-flash-think--1") == "gemini-2.5-flash"

    def test_gemini_image(self):
        assert normalize_model_name("gemini-2.5-flash-image") == "gemini-2.5-flash"

    def test_gemini_image_generation(self):
        assert normalize_model_name("gemini-2.0-flash-exp-image-generation") == "gemini-2.0-flash-exp"

    # ── 不应被修改的模型名 ──

    def test_no_suffix_gpt4o(self):
        assert normalize_model_name("gpt-4o") == "gpt-4o"

    def test_no_suffix_deepseek(self):
        assert normalize_model_name("deepseek-chat") == "deepseek-chat"

    def test_no_suffix_o3(self):
        assert normalize_model_name("o3") == "o3"

    def test_no_false_positive_flow(self):
        """'some-model-flow' 不应被 -low 匹配（需要带 - 前缀）。"""
        assert normalize_model_name("some-model-flow") == "some-model-flow"

    def test_no_false_positive_acode(self):
        """'deepseek-acode' 不应被 -code 匹配。"""
        # -code 检测的是 endswith("-code")，而 "-acode" 不以 "-code" 结尾... 等等，实际上它是以 "code" 结尾的
        # 但因为我们检测的是 lower.endswith("-code")，而 "deepseek-acode" 确实以 "-acode" 结尾，不是 "-code"
        # 所以不会被误匹配
        assert normalize_model_name("deepseek-acode") == "deepseek-acode"

    def test_gemini_image_preview_stripped(self):
        """gemini-2.5-flash-image-preview: -preview 剥离后 -image 也会被剥离。"""
        assert normalize_model_name("gemini-2.5-flash-image-preview") == "gemini-2.5-flash"

    # ── 边界情况 ──

    def test_empty_string(self):
        assert normalize_model_name("") == ""

    def test_none(self):
        assert normalize_model_name(None) is None

    def test_non_string(self):
        assert normalize_model_name(123) == 123

    # ── 审查补充用例 ──

    def test_double_suffix(self):
        """双重相同后缀迭代剥离。"""
        assert normalize_model_name("o3-high-high") == "o3"

    def test_thinking_zero_budget(self):
        assert normalize_model_name("claude-sonnet-4-thinking-0") == "claude-sonnet-4"

    def test_case_insensitive_simple(self):
        """简单后缀大小写不敏感。"""
        assert normalize_model_name("gpt-4o-HIGH") == "gpt-4o"

    def test_case_insensitive_thinking(self):
        assert normalize_model_name("claude-sonnet-4-THINKING") == "claude-sonnet-4"

    def test_case_insensitive_gemini_think(self):
        assert normalize_model_name("gemini-2.5-flash-THINK-8192") == "gemini-2.5-flash"

    def test_gemini_think_plus_search(self):
        """Gemini think + 简单后缀叠加。"""
        assert normalize_model_name("gemini-2.5-flash-think-8192-search") == "gemini-2.5-flash"

    def test_bare_think_no_number(self):
        """裸 -think（无数字）不应被 gemini regex 匹配，保持不变。"""
        assert normalize_model_name("gemini-2.5-flash-think") == "gemini-2.5-flash-think"

    def test_thinking_trailing_dash(self):
        """-thinking- 尾部无数字，regex 不匹配。"""
        assert normalize_model_name("model-thinking-") == "model-thinking-"


# ═══════════════════════════════════════
# match_default_price 测试
# ═══════════════════════════════════════

class TestMatchDefaultPrice:
    """默认价格库查找测试。"""

    # ── 精确匹配 ──

    def test_exact_gpt4o(self):
        assert match_default_price("gpt-4o") == (2.5, 10.0)

    def test_exact_claude_sonnet_4(self):
        assert match_default_price("claude-sonnet-4") == (3.0, 15.0)

    def test_exact_gemini_25_pro(self):
        assert match_default_price("gemini-2.5-pro") == (1.25, 10.0)

    def test_exact_o3(self):
        assert match_default_price("o3") == (2.0, 8.0)

    def test_exact_deepseek_chat(self):
        assert match_default_price("deepseek-chat") == (0.28, 0.42)

    # ── 前缀匹配（dated 版本号） ──

    def test_prefix_claude_dated(self):
        result = match_default_price("claude-3-5-sonnet-20241022")
        assert result == (3.0, 15.0)

    def test_prefix_gpt4o_dated(self):
        result = match_default_price("gpt-4o-2024-08-06")
        assert result == (2.5, 10.0)

    def test_prefix_gpt41_dated(self):
        result = match_default_price("gpt-4.1-2025-04-14")
        assert result == (2.0, 8.0)

    # ── 后缀归一化 + 匹配 ──

    def test_suffix_gpt4o_high(self):
        result = match_default_price("gpt-4o-high")
        assert result == (2.5, 10.0)

    def test_suffix_claude_thinking_search(self):
        result = match_default_price("claude-sonnet-4-thinking-32768-search")
        assert result == (3.0, 15.0)

    def test_suffix_o3_medium(self):
        result = match_default_price("o3-medium")
        assert result == (2.0, 8.0)

    def test_suffix_gemini_think(self):
        result = match_default_price("gemini-2.5-flash-think-8192")
        assert result == (0.3, 2.5)

    # ── 后缀 + 前缀组合 ──

    def test_dated_plus_suffix(self):
        result = match_default_price("claude-3-5-sonnet-20241022-thinking-search")
        assert result == (3.0, 15.0)

    # ── 最长前缀优先 ──

    def test_longest_prefix_gpt4o_mini(self):
        """gpt-4o-mini 应匹配 'gpt-4o-mini' 而非 'gpt-4o'。"""
        result = match_default_price("gpt-4o-mini")
        assert result == (0.15, 0.6)

    def test_longest_prefix_o3_mini(self):
        """o3-mini 应匹配 'o3-mini' 而非 'o3'。"""
        result = match_default_price("o3-mini")
        assert result == (1.1, 4.4)

    def test_longest_prefix_claude_opus_46(self):
        """claude-opus-4-6-xxx 应匹配 'claude-opus-4-6' 而非 'claude-opus-4'。"""
        result = match_default_price("claude-opus-4-6-20260101")
        assert result == (5.0, 25.0)

    def test_longest_prefix_claude_opus_40(self):
        """claude-opus-4-0-xxx 应匹配 'claude-opus-4-0'（$15/$75）。"""
        result = match_default_price("claude-opus-4-0-20250514")
        assert result == (15.0, 75.0)

    # ── 未知模型 ──

    def test_unknown_model(self):
        assert match_default_price("some-unknown-model-v2") is None

    def test_empty(self):
        assert match_default_price("") is None

    def test_none(self):
        assert match_default_price(None) is None

    def test_non_string(self):
        """非字符串输入不应崩溃。"""
        assert match_default_price(123) is None

    # ── 图像模型 ──

    def test_gpt_image_1_5(self):
        assert match_default_price("gpt-image-1.5") == (5.0, 32.0)

    def test_gpt_image_1(self):
        assert match_default_price("gpt-image-1") == (10.0, 40.0)

    def test_gpt_image_1_mini(self):
        assert match_default_price("gpt-image-1-mini") == (2.0, 8.0)

    def test_gemini_image_preview_price(self):
        """gemini-2.5-flash-image-preview 匹配 gemini-2.5-flash-image（图像输出费率）。"""
        result = match_default_price("gemini-2.5-flash-image-preview")
        assert result == (0.3, 30.0)

    # ── 审查补充用例 ──

    def test_suffix_gpt41_nano_high(self):
        """后缀剥离 + 最长前缀。"""
        result = match_default_price("gpt-4.1-nano-high")
        assert result == (0.1, 0.4)

    def test_suffix_o1_pro_high(self):
        """高费用模型后缀剥离。"""
        result = match_default_price("o1-pro-high")
        assert result == (150.0, 600.0)

    def test_case_insensitive_lookup(self):
        """大小写不敏感查找。"""
        result = match_default_price("GPT-4o")
        assert result == (2.5, 10.0)

    def test_mixed_case_with_suffix(self):
        result = match_default_price("Claude-Sonnet-4-THINKING")
        assert result == (3.0, 15.0)

    def test_partial_prefix_no_match(self):
        """'claude-3' 短于所有 key，不应匹配。"""
        assert match_default_price("claude-3") is None

    def test_audio_prefix_disambiguation(self):
        """gpt-4o-audio-preview 应匹配 'gpt-4o-audio' 而非 'gpt-4o'。"""
        result = match_default_price("gpt-4o-audio-preview")
        assert result == (2.5, 10.0)  # gpt-4o-audio 的价格


# ═══════════════════════════════════════
# 价格库完整性检查
# ═══════════════════════════════════════

class TestPriceDbIntegrity:
    """价格库数据完整性。"""

    def test_all_prices_are_tuples(self):
        for model, price in DEFAULT_MODEL_PRICES.items():
            assert isinstance(price, tuple), f"{model}: price is not a tuple"
            assert len(price) == 2, f"{model}: price tuple length != 2"

    def test_all_prices_non_negative(self):
        for model, (prompt, completion) in DEFAULT_MODEL_PRICES.items():
            assert prompt >= 0, f"{model}: negative prompt price {prompt}"
            assert completion >= 0, f"{model}: negative completion price {completion}"

    def test_all_keys_are_lowercase(self):
        for model in DEFAULT_MODEL_PRICES:
            assert model == model.lower(), f"{model}: key should be lowercase"

    def test_no_trailing_spaces(self):
        for model in DEFAULT_MODEL_PRICES:
            assert model == model.strip(), f"'{model}': key has trailing spaces"

    def test_major_providers_covered(self):
        """确保主要 provider 都有覆盖。"""
        keys = list(DEFAULT_MODEL_PRICES.keys())
        keys_str = " ".join(keys)
        assert "gpt-4" in keys_str, "Missing OpenAI GPT-4 family"
        assert "claude-" in keys_str, "Missing Anthropic Claude"
        assert "gemini-" in keys_str, "Missing Google Gemini"
        assert "deepseek" in keys_str, "Missing DeepSeek"
        assert "grok" in keys_str, "Missing xAI Grok"
        assert "gpt-image" in keys_str, "Missing OpenAI Image Generation"
