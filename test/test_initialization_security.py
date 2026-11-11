"""Initialization boundary tests: isolated app state and temporary databases only."""
import asyncio
import base64
import hashlib
import hmac
import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import db as database
from core import initialization, jwt_utils
from core.auth import authenticate_api_key_token, verify_admin_api_key, verify_api_key
from routes import setup, auth


def app_for(*, ready=False, keys=None):
    app = FastAPI()
    keys = keys if keys is not None else [{"api": "admin-test", "role": "admin"}, {"api": "user-test", "role": "user"}]
    app.state.api_keys_db = keys
    app.state.api_list = [k["api"] for k in keys]
    app.state.config = {"providers": [], "api_keys": keys, "preferences": {}}
    app.state.admin_initialized = ready
    return app


def request_for(app, token=None):
    headers = [(b"authorization", ("Bearer " + token).encode())] if token else []
    return Request({"type": "http", "app": app, "headers": headers, "client": ("127.0.0.1", 12345)})


def signed(payload, secret, algorithm="HS256"):
    def enc(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=")
    message = enc({"alg": algorithm, "typ": "JWT"}) + b"." + enc(payload)
    sig = base64.urlsafe_b64encode(hmac.new(secret.encode(), message, hashlib.sha256).digest()).rstrip(b"=")
    return (message + b"." + sig).decode()


@pytest.fixture(autouse=True)
def clean_secret(monkeypatch):
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.setattr(jwt_utils, "_CACHED_SECRET", None)
    monkeypatch.setattr(database, "DISABLE_DATABASE", False)
    monkeypatch.setattr(setup, "DISABLE_DATABASE", False)


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["user-test", "admin-test", "legacy-jwt"])
async def test_uninitialized_blocks_user_admin_and_forged_jwt(token):
    app = app_for()
    if token == "legacy-jwt":
        token = signed({"role": "admin", "exp": time.time() + 60}, jwt_utils.LEGACY_JWT_SECRET)
    request = request_for(app, token)
    for verify in (verify_api_key, verify_admin_api_key):
        with pytest.raises(HTTPException) as error:
            await verify(request, None)
        assert error.value.status_code == 503
    with pytest.raises(HTTPException) as error:
        authenticate_api_key_token(app, token, "127.0.0.1")
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_missing_state_and_disabled_database_fail_closed(monkeypatch):
    app = app_for()
    del app.state.admin_initialized
    read = AsyncMock(side_effect=AssertionError("Missing startup state must not query a global DB"))
    monkeypatch.setattr(initialization, "get_admin_user", read)
    with pytest.raises(HTTPException):
        await initialization.ensure_initialized(app)
    read.assert_not_awaited()
    monkeypatch.setattr(database, "DISABLE_DATABASE", True)
    monkeypatch.setenv("JWT_SECRET", "explicit-secret-does-not-initialize-admin")
    assert not await initialization.refresh_initialization(app, force=True)
    read.assert_not_awaited()


def test_jwt_has_no_fallback_and_requires_hs256_exp():
    payload = {"role": "admin", "exp": time.time() + 60}
    assert jwt_utils.decode_jwt(signed(payload, jwt_utils.LEGACY_JWT_SECRET)) is None
    with pytest.raises(RuntimeError):
        jwt_utils.issue_jwt(payload)
    jwt_utils.set_jwt_secret("trusted-test-secret")
    assert jwt_utils.is_admin_jwt(jwt_utils.issue_jwt({"role": "admin"}))
    assert jwt_utils.decode_jwt(signed(payload, "trusted-test-secret", "none")) is None
    for expiration in (None, True, "99999999999", float("nan"), float("inf"), time.time() - 1):
        bad = {"role": "admin"}
        if expiration is not None:
            bad["exp"] = expiration
        assert jwt_utils.decode_jwt(signed(bad, "trusted-test-secret")) is None


@pytest.mark.asyncio
async def test_initialized_keys_and_jwt_keep_permissions():
    app = app_for(ready=True)
    jwt_utils.set_jwt_secret("trusted-test-secret")
    assert await verify_api_key(request_for(app, "user-test"), None) == 1
    assert await verify_admin_api_key(request_for(app, "admin-test"), None) == "admin-test"
    with pytest.raises(HTTPException) as error:
        await verify_admin_api_key(request_for(app, "user-test"), None)
    assert error.value.status_code == 403
    token = jwt_utils.issue_jwt({"role": "admin"})
    assert await verify_admin_api_key(request_for(app, token), None) == token
    app.state.api_list = []
    with pytest.raises(HTTPException):
        authenticate_api_key_token(app, token, None)


