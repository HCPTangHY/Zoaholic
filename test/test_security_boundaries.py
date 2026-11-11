"""Regression coverage for media SSRF, static traversal and setup takeover."""

import asyncio
import base64
import hashlib
import hmac
import json
import socket
import ssl
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpcore
import httpx
import pytest
from fastapi import FastAPI, HTTPException

from core import file_utils, jwt_utils
from core.public_fetch import PublicNetworkBackend, PublicURLTransport
from routes import setup


class MemoryStream(httpcore.AsyncNetworkStream):
    def __init__(self, response):
        self.response = response
        self.writes = []
        self.tls = None
        self.closed = False

    async def read(self, max_bytes, timeout=None):
        result, self.response = self.response[:max_bytes], self.response[max_bytes:]
        return result

    async def write(self, buffer, timeout=None):
        self.writes.append(buffer)

    async def aclose(self):
        self.closed = True

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.tls = (ssl_context, server_hostname)
        return self


def dns_answers(*addresses):
    return [(socket.AF_INET6 if ':' in address else socket.AF_INET,
             socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (address, 443))
            for address in addresses]


def media_client(monkeypatch, responses, answers):
    streams = [MemoryStream(response) for response in responses]
    connect = AsyncMock(side_effect=streams)
    resolve = AsyncMock(side_effect=answers)
    monkeypatch.setattr(httpcore.AnyIOBackend, 'connect_tcp', connect)
    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolve)
    client = httpx.AsyncClient(transport=PublicURLTransport(), follow_redirects=False, trust_env=False)
    monkeypatch.setattr(file_utils, '_get_shared_fetch_client', AsyncMock(return_value=client))
    return client, connect, resolve, streams


@pytest.mark.asyncio
@pytest.mark.parametrize('address', [
    '127.0.0.1', '0.0.0.0', '10.1.2.3', '172.16.1.2', '192.168.1.1',
    '169.254.169.254', '100.100.100.200', '224.0.0.1', '240.0.0.1', '192.0.0.8',
    '::1', '::', 'fc00::1', 'fe80::1', 'ff02::1', '::ffff:127.0.0.1',
    '::ffff:224.0.0.1', '64:ff9b::7f00:1', '2002:7f00:1::', '3fff::1',
])
async def test_media_rejects_non_public_dns_answers_before_connecting(monkeypatch, address):
    client, connect, _, _ = media_client(monkeypatch, [], [dns_answers(address)])
    async with client:
        with pytest.raises(HTTPException) as error:
            await file_utils.fetch_url_content('https://media.example/file')
    assert error.value.status_code == 400
    assert 'media.example' not in error.value.detail
    assert address not in error.value.detail
    connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_media_rejects_mixed_public_private_dns(monkeypatch):
    backend = PublicNetworkBackend()
    connect = AsyncMock()
    monkeypatch.setattr(backend._backend, 'connect_tcp', connect)
    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo',
                        AsyncMock(return_value=dns_answers('8.8.8.8', '127.0.0.1')))
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp('mixed.example', 80)
    connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_media_keeps_host_tls_sni_and_content(monkeypatch):
    client, connect, resolve, streams = media_client(monkeypatch, [
        b'HTTP/1.1 200 OK\r\nContent-Type: image/png; charset=binary\r\nContent-Length: 5\r\n\r\nimage',
    ], [dns_answers('8.8.8.8')])
    async with client:
        assert await file_utils.fetch_url_content('https://media.example/image') == (b'image', 'image/png')
    assert connect.await_args.args[0] == '8.8.8.8'
    assert resolve.await_count == 1
    assert b'Host: media.example' in b''.join(streams[0].writes)
    context, hostname = streams[0].tls
    assert hostname == 'media.example'
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
    assert streams[0].closed


@pytest.mark.asyncio
async def test_redirect_to_internal_host_never_connects(monkeypatch):
    client, connect, _, streams = media_client(monkeypatch, [
        b'HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1/private\r\nContent-Length: 0\r\n\r\n',
    ], [dns_answers('8.8.8.8'), dns_answers('127.0.0.1')])
    async with client:
        with pytest.raises(HTTPException):
            await file_utils.fetch_url_content('https://media.example/file')
    assert connect.await_count == 1
    assert streams[0].closed


