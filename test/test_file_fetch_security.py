"""Offline SSRF regressions, exercising HTTPX/httpcore and a fake physical socket.

DNS and the final numeric TCP dial are observed separately. No external network,
production credentials, local-file HTTP requests or executable payloads are used.
"""

import asyncio
import base64
import socket
import ssl

import httpcore
import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException

from core import file_utils
from core.public_fetch import (
    PublicURLTransport, PublicNetworkBackend, UnsafeFileURL,
    validate_file_url, validate_public_address,
)


class WireStream(httpcore.AsyncNetworkStream):
    def __init__(self, response):
        self.response = response
        self.writes = []
        self.tls = []
        self.closed = False

    async def read(self, max_bytes, timeout=None):
        data, self.response = self.response[:max_bytes], self.response[max_bytes:]
        return data

    async def write(self, buffer, timeout=None):
        self.writes.append(buffer)

    async def aclose(self):
        self.closed = True

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.tls.append((server_hostname, ssl_context.verify_mode, ssl_context.check_hostname))
        return self

    def get_extra_info(self, info):
        return False if info == "is_readable" else None


class PhysicalBackend:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.streams = []

    async def connect_tcp(self, host, port, **kwargs):
        # A hostname passed here would allow a second DNS lookup and fail this test.
        validate_public_address(host)
        self.calls.append((host, port))
        stream = WireStream(self.responses.pop(0))
        self.streams.append(stream)
        return stream


def reply(body=b"public file", status=b"200 OK", headers=b""):
    return (b"HTTP/1.1 " + status + b"\r\nContent-Length: " + str(len(body)).encode()
            + b"\r\nConnection: close\r\n" + headers + b"\r\n" + body)


def dns_answer(ip, port=443):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))


@pytest_asyncio.fixture
async def resolver(monkeypatch):
    calls = []
    answers = {"files.example": ["93.184.216.34"], "cdn.example": ["8.8.8.8"]}

    async def resolve(host, port, **kwargs):
        calls.append((host, port))
        return [dns_answer(ip, port) for ip in answers[host]]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    return answers, calls


def client_with_wire(monkeypatch, responses):
    limits = httpx.Limits(max_connections=2, max_keepalive_connections=0)
    transport = PublicURLTransport(limits)
    physical = PhysicalBackend(responses)
    transport._pool._network_backend._backend = physical
    client = httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
                               headers={"Accept-Encoding": "identity"})

    async def shared():
        return client

    monkeypatch.setattr(file_utils, "_get_shared_fetch_client", shared)
    return client, physical


@pytest.mark.parametrize("address", [
    "127.0.0.1", "0.0.0.0", "10.1.2.3", "172.16.0.1", "192.168.1.1",
    "169.254.169.254", "100.64.0.1", "224.0.0.1", "240.0.0.1", "192.0.0.9",
    "192.0.2.1", "192.88.99.1", "198.18.0.1", "168.63.129.16", "::", "::1", "fc00::1",
    "fe80::1", "ff02::1", "::ffff:127.0.0.1", "::ffff:8.8.8.8",
    "64:ff9b::808:808", "64:ff9b:1::808:808", "2002:0808:0808::1",
    "2001:0000:4136:e378:8000:63bf:3fff:fdd2", "2001:db8::1", "3fff::1",
    "2001:4860::5efe:7f00:1", "2001:4860::200:5efe:7f00:1", "fe80::1%eth0",
])
def test_private_special_and_transition_addresses_rejected(address):
    with pytest.raises(UnsafeFileURL):
        validate_public_address(address)


@pytest.mark.parametrize("url", [
    "file:///etc/hosts", "ftp://files.example/file", "http://user:password@files.example/a",
    "http://127.0.0.1/file", "https://[::1]/file", "http://[fe80::1%25eth0]/file",
    "http:///file", "relative-file", "http://files.example:99999/file",
])
def test_url_validation_rejects_unsupported_and_private_urls(url):
    with pytest.raises(UnsafeFileURL):
        validate_file_url(url)


