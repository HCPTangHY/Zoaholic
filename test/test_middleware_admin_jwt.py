"""Offline regressions for login JWTs crossing the standard /v1 middleware.

Real JWT verification, middleware, auth dependencies and login route; database
and application startup are replaced with fixtures. Never uses real credentials.
"""
import asyncio
from contextlib import asynccontextmanager
import sys
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from fastapi import Depends, FastAPI, Request

import core.auth as auth
import core.jwt_utils as jwt
import core.middleware as middleware
import routes.auth as auth_routes
from core.security import hash_password


@pytest.fixture
def gateway(monkeypatch):
    monkeypatch.setattr(jwt, '_CACHED_SECRET', 'offline-jwt-regression-secret')
    app = FastAPI()
    entries = [
        {'api': 'fixture-user-key', 'role': 'user'},
        {'api': 'fixture-admin-key', 'role': 'admin'},
    ]
    app.state.config = {'api_keys': entries, 'preferences': {'log_raw_data_retention_hours': 0}}
    app.state.api_keys_db = entries
    app.state.api_list = [entry['api'] for entry in entries]
    app.state.paid_api_keys_states = {}
    app.state.global_rate_limit = [(10000, 60)]
    ready = asyncio.Event()
    ready.set()
    monkeypatch.setitem(sys.modules, 'main', SimpleNamespace(_db_ready=ready))
    monkeypatch.setattr(middleware, 'DISABLE_DATABASE', True)
    monkeypatch.setattr(middleware, 'is_global_ip_blocked', lambda *args: False)
    monkeypatch.setattr(middleware, 'is_key_ip_blocked', lambda *args: False)
    monkeypatch.setattr(auth, 'is_global_ip_blocked', lambda *args: False)
    monkeypatch.setattr(auth, 'is_key_ip_blocked', lambda *args: False)
    monkeypatch.setattr(middleware, 'on_request_start', lambda *a, **k: None)
    monkeypatch.setattr(middleware, 'on_request_model', lambda *a, **k: None)
    monkeypatch.setattr(middleware, 'on_request_end', lambda *a, **k: None)
    app.add_middleware(middleware.StatsMiddleware)

    @app.get('/v1/test-admin')
    async def admin_route(token=Depends(auth.verify_admin_api_key)):
        return {'ok': True, 'stats_key': middleware.request_info.get().get('api_key')}

    @app.get('/v1/test-key')
    async def key_route(index=Depends(auth.verify_api_key)):
        return {'index': index, 'stats_key': middleware.request_info.get().get('api_key')}

    @app.get('/v1/test-middleware')
    async def middleware_only(request: Request):
        return {'stats_key': middleware.request_info.get().get('api_key')}

    return app


def call(app, token=None, path='/v1/test-admin'):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            return await client.get(path, headers={'Authorization': 'Bearer ' + token} if token else {})
    return asyncio.run(run())


def test_valid_admin_jwt_maps_to_admin_not_first_key(gateway):
    token = jwt.issue_jwt({'sub': 'fixture-admin', 'role': 'admin'})
    for path in ['/v1/test-admin', '/v1/test-key']:
        response = call(gateway, token, path)
        assert response.status_code == 200
        assert response.json()['stats_key'] == 'fixture-admin-key'
        assert token not in response.text


@pytest.mark.parametrize('kind', ['expired', 'forged', 'user', 'malformed', 'missing'])
def test_invalid_credentials_never_reach_unprotected_v1_route(gateway, kind):
    token = jwt.issue_jwt({'sub': 'fixture', 'role': 'admin'})
    if kind == 'expired':
        token = jwt.issue_jwt({'sub': 'fixture', 'role': 'admin'}, expires_in_seconds=-60)
    elif kind == 'forged':
        with patch.object(jwt, '_CACHED_SECRET', 'wrong-offline-secret'):
            token = jwt.issue_jwt({'sub': 'fixture', 'role': 'admin'})
    elif kind == 'user':
        token = jwt.issue_jwt({'sub': 'fixture', 'role': 'user'})
    elif kind == 'malformed': token = 'not.a.valid-jwt'
    elif kind == 'missing': token = None
    assert call(gateway, token, '/v1/test-middleware').status_code == 403


def test_plain_api_keys_keep_their_role_boundaries(gateway):
    assert call(gateway, 'fixture-admin-key').status_code == 200
    assert call(gateway, 'fixture-user-key').status_code == 403
    response = call(gateway, 'fixture-user-key', '/v1/test-key')
    assert response.status_code == 200
    assert response.json()['index'] == 0