@pytest.mark.asyncio
async def test_public_relative_redirect_still_works(monkeypatch):
    client, connect, _, _ = media_client(monkeypatch, [
        b'HTTP/1.1 302 Found\r\nLocation: /actual\r\nConnection: close\r\nContent-Length: 0\r\n\r\n',
        b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: text/plain\r\n\r\nok',
    ], [dns_answers('8.8.8.8'), dns_answers('8.8.8.8')])
    async with client:
        assert await file_utils.fetch_url_content('https://media.example/file') == (b'ok', 'text/plain')
    assert connect.await_count == 2


@pytest.mark.asyncio
async def test_dns_rebinding_cannot_change_connected_address(monkeypatch):
    client, connect, resolve, _ = media_client(monkeypatch, [
        b'HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: 2\r\n\r\nok',
    ], [dns_answers('8.8.8.8'), dns_answers('127.0.0.1')])
    async with client:
        assert (await file_utils.fetch_url_content('http://rebind.example/file'))[0] == b'ok'
        with pytest.raises(HTTPException):
            await file_utils.fetch_url_content('http://rebind.example/file')
    assert resolve.await_count == 2
    assert connect.await_count == 1
    assert connect.await_args.args[0] == '8.8.8.8'


@pytest.mark.asyncio
@pytest.mark.parametrize('url', ['file:///etc/passwd', 'ftp://example.com/file',
                                'https://user:password@example.com/file', 'http:///file',
                                'http://[fe80::1%25eth0]/file', 'http://example.com:0/file'])
async def test_invalid_media_urls_fail_before_network_access(monkeypatch, url):
    client, connect, resolve, _ = media_client(monkeypatch, [], [])
    async with client:
        with pytest.raises(HTTPException):
            await file_utils.fetch_url_content(url)
    connect.assert_not_awaited()
    resolve.assert_not_awaited()


@pytest.fixture
def static_app(monkeypatch, tmp_path):
    import main
    root = tmp_path / 'static'
    root.mkdir()
    (root / 'index.html').write_text('frontend', encoding='utf-8')
    (root / 'app.js').write_text('asset', encoding='utf-8')
    (tmp_path / 'private.txt').write_text('private-secret', encoding='utf-8')
    monkeypatch.setattr(main, 'STATIC_ROOT', root)
    app = FastAPI()
    app.get('/{path:path}')(main.spa_fallback)
    return app, root


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['/..%2fprivate.txt', '/%2e%2e%2fprivate.txt',
                                 '/channels/..%2f..%2fprivate.txt', '/..%5cprivate.txt'])
async def test_static_encoded_traversal_is_never_served(static_app, path):
    app, _ = static_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get(path)
    assert 'private-secret' not in response.text
    assert response.status_code in (200, 404)  # Backslash is a literal filename on POSIX.


@pytest.mark.asyncio
async def test_static_assets_and_frontend_routes_still_work(static_app):
    app, _ = static_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        for path in ('/', '/channels', '/login', '/unknown-route'):
            response = await client.get(path)
            assert response.text == 'frontend'
            assert 'no-store' in response.headers['cache-control']
        response = await client.get('/app.js')
        assert response.text == 'asset'
        assert 'immutable' in response.headers['cache-control']


@pytest.mark.asyncio
@pytest.mark.parametrize('link_name', ['outside.txt', 'index.html'])
async def test_static_symlinks_cannot_escape_root(static_app, link_name):
    import main
    _, root = static_app
    link = root / link_name
    link.unlink(missing_ok=True)
    try:
        link.symlink_to(root.parent / 'private.txt')
    except OSError:
        pytest.skip('Creating symlinks requires local OS permission')
    with pytest.raises(HTTPException) as error:
        await main.spa_fallback(link_name)
    assert error.value.status_code == 404


def token_signed_with(secret):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b'=')
    header = encode({'alg': 'HS256', 'typ': 'JWT'})
    payload = encode({'sub': 'admin', 'role': 'admin', 'exp': int(time.time()) + 3600})
    data = header + b'.' + payload
    signature = base64.urlsafe_b64encode(hmac.new(secret.encode(), data, hashlib.sha256).digest()).rstrip(b'=')
    return (data + b'.' + signature).decode()


def test_default_jwt_secret_is_random_and_rejects_known_forgery(monkeypatch):
    monkeypatch.setattr(jwt_utils, '_CACHED_SECRET', None)
    monkeypatch.delenv('JWT_SECRET', raising=False)
    secret = jwt_utils._jwt_secret()
    assert len(secret) >= 48
    assert jwt_utils._jwt_secret() == secret
    assert not jwt_utils.is_admin_jwt(token_signed_with('dev-insecure-jwt-secret'))
    assert jwt_utils.is_admin_jwt(jwt_utils.issue_jwt({'role': 'admin'}))


