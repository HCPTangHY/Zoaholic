"""
内置默认模型价格库。

价格单位：USD / 百万 tokens (prompt_price, completion_price)。
最后更新：2026-04-08

维护说明：
  - 加模型：在对应 provider 区域加一行
  - 改价格：直接改 tuple 里的数字
  - 更新后修改 PRICES_LAST_UPDATED 日期
  - key 是模型名前缀，匹配时取最长前缀命中
  - 同一模型的不同版本号（如 -20250514）通过前缀匹配自动覆盖
"""

import re

from core.model_name_utils import normalize_model_name

# 元数据
PRICES_LAST_UPDATED = "2026-04-08"

# 渠道前缀中可能出现的括号包裹模式：【xx】、[xx]、(xx)、（xx）
_BRACKET_PREFIX_RE = re.compile(
    r"^(?:[\[【（(][^\]】）)]*[\]】）)])+[-_]?"
)

# key = 模型名前缀（规范名，不含插件后缀）
# value = (prompt_price_per_M_tokens, completion_price_per_M_tokens)
DEFAULT_MODEL_PRICES: dict[str, tuple[float, float]] = {

    # ═══════════════════════════════════════
    # OpenAI — GPT-5 系列
    # ═══════════════════════════════════════
    "gpt-5.2-pro":          (21.0, 168.0),
    "gpt-5.2":              (1.75, 14.0),
    "gpt-5.1":              (1.25, 10.0),
    "gpt-5-mini":           (0.25, 2.0),
    "gpt-5-nano":           (0.05, 0.4),
    "gpt-5":                (1.25, 10.0),

    # ═══════════════════════════════════════
    # OpenAI — GPT-4 系列
    # ═══════════════════════════════════════
    "gpt-4.1-mini":         (0.4, 1.6),
    "gpt-4.1-nano":         (0.1, 0.4),
    "gpt-4.1":              (2.0, 8.0),
    "gpt-4o-audio":         (2.5, 10.0),
    "gpt-4o-mini":          (0.15, 0.6),
    "gpt-4o":               (2.5, 10.0),
    "gpt-4-turbo":          (10.0, 30.0),
    "gpt-4":                (30.0, 60.0),
    "gpt-3.5-turbo":        (0.5, 1.5),
    "chatgpt-4o-latest":    (5.0, 15.0),   # deprecated by OpenAI

    # ═══════════════════════════════════════
    # OpenAI — 图像生成
    # 统一按 1000 completion tokens = 1 张图换算：
    # completion_price = 实际每图成本 × 1000
    # ═══════════════════════════════════════
    "gpt-image-1.5":        (5.0, 32.0),    # $0.032/img
    "gpt-image-1-mini":     (2.0, 8.0),     # $0.008/img
    "gpt-image-1":          (10.0, 40.0),   # $0.040/img

    # ═══════════════════════════════════════
    # OpenAI — O 系列（推理）
    # ═══════════════════════════════════════
    "o1-pro":               (150.0, 600.0),
    "o1-mini":              (1.1, 4.4),
    "o1":                   (15.0, 60.0),
    "o3-pro":               (20.0, 80.0),
    "o3-mini":              (1.1, 4.4),
    "o3":                   (2.0, 8.0),
    "o4-mini":              (1.1, 4.4),

    # ═══════════════════════════════════════
    # Anthropic — Claude 4.x
    # ═══════════════════════════════════════
    # 4.6 Fast Mode（6× 标准价，2.5x 推理速度）
    "claude-opus-4.6-fast":     (30.0, 150.0),
    "claude-opus-4-6-fast":     (30.0, 150.0),
    # 4.6 系列（最新，大幅降价）
    "claude-opus-4-6":      (5.0, 25.0),
    "claude-opus-4.6":      (5.0, 25.0),
    "claude-sonnet-4-6":    (3.0, 15.0),
    "claude-sonnet-4.6":    (3.0, 15.0),
    # 4.0 系列
    "claude-opus-4-0":      (15.0, 75.0),
    "claude-sonnet-4-0":    (3.0, 15.0),
    # 无版本号别名（指向最新 4.6）
    "claude-opus-4":        (5.0, 25.0),
    "claude-sonnet-4":      (3.0, 15.0),
    # Haiku 4.5
    "claude-haiku-4":       (1.0, 5.0),

    # ═══════════════════════════════════════
    # Anthropic — Claude 3.x
    # ═══════════════════════════════════════
    # 3.7 Sonnet（两种命名风格）
    "claude-3.7-sonnet":    (3.0, 15.0),
    "claude-3-7-sonnet":    (3.0, 15.0),
    # 3.5 Sonnet（两种命名风格）
    "claude-3.5-sonnet":    (3.0, 15.0),
    "claude-3-5-sonnet":    (3.0, 15.0),
    # 3.5 Haiku（两种命名风格）
    "claude-3.5-haiku":     (0.8, 4.0),
    "claude-3-5-haiku":     (0.8, 4.0),
    # 3.0 系列
    "claude-3-opus":        (15.0, 75.0),
    "claude-3-sonnet":      (3.0, 15.0),
    "claude-3-haiku":       (0.25, 1.25),

    # ═══════════════════════════════════════
    # Google — Gemini 3.x（预览）
    # ═══════════════════════════════════════
    "gemini-3.1-pro":           (2.0, 12.0),
    "gemini-3.1-flash-lite":    (0.25, 1.5),
    "gemini-3-pro":             (2.0, 12.0),
    "gemini-3-flash":           (0.5, 3.0),

    # ═══════════════════════════════════════
    # Google — Gemini 图像生成
    # 统一按 1000 completion tokens = 1 张图换算：
    # completion_price = 实际每图成本 × 1000
    # ═══════════════════════════════════════
    "gemini-3-pro-image":       (2.0, 134.0),   # $0.134/img
    "gemini-2.5-flash-image":   (0.3, 39.0),    # $0.039/img

    # ═══════════════════════════════════════
    # Google — Gemini 2.5
    # ═══════════════════════════════════════
    # 价格为 ≤200K context 标准档
    "gemini-2.5-pro":       (1.25, 10.0),
    "gemini-2.5-flash-lite": (0.1, 0.4),
    "gemini-2.5-flash":     (0.3, 2.5),

    # ═══════════════════════════════════════
    # Google — Gemini 2.0 / 1.5
    # ═══════════════════════════════════════
    "gemini-2.0-flash-lite": (0.075, 0.3),
    "gemini-2.0-flash":     (0.1, 0.4),
    "gemini-1.5-pro":       (1.25, 5.0),
    "gemini-1.5-flash":     (0.075, 0.3),

    # ═══════════════════════════════════════
    # DeepSeek — V3.2 统一定价（2026-03）
    # ═══════════════════════════════════════
    "deepseek-chat":        (0.28, 0.42),
    "deepseek-reasoner":    (0.28, 0.42),
    "deepseek-r1":          (0.28, 0.42),
    "deepseek-v3":          (0.28, 0.42),

    # ═══════════════════════════════════════
    # xAI (Grok)
    # ═══════════════════════════════════════
    "grok-3-mini":          (0.3, 0.5),
    "grok-3":               (3.0, 15.0),
    "grok-2":               (2.0, 10.0),

    # ═══════════════════════════════════════
    # Mistral
    # ═══════════════════════════════════════
    "mistral-large":        (2.0, 6.0),
    "mistral-small":        (0.15, 0.6),
    "codestral":            (0.3, 0.9),
    "pixtral-large":        (2.0, 6.0),
    "ministral-8b":         (0.1, 0.1),
    "ministral-3b":         (0.04, 0.04),

    # ═══════════════════════════════════════
    # Meta Llama (via providers, 取主流 provider 均价)
    # ═══════════════════════════════════════
    "llama-4-maverick":     (0.15, 0.6),
    "llama-4-scout":        (0.10, 0.3),
    "llama-3.3":            (0.1, 0.3),
    "llama-3.1-405b":       (0.9, 0.9),
    "llama-3.1-70b":        (0.6, 0.6),
    "llama-3.1-8b":         (0.05, 0.05),

    # ═══════════════════════════════════════
    # 其他
    # ═══════════════════════════════════════
    "command-r-plus":       (2.5, 10.0),
    "command-r":            (0.15, 0.6),
    "qwen-max":             (1.6, 6.4),
    "qwen-plus":            (0.4, 1.2),
    "qwen-turbo":           (0.05, 0.2),
}


