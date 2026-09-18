import os
import sys
from time import time

import pytest
from fastapi.responses import JSONResponse, StreamingResponse

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.channels.registry import register_channel, unregister_channel
from routes import channels as channels_route


class _DummyApp:
    """为渠道测试 helper 提供最小 app，避免单元测试依赖真实服务进程。"""

    def __init__(self):
        # 修改原因：_build_test_provider 只需要读取 app 参数以保持与路由调用签名一致。
        # 修改方式：提供带 state.config 的最小对象，不启动真实 FastAPI 应用。
        # 目的：让测试专注于 provider 构建、请求构建和响应提取的稳定行为。
        self.state = type("State", (), {"config": {}})()


def test_key_candidate_helpers_preserve_string_dict_and_disabled_shapes():
    """测试 Key 归一化，覆盖字符串、对象和禁用标记三种输入格式。"""
    # 修改原因：/v1/channels/test 需要兼容前端保存态、逐 Key 测试态和旧格式。
    # 修改方式：直接断言模块级 helper 的归一化结果，不经过网络请求。
    # 目的：重构后保留原来对 string、dict、list 形式 Key 的兼容能力。
    assert channels_route._normalize_key_item("  sk-live  ") == "sk-live"
    assert channels_route._normalize_key_item({"key": " sk-disabled ", "disabled": True}) == "!sk-disabled"
    assert channels_route._normalize_key_item({"sk-labeled": "main"}) == "sk-labeled"
    assert channels_route._collect_key_candidates([
        "!sk-off",
        {"key": " sk-on ", "disabled": False},
        {"sk-dict": "label"},
    ]) == ["!sk-off", "sk-on", "sk-dict"]


def test_build_test_provider_cleans_snapshot_selects_key_and_resolves_prefixed_alias():
    """测试 provider 构建，确保运行时字段清理、Key 选择和前缀别名都保留。"""
    # 修改原因：重构把原先集中在 test_channel 内的 provider 构建逻辑移出函数。
    # 修改方式：注册临时渠道，构造包含运行时字段、Key 池、model_prefix 和别名的快照。
    # 目的：防止后续改动重新把运行时字段带入 handler，或破坏前缀模型测试。
    engine = "channel-test-route-helper"
    unregister_channel(engine)
    register_channel(id=engine, type_name="openai", default_base_url="https://default.example/v1")
    try:
        test_config = {
            "engine": engine,
            "provider_snapshot": {
                "provider": "snapshot-provider",
                "engine": engine,
                "base_url": "api.example.test/v1/",
                "api": ["!sk-disabled", {"sk-active": "primary"}],
                "api_keys": [{"key": "sk-later", "disabled": False}],
                "model_prefix": "[p]",
                "model": [{"real-model": "alias-model"}],
                "sub_channels": [{"engine": "openai"}],
                "_is_sub_channel": True,
                "_parent_provider": {"provider": "parent"},
                "_model_dict_cache": {"stale": "stale"},
                "_virtual_route_provider": True,
                "_virtual_priority": 9,
            },
            "model": "alias-model",
            "upstream_model": "real-model",
        }

        provider, selected_api_key, resolved_engine = channels_route._build_test_provider(test_config, _DummyApp())

        assert resolved_engine == engine
        assert selected_api_key == "sk-active"
        assert provider["api"] == "sk-active"
        assert provider["base_url"] == "https://api.example.test/v1"
        assert provider["_model_dict_cache"] == {"[p]alias-model": "real-model"}
        assert test_config["_resolved_test_model"] == "[p]alias-model"
        assert "api_keys" not in provider
        assert "sub_channels" not in provider
        assert "_is_sub_channel" not in provider
        assert "_parent_provider" not in provider
        assert "_virtual_route_provider" not in provider
        assert "_virtual_priority" not in provider
    finally:
        unregister_channel(engine)


def test_build_test_request_uses_resolved_model_and_request_defaults():
    """测试请求构建，确认模型名、prompt 和默认参数集中处理。"""
    # 修改原因：前缀模型可能由 provider 构建阶段解析后写入内部字段。
    # 修改方式：_build_test_request 优先读取 _resolved_test_model，并统一处理默认 max_tokens。
    # 目的：让主路由只负责调度，不再散落请求参数转换逻辑。
    request = channels_route._build_test_request({
        "_resolved_test_model": "[p]alias-model",
        "prompt": 123,
        "stream": True,
        "max_tokens": "bad",
        "temperature": "0.7",
    })

    assert request.model == "[p]alias-model"
    assert request.messages[0].content == "123"
    assert request.stream is True
    assert request.max_tokens == 16
    assert request.temperature == 0.7


@pytest.mark.asyncio
async def test_extract_test_result_strips_gateway_prefix_from_json_body():
    """测试 JSON body 响应提取，确保内部网关错误前缀不会泄露到前端。"""
    # 修改原因：错误信息格式化从主函数中拆出后，仍需保持前端展示字段不变。
    # 修改方式：构造 JSONResponse 并断言标准返回结构。
    # 目的：保留 success、latency_ms、message、error 等原 API 响应字段。
    response = JSONResponse(
        status_code=502,
        content={"error": {"message": "Error: Current provider response failed: upstream failed"}},
    )

    result = await channels_route._extract_test_result(response, time())

    assert result["success"] is False
    assert result["message"] == "HTTP 502"
    assert result["error"] == "upstream failed"
    assert result["upstream_status_code"] == 502
    assert result["auth_failed"] is False
    assert result["response_preview"] is None


@pytest.mark.asyncio
async def test_extract_test_result_reads_body_iterator_and_strips_all_provider_prefix():
    """测试流式 body_iterator 响应提取，保留原来的错误预览解析路径。"""
    # 修改原因：request_model 可能返回 StreamingResponse，重构不能只处理 body 字段。
    # 修改方式：使用 StreamingResponse 模拟 body_iterator，并断言 All providers 前缀被剥离。
    # 目的：让普通响应和流式响应都返回同一套测试结果结构。
    async def chunks():
        yield b'{"error":"All providers error: final upstream error"}'

    response = StreamingResponse(chunks(), status_code=500)

    result = await channels_route._extract_test_result(response, time())

    assert result["success"] is False
    assert result["message"] == "HTTP 500"
    assert result["error"] == "final upstream error"
    assert result["upstream_status_code"] == 500