@pytest.mark.asyncio
async def test_literal_private_url_rejected_before_client_creation(monkeypatch):
    async def forbidden():
        pytest.fail("Private URL reached HTTP client")
    monkeypatch.setattr(file_utils, "_get_shared_fetch_client", forbidden)
    with pytest.raises(HTTPException) as error:
        await file_utils.fetch_url_content("http://127.0.0.1:8101/anything")
    assert error.value.status_code == 400
    assert "127.0.0.1" not in error.value.detail


@pytest.mark.asyncio
async def test_public_fetch_pins_ip_preserves_host_tls_sni_and_verification(monkeypatch, resolver):
    client, physical = client_with_wire(monkeypatch, [reply(headers=b"Content-Type: text/plain; charset=utf-8\r\n")])
    async with client:
        content, mime = await file_utils.fetch_url_content("https://files.example/document?signature=not-logged")
    assert content == b"public file" and mime == "text/plain"
    assert resolver[1] == [("files.example", 443)]
    assert physical.calls == [("93.184.216.34", 443)]
    stream = physical.streams[0]
    assert stream.tls == [("files.example", ssl.CERT_REQUIRED, True)]
    request = b"".join(stream.writes).lower()
    assert b"host: files.example\r\n" in request
    assert b"accept-encoding: identity\r\n" in request


@pytest.mark.asyncio
async def test_ipv6_public_dns_answer_uses_numeric_connection(monkeypatch, resolver):
    resolver[0]["files.example"] = ["2606:4700:4700::1111"]
    client, physical = client_with_wire(monkeypatch, [reply()])
    async with client:
        await file_utils.fetch_url_content("https://files.example/file")
    assert physical.calls == [("2606:4700:4700::1111", 443)]
    assert physical.streams[0].tls[0][0] == "files.example"


@pytest.mark.asyncio
async def test_mixed_public_private_dns_answers_never_dial(monkeypatch, resolver):
    resolver[0]["files.example"] = ["93.184.216.34", "127.0.0.1"]
    client, physical = client_with_wire(monkeypatch, [])
    async with client:
        with pytest.raises(HTTPException) as error:
            await file_utils.fetch_url_content("https://files.example/file")
    assert error.value.status_code == 400 and physical.calls == []


@pytest.mark.asyncio
async def test_dns_rebinding_cannot_change_validated_socket_destination(monkeypatch):
    dns_calls = []

    async def rebinding_resolver(host, port, **kwargs):
        dns_calls.append(host)
        return [dns_answer("93.184.216.34" if len(dns_calls) == 1 else "127.0.0.1", port)]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", rebinding_resolver)
    client, physical = client_with_wire(monkeypatch, [reply()])
    async with client:
        await file_utils.fetch_url_content("https://files.example/file")
        with pytest.raises(HTTPException):
            await file_utils.fetch_url_content("https://files.example/another-file")
    assert dns_calls == ["files.example", "files.example"]
    assert physical.calls == [("93.184.216.34", 443)]


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["http://127.0.0.1/private", "http://[::1]/private", "file:///private", "http://user:password@cdn.example/a"])
async def test_redirect_to_private_or_credentials_stops_before_second_request(monkeypatch, resolver, location):
    client, physical = client_with_wire(monkeypatch, [reply(b"", b"302 Found", b"Location: " + location.encode() + b"\r\n")])
    async with client:
        with pytest.raises(HTTPException):
            await file_utils.fetch_url_content("https://files.example/file")
    assert len(physical.calls) == 1
    assert resolver[1] == [("files.example", 443)]


@pytest.mark.asyncio
async def test_redirect_hostname_is_checked_at_second_connection(monkeypatch, resolver):
    resolver[0]["cdn.example"] = ["10.0.0.9"]
    client, physical = client_with_wire(monkeypatch, [reply(b"", b"302 Found", b"Location: https://cdn.example/file\r\n")])
    async with client:
        with pytest.raises(HTTPException):
            await file_utils.fetch_url_content("https://files.example/file")
    assert resolver[1] == [("files.example", 443), ("cdn.example", 443)]
    assert physical.calls == [("93.184.216.34", 443)]


@pytest.mark.asyncio
async def test_obfuscated_ipv4_hostname_is_checked_after_dns(monkeypatch, resolver):
    resolver[0]["2130706433"] = ["127.0.0.1"]
    client, physical = client_with_wire(monkeypatch, [])
    async with client:
        with pytest.raises(HTTPException):
            await file_utils.fetch_url_content("http://2130706433/file")
    assert physical.calls == []


