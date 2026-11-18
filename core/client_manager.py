"""
HTTP 客户端管理模块

负责统一管理 httpx.AsyncClient 连接池，根据 host + proxy 维度复用客户端。
"""

import asyncio
import os
from contextlib import asynccontextmanager
from urllib.parse import urlparse
from typing import Dict, Optional

import httpx

from core.env import env_bool
from core.utils import get_proxy


_POOL_ACQUIRE_TIMEOUT = 10.0
_KEEPALIVE_EXPIRY = 120.0
_HTTP2_DEFAULT_HOSTS = (
    "anthropic.com",
    "claude.ai",
    "openai.com",
    "chatgpt.com",
    "googleapis.com",
    "openrouter.ai",
    "deepseek.com",
    "x.ai",
    "cloudflare.com",
    "groq.com",
    "mistral.ai",
    "aliyuncs.com",
)


async def _enforce_pool_acquire_timeout(request: httpx.Request) -> None:
    """防止单次请求的长读取超时覆盖连接池等待上限。"""
    timeout = request.extensions.get("timeout")
    if not isinstance(timeout, dict):
        return
    pool_timeout = timeout.get("pool")
    if pool_timeout is None or pool_timeout > _POOL_ACQUIRE_TIMEOUT:
        timeout["pool"] = _POOL_ACQUIRE_TIMEOUT


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
            client_key += f"_{proxy.strip()}"

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
                        max_keepalive_connections=self.max_keepalive_connections,
                        keepalive_expiry=_KEEPALIVE_EXPIRY,
                    )

                    client_config = {
                        **self.default_config,
                        "timeout": timeout,
                        "limits": limits,
                    }
                    # 修改原因：渠道传入 timeout=900 这类整数时，httpx 会把 pool timeout 也覆盖为 900 秒。
                    # 修改方式：在请求发送前把 pool timeout 限制为 10 秒，并保留调用方设置的 read timeout。
                    # 目的：连接池耗尽时尽快进入渠道重试，避免 keepalive 先返回假 200 后等待十五分钟。
                    event_hooks = dict(client_config.get("event_hooks") or {})
                    event_hooks["request"] = [
                        *event_hooks.get("request", []),
                        _enforce_pool_acquire_timeout,
                    ]
                    client_config["event_hooks"] = event_hooks

                    # 对主流官方 HTTPS 上游默认开启 HTTP/2 多路复用（可通过 HTTP2_ENABLED 环境变量控制）
                    if "http2" not in client_config and "transport" not in client_config:
                        host_lower = host.lower()
                        if os.getenv("HTTP2_ENABLED") is not None:
                            client_config["http2"] = env_bool("HTTP2_ENABLED", True)
                        elif parsed_url.scheme == "https" and any(h in host_lower for h in _HTTP2_DEFAULT_HOSTS):
                            client_config["http2"] = True

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
