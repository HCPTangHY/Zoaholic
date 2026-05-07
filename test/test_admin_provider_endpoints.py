import os
import sys

import httpx
import pytest
from fastapi import FastAPI

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routes import admin


# 修改原因：渠道保存需要从全量 providers 覆盖改为单渠道局部接口，必须先固定后端契约。
# 修改方式：构造只包含 admin router 的测试应用，并用 httpx.ASGITransport 兼容当前 httpx 版本。
# 目的：验证接口只影响目标渠道，同时避免测试写入真实 api.yaml 或数据库。
def _make_admin_app(monkeypatch):
    app = FastAPI()
    app.state.config = {
        "providers": [
            {"provider": "alpha", "engine": "openai", "preferences": {"weight": 1}},
            {"provider": "beta", "engine": "anthropic", "preferences": {"weight": 2}},
        ]
    }
    app.state.api_list = []
    app.state.api_keys_db = None

    persist_calls = []

    async def fake_persist_config(target_app, sections_to_verify=None):
        persist_calls.append((target_app, sections_to_verify))

    monkeypatch.setattr(admin, "get_app", lambda: app)
    monkeypatch.setattr(admin, "_persist_config", fake_persist_config, raising=False)
    app.dependency_overrides[admin.verify_admin_api_key] = lambda: 0
    app.dependency_overrides[admin.rate_limit_dependency] = lambda: None
    app.include_router(admin.router)

    return app, persist_calls


# 修改原因：多个测试都要通过 ASGI 调用路由，重复创建客户端容易引入不一致。
# 修改方式：统一封装 async 客户端上下文，直接使用内存 ASGI transport。
# 目的：保留真实路由状态码验证，同时不启动外部服务。
def _make_async_client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


# 修改原因：编辑渠道时前端会传完整的单个 provider 对象，后端不能做深度合并。
# 修改方式：PUT 后断言目标对象被整体替换，旧 preferences 字段不再保留。
# 目的：避免局部接口暗中保留旧字段，导致前端删除字段后无法生效。
@pytest.mark.asyncio
async def test_update_provider_replaces_only_target_provider(monkeypatch):
    app, persist_calls = _make_admin_app(monkeypatch)

    async with _make_async_client(app) as client:
        response = await client.put("/v1/providers/alpha", json={"provider": "alpha-renamed", "engine": "gemini"})

    assert response.status_code == 200
    assert response.json() == {"message": "Provider updated", "provider_id": "alpha"}
    assert app.state.config["providers"] == [
        {"provider": "alpha-renamed", "engine": "gemini"},
        {"provider": "beta", "engine": "anthropic", "preferences": {"weight": 2}},
    ]
    assert len(persist_calls) == 1


# 修改原因：局部更新接口只能定位已有渠道，不能在 PUT 路径下静默新增。
# 修改方式：请求不存在的 provider_id 并断言返回 404，配置列表保持不变。
# 目的：让前端能明确区分“被其他设备删除”和“保存成功”。
@pytest.mark.asyncio
async def test_update_provider_returns_404_when_provider_missing(monkeypatch):
    app, persist_calls = _make_admin_app(monkeypatch)

    async with _make_async_client(app) as client:
        response = await client.put("/v1/providers/missing", json={"provider": "missing"})

    assert response.status_code == 404
    assert app.state.config["providers"] == [
        {"provider": "alpha", "engine": "openai", "preferences": {"weight": 1}},
        {"provider": "beta", "engine": "anthropic", "preferences": {"weight": 2}},
    ]
    assert persist_calls == []


# 修改原因：删除渠道必须只移除目标 provider，不能依赖前端提交完整 providers 列表。
# 修改方式：DELETE 后检查剩余列表和持久化调用次数。
# 目的：消除多浏览器并发保存时删除操作覆盖其他渠道变更的风险。
@pytest.mark.asyncio
async def test_delete_provider_removes_only_target_provider(monkeypatch):
    app, persist_calls = _make_admin_app(monkeypatch)

    async with _make_async_client(app) as client:
        response = await client.delete("/v1/providers/beta")

    assert response.status_code == 200
    assert response.json() == {"message": "Provider deleted", "provider_id": "beta"}
    assert app.state.config["providers"] == [
        {"provider": "alpha", "engine": "openai", "preferences": {"weight": 1}},
    ]
    assert len(persist_calls) == 1


# 修改原因：新增渠道必须由独立 POST 接口完成，并阻止重名渠道破坏 provider 唯一定位。
# 修改方式：分别覆盖成功创建、重名冲突和缺少 provider 字段三种分支。
# 目的：保证前端新增弹窗不再需要提交全量 providers 数组。
@pytest.mark.asyncio
async def test_create_provider_appends_new_provider_and_rejects_invalid_requests(monkeypatch):
    app, persist_calls = _make_admin_app(monkeypatch)

    async with _make_async_client(app) as client:
        created = await client.post("/v1/providers", json={"provider": "gamma", "engine": "openai"})
        conflicted = await client.post("/v1/providers", json={"provider": "gamma", "engine": "openai"})
        invalid = await client.post("/v1/providers", json={"engine": "openai"})

    assert created.status_code == 201
    assert created.json() == {"message": "Provider created", "provider_id": "gamma"}
    assert conflicted.status_code == 409
    assert invalid.status_code == 400
    assert app.state.config["providers"] == [
        {"provider": "alpha", "engine": "openai", "preferences": {"weight": 1}},
        {"provider": "beta", "engine": "anthropic", "preferences": {"weight": 2}},
        {"provider": "gamma", "engine": "openai"},
    ]
    assert len(persist_calls) == 1
