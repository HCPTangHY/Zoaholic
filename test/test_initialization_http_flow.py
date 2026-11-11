"""Exercise setup and auth together through ASGI using a disposable SQLite DB."""
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import db as database
from core import initialization, jwt_utils
from core.auth import verify_api_key
from routes import auth, setup


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_config", [False, True])
async def test_setup_to_authenticated_api_flow(tmp_path, monkeypatch, existing_config):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "setup.db"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(database.AdminUser.__table__.create)

    @asynccontextmanager
    async def session_scope():
        async with sessions() as session:
            yield session

    for module in (database, setup):
        monkeypatch.setattr(module, "async_session_scope", session_scope)
        monkeypatch.setattr(module, "DISABLE_DATABASE", False)
    monkeypatch.setattr(database, "DB_TYPE", "sqlite")
    monkeypatch.setattr(auth, "DISABLE_DATABASE", False)
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.setattr(jwt_utils, "_CACHED_SECRET", None)
    app = FastAPI()
    keys = [{"api": "owner-key", "role": "admin"}, {"api": "user-key", "role": "user"}] if existing_config else []
    app.state.config = {"providers": [], "api_keys": keys, "preferences": {}}
    app.state.api_keys_db = keys
    app.state.api_list = [key["api"] for key in keys]
    app.state.global_rate_limit = [(999999, 60)]
    app.state.admin_initialized = False
    app.state.initialization_checked = True
    for module in (setup, auth):
        monkeypatch.setattr(module, "get_app", lambda: app)

    async def save_config(conf, **kwargs):
        return conf, conf["api_keys"], [key["api"] for key in conf["api_keys"]]

    monkeypatch.setattr(setup, "update_config", save_config)
    app.include_router(setup.router)
    app.include_router(auth.router)

    @app.get("/v1/test")
    async def protected(index: int = Depends(verify_api_key)):
        return {"index": index}

    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://isolated.test") as client:
            status = (await client.get("/setup/status")).json()
            assert status["needs_setup"] and status["database_enabled"]
            assert (await client.get("/v1/test", headers={"Authorization": "Bearer owner-key"})).status_code == 503
            payload = {"username": "owner", "password": "private-test-password", "confirm_password": "private-test-password"}
            if existing_config:
                for headers in ({}, {"Authorization": "Bearer user-key"}):
                    assert (await client.post("/setup/init", json=payload, headers=headers)).status_code == 403
                assert await initialization.get_admin_user() is None
            headers = {"Authorization": "Bearer owner-key"} if existing_config else {}
            response = await client.post("/setup/init", json=payload, headers=headers)
            assert response.status_code == 200, response.text
            assert app.state.admin_initialized
            assert not (await client.get("/setup/status")).json()["needs_setup"]
            if existing_config:
                assert response.json()["admin_api_key"] == "owner-key"
                assert app.state.api_list == ["owner-key", "user-key"]
                assert (await client.get("/v1/test", headers={"Authorization": "Bearer user-key"})).status_code == 200
            assert (await client.post("/setup/init", json=payload)).status_code == 409
            login = await client.post("/auth/login", json={"username": "owner", "password": "private-test-password"})
            assert login.status_code == 200, login.text
            token = login.json()["access_token"]
            me = await client.get("/auth/me", headers={"Authorization": "Bearer " + token})
            assert me.status_code == 200 and me.json()["role"] == "admin"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_websocket_uninitialized_closes_before_accept():
    from core.dialects.openai_responses_ws import router
    app = FastAPI()
    app.state.admin_initialized = False
    app.state.api_list = ["user-key"]
    app.state.api_keys_db = [{"api": "user-key", "role": "user"}]
    app.include_router(router)
    messages = []

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        messages.append(message)

    scope = {"type": "websocket", "asgi": {"version": "3.0"}, "scheme": "ws", "path": "/v1/responses",
             "raw_path": b"/v1/responses", "query_string": b"", "headers": [(b"authorization", b"Bearer user-key")],
             "client": ("127.0.0.1", 10000), "server": ("isolated.test", 80), "subprotocols": []}
    await app(scope, receive, send)
    assert messages == [{"type": "websocket.close", "code": 1008, "reason": "Instance is not initialized"}]
