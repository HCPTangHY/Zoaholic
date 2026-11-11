"""HTTP transport for untrusted media URLs, with DNS pinned at connect time."""

import asyncio
import ipaddress
import socket

import httpcore
import httpx

_IPV4_PROTOCOL_ASSIGNMENTS = ipaddress.ip_network("192.0.0.0/24")
_IPV6_GLOBAL_UNICAST = ipaddress.ip_network("2000::/3")
_IPV6_DOCUMENTATION = ipaddress.ip_network("3fff::/20")


class PublicNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self) -> None:
        self._backend = httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        # Resolve once, validate every answer, then connect to a numeric address.
        # The original hostname stays in the pool origin, HTTP Host and TLS SNI.
        try:
            async with asyncio.timeout(timeout):
                answers = await asyncio.get_running_loop().getaddrinfo(
                    host, port, type=socket.SOCK_STREAM
                )
                addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
                if not addresses:
                    raise ValueError("No addresses")
                for address in addresses:
                    ip = ipaddress.ip_address(address)
                    mapped = getattr(ip, "ipv4_mapped", None)
                    effective_ip = mapped or ip
                    if not effective_ip.is_global or effective_ip.is_multicast or effective_ip.is_reserved:
                        raise ValueError("Non-public address")
                    # Python 3.11/early 3.12 classify some IANA special-use ranges as global.
                    if (
                        isinstance(effective_ip, ipaddress.IPv4Address)
                        and effective_ip in _IPV4_PROTOCOL_ASSIGNMENTS
                    ):
                        raise ValueError("Special-use IPv4 address")
                    if isinstance(ip, ipaddress.IPv6Address) and not mapped:
                        # Exclude translation/tunnel ranges that can embed private IPv4 destinations.
                        if (
                            ip not in _IPV6_GLOBAL_UNICAST or ip in _IPV6_DOCUMENTATION
                            or ip.sixtofour or ip.teredo
                        ):
                            raise ValueError("Non-public IPv6 address")

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
        async for chunk in self._stream:
            yield chunk

    async def aclose(self):
        await self._stream.aclose()


class PublicURLTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(trust_env=False),
            max_connections=50,
            max_keepalive_connections=20,
            http2=True,
            network_backend=PublicNetworkBackend(),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
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
        await self._pool.aclose()
