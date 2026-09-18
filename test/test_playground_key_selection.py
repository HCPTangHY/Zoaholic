import asyncio
import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from routes import channels as channels_route


def _read_json_response(response):
    """读取 JSONResponse 的测试内容。"""
    # 修改原因：Playground Key 端点现在直接返回 JSONResponse，测试需要稳定读取响应体。
    # 修改方式：把 response.body 统一按 UTF-8 解码后交给 json.loads。
    # 目的：让断言只关注端点契约，不依赖 FastAPI 测试客户端。
    return json.loads(response.body.decode("utf-8"))


def test_playground_keys_endpoint_returns_global_user_api_keys(monkeypatch):
    """测试 Playground Key 列表返回全局 api_keys，而不是 provider 上游 Key。"""
    # 修改原因：Playground 的 Key 选择需求已从 provider 上游密钥改为全局用户 api_key。
    # 修改方式：构造最小 app.state.config，并直接调用端点验证完整 api、名称、序号和脱敏文本。
    # 目的：防止后续改动重新把该端点与 model 或 provider 绑定。
    app = SimpleNamespace(
        state=SimpleNamespace(
            config={
                "api_keys": [
                    {
                        "api": "sk-test-admin-key-000000",
                        "role": "admin",
                        "model": ["all"],
                        "name": "admin",
                    },
                    {
                        "api": "sk-test-user-key-000000000000000000000000",
                        "role": "user",
                        "name": "HCP",
                    },
                ]
            }
        )
    )
    monkeypatch.setattr(channels_route, "get_app", lambda: app)

    response = asyncio.run(channels_route.get_playground_keys(token="admin-token"))

    assert _read_json_response(response) == {
        "keys": [
            {
                "index": 0,
                "name": "admin",
                "masked_key": "sk-...0000",
                "api": "sk-test-admin-key-000000",
            },
            {
                "index": 1,
                "name": "HCP",
                "masked_key": "sk-...0000",
                "api": "sk-test-user-key-000000000000000000000000",
            },
        ]
    }


def test_playground_keys_endpoint_skips_invalid_items_and_masks_short_keys(monkeypatch):
    """测试无效全局 Key 会被跳过，短 Key 不展示片段。"""
    # 修改原因：api.yaml 可能包含空项或非 dict 项，端点不能因此返回不可用选项。
    # 修改方式：混合构造有效、短值、空值和非 dict 项，断言只返回可用用户 Key。
    # 目的：保证前端下拉框只拿到可用于 Authorization Bearer 的 api 值。
    app = SimpleNamespace(
        state=SimpleNamespace(
            config={
                "api_keys": [
                    {"api": "1234567", "name": "short"},
                    {"api": ""},
                    "sk-provider-key-should-not-appear",
                    {"api": "   "},
                ]
            }
        )
    )
    monkeypatch.setattr(channels_route, "get_app", lambda: app)

    response = asyncio.run(channels_route.get_playground_keys(token="admin-token"))

    assert _read_json_response(response) == {
        "keys": [
            {
                "index": 0,
                "name": "short",
                "masked_key": "***",
                "api": "1234567",
            }
        ]
    }
