"""
HTTP 客户端管理模块

负责统一管理 httpx.AsyncClient 连接池，根据 host + proxy 维度复用客户端。
"""

import asyncio
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

    def __init__(self, pool_size: int = 300, max_keepalive_connections: int = 100) -> None:
        """
        初始化客户端管理器
        
        Args:
            pool_size: 最大并发连接数（增加到300支持更多长时间请求）
            max_keepalive_connections: keepalive 连接数
        """
        self.pool_size = pool_size
        self.max_keepalive_connections = max_keepalive_connections
        self.clients: Dict[str, httpx.AsyncClient] = {}
        self.default_config: dict = {}
        self._client_locks: Dict[str, asyncio.Lock] = {}

    async def init(self, default_config: dict) -> None:
        """
        设置默认 client 配置
        """
        self.default_config = default_config

    def _get_client_lock(self, client_key: str) -> asyncio.Lock:
        lock = self._client_locks.get(client_key)
        if lock is None:
            lock = asyncio.Lock()
            self._client_locks[client_key] = lock
        return lock

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
            async with self._get_client_lock(client_key):
                if client_key not in self.clients:
                    timeout = httpx.Timeout(
                        connect=15.0,
                        read=None,  # 保持None，由各渠道自行控制超时
                        write=300.0,  # 写入超时增加到300秒（5分钟），支持大型请求体（多图片/PDF）
                        pool=10.0,  # 获取连接的超时（防止永久阻塞）
                    )
                    limits = httpx.Limits(
                        max_connections=self.pool_size,
                        max_keepalive_connections=self.max_keepalive_connections
                    )

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
        self._client_locks.clear()

    async def reset_client(self, host: str) -> bool:
        """
        重置指定 host 的客户端连接
        
        用于解决 HTTP/2 连接老化导致的 StreamReset 错误
        
        Args:
            host: 要重置的 host
            
        Returns:
            是否找到并重置了客户端
        """
        keys_to_remove = [k for k in self.clients.keys() if host in k]
        if not keys_to_remove:
            return False
        
        for key in keys_to_remove:
            client = self.clients.pop(key)
            await client.aclose()
            self._client_locks.pop(key, None)
        return True

    async def reset_all_clients(self) -> int:
        """
        重置所有客户端连接（不需要重启服务）
        
        Returns:
            重置的客户端数量
        """
        count = len(self.clients)
        await self.close()
        return count