"""
HTTP 客户端管理模块

负责统一管理 httpx.AsyncClient 连接池，根据 host + proxy 维度复用客户端。
"""

from contextlib import asynccontextmanager
from urllib.parse import urlparse
from typing import Dict, Optional

import httpx

from core.utils import get_proxy


class ClientManager:
    """
    HTTP 客户端管理器

    - 按 host + proxy 维度复用 httpx.AsyncClient
    - 通过 init() 注入默认配置（headers/http2/verify/follow_redirects 等）
    """

    def __init__(self, pool_size: int = 100) -> None:
        self.pool_size = pool_size
        self.clients: Dict[str, httpx.AsyncClient] = {}
        self.default_config: dict = {}

    async def init(self, default_config: dict) -> None:
        """
        设置默认 client 配置
        """
        self.default_config = default_config

    @asynccontextmanager
    async def get_client(self, base_url: str, proxy: Optional[str] = None):
        """
        获取或创建指定 base_url + proxy 对应的 AsyncClient
        """
        parsed_url = urlparse(base_url)
        host = parsed_url.netloc

        client_key = f"{host}"
        if proxy:
            # 规范化 socks5 代理前缀，保持与原实现一致
            proxy_normalized = proxy.replace("socks5h://", "socks5://")
            client_key += f"_{proxy_normalized}"

        if client_key not in self.clients:
            timeout = httpx.Timeout(
                connect=15.0,
                read=None,  # 为单个请求设置超时
                write=30.0,
                pool=self.pool_size,
            )
            limits = httpx.Limits(max_connections=self.pool_size)

            client_config = {
                **self.default_config,
                "timeout": timeout,
                "limits": limits,
            }

            client_config = get_proxy(proxy, client_config)

            self.clients[client_key] = httpx.AsyncClient(**client_config)

        try:
            yield self.clients[client_key]
        finally:
            # 不在这里关闭客户端，由 close() 统一管理连接池生命周期
            pass

    async def close(self) -> None:
        """
        关闭所有已创建的 AsyncClient，并清空连接池
        """
        for client in self.clients.values():
            await client.aclose()
        self.clients.clear()