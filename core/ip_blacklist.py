"""IP 黑名单解析与匹配工具。"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address, ip_network
import os
from typing import Any, Iterable, Union

from fastapi import HTTPException
from fastapi.responses import JSONResponse


IPAddress = Union[IPv4Address, IPv6Address]
IPNetwork = Union[IPv4Network, IPv6Network]
IP_BLOCKED_MESSAGE = "Your IP has been blocked. Please contact the administrator."
IP_BLOCKED_TYPE = "ip_blocked"
_DEFAULT_TRUSTED_PROXIES = (
    "127.0.0.0/8",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "::1/128",
    "fc00::/7",
)
_TRUSTED_PROXY_CACHE: tuple[str, bool, ParsedIPBlacklist] | None = None


@dataclass(frozen=True)
class ParsedIPBlacklist:
    """预解析后的 IP 黑名单，精确 IP 用 set，CIDR 网段用 list。"""

    exact: frozenset[IPAddress]
    networks: tuple[IPNetwork, ...]


def _coerce_blacklist_items(raw_entries: Any) -> list[str]:
    """把配置中的黑名单字段规范成字符串列表。

    修改原因：api.yaml 可能来自 YAML、JSON 或前端保存，用户也可能用逗号/换行混合输入。
    修改方式：支持 list、tuple、set 和字符串，并把逗号、换行、空白分隔出的条目逐一去空。
    目的：让顶层 ip_blacklist 和 api_keys[].ip_blacklist 都稳定保存为字符串数组。
    """
    if raw_entries is None:
        return []

    values: list[Any]
    if isinstance(raw_entries, str):
        values = [raw_entries]
    elif isinstance(raw_entries, (list, tuple, set)):
        values = list(raw_entries)
    else:
        values = [raw_entries]

    normalized: list[str] = []
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        for part in text.replace(",", "\n").splitlines():
            item = part.strip()
            if item:
                normalized.append(item)
    return normalized


def normalize_ip_blacklist(raw_entries: Any) -> list[str]:
    """规范并校验 IP 黑名单条目，返回可持久化的字符串数组。"""
    entries = _coerce_blacklist_items(raw_entries)
    for entry in entries:
        # 修改原因：需要在保存配置时就发现无效 IP/CIDR，避免运行时鉴权出现静默绕过。
        # 修改方式：CIDR 走 ip_network(strict=False)，精确 IP 走 ip_address，二者任一失败即抛错。
        # 目的：让管理端保存阶段直接返回配置错误，而不是在请求期才暴露。
        try:
            if "/" in entry:
                ip_network(entry, strict=False)
            else:
                ip_address(entry)
        except ValueError as exc:
            raise ValueError(f"Invalid ip_blacklist entry: {entry}") from exc
    return entries


def parse_ip_blacklist(raw_entries: Any) -> ParsedIPBlacklist:
    """把配置条目预解析为匹配结构。"""
    exact: set[_BaseAddress] = set()
    networks: list[_BaseNetwork] = []
    for entry in normalize_ip_blacklist(raw_entries):
        if "/" in entry:
            networks.append(ip_network(entry, strict=False))
        else:
            exact.add(ip_address(entry))
    return ParsedIPBlacklist(exact=frozenset(exact), networks=tuple(networks))


def build_key_ip_blacklists(api_keys: Iterable[Any]) -> list[ParsedIPBlacklist]:
    """按 api_keys 顺序构建 Key 级黑名单列表。"""
    parsed: list[ParsedIPBlacklist] = []
    for api_key in api_keys or []:
        raw = api_key.get("ip_blacklist") if isinstance(api_key, dict) else None
        parsed.append(parse_ip_blacklist(raw))
    return parsed


def is_ip_blacklisted(parsed: ParsedIPBlacklist | None, client_ip: str | None) -> bool:
    """检查客户端 IP 是否命中预解析黑名单。"""
    if not parsed or not client_ip:
        return False

    try:
        address = ip_address(str(client_ip).strip())
    except ValueError:
        return False

    if address in parsed.exact:
        return True
    return any(address in network for network in parsed.networks)


def apply_runtime_ip_blacklists(app: Any, config: dict | None = None, api_keys_db: list | None = None) -> None:
    """从配置重建运行时黑名单缓存。"""
    # 修改原因：启动加载配置时，调用方还没有把刚解析出的 config/api_keys_db 赋给 app.state。
    # 修改方式：允许调用方显式传入 config 和 api_keys_db；未传时再回退读取 app.state。
    # 目的：启动、setup 和管理端热更新三条路径都能立即得到正确的运行时黑名单缓存。
    if config is None:
        config = getattr(app.state, "config", None) or {}
    if api_keys_db is None:
        api_keys_db = getattr(app.state, "api_keys_db", None)
    if not isinstance(api_keys_db, list):
        api_keys_db = config.get("api_keys") if isinstance(config, dict) else []
    if not isinstance(api_keys_db, list):
        api_keys_db = []

    # 修改原因：IP 黑名单需要随启动和热更新进入内存缓存，不能每个请求重复解析 CIDR。
    # 修改方式：顶层配置保存为 app.state.global_ip_blacklist，Key 级配置按 api_keys 顺序保存为列表。
    # 目的：请求期只做 set 查询和网段遍历，保持鉴权路径开销较低。
    setattr(app.state, "global_ip_blacklist", parse_ip_blacklist(config.get("ip_blacklist") if isinstance(config, dict) else []))
    setattr(app.state, "api_key_ip_blacklists", build_key_ip_blacklists(api_keys_db))


def ip_blocked_error_response() -> JSONResponse:
    """返回需求指定的 IP 拦截响应。"""
    return JSONResponse(
        status_code=403,
        content={"error": {"message": IP_BLOCKED_MESSAGE, "type": IP_BLOCKED_TYPE}},
    )


def raise_ip_blocked() -> None:
    """在 FastAPI 依赖鉴权中抛出可被全局异常处理器识别的 IP 拦截错误。"""
    raise HTTPException(status_code=403, detail={"message": IP_BLOCKED_MESSAGE, "type": IP_BLOCKED_TYPE})


def get_client_ip_from_request_info(default: str | None = None) -> str | None:
    """从 middleware 初始化的 request_info 中读取 client_ip。"""
    try:
        from core.middleware import request_info

        current_info = request_info.get() or {}
        client_ip = current_info.get("client_ip")
        return str(client_ip).strip() if client_ip else default
    except Exception:
        return default


def _is_trusted_proxy(peer_host: str | None) -> bool:
    """检查直连上游节点是否位于代理转发白名单网段内。"""
    global _TRUSTED_PROXY_CACHE
    if not peer_host or peer_host == "unknown":
        return False
    raw_env = (os.getenv("TRUSTED_PROXIES") or "").strip()
    if _TRUSTED_PROXY_CACHE is None or _TRUSTED_PROXY_CACHE[0] != raw_env:
        if raw_env == "*":
            _TRUSTED_PROXY_CACHE = (raw_env, True, ParsedIPBlacklist(frozenset(), ()))
        elif raw_env.lower() in ("none", "false", "0"):
            _TRUSTED_PROXY_CACHE = (raw_env, False, ParsedIPBlacklist(frozenset(), ()))
        elif raw_env:
            _TRUSTED_PROXY_CACHE = (raw_env, False, parse_ip_blacklist(raw_env))
        else:
            _TRUSTED_PROXY_CACHE = (raw_env, False, parse_ip_blacklist(_DEFAULT_TRUSTED_PROXIES))
    _, trust_all, parsed_proxies = _TRUSTED_PROXY_CACHE
    if trust_all:
        return True
    return is_ip_blacklisted(parsed_proxies, peer_host)


def extract_client_ip(headers: Any, fallback_host: str | None = None) -> str:
    """统一提取客户端 IP（CF-Connecting-IP > X-Forwarded-For > X-Real-IP > fallback）。"""
    if headers is not None and (fallback_host is None or _is_trusted_proxy(fallback_host)):
        cf_ip = (headers.get("cf-connecting-ip") or "").strip()
        if cf_ip:
            return cf_ip
        forwarded_for = headers.get("x-forwarded-for")
        if forwarded_for:
            hops = [part.strip() for part in str(forwarded_for).split(",") if part.strip()]
            if hops:
                return hops[-1]
        real_ip = (headers.get("x-real-ip") or "").strip()
        if real_ip:
            return real_ip
    return fallback_host or "unknown"


def is_global_ip_blocked(app: Any, client_ip: str | None) -> bool:
    """检查全局 IP 黑名单。"""
    parsed = getattr(app.state, "global_ip_blacklist", None)
    return is_ip_blacklisted(parsed, client_ip)


def is_key_ip_blocked(app: Any, api_index: int | None, client_ip: str | None) -> bool:
    """检查指定 API Key 的 IP 黑名单。"""
    if api_index is None:
        return False
    key_blacklists = getattr(app.state, "api_key_ip_blacklists", []) or []
    if not isinstance(key_blacklists, list) or api_index < 0 or api_index >= len(key_blacklists):
        return False
    return is_ip_blacklisted(key_blacklists[api_index], client_ip)
