"""content_word_filter 插件回归测试（离线，不依赖运行中的服务）。"""
import asyncio

import pytest
from fastapi import HTTPException

from core.models import ContentItem, ImageUrl, Message, RequestModel
from plugins import content_word_filter as cwf


def enabled(**params):
    """构造结构化（dict）形式的 enabled_plugins。"""
    return [{"name": "content_word_filter", "params": params}]


def run(request_data, enabled_plugins):
    return cwf.apply_word_filter(request_data, enabled_plugins)


def chat(*messages):
    return RequestModel(model="gpt-4", messages=list(messages))


def dict_chat(*messages):
    return {"model": "gpt-4", "messages": list(messages)}


# ==================== 参数解析 ====================


def test_parse_comma_separated_string_options():
    raw = cwf._parse_options_string("mode=replace,words=bad1|bad2,replacement=***")
    cfg = cwf._build_config(raw)
    assert cfg.mode == "replace"
    assert cfg.words == ("bad1", "bad2")
    assert cfg.replacement == "***"


def test_parse_multiline_textarea_words():
    raw = cwf._parse_options_string("mode=strip\nwords=bad1\nbad2\nbad3\nreplacement=#")
    cfg = cwf._build_config(raw)
    assert cfg.mode == "strip"
    assert cfg.words == ("bad1", "bad2", "bad3")
    assert cfg.replacement == "#"


def test_parse_dict_options_with_list_words():
    cfg = cwf._build_config({"words": ["a", "b", "a"], "case_sensitive": "true"})
    assert cfg.words == ("a", "b")
    assert cfg.case_sensitive is True


def test_invalid_mode_and_match_type_fall_back():
    cfg = cwf._build_config({"mode": "nope", "match_type": "nope", "words": "x"})
    assert cfg.mode == "replace"
    assert cfg.match_type == "substring"


def test_scope_supports_multiple_roles():
    cfg = cwf._build_config({"scope": "user,system", "words": "x"})
    assert set(cfg.scopes) == {"user", "system"}
    assert cfg.covers("user") and cfg.covers("system")
    assert not cfg.covers("assistant")


def test_plugin_not_enabled_is_noop():
    request = chat(Message(role="user", content="hello bad"))
    run(request, ["some_other_plugin:foo=1"])
    assert request.messages[0].content == "hello bad"


# ==================== pydantic / dict 双路径 ====================


def test_replace_on_pydantic_request_model():
    request = chat(Message(role="user", content="hello badword here"))
    run(request, enabled(mode="replace", words="badword", replacement="***"))
    assert request.messages[0].content == "hello *** here"


def test_replace_on_plain_dict_payload():
    request = dict_chat({"role": "user", "content": "hello badword here"})
    run(request, enabled(mode="replace", words="badword", replacement="***"))
    assert request["messages"][0]["content"] == "hello *** here"


def test_string_option_form_works_end_to_end():
    request = chat(Message(role="user", content="hello badword here"))
    run(request, ["content_word_filter:mode=replace,words=badword,replacement=***"])
    assert request.messages[0].content == "hello *** here"


def test_non_chat_request_without_messages_is_untouched():
    payload = {"model": "dall-e-3", "prompt": "badword"}
    assert run(payload, enabled(words="badword")) is payload
    assert payload["prompt"] == "badword"


# ==================== 多模态 content ====================


def test_multimodal_blocks_are_filtered_independently():
    request = chat(
        Message(
            role="user",
            content=[
                ContentItem(type="text", text="aaa badword"),
                ContentItem(type="text", text="bbb"),
            ],
        )
    )
    run(request, enabled(mode="replace", words="badword", replacement="***"))
    assert [item.text for item in request.messages[0].content] == ["aaa ***", "bbb"]


def test_dict_content_blocks_are_filtered():
    request = dict_chat(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "aaa badword"},
                {"type": "text", "text": "bbb"},
            ],
        }
    )
    run(request, enabled(mode="replace", words="badword", replacement="***"))
    assert [b["text"] for b in request["messages"][0]["content"]] == ["aaa ***", "bbb"]


