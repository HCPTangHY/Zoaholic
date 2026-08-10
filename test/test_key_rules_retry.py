import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.key_rules import apply_key_rule_retry_override, match_key_rules, resolve_key_rules


def test_key_rules_preserve_boolean_retry_and_match_result():
    """retry 是规则动作的一部分，规范化和匹配结果都必须保留布尔三态中的显式 true/false。"""
    rules = resolve_key_rules({
        "key_rules": [
            {"match": {"status": [429]}, "duration": 30, "retry": True},
            {"match": {"keyword": ["quota"]}, "duration": 0, "retry": False},
        ]
    })

    assert rules[0]["retry"] is True
    assert rules[1]["retry"] is False
    assert match_key_rules(rules, 429, "rate limited")["retry"] is True
    assert match_key_rules(rules, 500, "quota exceeded")["retry"] is False


def test_key_rules_omit_absent_or_non_boolean_retry():
    """retry 缺失表示走默认硬编码逻辑，非布尔值不应被误解释为强制重试或禁止重试。"""
    rules = resolve_key_rules({
        "key_rules": [
            {"match": {"status": [500]}, "duration": 3},
            {"match": {"status": [502]}, "duration": 3, "retry": "false"},
        ]
    })

    assert "retry" not in rules[0]
    assert "retry" not in rules[1]
    assert "retry" not in match_key_rules(rules, 500, "server error")
    assert "retry" not in match_key_rules(rules, 502, "bad gateway")


def test_apply_key_rule_retry_override_three_state():
    """handler 在默认重试判断之后调用该 helper，使规则中的 true/false 覆盖默认值，缺失时保持原值。"""
    assert apply_key_rule_retry_override({"retry": True}, False) is True
    assert apply_key_rule_retry_override({"retry": False}, True) is False
    assert apply_key_rule_retry_override({}, True) is True
    assert apply_key_rule_retry_override(None, False) is False
