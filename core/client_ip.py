"""统一的客户端真实 IP 解析（HTTP 与 WebSocket 共用）。

修改原因：旧逻辑无条件信任 X-Forwarded-For / X-Real-IP，且取 XFF 最左值，
直连或伪造头即可欺骗 IP 黑名单、配额和统计。
修改方式：仅当直连对端属于可信代理（TRUSTED_PROXIES）时才解析转发头，
并按“从右向左跳过可信代理”的标准算法取第一个不可信地址。
目的：默认信任回环与私网代理（兼容本机 nginx / Docker 网桥反代），
外部直连无法伪造来源 IP；需要更严格时显式配置可信代理列表。
"""

from __future__ import annotations

import os
from ipaddress import ip_address, ip_network
from typing import Optional

# 默认可信代理：回环 + RFC1918/ULA 私网（覆盖本机反代与 Docker 网桥场景）。
# 公网直连对端永远不可信，其转发头会被忽略。
DEFAULT_TRUSTED_PROXIES = (
    "127.0.0.0/8,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fd00::/8"
)

_ENV_NAME = "TRUSTED_PROXIES"

# 解析结果缓存：进程内环境变量不变，避免每个请求重复解析 CIDR。
_cached_raw: Optional[str] = None
_cached_networks: Optional[list] = None
_cached_trust_all: bool = False


def _load_trusted_networks() -> tuple[list, bool]:
    """读取并缓存 TRUSTED_PROXIES 配置。

    返回 (networks, trust_all)。值 "*" 表示信任所有对端（兼容旧行为，不推荐）。
    """
    global _cached_raw, _cached_networks, _cached_trust_all

    raw = (os.getenv(_ENV_NAME) or DEFAULT_TRUSTED_PROXIES).strip()
    if raw == _cached_raw and _cached_networks is not None:
        return _cached_networks, _cached_trust_all

    trust_all = False
    networks = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if part == "*":
            trust_all = True
            continue
        try:
            networks.append(ip_network(part, strict=False))
        except ValueError:
            # 配置错误的条目直接忽略，宁可少信任也不放大信任面
            continue

    _cached_raw = raw
    _cached_networks = networks
    _cached_trust_all = trust_all
    return networks, trust_all


def is_trusted_proxy(peer_ip: Optional[str]) -> bool:
    """判断直连对端是否属于可信代理。"""
    if not peer_ip:
        return False
    networks, trust_all = _load_trusted_networks()
    if trust_all:
        return True
    try:
        addr = ip_address(peer_ip.strip())
    except ValueError:
        return False
    return any(addr in net for net in networks)


def resolve_client_ip(
    peer_ip: Optional[str],
    forwarded_for: Optional[str] = None,
    real_ip: Optional[str] = None,
) -> str:
    """解析客户端真实 IP。

    - 对端不可信：直接返回对端地址，忽略一切转发头（防伪造）。
    - 对端可信：在 X-Forwarded-For 中从右向左找第一个不可信地址；
      整条链都可信时取最左值；无 XFF 时回退 X-Real-IP，再回退对端。
    """
    peer = (peer_ip or "").strip()

    if not is_trusted_proxy(peer):
        return peer or "unknown"

    if forwarded_for:
        parts = [p.strip() for p in forwarded_for.split(",") if p.strip()]
        for addr in reversed(parts):
            if not is_trusted_proxy(addr):
                return addr
        if parts:
            return parts[0]

    if real_ip and real_ip.strip():
        return real_ip.strip()

    return peer or "unknown"