def test_image_blocks_are_preserved():
    request = chat(
        Message(
            role="user",
            content=[
                ContentItem(type="text", text="badword"),
                ContentItem(type="image_url", image_url=ImageUrl(url="http://x/y.png")),
            ],
        )
    )
    run(request, enabled(mode="strip", words="badword"))
    blocks = request.messages[0].content
    assert len(request.messages) == 1
    assert [b.type for b in blocks] == ["image_url"]


def test_unknown_content_type_is_not_stringified():
    sentinel = {"weird": "badword"}
    request = dict_chat({"role": "user", "content": sentinel})
    run(request, enabled(mode="replace", words="badword"))
    assert request["messages"][0]["content"] is sentinel


# ==================== 三种模式 ====================


def test_strip_removes_word_and_empty_message():
    request = chat(
        Message(role="user", content="badword"),
        Message(role="user", content="keep me"),
    )
    run(request, enabled(mode="strip", words="badword"))
    assert [m.content for m in request.messages] == ["keep me"]


def test_strip_keeps_message_when_disabled():
    request = chat(Message(role="user", content="badword"))
    run(request, enabled(mode="strip", words="badword", strip_empty_message="false"))
    assert [m.content for m in request.messages] == [""]


def test_strip_never_empties_messages_entirely():
    request = chat(Message(role="user", content="badword"))
    run(request, enabled(mode="strip", words="badword"))
    assert len(request.messages) == 1
    assert request.messages[0].content == ""


def test_strip_keeps_tool_messages():
    request = chat(
        Message(role="user", content="hi"),
        Message(role="tool", content="badword", tool_call_id="call_1"),
    )
    run(request, enabled(mode="strip", words="badword", scope="all"))
    assert len(request.messages) == 2
    assert request.messages[1].content == ""


def test_reject_raises_http_exception():
    request = chat(Message(role="user", content="hello badword"))
    with pytest.raises(HTTPException) as exc:
        run(request, enabled(mode="reject", words="badword"))
    assert exc.value.status_code == 400
    assert exc.value.detail["error"]["code"] == "content_word_filter"


def test_reject_status_and_message_are_configurable():
    request = chat(Message(role="user", content="hello badword"))
    with pytest.raises(HTTPException) as exc:
        run(request, enabled(mode="reject", words="badword", reject_status="451", reject_message="nope"))
    assert exc.value.status_code == 451
    assert exc.value.detail["error"]["message"] == "nope"


def test_reject_does_not_trigger_without_hit():
    request = chat(Message(role="user", content="clean text"))
    run(request, enabled(mode="reject", words="badword"))
    assert request.messages[0].content == "clean text"


# ==================== 匹配语义 ====================


def test_scope_limits_roles():
    request = chat(
        Message(role="system", content="badword"),
        Message(role="user", content="badword"),
    )
    run(request, enabled(mode="replace", words="badword", replacement="*", scope="user"))
    assert request.messages[0].content == "badword"
    assert request.messages[1].content == "*"


def test_top_level_system_field_is_filtered():
    request = chat(Message(role="user", content="hi"))
    request.system = "you are badword"
    run(request, enabled(mode="replace", words="badword", replacement="*", scope="system"))
    assert request.system == "you are *"


def test_word_mode_matches_cjk():
    request = chat(Message(role="user", content="这是屏蔽词测试"))
    run(request, enabled(mode="replace", words="屏蔽词", replacement="**", match_type="word"))
    assert request.messages[0].content == "这是**测试"


def test_word_mode_respects_ascii_boundaries():
    request = chat(Message(role="user", content="bad badly"))
    run(request, enabled(mode="replace", words="bad", replacement="*", match_type="word"))
    assert request.messages[0].content == "* badly"


def test_substring_mode_matches_inside_words():
    request = chat(Message(role="user", content="badly"))
    run(request, enabled(mode="replace", words="bad", replacement="*", match_type="substring"))
    assert request.messages[0].content == "*ly"