def test_admin_jwt_preserves_disabled_key_console_exemption(gateway):
    gateway.state.api_keys_db[1]['enabled'] = False
    token = jwt.issue_jwt({'sub': 'fixture-admin', 'role': 'admin'})
    assert call(gateway, token).status_code == 200
    assert call(gateway, 'fixture-admin-key').status_code == 403


@pytest.mark.parametrize('scope', ['global', 'key'])
def test_admin_jwt_cannot_bypass_ip_blacklists(gateway, monkeypatch, scope):
    token = jwt.issue_jwt({'sub': 'fixture-admin', 'role': 'admin'})
    seen = []
    if scope == 'global':
        monkeypatch.setattr(middleware, 'is_global_ip_blocked', lambda *a: True)
    else:
        def blocked(app, index, ip):
            seen.append(index)
            return index == 1
        monkeypatch.setattr(middleware, 'is_key_ip_blocked', blocked)
    assert call(gateway, token).status_code == 403
    if scope == 'key': assert seen == [1]


@pytest.mark.parametrize('entries', [[], [{'api': 'a', 'role': 'user'}, {'api': 'b', 'role': 'user'}]])
def test_missing_admin_mapping_fails_closed(gateway, entries):
    gateway.state.api_keys_db = entries
    gateway.state.config['api_keys'] = entries
    gateway.state.api_list = [entry['api'] for entry in entries]
    assert call(gateway, jwt.issue_jwt({'role': 'admin'})).status_code == 403


def test_single_key_admin_fallback_matches_existing_auth_contract(gateway):
    entries = [{'api': 'single-fixture-key'}]
    gateway.state.api_keys_db = entries
    gateway.state.config['api_keys'] = entries
    gateway.state.api_list = ['single-fixture-key']
    response = call(gateway, jwt.issue_jwt({'role': 'admin'}))
    assert response.status_code == 200
    assert response.json()['stats_key'] == 'single-fixture-key'


def test_login_token_reaches_protected_management_route(gateway, monkeypatch):
    # Exercise the real /auth/login handler without production DB or credentials.
    user = SimpleNamespace(username='fixture-admin', password_hash=hash_password('fixture-password'), jwt_secret=None)
    class FakeDB:
        async def get(self, *args): return user
    @asynccontextmanager
    async def session(): yield FakeDB()
    monkeypatch.setattr(auth_routes, 'DISABLE_DATABASE', False)
    monkeypatch.setattr(auth_routes, 'DB_TYPE', 'sqlite')
    monkeypatch.setattr(auth_routes, 'async_session_scope', session)
    monkeypatch.setattr(auth_routes, 'get_app', lambda: gateway)
    gateway.include_router(auth_routes.router)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway), base_url='http://test') as client:
            login = await client.post('/auth/login', json={'username': 'fixture-admin', 'password': 'fixture-password'})
            assert login.status_code == 200
            token = login.json()['access_token']
            headers = {'Authorization': 'Bearer ' + token}
            me = await client.get('/auth/me', headers=headers)
            assert me.status_code == 200
            admin = await client.get('/v1/test-admin', headers=headers)
            assert admin.status_code == 200
            assert admin.json()['stats_key'] == 'fixture-admin-key'
    asyncio.run(run())


def test_byok_identity_still_uses_template_and_rejects_bare_template(gateway):
    from core.byok import build_byok_prefixes
    gateway.state.api_keys_db.append({'api': 'fixture-byok-*', 'role': 'user'})
    gateway.state.api_list.append('fixture-byok-*')
    gateway.state.byok_prefixes = build_byok_prefixes(gateway.state.api_keys_db)
    response = call(gateway, 'fixture-byok-upstream-secret', '/v1/test-key')
    assert response.status_code == 200
    assert response.json()['stats_key'] == 'fixture-byok-*'
    assert 'upstream-secret' not in response.text
    assert call(gateway, 'fixture-byok-*', '/v1/test-key').status_code == 403
    assert call(gateway, 'fixture-byok-upstream-secret').status_code == 403


def test_out_of_bounds_admin_mapping_fails_closed(gateway):
    gateway.state.api_list = ['fixture-user-key']
    assert call(gateway, jwt.issue_jwt({'role': 'admin'})).status_code == 403