def test_configured_and_persisted_jwt_secrets_remain_supported(monkeypatch):
    monkeypatch.setattr(jwt_utils, '_CACHED_SECRET', None)
    monkeypatch.setenv('JWT_SECRET', 'environment-test-secret')
    assert jwt_utils.is_admin_jwt(token_signed_with('environment-test-secret'))
    jwt_utils.set_jwt_secret('database-test-secret')
    assert jwt_utils.is_admin_jwt(token_signed_with('database-test-secret'))
    assert not jwt_utils.is_admin_jwt(token_signed_with('environment-test-secret'))


@pytest.fixture
def setup_app(monkeypatch):
    app = FastAPI()
    keys = [{'api': 'admin-key', 'role': 'admin'}, {'api': 'user-key', 'role': 'user'}]
    app.state.api_list = [key['api'] for key in keys]
    app.state.api_keys_db = keys
    app.state.config = {'providers': [], 'api_keys': keys, 'preferences': {}}
    app.include_router(setup.router)
    monkeypatch.setattr(setup, 'get_app', lambda: app)
    monkeypatch.setattr(setup, 'DISABLE_DATABASE', False)
    monkeypatch.setattr(setup, '_setup_lock', asyncio.Lock())
    monkeypatch.setattr(setup, '_get_admin_user', AsyncMock(return_value=None))
    monkeypatch.setattr(setup, '_create_admin_user', AsyncMock())
    monkeypatch.setattr(setup, 'update_config', AsyncMock())
    monkeypatch.setattr(jwt_utils, '_CACHED_SECRET', None)
    monkeypatch.delenv('JWT_SECRET', raising=False)
    return app


SETUP_PAYLOAD = {'username': 'admin', 'password': 'password123', 'confirm_password': 'password123'}


@pytest.mark.asyncio
@pytest.mark.parametrize('token', [None, 'wrong-key', 'user-key', 'forged-jwt'])
async def test_existing_config_setup_requires_admin_credentials(setup_app, token):
    if token == 'forged-jwt':
        token = token_signed_with('dev-insecure-jwt-secret')
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=setup_app), base_url='http://test') as client:
        response = await client.post('/setup/init', json=SETUP_PAYLOAD, headers=headers)
    assert response.status_code == 403
    assert 'admin-key' not in response.text
    setup._create_admin_user.assert_not_awaited()
    setup.update_config.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('header', ['Authorization', 'x-api-key'])
async def test_existing_config_can_be_repaired_with_admin_key(setup_app, header):
    value = 'Bearer admin-key' if header == 'Authorization' else 'admin-key'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=setup_app), base_url='http://test') as client:
        response = await client.post('/setup/init', json=SETUP_PAYLOAD, headers={header: value})
    assert response.status_code == 200
    assert response.json()['admin_api_key'] == 'admin-key'
    setup._create_admin_user.assert_awaited_once()
    setup.update_config.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('has_config', [True, False])
async def test_existing_admin_can_never_be_reset_through_setup(setup_app, monkeypatch, has_config):
    if not has_config:
        setup_app.state.api_list = []
    monkeypatch.setattr(setup, '_get_admin_user', AsyncMock(return_value=SimpleNamespace(username='original')))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=setup_app), base_url='http://test') as client:
        response = await client.post('/setup/init', json=SETUP_PAYLOAD)
    assert response.status_code == 409
    setup._create_admin_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_blank_setup_is_anonymous_and_serialized(setup_app, monkeypatch):
    setup_app.state.api_list = []
    setup_app.state.api_keys_db = []
    setup_app.state.config = {}
    record = None

    async def get_admin():
        await asyncio.sleep(0)
        return record

    async def create_admin(*args):
        nonlocal record
        record = SimpleNamespace(username=args[0])

    async def update(conf, **kwargs):
        return conf, conf['api_keys'], [key['api'] for key in conf['api_keys']]

    monkeypatch.setattr(setup, '_get_admin_user', get_admin)
    create = AsyncMock(side_effect=create_admin)
    monkeypatch.setattr(setup, '_create_admin_user', create)
    monkeypatch.setattr(setup, 'update_config', AsyncMock(side_effect=update))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=setup_app), base_url='http://test') as client:
        results = await asyncio.gather(*[client.post('/setup/init', json=SETUP_PAYLOAD) for _ in range(2)])
    assert sorted(response.status_code for response in results) == [200, 409]
    create.assert_awaited_once()