def test_case_insensitive_by_default():
    request = chat(Message(role="user", content="BadWord"))
    run(request, enabled(mode="replace", words="badword", replacement="*"))
    assert request.messages[0].content == "*"


def test_case_sensitive_option():
    request = chat(Message(role="user", content="BadWord"))
    run(request, enabled(mode="replace", words="badword", replacement="*", case_sensitive="true"))
    assert request.messages[0].content == "BadWord"


def test_replacement_is_literal_not_regex_template():
    request = chat(Message(role="user", content="path bad here"))
    run(request, enabled(mode="replace", words="bad", replacement=r"C:\new"))
    assert request.messages[0].content == r"path C:\new here"


def test_pattern_mode_regex():
    request = chat(Message(role="user", content="call 13800138000 now"))
    run(request, enabled(mode="replace", words=r"\d{11}", replacement="[phone]", match_type="pattern"))
    assert request.messages[0].content == "call [phone] now"


def test_pattern_mode_skips_invalid_and_empty_matching():
    assert cwf._build_matchers(("[unclosed",), False, "pattern") == ()
    assert cwf._build_matchers(("a*",), False, "pattern") == ()
    assert cwf._build_matchers(("x" * (cwf.MAX_PATTERN_LENGTH + 1),), False, "pattern") == ()


def test_matchers_are_cached():
    cwf._build_matchers.cache_clear()
    first = cwf._build_matchers(("bad",), False, "substring")
    second = cwf._build_matchers(("bad",), False, "substring")
    assert first is second
    assert cwf._build_matchers.cache_info().hits >= 1


# ==================== 拦截器入口 ====================


def test_inbound_interceptor_entrypoint():
    request = chat(Message(role="user", content="hello badword"))
    asyncio.run(
        cwf.content_word_filter_inbound(request, None, {}, enabled(mode="replace", words="badword", replacement="*"))
    )
    assert request.messages[0].content == "hello *"


def test_channel_inbound_interceptor_entrypoint():
    request = chat(Message(role="user", content="hello badword"))
    asyncio.run(
        cwf.content_word_filter_channel_inbound(
            request, None, {}, {}, enabled(mode="replace", words="badword", replacement="*")
        )
    )
    assert request.messages[0].content == "hello *"


# ==================== 框架层：两个阶段的拒绝语义对齐 ====================


@pytest.fixture
def registered_plugin():
    cwf.setup(None)
    yield
    cwf.teardown(None)


def test_registry_inbound_reject_propagates(registered_plugin):
    from core.plugins.interceptors import apply_inbound_interceptors

    request = chat(Message(role="user", content="hello badword"))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(apply_inbound_interceptors(request, None, {}, ["content_word_filter:mode=reject,words=badword"]))
    assert exc.value.status_code == 400


def test_registry_channel_inbound_reject_propagates(registered_plugin):
    from core.plugins.interceptors import apply_channel_inbound_interceptors

    request = chat(Message(role="user", content="hello badword"))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            apply_channel_inbound_interceptors(
                request, None, {}, {}, ["content_word_filter:mode=reject,words=badword,reject_status=451"]
            )
        )
    assert exc.value.status_code == 451


def test_registry_channel_inbound_generic_error_still_swallowed():
    from core.plugins.interceptors import (
        apply_channel_inbound_interceptors,
        register_channel_inbound_interceptor,
        unregister_channel_inbound_interceptor,
    )

    async def boom(request_data, request, provider, api_key_info, enabled_plugins):
        raise RuntimeError("boom")

    register_channel_inbound_interceptor("test_boom", boom, plugin_name="test_boom_plugin")
    try:
        request = chat(Message(role="user", content="hi"))
        result = asyncio.run(apply_channel_inbound_interceptors(request, None, {}, {}, ["test_boom_plugin"]))
        assert result is request
    finally:
        unregister_channel_inbound_interceptor("test_boom")