# 图像模型统一注入的 completion tokens（1000 tokens = 1 张图）
IMAGE_TOKENS_PER_REQUEST = 1000


def _match_in_db(model_name: str, db: dict) -> object | None:
    """
    通用匹配引擎：在给定 dict 中查找模型价格 / token 估算。

    策略：原始名 + 归一化名都尝试，取最长 key（word boundary）。
    未命中时逐段剥离渠道前缀重试。
    """
    if not model_name or not isinstance(model_name, str):
        return None

    clean = _BRACKET_PREFIX_RE.sub("", model_name)
    raw_lower = clean.lower()
    normalized_lower = normalize_model_name(clean).lower()

    candidates = [raw_lower]
    if normalized_lower != raw_lower:
        candidates.append(normalized_lower)

    best_result = None
    best_len = -1

    def _update(name: str):
        nonlocal best_result, best_len
        if name in db and len(name) > best_len:
            best_result = db[name]
            best_len = len(name)
        for k, v in db.items():
            if name.startswith(k) and len(k) > best_len:
                rest = name[len(k):]
                if not rest or rest[0] == "-":
                    best_result = v
                    best_len = len(k)

    for c in candidates:
        _update(c)
    if best_result is not None:
        return best_result

    for c in candidates:
        stripped = c
        while "-" in stripped:
            stripped = stripped[stripped.index("-") + 1:]
            _update(stripped)
    return best_result


def match_default_price(model_name: str):
    """
    从内置默认价格库中查找模型价格。

    Returns:
        (prompt_price, completion_price) 元组，或 None
    """
    return _match_in_db(model_name, DEFAULT_MODEL_PRICES)