@pytest.mark.asyncio
async def test_allowed_numeric_socket_does_not_issue_a_second_dns_query(monkeypatch):
    # Exercise the real AnyIO backend through asyncio.create_connection. It must
    # receive a numeric address; intercept only sock_connect, before real I/O.
    loop = asyncio.get_running_loop()
    dns_calls, socket_targets = [], []

    async def resolver(host, port, **kwargs):
        dns_calls.append(host)
        return [dns_answer("93.184.216.34" if len(dns_calls) == 1 else "127.0.0.1", port)]

    async def intercept(sock, address):
        socket_targets.append(address)
        raise ConnectionRefusedError("isolated socket boundary")

    monkeypatch.setattr(loop, "getaddrinfo", resolver)
    monkeypatch.setattr(loop, "sock_connect", intercept)
    backend = PublicNetworkBackend()
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("files.example", 443, timeout=1)
    assert dns_calls == ["files.example"]
    assert socket_targets == [("93.184.216.34", 443)]


@pytest.mark.asyncio
async def test_public_redirect_allowed_and_loop_is_bounded(monkeypatch, resolver):
    client, physical = client_with_wire(monkeypatch, [
        reply(b"", b"302 Found", b"Location: https://cdn.example/file\r\n"), reply(b"from cdn"),
    ])
    async with client:
        assert (await file_utils.fetch_url_content("https://files.example/file"))[0] == b"from cdn"
    assert physical.calls == [("93.184.216.34", 443), ("8.8.8.8", 443)]
    monkeypatch.setattr(file_utils, "MAX_FILE_REDIRECTS", 2)
    client, physical = client_with_wire(monkeypatch, [reply(b"", b"302 Found", b"Location: /loop\r\n")] * 3)
    async with client:
        with pytest.raises(HTTPException, match="redirect limit"):
            await file_utils.fetch_url_content("https://files.example/loop")
    assert len(physical.calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    reply(b"123456789"),
    b"HTTP/1.1 200 OK\r\nConnection: close\r\nTransfer-Encoding: chunked\r\n\r\n5\r\n12345\r\n4\r\n6789\r\n0\r\n\r\n",
])
async def test_download_limit_with_and_without_content_length(monkeypatch, resolver, response):
    monkeypatch.setattr(file_utils, "MAX_FILE_BYTES", 8)
    client, physical = client_with_wire(monkeypatch, [response])
    async with client:
        with pytest.raises(HTTPException, match="download limit"):
            await file_utils.fetch_url_content("https://files.example/file")
    assert physical.streams[0].closed


@pytest.mark.asyncio
async def test_unsolicited_compression_rejected_without_decompression(monkeypatch, resolver):
    # Deliberately invalid gzip: no decompressor should run before rejection.
    client, physical = client_with_wire(monkeypatch, [reply(b"not-gzip", headers=b"Content-Encoding: gzip\r\n")])
    async with client:
        with pytest.raises(HTTPException, match="Compressed HTTP"):
            await file_utils.fetch_url_content("https://files.example/file")
    assert physical.streams[0].closed


@pytest.mark.asyncio
async def test_dns_timeout_errors_do_not_expose_signed_url_or_network_details(monkeypatch):
    async def fail(host, port, **kwargs):
        raise socket.gaierror("private resolver internal detail")
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fail)
    client, physical = client_with_wire(monkeypatch, [])
    async with client:
        with pytest.raises(HTTPException) as error:
            await file_utils.fetch_url_content("https://files.example/file?token=secret")
    assert error.value.detail == "Unable to download the requested file"
    assert physical.calls == []


@pytest.mark.asyncio
async def test_dns_resolution_is_awaitable_and_in_connect_deadline(monkeypatch):
    started = asyncio.Event()
    async def stall(*args, **kwargs):
        started.set()
        await asyncio.sleep(30)
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", stall)
    backend = PublicNetworkBackend()
    task = asyncio.create_task(backend.connect_tcp("files.example", 443, timeout=0.02))
    await asyncio.wait_for(started.wait(), 0.1)
    with pytest.raises(httpcore.ConnectTimeout):
        await task


