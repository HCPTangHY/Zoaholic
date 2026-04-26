"""
模型名称归一化工具。

剥离插件附加的后缀（-thinking、-search、-high 等），
使价格匹配能找到基础模型。
此模块是已知后缀模式的唯一来源。
"""

import re

# 编译一次，模块加载时执行
_THINKING_BUDGET_RE = re.compile(r"-thinking(?:-\d+)?$", re.IGNORECASE)
_GEMINI_THINK_RE = re.compile(r"-think--?\d+$", re.IGNORECASE)

# 简单后缀，按长度降序排列（长的先检测，避免部分匹配）
_SIMPLE_SUFFIXES = (
    "-image-generation",
    "-artifacts",
    "-computer",
    "-preview",
    "-search",
    "-image",
    "-code",
    "-xhigh",
    "-minimal",
    "-medium",
    "-high",
    "-none",
    "-low",
)


def normalize_model_name(model: str) -> str:
    """
    剥离已知的插件后缀，返回基础模型名。

    支持的后缀模式：
      - Claude 工具:   -thinking(-N)?, -search, -code, -computer, -artifacts
      - OpenAI 推理:   -high, -medium, -low, -minimal, -none, -xhigh
      - Gemini 思考:   -think-N (N 可为负数)
      - Gemini 图片:   -image, -image-generation

    多个后缀可叠加（如 -thinking-32768-search），会迭代剥离。

    Examples:
        >>> normalize_model_name("claude-sonnet-4-thinking-32768-search")
        'claude-sonnet-4'
        >>> normalize_model_name("gpt-4o-high")
        'gpt-4o'
        >>> normalize_model_name("gemini-2.5-flash-think-8192")
        'gemini-2.5-flash'
        >>> normalize_model_name("gpt-4o")
        'gpt-4o'
    """
    if not isinstance(model, str) or not model:
        return model

    result = model
    changed = True
    while changed:
        changed = False

        # 1. Claude thinking（含可选预算）: -thinking 或 -thinking-N
        m = _THINKING_BUDGET_RE.search(result)
        if m:
            result = result[:m.start()]
            changed = True
            continue

        # 2. Gemini thinking 预算: -think-N（N 可为负数）
        m = _GEMINI_THINK_RE.search(result)
        if m:
            result = result[:m.start()]
            changed = True
            continue

        # 3. 简单字符串后缀
        lower = result.lower()
        for suffix in _SIMPLE_SUFFIXES:
            if lower.endswith(suffix):
                result = result[:-len(suffix)]
                changed = True
                break

    return result
