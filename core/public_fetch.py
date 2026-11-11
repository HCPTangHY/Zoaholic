"""HTTP transport for untrusted media URLs, with DNS pinned at connect time."""

import asyncio
import ipaddress
import socket
from contextlib import contextmanager

import httpcore
import httpx

_IPV6_GLOBAL_UNICAST = ipaddress.ip_network("2000::/3")
_BLOCKED_V4 = tuple(ipaddress.ip_network(network) for network in (
    "192.0.0.0/24", "192.88.99.0/24", "168.63.129.16/32",
))
_BLOCKED_V6 = tuple(ipaddress.ip_network(network) for network in (
    "64:ff9b::/96", "64:ff9b:1::/48", "2001::/23", "2002::/16", "3fff::/20",
))


class UnsafeFileURL(ValueError):
    """A user-supplied file URL targets a destination outside the public network."""


def validate_public_address(address: str) -> str:
    if "%" in address:
        raise UnsafeFileURL("Scoped IP addresses are not allowed")
    ip = ipaddress.ip_address(address)
    if not ip.is_global or any((ip.is_private, ip.is_loopback, ip.is_link_local,
                               ip.is_multicast, ip.is_reserved, ip.is_unspecified)):
        raise UnsafeFileURL("Only public destinations are allowed")
    if isinstance(ip, ipaddress.IPv4Address):
        if any(ip in network for network in _BLOCKED_V4):
            raise UnsafeFileURL("Platform addresses are not allowed")
    elif (ip not in _IPV6_GLOBAL_UNICAST or ip.ipv4_mapped is not None
          or any(ip in network for network in _BLOCKED_V6)
          or ((int(ip) >> 32) & 0xFFFFFFFF) in (0x00005EFE, 0x02005EFE)):
        # Tunnel/translation addresses may encode a private IPv4 destination.
        raise UnsafeFileURL("IPv6 transition addresses are not allowed")
    return ip.compressed


def validate_file_url(value: str | httpx.URL) -> httpx.URL:
    try:
        url = httpx.URL(value)
        if (url.scheme not in ("http", "https") or not url.host or url.userinfo
                or "%" in url.host or (url.port is not None and not 1 <= url.port <= 65535)):
            raise UnsafeFileURL("Invalid file URL")
        try:
            ipaddress.ip_address(url.host)
        except ValueError:
            pass  # DNS names are checked at the actual connection boundary.
        else:
            validate_public_address(url.host)
        return url
    except (httpx.InvalidURL, ValueError) as exc:
        raise UnsafeFileURL("File URL must use an allowed public destination") from exc


# Specific exceptions precede their base classes. Both request setup and streamed
# response reads must expose HTTPX's public exception contract, including close.
_HTTP_CORE_ERRORS = tuple((getattr(httpcore, name), getattr(httpx, name)) for name in (
    "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout", "TimeoutException",
    "ConnectError", "ReadError", "WriteError", "NetworkError", "ProxyError",
    "UnsupportedProtocol", "LocalProtocolError", "RemoteProtocolError", "ProtocolError",
))


@contextmanager
def _map_httpcore_errors():
    try:
        yield
    except Exception as exc:
        for core_type, httpx_type in _HTTP_CORE_ERRORS:
            if isinstance(exc, core_type):
                raise httpx_type(str(exc)) from exc
        raise


class PublicNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self) -> None:
        self._backend = httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        # Resolve once, validate every answer, then connect to a numeric address.
        # The original hostname stays in the pool origin, HTTP Host and TLS SNI.
        try:
            async with asyncio.timeout(timeout):
                try:
                    ipaddress.ip_address(host)
                except ValueError:
                    answers = await asyncio.get_running_loop().getaddrinfo(
                        host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM,
                    )
                    addresses = list(dict.fromkeys(validate_public_address(answer[4][0]) for answer in answers))
                else:
                    addresses = [validate_public_address(host)]
                if not addresses:
                    raise ValueError("No addresses")

                for index, address in enumerate(addresses):
                    try:
                        return await self._backend.connect_tcp(
                            address, port, timeout=timeout, local_address=local_address,
                            socket_options=socket_options,
                        )
                    except (httpcore.ConnectError, httpcore.ConnectTimeout):
                        if index == len(addresses) - 1:
                            raise
        except TimeoutError:
            raise httpcore.ConnectTimeout("File URL connection timed out") from None
        except (OSError, ValueError):
            raise httpcore.ConnectError("File URL must resolve only to public addresses") from None

    async def sleep(self, seconds):
        await self._backend.sleep(seconds)


class _ResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream):
        self._stream = stream

    async def __aiter__(self):
        with _map_httpcore_errors():
            async for chunk in self._stream:
                yield chunk

    async def aclose(self):
        with _map_httpcore_errors():
            await self._stream.aclose()


class PublicURLTransport(httpx.AsyncBaseTransport):
    def __init__(self, limits: httpx.Limits | None = None) -> None:
        limits = limits or httpx.Limits(max_connections=50, max_keepalive_connections=20)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(trust_env=False),
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            http2=True,
            network_backend=PublicNetworkBackend(),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        with _map_httpcore_errors():
            response = await self._pool.handle_async_request(httpcore.Request(
                method=request.method,
                url=httpcore.URL(
                    scheme=request.url.raw_scheme, host=request.url.raw_host,
                    port=request.url.port, target=request.url.raw_path,
                ),
                headers=request.headers.raw,
                content=request.stream,
                extensions=request.extensions,
            ))
        return httpx.Response(
            response.status, headers=response.headers,
            stream=_ResponseStream(response.stream), extensions=response.extensions,
        )

    async def aclose(self) -> None:
        with _map_httpcore_errors():
            await self._pool.aclose()