@pytest.mark.asyncio
async def test_shared_client_disables_proxy_environment_and_validates_certificates(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9999")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    monkeypatch.setenv("SSL_CERT_FILE", "/path/that/must/not/be/read")
    monkeypatch.setattr(file_utils, "_shared_fetch_client", None)
    monkeypatch.setattr(file_utils, "_shared_fetch_client_lock", asyncio.Lock())
    try:
        client = await file_utils._get_shared_fetch_client()
        assert not client._trust_env and not client.follow_redirects
        assert not client._mounts
        pool = client._transport._pool
        assert isinstance(pool._network_backend, PublicNetworkBackend)
        assert pool._ssl_context.verify_mode == ssl.CERT_REQUIRED
        assert pool._ssl_context.check_hostname
    finally:
        await file_utils.close_shared_fetch_client()


@pytest.mark.asyncio
async def test_data_uri_and_raw_base64_compatibility():
    data = "data:text/plain;base64," + base64.b64encode(b"hello").decode()
    assert await file_utils.get_base64_file(data) == (data, "text/plain")
    assert await file_utils.get_base64_file("aGVsbG8=") == ("aGVsbG8=", "application/octet-stream")
    with pytest.raises(HTTPException):
        await file_utils.get_base64_file("file:///not-read")


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [
    "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout", "TimeoutException",
    "ConnectError", "ReadError", "WriteError", "NetworkError", "ProxyError",
    "UnsupportedProtocol", "LocalProtocolError", "RemoteProtocolError", "ProtocolError",
])
async def test_public_transport_maps_request_errors_to_httpx(monkeypatch, name):
    from unittest.mock import AsyncMock

    transport = PublicURLTransport()
    monkeypatch.setattr(transport._pool, "handle_async_request", AsyncMock(side_effect=getattr(httpcore, name)("synthetic")))
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        with pytest.raises(getattr(httpx, name)) as error:
            await client.get("https://no-network.example/file")
    assert type(error.value) is getattr(httpx, name)
    assert isinstance(error.value.__cause__, getattr(httpcore, name))


@pytest.mark.asyncio
@pytest.mark.parametrize("stage,name", [
    ("read", "ReadTimeout"), ("read", "ReadError"), ("read", "RemoteProtocolError"),
    ("close", "WriteError"), ("close", "NetworkError"),
])
async def test_public_transport_maps_response_stream_errors(monkeypatch, stage, name):
    from unittest.mock import AsyncMock

    class FailingStream:
        async def __aiter__(self):
            if stage == "read":
                raise getattr(httpcore, name)("synthetic stream failure")
            yield b"valid body"

        async def aclose(self):
            if stage == "close":
                raise getattr(httpcore, name)("synthetic close failure")

    transport = PublicURLTransport()
    monkeypatch.setattr(transport._pool, "handle_async_request", AsyncMock(return_value=httpcore.Response(200, content=FailingStream())))
    async with transport:
        response = await transport.handle_async_request(httpx.Request("GET", "https://no-network.example/file"))
        with pytest.raises(getattr(httpx, name)) as error:
            if stage == "read":
                await response.aread()
            else:
                await response.aclose()
    assert type(error.value) is getattr(httpx, name)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["request", "read"])
async def test_public_transport_preserves_cancellation(monkeypatch, stage):
    from unittest.mock import AsyncMock

    transport = PublicURLTransport()

    class CancelledStream:
        async def __aiter__(self):
            raise asyncio.CancelledError()
            yield b"unreachable"

        async def aclose(self):
            pass

    if stage == "request":
        handler = AsyncMock(side_effect=asyncio.CancelledError())
    else:
        handler = AsyncMock(return_value=httpcore.Response(200, content=CancelledStream()))
    monkeypatch.setattr(transport._pool, "handle_async_request", handler)
    async with transport:
        with pytest.raises(asyncio.CancelledError):
            response = await transport.handle_async_request(httpx.Request("GET", "https://no-network.example/file"))
            await response.aread()


@pytest.mark.asyncio
async def test_fetch_preserves_cancellation(monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(file_utils, "_get_shared_fetch_client", AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await file_utils.fetch_url_content("https://no-network.example/file")