@pytest.mark.asyncio
async def test_setup_recovery_requires_explicit_static_admin_and_never_writes_on_denial(monkeypatch):
    app = app_for()
    monkeypatch.setattr(setup, "get_app", lambda: app)
    monkeypatch.setattr(setup, "_get_admin_user", AsyncMock(return_value=None))
    create = AsyncMock()
    monkeypatch.setattr(setup, "_create_admin_user", create)
    monkeypatch.setattr(setup, "refresh_initialization", AsyncMock(return_value=True))
    payload = setup.SetupInitRequest(username="owner", password="password-test", confirm_password="password-test")
    jwt_utils.set_jwt_secret("trusted-test-secret")
    for token in (None, "user-test", jwt_utils.issue_jwt({"role": "admin"})):
        with pytest.raises(HTTPException) as error:
            await setup.setup_init(request_for(app, token), payload)
        assert error.value.status_code == 403
    create.assert_not_awaited()
    result = await setup.setup_init(request_for(app, "admin-test"), payload)
    assert result.admin_api_key == "admin-test"
    create.assert_awaited_once()
    app.state.api_list = ["user-test"]
    app.state.api_keys_db = [{"api": "user-test", "role": "user"}]
    with pytest.raises(HTTPException) as error:
        await setup.setup_init(request_for(app, "user-test"), payload)
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_existing_admin_cannot_be_replaced_and_no_config_requires_existing_password(monkeypatch):
    from core.security import hash_password
    app = app_for()
    existing = SimpleNamespace(username="owner", password_hash=hash_password("original-password"), jwt_secret="secret")
    monkeypatch.setattr(setup, "get_app", lambda: app)
    monkeypatch.setattr(setup, "_get_admin_user", AsyncMock(return_value=existing))
    create = AsyncMock()
    monkeypatch.setattr(setup, "_create_admin_user", create)
    payload = setup.SetupInitRequest(username="attacker", password="new-password", confirm_password="new-password")
    with pytest.raises(HTTPException) as error:
        await setup.setup_init(request_for(app), payload)
    assert error.value.status_code == 409
    app.state.api_list = []
    with pytest.raises(HTTPException) as error:
        await setup.setup_init(request_for(app), payload)
    assert error.value.status_code == 403
    create.assert_not_awaited()
    assert existing.username == "owner"


@pytest.mark.asyncio
async def test_auth_me_setup_login_and_dialect_gate_unready(monkeypatch):
    from core.dialects.router import _create_dialect_verify_api_key
    app = app_for()
    monkeypatch.setattr(auth, "get_app", lambda: app)
    monkeypatch.setattr(setup, "get_app", lambda: app)
    jwt_utils.set_jwt_secret("trusted-test-secret")
    token = jwt_utils.issue_jwt({"role": "admin"})
    for call in (
        auth.me(request_for(app, token)),
        setup.setup_login(setup.SetupLoginRequest(username="owner", password="password")),
        _create_dialect_verify_api_key("openai")(request_for(app, "user-test"), None),
    ):
        with pytest.raises(HTTPException) as error:
            await call
        assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_state_refresh_preserves_ready_during_db_read_and_does_not_repair_on_get(monkeypatch):
    app = app_for(ready=True)
    entered = asyncio.Event()
    release = asyncio.Event()
    async def read():
        entered.set()
        await release.wait()
        return SimpleNamespace(username="owner", password_hash="hash", jwt_secret="stored-secret")
    monkeypatch.setattr(initialization, "get_admin_user", read)
    task = asyncio.create_task(initialization.refresh_initialization(app, force=True))
    await entered.wait()
    initialization.require_initialized(app)
    release.set()
    assert await task
    monkeypatch.setattr(initialization, "get_admin_user", AsyncMock(return_value=SimpleNamespace(username="owner", password_hash="hash", jwt_secret=None)))
    repair = AsyncMock(side_effect=AssertionError("Read-only refresh must not generate a secret"))
    monkeypatch.setattr(initialization, "_ensure_persisted_secret", repair)
    assert not await initialization.refresh_initialization(app, force=True)
    repair.assert_not_awaited()


@pytest.mark.asyncio
async def test_persisted_secret_upgrade_and_setup_creation_are_atomic_across_workers(tmp_path, monkeypatch):
    from db import AdminUser
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "isolated.db"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(AdminUser.__table__.create)
    @asynccontextmanager
    async def scope():
        async with sessions() as session:
            yield session
    monkeypatch.setattr(database, "async_session_scope", scope)
    monkeypatch.setattr(database, "DB_TYPE", "sqlite")
    monkeypatch.setattr(setup, "async_session_scope", scope)
    try:
        results = await asyncio.gather(
            setup._create_admin_user("owner-a", "password-a", "first-secret"),
            setup._create_admin_user("owner-b", "password-b", "second-secret"),
            return_exceptions=True,
        )
        assert sum(result is None for result in results) == 1
        assert any(isinstance(result, HTTPException) and result.status_code == 409 for result in results)
        async with sessions() as session:
            admin = await session.get(AdminUser, 1)
            original_username = admin.username
            admin.jwt_secret = None
            await session.commit()
        a, b = app_for(), app_for()
        await asyncio.gather(
            initialization.refresh_initialization(a, force=True, repair_secret=True),
            initialization.refresh_initialization(b, force=True, repair_secret=True),
        )
        assert a.state.admin_initialized and b.state.admin_initialized
        row = await initialization.get_admin_user()
        assert row.username == original_username
        assert row.jwt_secret and row.jwt_secret != jwt_utils.LEGACY_JWT_SECRET
        persisted = row.jwt_secret
        jwt_utils._CACHED_SECRET = None
        restarted = app_for()
        assert await initialization.refresh_initialization(restarted, force=True)
        assert jwt_utils._CACHED_SECRET == persisted
        read = AsyncMock(side_effect=AssertionError("Ready requests must not query DB"))
        monkeypatch.setattr(initialization, "get_admin_user", read)
        await initialization.ensure_initialized(restarted)
        read.assert_not_awaited()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_unready_worker_refreshes_after_other_worker_setup(monkeypatch):
    app = app_for()
    app.state.initialization_checked = True
    monkeypatch.setattr(initialization, "get_admin_user", AsyncMock(return_value=SimpleNamespace(username="owner", password_hash="hash", jwt_secret="persistent-secret")))
    await initialization.ensure_initialized(app)
    assert app.state.admin_initialized
    assert jwt_utils._CACHED_SECRET == "persistent-secret"
