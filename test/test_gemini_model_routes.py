"""Gemini 路由离线回归：带斜杠的模型别名不能落入 GET 兜底返回 405。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import quote

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from core.dialects import dialect_router
from routes.deps import rate_limit_dependency


@pytest.fixture
def gateway(monkeypatch):
    app = FastAPI()
    app.state.api_list = ["test-key"]
    app.state.api_keys_db = [{"api": "test-key", "role": "user"}]
    app.state.config = {}
    # 只隔离限流与上游调用，保留真实注册顺序、Gemini 解析和鉴权依赖。
    app.dependency_overrides[rate_limit_dependency] = lambda: None
    app.include_router(dialect_router)

    @app.get("/{path:path}")
    async def fallback(path: str):
        return JSONResponse({"fallback": path}, status_code=404)

    async def request_model(**kwargs):
        parsed = kwargs["request_data"]
        return JSONResponse(
            {"model": parsed.model, "stream": parsed.stream},
            headers={"x-zoaholic-passthrough": "true"},
        )

    upstream = AsyncMock(side_effect=request_model)
    monkeypatch.setattr("routes.deps.get_model_handler", lambda: SimpleNamespace(request_model=upstream))
    models = [
        {"name": "models/gemini-test"},
        {"name": "models/vendor/gemini-test"},
        {"name": "models/vendor/team/gemini-test"},
    ]
    catalog = AsyncMock(return_value=models)
    monkeypatch.setattr("core.dialects.gemini._get_gemini_models", catalog)
    return app, upstream, catalog


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["/v1", "/v1beta"])
@pytest.mark.parametrize("model", ["gemini-test", "vendor/gemini-test", "vendor/team/gemini-test"])
@pytest.mark.parametrize("action", ["generateContent", "streamGenerateContent"])
@pytest.mark.parametrize("encode_slashes", [False, True])
async def test_generate_preserves_complete_model(gateway, prefix, model, action, encode_slashes):
    app, upstream, _ = gateway
    model_path = quote(model, safe="" if encode_slashes else "/")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test.invalid") as client:
        response = await client.post(
            f"{prefix}/models/{model_path}:{action}?alt=sse",
            headers={"x-goog-api-key": "test-key"},
            json={"contents": [{"role": "user", "parts": [{"text": "hello"}]}]},
        )
    assert response.status_code == 200, response.text
    assert response.json() == {"model": model, "stream": action == "streamGenerateContent"}
    upstream.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["/v1", "/v1beta"])
@pytest.mark.parametrize("model", ["gemini-test", "vendor/gemini-test", "vendor/team/gemini-test"])
async def test_model_details_preserve_complete_model(gateway, prefix, model):
    app, upstream, catalog = gateway
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test.invalid") as client:
        response = await client.get(f"{prefix}/models/{model}", headers={"x-goog-api-key": "test-key"})
    assert response.status_code == 200
    assert response.json()["name"] == f"models/{model}"
    catalog.assert_awaited_once()
    upstream.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["/v1", "/v1beta"])
@pytest.mark.parametrize("action", [None, "generateContent", "streamGenerateContent"])
@pytest.mark.parametrize("headers", [{}, {"x-goog-api-key": "wrong-key"}])
async def test_namespaced_routes_still_require_auth(gateway, prefix, action, headers):
    app, upstream, catalog = gateway
    path = f"{prefix}/models/vendor/gemini-test" + (f":{action}" if action else "")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test.invalid") as client:
        response = await client.request("POST" if action else "GET", path, headers=headers, json={})
    assert response.status_code == 403, response.text
    upstream.assert_not_awaited()
    catalog.assert_not_awaited()


@pytest.mark.asyncio
async def test_model_list_is_not_shadowed_by_detail_route(gateway):
    app, upstream, catalog = gateway
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test.invalid") as client:
        response = await client.get("/v1beta/models", headers={"x-goog-api-key": "test-key"})
    assert response.status_code == 200
    assert response.json()["models"] == catalog.return_value
    catalog.assert_awaited_once()
    upstream.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["/v1", "/v1beta"])
async def test_missing_namespaced_model_returns_model_not_found(gateway, prefix):
    app, upstream, catalog = gateway
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test.invalid") as client:
        response = await client.get(f"{prefix}/models/vendor/missing", headers={"x-goog-api-key": "test-key"})
    assert response.status_code == 404
    assert response.json()["detail"] == "Model not found: vendor/missing"
    catalog.assert_awaited_once()
    upstream.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["/v1", "/v1beta"])
async def test_unknown_action_does_not_invoke_model(gateway, prefix):
    app, upstream, _ = gateway
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test.invalid") as client:
        response = await client.post(
            f"{prefix}/models/vendor/gemini-test:unknownAction",
            headers={"x-goog-api-key": "test-key"}, json={},
        )
    assert response.status_code == 405
    upstream.assert_not_awaited()
